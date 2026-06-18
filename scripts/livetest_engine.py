"""LiveTest Engine — Parity Test by Replaying Historical Data Through LiveTrader.

Runs the real LiveTrader class against historical data using simulated
DataFeed and Execution adapters, producing a trade ledger that can be
directly compared against BacktestEngine output.

This is the DEFINITIVE parity check: if both engines produce the same
trades on the same data, the live trader is behaving correctly.

Usage:
    python scripts/livetest_engine.py \\
        --config configs/strategies/my_strategy.json \\
        --data data/processed/CL_set_07.parquet \\
        --warmup-bars 15000 \\
        --output reports/livetest_trades.csv

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from src.live_execution.adapters.simulated_data_feed import (
    SimulatedDataFeed,
    _make_bar_object,
)
from src.live_execution.adapters.simulated_execution import SimulatedExecution
from src.live_execution.live_trader import LiveTrader
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy

log = logging.getLogger("LiveTestEngine")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ROLLING_BARS = 44_000   # Match LiveTrader's _MAX_ROLLING_BARS
_DEFAULT_WARMUP_BARS = 15_000


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_and_split_data(
    data_path: str,
    warmup_bars: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load a parquet and split into warmup / replay slices.

    Returns:
        (warmup_df, replay_df) — both with DatetimeIndex named "DateTime"
        and columns [DateTime, Open, High, Low, Close, Volume].
    """
    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")

    df = pd.read_parquet(path)

    # Normalize index
    if "DateTime" in df.columns and not isinstance(df.index, pd.DatetimeIndex):
        df = df.set_index("DateTime", drop=False)
    elif isinstance(df.index, pd.DatetimeIndex):
        if "DateTime" not in df.columns:
            df["DateTime"] = df.index
    df.index.name = "DateTime"

    # Ensure tz-naive UTC
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    if "DateTime" in df.columns and hasattr(df["DateTime"].dt, "tz") and df["DateTime"].dt.tz is not None:
        df["DateTime"] = df["DateTime"].dt.tz_convert("UTC").dt.tz_localize(None)

    # Sort chronologically
    df = df.sort_index()

    # Ensure OHLCV columns exist
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Data file missing required columns: {missing}")

    total = len(df)
    if warmup_bars >= total:
        raise ValueError(
            f"warmup_bars ({warmup_bars}) >= total bars ({total}). "
            f"Need at least warmup_bars + 1 bars for replay."
        )

    warmup_df = df.iloc[:warmup_bars].copy()
    replay_df = df.iloc[warmup_bars:].copy()

    log.info(
        "Data loaded: total=%d  warmup=%d (%s → %s)  replay=%d (%s → %s)",
        total,
        len(warmup_df), warmup_df.index[0], warmup_df.index[-1],
        len(replay_df), replay_df.index[0], replay_df.index[-1],
    )

    return warmup_df, replay_df


# ---------------------------------------------------------------------------
# LiveTrader Bootstrap (bypass IBKR-specific startup)
# ---------------------------------------------------------------------------

def _bootstrap_trader(
    trader: LiveTrader,
    warmup_df: pd.DataFrame,
    sim_data: SimulatedDataFeed,
    sim_exec: SimulatedExecution,
) -> None:
    """Initialize the LiveTrader's internal state without calling start().

    This reproduces the essential effects of start() → _warm_start() →
    _subscribe() without any IBKR-specific steps (connect, front-month
    resolution, reconnection, heartbeat, etc.).
    """
    # 1. Mark as connected
    sim_data.connect()
    sim_exec.connect()

    # 2. Register execution callbacks (fills → _on_standard_execution_event)
    trader._callbacks_registered = False
    trader.exec_client.register_order_status_callback(
        trader._on_standard_execution_event
    )
    trader._callbacks_registered = True

    # 3. Set front-month contract (mock values — needed by order placement)
    trader._front_month_local_symbol = "CLZ9"
    trader._front_month_str = "202612"

    # 4. Warm-start: set rolling_df directly from warmup data
    #    This bypasses DataManager.initialize() which would try IBKR backfill.
    rolling_df = warmup_df.copy()
    if "DateTime" in rolling_df.columns and not isinstance(rolling_df.index, pd.DatetimeIndex):
        rolling_df = rolling_df.set_index("DateTime", drop=False)
    rolling_df.index.name = "DateTime"

    # Trim to max rolling window
    if len(rolling_df) > _MAX_ROLLING_BARS:
        rolling_df = rolling_df.iloc[-_MAX_ROLLING_BARS:]

    trader.rolling_df_5m = rolling_df
    trader._last_bar_time_5m = rolling_df.index[-1]

    # 5. Subscribe to simulated live bars (creates the mock BarDataList)
    trader._live_bars_5m = sim_data.subscribe_live_bars(
        symbol="CL", continuous=True, bar_size="5 mins",
    )
    # Hook the LiveTrader's callback onto the mock subscription
    trader._live_bars_5m.updateEvent += trader._on_bar_update_5m

    # 6. Subscribe front-month stream (mock — needed to avoid NoneType errors)
    trader._front_month_bars = sim_data.subscribe_live_bars(
        symbol="CL", continuous=False, bar_size="5 mins",
    )
    trader._front_month_bars.updateEvent += trader._on_front_month_bar_update

    # 7. Mark as running
    trader._running = True

    log.info(
        "LiveTrader bootstrapped: rolling_df=%d bars, last_bar=%s",
        len(trader.rolling_df_5m), trader._last_bar_time_5m,
    )


# ---------------------------------------------------------------------------
# Clock Injection (minimal — telemetry timestamps only)
# ---------------------------------------------------------------------------

def _inject_sim_clock(trader: LiveTrader) -> None:
    """Override _utc_iso_now() to return the current simulation bar time.

    Trading decisions use bar_time (from the data feed), NOT wall-clock.
    This override only affects telemetry timestamps for cleaner audit logs.
    """
    trader._sim_bar_time = None
    original_utc_iso_now = trader._utc_iso_now

    def _sim_utc_iso_now() -> str:
        if trader._sim_bar_time is not None:
            return trader._sim_bar_time.isoformat()
        return original_utc_iso_now()

    trader._utc_iso_now = _sim_utc_iso_now


# ---------------------------------------------------------------------------
# Replay Loop
# ---------------------------------------------------------------------------

def run_simulation(
    trader: LiveTrader,
    sim_data: SimulatedDataFeed,
    sim_exec: SimulatedExecution,
    replay_df: pd.DataFrame,
    progress_every: int = 500,
) -> pd.DataFrame:
    """Replay historical bars through the LiveTrader.

    For each bar:
      1. Update the sim clock to the bar's timestamp
      2. Feed the bar to the matching engine (evaluate resting TP/SL)
      3. Push the bar into the LiveTrader via the subscription callback
      4. Flush deferred entry fill callbacks

    Args:
        trader: Bootstrapped LiveTrader instance.
        sim_data: SimulatedDataFeed adapter.
        sim_exec: SimulatedExecution matching engine.
        replay_df: DataFrame of bars to replay.
        progress_every: Log progress every N bars.

    Returns:
        DataFrame of completed trades from the matching engine.
    """
    total_bars = len(replay_df)
    log.info("Starting replay of %d bars...", total_bars)

    # Get the main subscription (5m brain stream)
    bars_5m = sim_data.get_subscription()
    if bars_5m is None:
        raise RuntimeError("No 5m subscription found — bootstrap failed")

    t0 = time.perf_counter()

    for i, (bar_time, row) in enumerate(replay_df.iterrows()):
        # Ensure bar_time is a proper Timestamp
        if not isinstance(bar_time, pd.Timestamp):
            bar_time = pd.Timestamp(bar_time)

        # Ensure tz-naive
        if bar_time.tzinfo is not None:
            bar_time = bar_time.tz_convert("UTC").tz_localize(None)

        open_ = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])
        volume = float(row.get("Volume", 0))

        # 1. Update sim clock
        trader._sim_bar_time = bar_time

        # 2. Feed bar to matching engine (evaluate resting TP/SL FIRST)
        sim_exec.on_bar_feed(
            bar_time=bar_time,
            open_=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )

        # 3. Push bar into LiveTrader's subscription callback
        #    LiveTrader._on_bar_update_5m reads bars[-2] (completed bar)
        #    and bars[-1] (new incomplete bar).  We push a completed bar
        #    at [-2] and a dummy at [-1].
        completed_bar = _make_bar_object(bar_time, open_, high, low, close, volume)

        # Push: append completed bar, then append a dummy "new" bar
        # so len(bars) >= 2 and bars[-2] is the completed one.
        bars_5m.append(completed_bar)

        # Create dummy "new incomplete bar" (bar that just opened)
        dummy_new = _make_bar_object(
            bar_time + pd.Timedelta(minutes=5),
            close, close, close, close, 0,
        )
        bars_5m.append(dummy_new)

        # Fire the updateEvent callback (has_new_bar=True)
        bars_5m.updateEvent.fire(bars_5m, True)

        # 4. Flush deferred entry fill callbacks
        #    (fires _on_standard_execution_event which calls place_child_orders)
        sim_exec.flush_deferred_callbacks()

        # Trim bars list to prevent unbounded memory growth
        if len(bars_5m) > 100:
            del bars_5m[:-4]

        # Progress logging
        if (i + 1) % progress_every == 0:
            elapsed = time.perf_counter() - t0
            bars_per_sec = (i + 1) / elapsed if elapsed > 0 else 0
            log.info(
                "Progress: %d/%d bars (%.0f%%) — %.0f bars/sec — trades=%d — pnl=$%.2f",
                i + 1, total_bars, 100 * (i + 1) / total_bars,
                bars_per_sec, sim_exec.trade_count, sim_exec.total_pnl,
            )

    elapsed = time.perf_counter() - t0
    log.info(
        "Replay complete: %d bars in %.1fs (%.0f bars/sec)",
        total_bars, elapsed, total_bars / elapsed if elapsed > 0 else 0,
    )

    return sim_exec.export_ledger()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_summary(ledger: pd.DataFrame, sim_exec: SimulatedExecution) -> None:
    """Print a human-readable summary of the simulation results."""
    print("\n" + "=" * 60)
    print("  LIVETEST ENGINE — SIMULATION RESULTS")
    print("=" * 60)

    if ledger.empty:
        print("  No trades executed during simulation.")
        print("=" * 60)
        return

    n = len(ledger)
    wins = (ledger["net_pnl_dollars"] > 0).sum()
    losses = (ledger["net_pnl_dollars"] <= 0).sum()
    total_pnl = ledger["net_pnl_dollars"].sum()
    avg_pnl = ledger["net_pnl_dollars"].mean()
    max_win = ledger["net_pnl_dollars"].max()
    max_loss = ledger["net_pnl_dollars"].min()
    avg_bars = ledger["duration_bars"].mean()
    total_commission = ledger["commission_dollars"].sum()

    # By side
    long_mask = ledger["signal_side"] == "LONG"
    short_mask = ledger["signal_side"] == "SHORT"

    # By exit reason
    tp_count = (ledger["exit_reason"] == "TP_HIT").sum()
    sl_count = (ledger["exit_reason"] == "SL_HIT").sum()
    tb_count = (ledger["exit_reason"] == "TIME_BARRIER").sum()

    print(f"  Total Trades:     {n}")
    print(f"  Wins / Losses:    {wins} / {losses}  (WR={100*wins/n:.1f}%)")
    print(f"  Total PnL:        ${total_pnl:,.2f}")
    print(f"  Avg PnL/trade:    ${avg_pnl:,.2f}")
    print(f"  Best / Worst:     ${max_win:,.2f} / ${max_loss:,.2f}")
    print(f"  Avg Hold (bars):  {avg_bars:.1f}")
    print(f"  Total Commission: ${total_commission:,.2f}")
    print()
    print(f"  By Side:")
    print(f"    Long:  {long_mask.sum()} trades, PnL=${ledger.loc[long_mask, 'net_pnl_dollars'].sum():,.2f}")
    print(f"    Short: {short_mask.sum()} trades, PnL=${ledger.loc[short_mask, 'net_pnl_dollars'].sum():,.2f}")
    print()
    print(f"  By Exit Reason:")
    print(f"    TP:           {tp_count}")
    print(f"    SL:           {sl_count}")
    print(f"    Time Barrier: {tb_count}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="LiveTest Engine — replay historical data through LiveTrader",
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to strategy config JSON (e.g., configs/strategies/my_strategy.json)",
    )
    parser.add_argument(
        "--data", required=True,
        help="Path to historical data parquet (e.g., data/processed/CL_set_07.parquet)",
    )
    parser.add_argument(
        "--warmup-bars", type=int, default=_DEFAULT_WARMUP_BARS,
        help=f"Number of bars for warmup (default: {_DEFAULT_WARMUP_BARS})",
    )
    parser.add_argument(
        "--output", default=None,
        help="Path to save trade ledger CSV (default: reports/livetest_trades.csv)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--progress-every", type=int, default=500,
        help="Log progress every N bars (default: 500)",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Suppress noisy loggers
    logging.getLogger("TelemetryDB").setLevel(logging.WARNING)
    logging.getLogger("TelegramAlerter").setLevel(logging.WARNING)

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    log.info("=" * 60)
    log.info("  LIVETEST ENGINE")
    log.info("  Config:      %s", config_path)
    log.info("  Data:        %s", args.data)
    log.info("  Warmup bars: %d", args.warmup_bars)
    log.info("=" * 60)

    # 1. Load and split data
    warmup_df, replay_df = load_and_split_data(args.data, args.warmup_bars)

    # 2. Create adapters
    sim_data = SimulatedDataFeed(warmup_df=warmup_df, replay_df=replay_df)
    sim_exec = SimulatedExecution()

    # 3. Create Strategy
    strategy = ConfigurableStrategy(config_path=str(config_path))
    log.info(
        "Strategy: %s  direction=%s  features=%d",
        strategy.name, strategy.direction, len(strategy.feature_names),
    )

    # 4. Create LiveTrader with simulated adapters
    #    Use a temp DB for telemetry so we don't pollute the real one.
    import tempfile
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    tmp_db_path = tmp_db.name

    # Use the real seed path — DataManager needs it for initialization.
    # But since we bypass DataManager.initialize() in bootstrap, the seed
    # doesn't actually need to exist.  We use a dummy path.
    trader = LiveTrader(
        data_client=sim_data,
        exec_client=sim_exec,
        strategy=strategy,
        db_path=tmp_db_path,
        seed_path=str(_PROJECT_ROOT / "data" / "raw" / "cl-5m_bk.csv"),
        cache_path=str(_PROJECT_ROOT / "data" / "processed" / "warm_start_cache_sim.parquet"),
        quantity=1,
        dry_run=False,
        entry_mode="market",  # Use market orders in simulation
    )

    # 5. Inject sim clock and bootstrap
    _inject_sim_clock(trader)
    _bootstrap_trader(trader, warmup_df, sim_data, sim_exec)

    # 6. Run simulation
    ledger = run_simulation(
        trader, sim_data, sim_exec, replay_df,
        progress_every=args.progress_every,
    )

    # 7. Export and summarize
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = _PROJECT_ROOT / "reports" / "livetest_trades.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(output_path, index=False)
    log.info("Trade ledger saved to %s", output_path)

    print_summary(ledger, sim_exec)

    # Cleanup
    try:
        os.unlink(tmp_db_path)
    except Exception:
        pass

    return ledger


if __name__ == "__main__":
    main()
