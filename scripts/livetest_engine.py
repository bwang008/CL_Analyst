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
from types import SimpleNamespace

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
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent
from src.live_execution.live_trader import LiveTrader
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.data_manager import DataManager

log = logging.getLogger("LiveTestEngine")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ROLLING_BARS = 44_000   # Match LiveTrader's _MAX_ROLLING_BARS
_DEFAULT_WARMUP_BARS = 15_000

# Bar timedelta lookup
_BAR_TIMEDELTA = {
    "5m": pd.Timedelta(minutes=5),
    "1h": pd.Timedelta(hours=1),
    "2h": pd.Timedelta(hours=2),
    "4h": pd.Timedelta(hours=4),
}


# ---------------------------------------------------------------------------
# Mock DataManager (for 1h feature pipeline)
# ---------------------------------------------------------------------------

class _MockDataManager:
    """Lightweight DataManager wrapper for simulation.

    Wraps a rolling DataFrame and provides ``get_ratio_adjusted_df()``
    and ``append_bar()`` without any IBKR, cache, or seed logic.
    In simulation, we don't have rollover ratios, so the ratio-adjusted
    df is identical to the raw df.
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df.copy()
        self.cache_path = Path("__sim_mock__")  # never written

    def get_ratio_adjusted_df(self) -> pd.DataFrame:
        return self._df.copy()

    def append_bar(self, new_row: pd.DataFrame) -> None:
        self._df = pd.concat([self._df, new_row])
        if len(self._df) > _MAX_ROLLING_BARS:
            self._df = self._df.iloc[-_MAX_ROLLING_BARS:]

    @property
    def dataframe(self) -> pd.DataFrame:
        return self._df

    def save_cache(self) -> None:
        pass  # no-op


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
    bar_size: str = "5m",
) -> None:
    """Initialize the LiveTrader's internal state without calling start().

    This reproduces the essential effects of start() → _warm_start() →
    _subscribe() without any IBKR-specific steps (connect, front-month
    resolution, reconnection, heartbeat, etc.).

    Supports both 5m and 1h bar sizes.
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
    rolling_df = warmup_df.copy()
    if "DateTime" in rolling_df.columns and not isinstance(rolling_df.index, pd.DatetimeIndex):
        rolling_df = rolling_df.set_index("DateTime", drop=False)
    rolling_df.index.name = "DateTime"

    # Trim to max rolling window
    if len(rolling_df) > _MAX_ROLLING_BARS:
        rolling_df = rolling_df.iloc[-_MAX_ROLLING_BARS:]

    if bar_size in ("1h", "2h", "4h"):
        # ── Hourly mode ───────────────────────────────────────────
        # The 1h callback is the primary inference path.
        # rolling_df_5m is ALSO populated with the same bars because
        # _check_trailing_stop() always reads rolling_df_5m.iloc[-1]
        # for bar extremes tracking (this mirrors production where
        # 5m bars keep flowing independently of the 1h stream).
        trader.rolling_df_1h = rolling_df
        trader._last_bar_time_1h = rolling_df.index[-1]

        # Populate rolling_df_5m with the SAME data so trailing stop
        # can read bar extremes. In production, 5m bars flow separately;
        # in simulation, we share the 1h bars as a conservative proxy.
        trader.rolling_df_5m = rolling_df.copy()
        trader._last_bar_time_5m = rolling_df.index[-1]

        # Replace data_manager_1h with our mock (provides get_ratio_adjusted_df)
        trader.data_manager_1h = _MockDataManager(rolling_df)

        # Subscribe 1h bars and hook the callback
        trader._live_bars_1h = sim_data.subscribe_live_bars(
            symbol="CL", continuous=True, bar_size="1 hour",
        )
        trader._live_bars_1h.updateEvent += trader._on_bar_update_1h

        log.info(
            "LiveTrader bootstrapped (1h): rolling_df_1h=%d bars, last_bar=%s",
            len(trader.rolling_df_1h), trader._last_bar_time_1h,
        )
    else:
        # ── 5m mode ───────────────────────────────────────────────
        trader.rolling_df_5m = rolling_df
        trader._last_bar_time_5m = rolling_df.index[-1]

        trader._live_bars_5m = sim_data.subscribe_live_bars(
            symbol="CL", continuous=True, bar_size="5 mins",
        )
        trader._live_bars_5m.updateEvent += trader._on_bar_update_5m

        log.info(
            "LiveTrader bootstrapped (5m): rolling_df_5m=%d bars, last_bar=%s",
            len(trader.rolling_df_5m), trader._last_bar_time_5m,
        )

    # Subscribe front-month stream (mock — needed to avoid NoneType errors)
    trader._front_month_bars = sim_data.subscribe_live_bars(
        symbol="CL", continuous=False, bar_size="5 mins",
    )
    trader._front_month_bars.updateEvent += trader._on_front_month_bar_update

    # Mark as running
    trader._running = True


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
    bar_size: str = "5m",
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
        bar_size: Bar size ("5m", "1h", etc.).
        progress_every: Log progress every N bars.

    Returns:
        DataFrame of completed trades from the matching engine.
    """
    total_bars = len(replay_df)
    log.info("Starting replay of %d bars...", total_bars)

    # Get the main subscription (matches bar_size)
    bars_sub = sim_data.get_subscription()
    if bars_sub is None:
        raise RuntimeError("No subscription found — bootstrap failed")

    bar_td = _BAR_TIMEDELTA.get(bar_size, pd.Timedelta(minutes=5))

    # Track which entry fills have already had bracket children placed
    _placed_children: set = set()

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

        # 2b. Deliver EXIT-fill callbacks BEFORE the bar's evaluation so
        #     on_exit/cooldown state is current when the exit bar is
        #     evaluated — production delivers fills in real time ahead of
        #     the bar-close evaluation (F(2), human-authorized 2026-07-03).
        #     The post-fire flush below still handles same-bar ENTRY fills
        #     placed during the evaluation itself.
        sim_exec.flush_deferred_callbacks()

        # 3. Push bar into LiveTrader's subscription callback
        #    LiveTrader._on_bar_update_Xm reads bars[-2] (completed bar)
        #    and bars[-1] (new incomplete bar).  We push a completed bar
        #    at [-2] and a dummy at [-1].
        completed_bar = _make_bar_object(bar_time, open_, high, low, close, volume)

        # Push: append completed bar, then append a dummy "new" bar
        # so len(bars) >= 2 and bars[-2] is the completed one.
        bars_sub.append(completed_bar)

        # Create dummy "new incomplete bar" (bar that just opened)
        dummy_new = _make_bar_object(
            bar_time + bar_td,
            close, close, close, close, 0,
        )
        bars_sub.append(dummy_new)

        # 3b. In hourly mode, sync rolling_df_5m with the latest bar
        #     so that _check_trailing_stop() can read bar extremes.
        #     In production, 5m bars flow independently; here we mirror
        #     each 1h bar into the 5m rolling window as a proxy.
        #     MUST be done BEFORE updateEvent.fire() so _check_trailing_stop
        #     sees the current bar's High/Low (matching backtest evaluation order).
        if bar_size in ("1h", "2h", "4h"):
            new_row = pd.DataFrame([{
                "DateTime": bar_time,
                "Open": open_, "High": high,
                "Low": low, "Close": close,
                "Volume": volume,
            }])
            new_row = new_row.set_index(
                pd.DatetimeIndex(new_row["DateTime"]), drop=False
            )
            new_row.index.name = "DateTime"
            trader.rolling_df_5m = pd.concat([trader.rolling_df_5m, new_row])
            if len(trader.rolling_df_5m) > _MAX_ROLLING_BARS:
                trader.rolling_df_5m = trader.rolling_df_5m.iloc[-_MAX_ROLLING_BARS:]

        # Fire the updateEvent callback (has_new_bar=True)
        bars_sub.updateEvent.fire(bars_sub, True)

        # 4. Flush deferred entry fill callbacks
        #    (fires _on_standard_execution_event → telemetry recording)
        sim_exec.flush_deferred_callbacks()

        # 5. Register the TP/SL children that live_trader placed on the entry
        #    fill in _open_orders so _check_trailing_stop() can find them to
        #    modify. Do NOT place children here: _on_standard_execution_event's
        #    entry branch already calls _place_bracket_children_on_fill on the
        #    fill callback — a second placement would overwrite
        #    _tp_order_ids/_sl_order_id and orphan the first child set, whose
        #    fills then arrive UNRECOGNIZED (misrouted exits, phantom entries).
        last_filled = getattr(trader, "_last_filled_entry_order_id", None)
        if last_filled is not None and last_filled not in _placed_children:
            _placed_children.add(last_filled)
            # Look up fill details from the sim execution
            entry_ctx = sim_exec._active_entry
            if entry_ctx is not None:
                try:
                    for oid in (trader._tp_order_ids or []):
                        trader._open_orders[oid] = StandardExecutionEvent(
                            order_id=oid,
                            symbol=entry_ctx["symbol"],
                            status="PreSubmitted",
                            filled_qty=0,
                            remaining_qty=entry_ctx["quantity"],
                            avg_price=0.0,
                            raw_event=SimpleNamespace(
                                order=SimpleNamespace(
                                    orderId=oid, orderType="LMT",
                                    auxPrice=0.0,
                                ),
                            ),
                        )
                    sl_oid = trader._sl_order_id
                    if sl_oid is not None:
                        # Look up actual SL price from resting orders
                        sl_resting = sim_exec._resting_orders.get(sl_oid)
                        sl_price = sl_resting.price if sl_resting else 0.0
                        trader._open_orders[sl_oid] = StandardExecutionEvent(
                            order_id=sl_oid,
                            symbol=entry_ctx["symbol"],
                            status="PreSubmitted",
                            filled_qty=0,
                            remaining_qty=entry_ctx["quantity"],
                            avg_price=0.0,
                            raw_event=SimpleNamespace(
                                order=SimpleNamespace(
                                    orderId=sl_oid, orderType="STP",
                                    auxPrice=sl_price,
                                ),
                            ),
                        )
                except Exception as e:
                    log.warning("Failed to register bracket children: %s", e)

        # Trim bars list to prevent unbounded memory growth
        if len(bars_sub) > 100:
            del bars_sub[:-4]

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
        help="Path to historical data parquet (e.g., data/processed/CL_HourSet_11.parquet)",
    )
    parser.add_argument(
        "--exec-data", default=None,
        help="Path to raw/panama execution price CSV (for split-brain)",
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
        "--slippage-per-side", type=float, default=0.01,
        help="Slippage per side in price units (default: 0.01)",
    )
    parser.add_argument(
        "--progress-every", type=int, default=500,
        help="Log progress every N bars (default: 500)",
    )
    parser.add_argument(
        "--telegram", action="store_true", default=False,
        help="Enable real Telegram notifications during simulation (default: OFF)",
    )
    parser.add_argument(
        "--predictions-dir", default=None,
        help="Base path to load prediction CSVs from instead of running inference",
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

    # 0. Read bar_size from config
    with open(config_path) as f:
        raw_config = json.load(f)
    bar_size = raw_config.get("bar_size", "5m").lower()
    log.info("  Bar size:    %s", bar_size)

    # 1. Load and split data
    warmup_df, replay_df = load_and_split_data(args.data, args.warmup_bars)

    # 2. Create adapters
    sim_data = SimulatedDataFeed(warmup_df=warmup_df, replay_df=replay_df)
    sim_exec = SimulatedExecution(slippage_per_side=args.slippage_per_side)

    # 3. Create Strategy
    strategy = ConfigurableStrategy(config_path=str(config_path))
    log.info(
        "Strategy: %s  direction=%s  features=%d  bar_size=%s",
        strategy.name, strategy.direction, len(strategy.feature_names), bar_size,
    )

    if args.predictions_dir:
        base_pred_dir = Path(args.predictions_dir)
        models_cfg = raw_config.get("models", {})
        
        long_preds = None
        short_preds = None
        
        long_path_str = models_cfg.get("long", {}).get("predictions_path")
        if long_path_str:
            long_path = base_pred_dir / long_path_str
            if long_path.exists():
                long_df = pd.read_csv(long_path)
                if "DateTime" in long_df.columns:
                    long_df["DateTime"] = pd.to_datetime(long_df["DateTime"])
                    long_preds = long_df.set_index("DateTime")["prob_Buy"].to_dict()
                    log.info("Loaded LONG predictions from %s", long_path)
        
        short_path_str = models_cfg.get("short", {}).get("predictions_path")
        if short_path_str:
            short_path = base_pred_dir / short_path_str
            if short_path.exists():
                short_df = pd.read_csv(short_path)
                if "DateTime" in short_df.columns:
                    short_df["DateTime"] = pd.to_datetime(short_df["DateTime"])
                    short_preds = short_df.set_index("DateTime")["prob_Sell"].to_dict()
                    log.info("Loaded SHORT predictions from %s", short_path)
        
        # Monkey patch evaluate
        orig_evaluate = strategy.evaluate
        def monkey_evaluate(
            features: pd.DataFrame,
            current_price: float,
            atr_value: float | None,
            current_position: int,
            *,
            atr_value_long: float | None = None,
            atr_value_short: float | None = None,
        ):
            dt = pd.Timestamp(features.index[-1])
            if dt.tz is not None:
                dt = dt.tz_convert("UTC").tz_localize(None)
                
            orig_run_inference = strategy._run_inference
            def mock_run_inference(learner, _features):
                if learner == strategy._long_learner:
                    return float(long_preds.get(dt, 0.0)) if long_preds else 0.0
                elif learner == strategy._short_learner:
                    return float(short_preds.get(dt, 0.0)) if short_preds else 0.0
                return 0.0
            
            strategy._run_inference = mock_run_inference
            try:
                return orig_evaluate(
                    features, current_price, atr_value, current_position,
                    atr_value_long=atr_value_long, atr_value_short=atr_value_short
                )
            finally:
                strategy._run_inference = orig_run_inference
                
        strategy.evaluate = monkey_evaluate
        log.info("Monkey-patched strategy.evaluate() with prediction injection.")

    # 4. Create LiveTrader with simulated adapters
    import tempfile
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    tmp_db_path = tmp_db.name

    # For hourly configs, we need a valid seed path to avoid FileNotFoundError
    # in LiveTrader.__init__. We use the real seed path for the 5m DataManager
    # (which is always created even for 1h strategies) but skip its initialization.
    from src.data_paths import get_data_path as _dp_data_path, get_data_root as _dp_data_root
    seed_path_5m = str(_dp_data_path("raw/cl-5m_bk.csv"))
    cache_path_5m = str(_dp_data_root() / "processed" / "warm_start_cache_sim.parquet")

    trader = LiveTrader(
        data_client=sim_data,
        exec_client=sim_exec,
        strategy=strategy,
        db_path=tmp_db_path,
        seed_path=seed_path_5m,
        cache_path=cache_path_5m,
        quantity=1,
        dry_run=False,
        entry_mode="market",
    )

    # 5. Inject sim clock and bootstrap
    _inject_sim_clock(trader)

    # PARITY FIX: _reset_position_state() references self._strategy (private),
    # but LiveTrader stores it as self.strategy (public). Without this alias,
    # on_exit() is never called after TP/SL fills, so the strategy's cooldown
    # counters never activate — causing immediate re-entry after SL exits.
    trader._strategy = trader.strategy

    # 6. Disable Telegram unless explicitly opted in (prevents flooding
    #    real channels with hundreds of simulated trade notifications).
    if not args.telegram:
        trader._telegram.enabled = False
        log.info("Telegram alerts DISABLED for simulation (use --telegram to enable)")

    # 7. Bootstrap trader
    _bootstrap_trader(trader, warmup_df, sim_data, sim_exec, bar_size=bar_size)

    # 8. Run simulation
    ledger = run_simulation(
        trader, sim_data, sim_exec, replay_df,
        bar_size=bar_size,
        progress_every=args.progress_every,
    )

    # 9. Export and summarize
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
