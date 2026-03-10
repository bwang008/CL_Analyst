"""
Live Event-Driven Execution Engine for CL Futures.

This module implements the live trading loop that:
1. Uses DataManager for warm-start initialization (seed CSV + IBKR backfill)
2. Subscribes to live 5-minute bars from IBKR (Two-Stream architecture)
   - Brain stream: Continuous contract for signal generation
   - Hands stream: Front-month contract for execution + raw data logging
3. Maintains a rolling window and generates features via AlphaFactory
4. Delegates trade decisions to a pluggable Strategy object
5. Executes bracket orders on IBKR Paper Trading
6. Logs all activity to SQLite telemetry (smoothed + raw front-month)

Usage:
    conda run -n trader python -m src.live_execution.live_trader
    conda run -n trader python -m src.live_execution.live_trader --dry-run
    conda run -n trader python -m src.live_execution.live_trader --strategy BUY70_SIZED_MANATEE
    conda run -n trader python -m src.live_execution.live_trader --config configs/strategies/manatee.json

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Load .env file (CL_DATA_ROOT, etc.) before reading env-based constants
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Project imports
from src.features.alpha_factory import AlphaFactory
from src.live_execution.strategy import Strategy, TradeSignal
from src.live_execution.strategies.buy70_sized_manatee import Buy70SizedManatee
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.data_manager import DataManager
from src.live_execution.ibkr_client import (
    IBKRConnectionManager,
    build_cl_contract,
    ib_bars_to_dataframe,
)
from src.live_execution.telemetry import TelemetryDB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from src.data_paths import get_data_path as _dp_data_path, get_data_root as _dp_data_root

_DEFAULT_DB_PATH = str(_dp_data_path("live_telemetry.db"))

# AlphaFactory windows used during training (set_05/set_06)
_ALPHA_WINDOWS = [864, 2016, 4032, 10080]  # 3d, 7d, 14d, 35d in 5-min bars

# Rolling window size — must be >= largest seed lookback (150 days × 288 bars/day)
# plus margin for IBKR backfill and live bars.
# Parity note (2026-03-08): 52/80 features diverged >2σ when window was too small.
# Features with long lookbacks (MACRO_3M, VOL_ROC_10080) need the full history.
_MAX_ROLLING_BARS = 44_000

# Trade parameters (engine-level safety rails)
_DEFAULT_QUANTITY = 1  # 1 CL contract (base lot)
_MAX_HOLD_BARS = 288  # 24 hours on 5-min bars

# Strategy registry — maps CLI names to strategy classes
_STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "BUY70_SIZED_MANATEE": Buy70SizedManatee,
}
_DEFAULT_STRATEGY = "BUY70_SIZED_MANATEE"

# Polling interval in seconds (ib.sleep)
_POLL_INTERVAL = 5.0

# Reconnection parameters
_RECONNECT_BASE_DELAY = 5.0      # Initial delay before reconnect attempt (seconds)
_RECONNECT_MAX_DELAY = 300.0     # Max backoff delay (5 minutes)
_RECONNECT_MAX_ATTEMPTS = 50     # Max retry attempts (~2+ hours of retries)

# Auto-restart parameters (process-level recovery)
_RESTART_MAX_ATTEMPTS = 5        # Max full restart attempts
_RESTART_DELAY = 300.0           # Delay between restart attempts (5 minutes)

# Default paths for DataManager (CL_DATA_ROOT primary, repo-local fallback)
_DEFAULT_SEED_PATH = str(_dp_data_path("raw/cl-5m_bk.csv"))
_DEFAULT_CACHE_PATH = str(
    _dp_data_root() / "processed" / "warm_start_cache.parquet"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_DIR = _PROJECT_ROOT / "reports"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class CLOnlyLogFilter(logging.Filter):
    """Suppress ib_insync log messages about non-CL positions/trades
    and verbose callback dumps that are redundant with our [TRADE] lines.

    IBKR reports historical positions, portfolio updates, executions,
    and commission reports for ALL symbols in the account, even those
    with 0 position (closed-out stocks like XOM, MSFT, V, COP).
    ib_insync logs every one of these at INFO level, cluttering the
    live trader output.  This filter drops:
      1. Non-CL messages (Stock(), wrong symbol)
      2. Verbose callback dumps (placeOrder, orderStatus, execDetails,
         commissionReport, updatePortfolio, position) — these log
         entire Trade/Fill/PortfolioItem repr strings (500+ chars each)
         and are redundant with our concise [TRADE] ENTRY/FILL/EXIT lines.
    """

    _NON_CL_RE = re.compile(
        r"(?:"
        r"Stock\("
        r"|symbol='(?!CL\b)\w+"
        r")",
    )

    # Verbose ib_insync callback messages — redundant with our [TRADE] lines
    _VERBOSE_IBKR_RE = re.compile(
        r"^(?:"
        r"placeOrder:"
        r"|orderStatus:"
        r"|execDetails[ :]"
        r"|commissionReport:"
        r"|updatePortfolio:"
        r"|position:"
        r")",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if self._NON_CL_RE.search(msg):
            return False  # suppress non-CL message
        if self._VERBOSE_IBKR_RE.match(msg):
            return False  # suppress verbose callback dump
        return True


def _setup_file_logging(client_id: int) -> None:
    """Add a file handler so logs are persisted to disk.

    Writes to reports/livetrader_{N}.log in append mode.
    Logs accumulate across restarts for the same client_id.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / f"livetrader_{client_id}.log"
    file_handler = logging.FileHandler(
        log_file,
        mode="a",
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    )
    # Add to both our logger and the root logger
    logging.getLogger().addHandler(file_handler)
    log.info("File logging enabled: %s", log_file)


logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATE_FORMAT,
)
log = logging.getLogger("LiveTrader")

# Suppress non-CL noise from ib_insync internal logging
logging.getLogger("ib_insync").addFilter(CLOnlyLogFilter())


def _sigmoid(x: float) -> float:
    """Apply sigmoid to convert logit to probability."""
    return 1.0 / (1.0 + np.exp(-x))


# ---------------------------------------------------------------------------
# Feature Pipeline (replicates process_set_05/set_06 for live data)
# ---------------------------------------------------------------------------

def build_live_features(
    df: pd.DataFrame,
    feature_names: list[str],
) -> Optional[pd.DataFrame]:
    """
    Generate features from a rolling OHLCV DataFrame for live inference.

    Replicates the training pipeline (process_set_05/set_06):
    1. Add Time_Sin, Time_Cos from the DateTime index
    2. Run AlphaFactory.add_all_features(windows=_ALPHA_WINDOWS)
    3. Add Volume_Log
    4. Select the exact columns the model expects

    Args:
        df: Rolling OHLCV DataFrame with DateTime index and columns
            [Open, High, Low, Close, Volume].
        feature_names: The exact list of feature column names the model expects.

    Returns:
        Single-row DataFrame with the model's expected features,
        or None if features cannot be computed (e.g. NaN in required columns).
    """
    if len(df) < _ALPHA_WINDOWS[-1]:
        log.warning(
            "Not enough bars for feature generation: %d < %d",
            len(df), _ALPHA_WINDOWS[-1],
        )
        return None

    # Work on a copy to avoid mutating the rolling window
    work = df.copy()

    # 1. Add cyclical time features
    minutes = work.index.hour * 60 + work.index.minute
    work["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    work["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)

    # 2. Run AlphaFactory (adds log_ret, VOL_*, LIQ_*, STRUC_*, TREND_*,
    #    VOLFLOW_*, MOM_*, MACRO_*, ATR_14, etc.)
    work = AlphaFactory(work).add_all_features(
        windows=_ALPHA_WINDOWS,
        include_momentum=True,
        include_macro=True,
    )

    # 3. Add ATR_14 (in training, this was created by add_triple_barrier_target
    #    in data_processor.py, but we skip target generation for live inference)
    if "ATR_14" not in work.columns:
        import pandas_ta as ta  # noqa: F811
        atr_series = work.ta.atr(length=14)
        if atr_series is not None:
            work["ATR_14"] = atr_series

    # 4. Add Volume_Log (from normalize_features in training pipeline)
    work["Volume_Log"] = np.log1p(work["Volume"])

    # 5. Replace inf with NaN, then forward-fill (covers normal timeseries NaN)
    #    and backfill (covers warm-up NaN from large-window features like
    #    VOL_ROC_10080 or MACRO_3M that need more history than available).
    #    Final fillna(0) catches features that are all-NaN during cold start
    #    (e.g., VOL_VOLVOL_10080 needs 2×10080 bars, MACRO_3M needs ~25K bars).
    work.replace([np.inf, -np.inf], np.nan, inplace=True)
    work.ffill(inplace=True)
    work.bfill(inplace=True)
    work.fillna(0, inplace=True)

    # 6. Extract the last complete row with the model's expected columns
    missing_cols = set(feature_names) - set(work.columns)
    if missing_cols:
        log.error("Missing feature columns: %s", missing_cols)
        return None

    last_row = work[feature_names].iloc[[-1]]

    nan_count = last_row.isna().sum(axis=1).iloc[0]
    if nan_count > 0:
        nan_cols = last_row.columns[last_row.isna().iloc[0]].tolist()
        log.warning(
            "%d features still NaN after fill (cold start): %s",
            nan_count, nan_cols,
        )
        last_row = last_row.fillna(0)

    return last_row


# ---------------------------------------------------------------------------
# LiveTrader
# ---------------------------------------------------------------------------

class LiveTrader:
    """
    Event-driven live execution engine for CL futures.

    Architecture:
        IBKR → 5-min bars → rolling DataFrame → AlphaFactory →
        Strategy.evaluate() → bracket order → telemetry logging

    The engine is strategy-agnostic — all trade decision logic
    (model inference, threshold, bracket direction, sizing) is
    delegated to a pluggable Strategy object.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 10,
        strategy: Strategy,
        db_path: str = _DEFAULT_DB_PATH,
        seed_path: str = _DEFAULT_SEED_PATH,
        cache_path: str = _DEFAULT_CACHE_PATH,
        quantity: int = _DEFAULT_QUANTITY,
        dry_run: bool = False,
        entry_mode: str = "adaptive",
        adaptive_priority: str = "Normal",
        exit_mode: str = "market",
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.quantity = quantity
        self.dry_run = dry_run
        self.entry_mode = entry_mode
        self.adaptive_priority = adaptive_priority
        self.exit_mode = exit_mode

        # Strategy (owns model, config, threshold, sizing, bracket math)
        self.strategy = strategy
        self.feature_names: list[str] = strategy.feature_names

        # Read max_hold_bars from strategy config (keeps backtest & live in sync)
        strategy_config = getattr(strategy, "config", {})
        self._max_hold_bars: int = int(
            strategy_config.get("max_hold_bars", _MAX_HOLD_BARS)
        )
        # Cooldown: bars to wait after an exit before allowing new entries
        # (parity with backtest engine FSM COOLDOWN state)
        _cd_fallback: int = int(strategy_config.get("cooldown_bars", 5))
        self._tp_cooldown_bars: int = int(
            strategy_config.get("tp_cooldown_bars", _cd_fallback)
        )
        self._sl_cooldown_bars: int = int(
            strategy_config.get("sl_cooldown_bars", _cd_fallback)
        )
        self._cooldown_remaining: int = 0
        # Trailing stop config (parity with backtest engine)
        self._trailing_atr_mult: float = float(
            strategy_config.get("trailing_atr_mult", 100.0)
        )
        self._trailing_sl_atr_offset: float = float(
            strategy_config.get("trailing_sl_atr_offset", 0.25)
        )
        # Exit mode for time-barrier exits (separate from entry_mode)
        self._exit_mode: str = exit_mode
        log.info("Strategy: %s  direction=%s", strategy.name, strategy.direction)
        log.info(
            "Entry mode: %s  adaptive_priority=%s  max_hold_bars=%d  "
            "tp_cooldown=%d  sl_cooldown=%d  trailing_atr_mult=%.2f  "
            "trailing_sl_offset=%.2f  exit_mode=%s",
            entry_mode, adaptive_priority, self._max_hold_bars,
            self._tp_cooldown_bars, self._sl_cooldown_bars,
            self._trailing_atr_mult,
            self._trailing_sl_atr_offset, self._exit_mode,
        )

        # Telemetry
        self.telemetry = TelemetryDB(db_path)
        log.info("Telemetry DB: %s", db_path)

        # IBKR connection (not yet connected)
        self.manager = IBKRConnectionManager(
            host=host,
            port=port,
            client_id=client_id,
            readonly=dry_run,  # readonly in dry-run mode
        )

        # DataManager for warm-start
        self.data_manager = DataManager(
            seed_path=seed_path,
            cache_path=cache_path,
            ibkr_manager=self.manager,
        )

        # State
        self.rolling_df: Optional[pd.DataFrame] = None
        self._live_bars = None
        self._front_month_bars = None  # Two-Stream: raw front-month
        self._contract = None
        self._front_month_contract = None
        self._front_month_str: Optional[str] = None
        self._running = False
        self._last_bar_time: Optional[pd.Timestamp] = None
        self._subscriptions_lost = False  # Track connectivity drops
        self._resubscribe_pending = False  # Prevent duplicate resubscription scheduling
        self._callbacks_registered = False
        self._last_decision_context_by_order_id: dict[int, dict] = {}
        self._position_entry_bar_time: Optional[pd.Timestamp] = None
        self._position_bars_held: int = 0
        # Trailing stop state (parity with backtest engine _on_in_position)
        self._trailing_activated: bool = False
        self._entry_price: Optional[float] = None
        self._atr_at_entry: Optional[float] = None
        self._position_side: int = 0  # +1 long, -1 short
        self._highest_high: float = 0.0
        self._lowest_low: float = float("inf")
        self._run_id = (
            f"live-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        self._session_id = uuid.uuid4().hex
        self._hostname = socket.gethostname()
        self._process_id = os.getpid()
        self._environment = "paper" if self.port in (4002, 7497) else "live"

    # (_prob_to_lots moved to Strategy subclasses)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect to IBKR, warm-start via DataManager, and enter the event loop.

        If a disconnect is unrecoverable (all reconnect attempts fail),
        the engine will tear down and re-run start() from scratch up to
        _RESTART_MAX_ATTEMPTS times — equivalent to the user pressing
        Ctrl+C and re-running the script.
        """
        self._needs_restart = False
        self._restart_count = getattr(self, "_restart_count", 0)

        log.info("=" * 60)
        log.info("LiveTrader starting (dry_run=%s)", self.dry_run)
        log.info("=" * 60)

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            # Step 1: Connect
            log.info("Connecting to IBKR at %s:%d ...", self.host, self.port)
            self.manager.connect()
            log.info("Connected to IBKR")

            # Step 2: Register error handler for reconnection
            self.manager.ib.errorEvent += self._on_ib_error
            self._callbacks_registered = False
            self._register_execution_callbacks()

            # Step 3: Qualify continuous contract (Brain stream)
            self._contract = build_cl_contract(continuous=True)
            self._contract = self.manager.qualify_contract(self._contract)
            log.info("Qualified CL continuous contract: %s", self._contract)

            # Step 4: Resolve front-month contract (Hands stream)
            try:
                self._front_month_contract, self._front_month_str = (
                    self.manager.get_front_month_contract()
                )
                log.info(
                    "Front-month contract: %s (month=%s)",
                    self._front_month_contract.localSymbol,
                    self._front_month_str,
                )
            except Exception as exc:
                log.warning(
                    "Could not resolve front-month contract: %s. "
                    "Raw front-month logging will be disabled.",
                    exc,
                )

            # Step 5: Print CL-only account summary
            self._print_account_summary()

            # Step 6: Pass front-month ID to DataManager for rollover detection
            if self._front_month_contract is not None:
                self.data_manager.front_month_id = (
                    self._front_month_contract.localSymbol
                )

            # Step 7: Warm-start via DataManager
            self._warm_start()

            # Step 8: Subscribe to live bars (Brain stream)
            self._subscribe()

            # Step 9: Subscribe to front-month bars (Hands stream)
            if self._front_month_contract is not None:
                self._subscribe_front_month()

            # Step 10: Enter event loop
            self._running = True
            self._event_loop()

        except Exception:
            log.exception("Fatal error in LiveTrader")
            raise
        finally:
            self._shutdown()

        # ── Auto-restart if reconnection was unrecoverable ────────────
        if self._needs_restart:
            self._restart_count += 1
            if self._restart_count <= _RESTART_MAX_ATTEMPTS:
                log.info(
                    "=" * 60 + "\n"
                    "AUTO-RESTART %d/%d — waiting %.0fs before full restart...\n"
                    + "=" * 60,
                    self._restart_count, _RESTART_MAX_ATTEMPTS,
                    _RESTART_DELAY,
                )
                time.sleep(_RESTART_DELAY)
                # Reset state for fresh start
                self._needs_restart = False
                self._subscriptions_lost = False
                self._resubscribe_pending = False
                self._live_bars = None
                self._front_month_bars = None
                self.start()  # recursive restart
            else:
                log.error(
                    "AUTO-RESTART exhausted all %d attempts — "
                    "shutting down permanently.",
                    _RESTART_MAX_ATTEMPTS,
                )

    def _signal_handler(self, signum, frame) -> None:
        log.info("Received signal %d — shutting down gracefully...", signum)
        self._running = False

    def _shutdown(self) -> None:
        log.info("Shutting down...")
        if self._live_bars is not None:
            try:
                self.manager.cancel_subscription(self._live_bars)
            except Exception:
                pass
        if self._front_month_bars is not None:
            try:
                self.manager.cancel_subscription(self._front_month_bars)
            except Exception:
                pass
        # Save warm-start cache on shutdown
        try:
            self.data_manager.save_cache()
        except Exception:
            log.warning("Failed to save warm-start cache on shutdown.")
        self.manager.disconnect()
        self.telemetry.close()
        log.info("Shutdown complete.")

    def _register_execution_callbacks(self) -> None:
        """Register IBKR execution callbacks once per connection lifecycle."""
        if self._callbacks_registered:
            return
        self.manager.ib.orderStatusEvent += self._on_order_status
        self.manager.ib.execDetailsEvent += self._on_exec_details
        self.manager.ib.commissionReportEvent += self._on_commission_report
        self._callbacks_registered = True

    def _extract_contract_month(self, contract) -> Optional[str]:
        month = getattr(contract, "lastTradeDateOrContractMonth", None)
        if not month:
            return self._front_month_str
        month_str = str(month)
        return month_str[:6] if len(month_str) >= 6 else None

    def _build_event_id(
        self,
        *,
        event_type: str,
        event_ts: str,
        order_id: Optional[int] = None,
        exec_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        raw = "|".join(
            [
                event_type,
                event_ts,
                str(order_id or ""),
                str(exec_id or ""),
                str(status or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _utc_iso_now(self) -> str:
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    def _base_tradebook_fields(
        self,
        *,
        decision_ctx: Optional[dict] = None,
    ) -> dict:
        ctx = decision_ctx or {}
        return {
            "signal_id": ctx.get("signal_id"),
            "decision_id": ctx.get("decision_id"),
            "decision_timestamp_utc": ctx.get("decision_timestamp_utc"),
            "run_id": self._run_id,
            "session_id": self._session_id,
            "hostname": self._hostname,
            "process_id": self._process_id,
            "environment": self._environment,
        }

    def _reset_position_state(self) -> None:
        self._position_entry_bar_time = None
        self._position_bars_held = 0
        self._trailing_activated = False
        self._entry_price = None
        self._atr_at_entry = None
        self._position_side = 0
        self._highest_high = 0.0
        self._lowest_low = float("inf")

    def _check_trailing_stop(self) -> None:
        """Check if trailing stop should activate and modify IBKR SL order.

        Mirrors backtest engine _on_in_position trailing logic:
        - Track highest_high / lowest_low since entry
        - When price moves +trailing_atr_mult × ATR in favor, move SL
          to entry ± trailing_sl_atr_offset × ATR
        - Modify the live IBKR STP child order in-place
        """
        if self._trailing_activated:
            return
        if self._entry_price is None or self._atr_at_entry is None:
            return
        if self._atr_at_entry <= 0:
            return

        # Update bar extremes from the latest bar
        last_bar = self.rolling_df.iloc[-1]
        bar_high = float(last_bar["High"])
        bar_low = float(last_bar["Low"])
        self._highest_high = max(self._highest_high, bar_high)
        self._lowest_low = min(self._lowest_low, bar_low)

        # Check trailing trigger condition
        triggered = False
        if self._position_side == 1:  # Long
            if self._highest_high >= (
                self._entry_price
                + self._trailing_atr_mult * self._atr_at_entry
            ):
                triggered = True
        elif self._position_side == -1:  # Short
            if self._lowest_low <= (
                self._entry_price
                - self._trailing_atr_mult * self._atr_at_entry
            ):
                triggered = True

        if not triggered:
            return

        # Calculate new SL price
        offset = self._trailing_sl_atr_offset * self._atr_at_entry
        if self._position_side == 1:
            new_sl = self._entry_price + offset
        else:
            new_sl = self._entry_price - offset
        new_sl = round(new_sl, 2)

        log.info(
            "TRAILING STOP: activated — entry=%.2f  ATR=%.4f  "
            "trigger=%.2f×ATR  offset=%.2f×ATR  new_SL=%.2f",
            self._entry_price, self._atr_at_entry,
            self._trailing_atr_mult, self._trailing_sl_atr_offset,
            new_sl,
        )

        # Find and modify the STP child order on IBKR
        try:
            for t in self.manager.ib.openTrades():
                c = getattr(t, "contract", None)
                o = getattr(t, "order", None)
                if c is None or o is None:
                    continue
                if getattr(c, "symbol", None) != "CL":
                    continue
                parent_id = getattr(o, "parentId", 0) or 0
                if parent_id == 0:
                    continue  # skip parent entry orders
                order_type = getattr(o, "orderType", "")
                if order_type != "STP":
                    continue
                old_sl = getattr(o, "auxPrice", 0.0) or 0.0
                o.auxPrice = new_sl
                self.manager.ib.placeOrder(c, o)
                log.info(
                    "TRAILING STOP: modified SL order %d: %.2f → %.2f",
                    getattr(o, "orderId", 0), old_sl, new_sl,
                )
                self._trailing_activated = True
                return
            log.warning(
                "TRAILING STOP: triggered but no STP child order found"
            )
        except Exception:
            log.exception("TRAILING STOP: failed to modify SL order")

    def _check_time_barrier(
        self,
        *,
        bar_time: pd.Timestamp,
        current_price: float,
        atr_value: Optional[float],
    ) -> bool:
        """Enforce 24-hour (288-bar) exit to match backtests."""
        current_position = self.manager.get_cl_position()
        if current_position == 0:
            self._reset_position_state()
            return False

        if self._position_entry_bar_time is None:
            # First bar after detecting a position.
            self._position_entry_bar_time = bar_time
            self._position_bars_held = 0
            return False

        self._position_bars_held += 1
        if self._position_bars_held <= self._max_hold_bars:
            return False

        cancelled = self.manager.cancel_open_cl_orders()
        trade = self.manager.close_cl_position(
            exit_mode=self._exit_mode,
            current_price=current_price,
        )
        log.info(
            "[TRADE] EXIT: TIME BARRIER after %d bars "
            "(cancelled=%d orders, position=%d, price=%.2f)",
            self._position_bars_held, cancelled, current_position,
            current_price,
        )
        self.telemetry.log_signal(
            timestamp=bar_time,
            signal="Timeout",
            confidence_pct=0.0,
            action_taken="TIME_BARRIER_EXIT",
            current_price=current_price,
            atr_value=atr_value,
            exit_reason="REASON_TIMEOUT",
            order_id=getattr(getattr(trade, "order", None), "orderId", None)
            if trade is not None
            else None,
        )
        self._reset_position_state()
        return True

    def _on_order_status(self, trade) -> None:
        """Log order status transitions as append-only tradebook events."""
        try:
            order = getattr(trade, "order", None)
            status = getattr(trade, "orderStatus", None)
            contract = getattr(trade, "contract", None)
            if order is None or status is None:
                return

            # Human-readable [TRADE] log for key status transitions
            status_str = getattr(status, "status", "")
            symbol_str = getattr(contract, "localSymbol", None) or "CL"
            action_str = getattr(order, "action", "???")
            qty = float(getattr(order, "totalQuantity", 0) or 0)
            order_type = getattr(order, "orderType", "???")
            parent_id = getattr(order, "parentId", 0)

            if status_str == "Filled":
                avg_price = float(getattr(status, "avgFillPrice", 0) or 0)
                if parent_id and parent_id != 0:
                    # Child order filled (TP or SL)
                    if order_type == "LMT":
                        exit_type = "TP HIT"
                    elif order_type == "STP":
                        exit_type = "SL HIT"
                    else:
                        exit_type = order_type
                    log.info(
                        "[TRADE] EXIT: %s %.0f %s @ %.2f (%s)",
                        action_str, qty, symbol_str, avg_price, exit_type,
                    )
                    # Activate exit-type-specific cooldown
                    if exit_type == "SL HIT" and self._sl_cooldown_bars > 0:
                        self._cooldown_remaining = self._sl_cooldown_bars
                        log.info(
                            "COOLDOWN activated: %d bars after %s",
                            self._sl_cooldown_bars, exit_type,
                        )
                    elif exit_type == "TP HIT" and self._tp_cooldown_bars > 0:
                        self._cooldown_remaining = self._tp_cooldown_bars
                        log.info(
                            "COOLDOWN activated: %d bars after %s",
                            self._tp_cooldown_bars, exit_type,
                        )
                else:
                    # Parent entry order filled
                    log.info(
                        "[TRADE] FILLED: %s %.0f %s @ %.2f",
                        action_str, qty, symbol_str, avg_price,
                    )

            order_id = getattr(order, "orderId", None)
            ctx = self._last_decision_context_by_order_id.get(order_id)
            event_ts = self._utc_iso_now()
            event_id = self._build_event_id(
                event_type="ORDER_STATUS",
                event_ts=event_ts,
                order_id=order_id,
                status=status_str,
            )
            self.telemetry.log_tradebook_event(
                event_id=event_id,
                event_type="ORDER_STATUS",
                event_timestamp_utc=event_ts,
                order_id=order_id,
                perm_id=getattr(order, "permId", None),
                parent_order_id=parent_id,
                account=getattr(order, "account", None),
                symbol=getattr(contract, "symbol", None),
                local_symbol=getattr(contract, "localSymbol", None),
                contract_month=self._extract_contract_month(contract),
                side=action_str,
                action=action_str,
                order_type=order_type,
                time_in_force=getattr(order, "tif", None),
                status=status_str,
                order_qty=qty,
                cum_fill_qty=float(getattr(status, "filled", 0) or 0),
                remaining_qty=float(getattr(status, "remaining", 0) or 0),
                avg_fill_price=float(getattr(status, "avgFillPrice", 0) or 0),
                limit_price=float(getattr(order, "lmtPrice", 0) or 0),
                stop_price=float(getattr(order, "auxPrice", 0) or 0),
                **self._base_tradebook_fields(decision_ctx=ctx),
            )
        except Exception:
            log.exception("Failed to log ORDER_STATUS tradebook event")

    def _on_exec_details(self, trade, fill) -> None:
        """Log execution fills; supports partial fills as independent events."""
        try:
            order = getattr(trade, "order", None)
            status = getattr(trade, "orderStatus", None)
            contract = getattr(trade, "contract", None)
            execution = getattr(fill, "execution", None)
            if order is None or execution is None:
                return

            # Human-readable [TRADE] fill log
            fill_price = float(getattr(execution, "price", 0) or 0)
            fill_qty = float(getattr(execution, "shares", 0) or 0)
            symbol_str = getattr(contract, "localSymbol", None) or "CL"
            side_str = (
                getattr(execution, "side", None)
                or getattr(order, "action", "???")
            )
            log.info(
                "[TRADE] FILL: %s %.0f %s @ %.2f (execId=%s)",
                side_str, fill_qty, symbol_str, fill_price,
                getattr(execution, "execId", "?"),
            )

            order_id = getattr(order, "orderId", None)
            exec_id = getattr(execution, "execId", None)
            event_dt = getattr(execution, "time", None)
            if event_dt is not None:
                ts_obj = pd.Timestamp(event_dt)
                if ts_obj.tzinfo is not None:
                    ts_obj = ts_obj.tz_convert("UTC").tz_localize(None)
                event_ts = ts_obj.isoformat()
            else:
                event_ts = self._utc_iso_now()
            ctx = self._last_decision_context_by_order_id.get(order_id)
            expected_price = None if ctx is None else ctx.get("current_price")
            slippage = None
            if expected_price is not None and fill_price > 0:
                slippage = fill_price - float(expected_price)
            event_id = self._build_event_id(
                event_type="EXECUTION_FILL",
                event_ts=event_ts,
                order_id=order_id,
                exec_id=exec_id,
            )
            self.telemetry.log_tradebook_event(
                event_id=event_id,
                event_type="EXECUTION_FILL",
                event_timestamp_utc=event_ts,
                order_id=order_id,
                perm_id=getattr(order, "permId", None),
                parent_order_id=getattr(order, "parentId", None),
                broker_execution_id=exec_id,
                account=getattr(execution, "acctNumber", None)
                or getattr(order, "account", None),
                symbol=getattr(contract, "symbol", None),
                local_symbol=getattr(contract, "localSymbol", None),
                contract_month=self._extract_contract_month(contract),
                side=side_str,
                action=getattr(order, "action", None),
                order_type=getattr(order, "orderType", None),
                time_in_force=getattr(order, "tif", None),
                status=getattr(status, "status", None) if status else None,
                order_qty=float(getattr(order, "totalQuantity", 0) or 0),
                fill_qty=fill_qty,
                cum_fill_qty=float(getattr(status, "filled", 0) or 0)
                if status
                else None,
                remaining_qty=float(getattr(status, "remaining", 0) or 0)
                if status
                else None,
                avg_fill_price=float(getattr(status, "avgFillPrice", 0) or 0)
                if status
                else None,
                last_fill_price=fill_price,
                limit_price=float(getattr(order, "lmtPrice", 0) or 0),
                stop_price=float(getattr(order, "auxPrice", 0) or 0),
                slippage_estimate=slippage,
                realized_pnl=float(getattr(execution, "realizedPNL", 0) or 0),
                **self._base_tradebook_fields(decision_ctx=ctx),
            )
        except Exception:
            log.exception("Failed to log EXECUTION_FILL tradebook event")

    def _on_commission_report(self, trade, fill, report) -> None:
        """Log commission events that can arrive after fill events."""
        try:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            execution = getattr(fill, "execution", None)
            order_id = getattr(order, "orderId", None) if order is not None else None
            exec_id = getattr(execution, "execId", None) if execution is not None else None
            event_ts = self._utc_iso_now()
            ctx = self._last_decision_context_by_order_id.get(order_id)
            event_id = self._build_event_id(
                event_type="COMMISSION",
                event_ts=event_ts,
                order_id=order_id,
                exec_id=exec_id,
            )
            self.telemetry.log_tradebook_event(
                event_id=event_id,
                event_type="COMMISSION",
                event_timestamp_utc=event_ts,
                order_id=order_id,
                perm_id=getattr(order, "permId", None) if order is not None else None,
                parent_order_id=getattr(order, "parentId", None)
                if order is not None
                else None,
                broker_execution_id=exec_id,
                account=getattr(report, "acctNumber", None),
                symbol=getattr(contract, "symbol", None)
                if contract is not None
                else None,
                local_symbol=getattr(contract, "localSymbol", None)
                if contract is not None
                else None,
                contract_month=self._extract_contract_month(contract)
                if contract is not None
                else self._front_month_str,
                side=getattr(execution, "side", None)
                if execution is not None
                else None,
                action=getattr(order, "action", None) if order is not None else None,
                order_type=getattr(order, "orderType", None)
                if order is not None
                else None,
                time_in_force=getattr(order, "tif", None)
                if order is not None
                else None,
                commission=float(getattr(report, "commission", 0) or 0),
                fees=float(getattr(report, "commission", 0) or 0),
                realized_pnl=float(getattr(report, "realizedPNL", 0) or 0),
                **self._base_tradebook_fields(decision_ctx=ctx),
            )
        except Exception:
            log.exception("Failed to log COMMISSION tradebook event")

    # ------------------------------------------------------------------
    # Account summary
    # ------------------------------------------------------------------

    def _print_account_summary(self) -> None:
        """Print a CL-only account summary at startup."""
        w = 60  # box width
        try:
            acct = self.manager.get_account_summary()
            ts = self.telemetry.trade_summary()
        except Exception:
            log.warning("Could not retrieve account summary — skipping.")
            return

        # Position description
        pos = acct["cl_position"]
        if pos == 0:
            pos_str = "FLAT (0 contracts)"
        elif pos > 0:
            pos_str = f"LONG ({pos} contract{'s' if abs(pos)!=1 else ''})"
        else:
            pos_str = f"SHORT ({pos} contract{'s' if abs(pos)!=1 else ''})"

        # Date range
        if ts["first_signal"] and ts["last_signal"]:
            first = ts["first_signal"][:10]  # YYYY-MM-DD
            last = ts["last_signal"][:10]
            date_range = f"{first} → {last}"
        else:
            date_range = "No signals recorded"

        lines = [
            "=" * w,
            "ACCOUNT SUMMARY (CL Only)".center(w),
            "=" * w,
            f"  Account:           {acct['account'] or 'N/A'}",
            f"  Net Liquidation:   ${acct['net_liquidation']:>14,.2f}",
            f"  Available Funds:   ${acct['available_funds']:>14,.2f}",
            "-" * w,
            f"  CL Position:       {pos_str}",
            f"  CL Market Value:   ${acct['cl_market_value']:>14,.2f}",
            f"  CL Avg Cost:       ${acct['cl_avg_cost']:>14,.2f}",
            f"  CL Unrealized PnL: ${acct['cl_unrealized_pnl']:>14,.2f}",
            f"  CL Realized PnL:   ${acct['cl_realized_pnl']:>14,.2f}",
            "-" * w,
            "  Trade History (telemetry):",
            f"    Total Signals:     {ts['total_signals']}",
            f"    Executed Trades:   {ts['executed_trades']}",
            f"    Bars Recorded:     {ts['total_bars']}",
            f"    Date Range:        {date_range}",
            "=" * w,
        ]
        for line in lines:
            log.info(line)

    # ------------------------------------------------------------------
    # Warm start (replaces old _cold_start)
    # ------------------------------------------------------------------

    def _warm_start(self) -> None:
        """Initialize rolling window via DataManager (seed + backfill)."""
        log.info("Warm-start: initializing via DataManager...")
        self.rolling_df = self.data_manager.initialize()

        if len(self.rolling_df) == 0:
            raise RuntimeError(
                "Warm-start failed: no data available from seed or IBKR."
            )

        # Ensure DateTime index
        if "DateTime" in self.rolling_df.columns and not isinstance(
            self.rolling_df.index, pd.DatetimeIndex
        ):
            self.rolling_df = self.rolling_df.set_index("DateTime", drop=False)

        self._last_bar_time = self.rolling_df.index[-1]
        log.info(
            "Rolling window initialized: %d bars, latest=%s",
            len(self.rolling_df), self._last_bar_time,
        )

    # ------------------------------------------------------------------
    # Live bar subscription
    # ------------------------------------------------------------------

    def _subscribe(self) -> None:
        """Subscribe to live 5-min bars (Brain stream: continuous contract)."""
        log.info("Subscribing to live 5-min bars (Brain stream)...")
        self._live_bars = self.manager.subscribe_live_bars(
            self._contract,
            bar_size="5 mins",
            duration_str="60 S",
        )
        self._live_bars.updateEvent += self._on_bar_update
        log.info("Subscribed to continuous contract live bars")

    def _subscribe_front_month(self) -> None:
        """Subscribe to live 5-min bars (Hands stream: front-month contract)."""
        log.info(
            "Subscribing to front-month bars (Hands stream: %s)...",
            self._front_month_str,
        )
        self._front_month_bars = self.manager.subscribe_live_bars(
            self._front_month_contract,
            bar_size="5 mins",
            duration_str="60 S",
        )
        self._front_month_bars.updateEvent += self._on_front_month_bar_update
        log.info("Subscribed to front-month live bars")

    # ------------------------------------------------------------------
    # Reconnection & Gap Backfill
    # ------------------------------------------------------------------

    def _on_ib_error(self, reqId, errorCode, errorString, contract) -> None:
        """Handle IBKR error events for reconnection detection."""
        # Error 10182: keepUpToDate subscriptions lost
        if errorCode == 10182:
            log.warning("SUBSCRIPTIONS LOST (Error 10182) — will resubscribe on reconnect")
            self._subscriptions_lost = True

        # Error 1102: connectivity restored, data maintained
        # Error 1101: connectivity restored, data lost
        # Warning 2104: Market data farm connection is OK
        # Warning 2106: HMDS data farm connection is OK
        if errorCode in (1101, 1102, 2104, 2106) and self._subscriptions_lost:
            if self._resubscribe_pending:
                log.info("CONNECTIVITY RESTORED (code %d) — resubscription already scheduled, skipping", errorCode)
                return
            log.info("CONNECTIVITY RESTORED (code %d) — scheduling resubscription...", errorCode)
            self._resubscribe_pending = True
            # CRITICAL: Cannot call IBKR API methods (reqHistoricalData,
            # subscribe_live_bars) inside this callback because the asyncio
            # event loop is already running.  Schedule resubscription to
            # run on the next loop iteration via ensure_future.
            import asyncio
            loop = asyncio.get_event_loop()
            loop.call_soon(lambda: asyncio.ensure_future(self._deferred_resubscribe()))

    async def _deferred_resubscribe(self) -> None:
        """Fully async resubscription — runs on the next event loop iteration.

        Uses ib_insync's async API (reqHistoricalDataAsync) so it can
        run inside the event loop without crashing.  The sync API
        (reqHistoricalData) calls loop.run_until_complete() internally,
        which fails with 'This event loop is already running' even
        from an ensure_future coroutine.
        """
        try:
            # 1. Cancel stale subscriptions (sync, safe — no network request)
            if self._live_bars is not None:
                try:
                    self.manager.cancel_subscription(self._live_bars)
                except Exception:
                    pass
            if self._front_month_bars is not None:
                try:
                    self.manager.cancel_subscription(self._front_month_bars)
                except Exception:
                    pass

            # 2. Re-subscribe using async API
            log.info("Subscribing to live 5-min bars (Brain stream)...")
            self._live_bars = await self.manager.subscribe_live_bars_async(
                self._contract,
                bar_size="5 mins",
                duration_str="60 S",
            )
            self._live_bars.updateEvent += self._on_bar_update
            log.info("Subscribed to continuous contract live bars")

            if self._front_month_contract is not None:
                log.info(
                    "Subscribing to front-month bars (Hands stream: %s)...",
                    self._front_month_str,
                )
                self._front_month_bars = await self.manager.subscribe_live_bars_async(
                    self._front_month_contract,
                    bar_size="5 mins",
                    duration_str="60 S",
                )
                self._front_month_bars.updateEvent += self._on_front_month_bar_update
                log.info("Subscribed to front-month live bars")

            self._subscriptions_lost = False
            log.info("Reconnection complete — live bars flowing again")

        except Exception:
            log.exception("Deferred resubscription failed — will retry on next reconnect")
        finally:
            self._resubscribe_pending = False

    def _resubscribe_and_backfill(self) -> None:
        """Synchronous resubscription — used by _reconnect() after a clean reconnect.

        Cancels stale subscriptions and re-subscribes using the sync API.
        This is safe to call from _reconnect() because the event loop is
        NOT running at that point (we're inside a time.sleep-based retry
        loop, not inside ib.sleep).
        """
        # 1. Cancel stale subscriptions
        if self._live_bars is not None:
            try:
                self.manager.cancel_subscription(self._live_bars)
            except Exception:
                pass
            self._live_bars = None
        if self._front_month_bars is not None:
            try:
                self.manager.cancel_subscription(self._front_month_bars)
            except Exception:
                pass
            self._front_month_bars = None

        # 2. Re-subscribe using sync API (safe outside event loop)
        self._subscribe()

        if self._front_month_contract is not None:
            self._subscribe_front_month()

        # 3. Reset connectivity flags
        self._subscriptions_lost = False
        self._resubscribe_pending = False
        log.info("Resubscription complete — live bars restored")

    def _on_front_month_bar_update(self, bars, has_new_bar) -> None:
        """Callback for front-month bars — log raw data to telemetry."""
        if not has_new_bar or not bars:
            return

        new_bar = bars[-1]
        bar_time = pd.Timestamp(new_bar.date)
        # Normalize tz-aware timestamps to tz-naive UTC
        if bar_time.tzinfo is not None:
            bar_time = bar_time.tz_convert("UTC").tz_localize(None)

        self.telemetry.log_raw_bar(
            timestamp=bar_time,
            open_=new_bar.open,
            high=new_bar.high,
            low=new_bar.low,
            close=new_bar.close,
            volume=float(new_bar.volume),
            contract_month=self._front_month_str or "UNKNOWN",
        )
        log.debug(
            "RAW BAR [%s]: %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            self._front_month_str, bar_time,
            new_bar.open, new_bar.high, new_bar.low,
            new_bar.close, float(new_bar.volume),
        )

    def _on_bar_update(self, bars, has_new_bar) -> None:
        """Callback fired by ib_insync when continuous bars are updated."""
        if not has_new_bar or not bars:
            return

        # Convert the latest bar to a row
        new_bar = bars[-1]
        # Normalize tz-aware timestamps (IBKR sends US/Eastern) to tz-naive UTC
        raw_ts = pd.Timestamp(new_bar.date)
        if raw_ts.tzinfo is not None:
            raw_ts = raw_ts.tz_convert("UTC").tz_localize(None)
        new_row = pd.DataFrame(
            [{
                "DateTime": raw_ts,
                "Open": new_bar.open,
                "High": new_bar.high,
                "Low": new_bar.low,
                "Close": new_bar.close,
                "Volume": float(new_bar.volume),
            }]
        )
        new_row = new_row.set_index(
            pd.DatetimeIndex(new_row["DateTime"]), drop=False
        )
        new_row.index.name = "DateTime"

        bar_time = new_row.index[0]

        # Deduplicate: skip if we've already seen this bar
        if self._last_bar_time is not None and bar_time <= self._last_bar_time:
            return

        self._last_bar_time = bar_time
        log.info(
            "NEW BAR: %s  O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            bar_time,
            new_row["Open"].iloc[0],
            new_row["High"].iloc[0],
            new_row["Low"].iloc[0],
            new_row["Close"].iloc[0],
            new_row["Volume"].iloc[0],
        )

        # Append to rolling window
        self.rolling_df = pd.concat([self.rolling_df, new_row])
        if len(self.rolling_df) > _MAX_ROLLING_BARS:
            self.rolling_df = self.rolling_df.iloc[-_MAX_ROLLING_BARS:]

        # Append to warm-start cache (DataManager)
        self.data_manager.append_bar(new_row)

        # Log bar to telemetry (smoothed continuous data)
        self.telemetry.log_bar(
            timestamp=bar_time,
            open_=new_row["Open"].iloc[0],
            high=new_row["High"].iloc[0],
            low=new_row["Low"].iloc[0],
            close=new_row["Close"].iloc[0],
            volume=new_row["Volume"].iloc[0],
        )

        # Run inference pipeline
        self._on_new_bar(bar_time)

    # ------------------------------------------------------------------
    # Inference + Execution
    # ------------------------------------------------------------------

    def _on_new_bar(self, bar_time: pd.Timestamp) -> None:
        """Run feature generation, strategy evaluation, and potentially execute a trade."""
        # 0. Track cooldown state (don't return early — we still want INFERENCE
        #    and BRACKET to always be visible in logs)
        in_cooldown = False
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            in_cooldown = True

        # 1. Generate features (always — needed for INFERENCE display)
        features = build_live_features(self.rolling_df, self.feature_names)
        if features is None:
            log.info("Feature generation skipped (insufficient data or NaN)")
            return

        current_price = float(self.rolling_df["Close"].iloc[-1])

        # Get ATR from the features
        atr_value = None
        if "ATR_14" in features.columns:
            atr_value = float(features["ATR_14"].iloc[0])

        # Enforce 24-hour time barrier on any open position (engine safety rail)
        if not in_cooldown and self._check_time_barrier(
            bar_time=bar_time,
            current_price=current_price,
            atr_value=atr_value,
        ):
            return

        # 2. Position guard: check both filled position AND pending orders
        #    to prevent duplicate entries when Adaptive Algo is still working
        current_position = self.manager.get_cl_position()
        pending_cl_entry_orders = 0
        try:
            for t in self.manager.ib.openTrades():
                c = getattr(t, "contract", None)
                o = getattr(t, "order", None)
                s = getattr(t, "orderStatus", None)
                if c is None or o is None:
                    continue
                if getattr(c, "symbol", None) != "CL":
                    continue
                order_status = getattr(s, "status", "") if s else ""
                parent_id = getattr(o, "parentId", 0) or 0
                # Only count parent entry orders (parentId==0), not TP/SL children
                if parent_id == 0 and order_status in (
                    "Submitted", "PreSubmitted", "PendingSubmit",
                ):
                    pending_cl_entry_orders += 1
        except Exception:
            log.debug("Failed to check pending orders", exc_info=True)

        # Treat pending entry orders as an effective position to block duplicates
        effective_position = current_position
        if pending_cl_entry_orders > 0 and current_position == 0:
            effective_position = pending_cl_entry_orders  # non-zero → blocks entry
            log.info(
                "POSITION GUARD: portfolio=0 but %d pending CL entry order(s) "
                "— treating as position=%d",
                pending_cl_entry_orders, effective_position,
            )

        # Log human-friendly PnL + bracket summary when holding a position
        if current_position != 0:
            # Find TP/SL bracket child orders
            tp_price_live = None
            sl_price_live = None
            try:
                for t in self.manager.ib.openTrades():
                    c = getattr(t, "contract", None)
                    o = getattr(t, "order", None)
                    if c is None or o is None:
                        continue
                    if getattr(c, "symbol", None) != "CL":
                        continue
                    parent_id = getattr(o, "parentId", 0) or 0
                    if parent_id == 0:
                        continue  # skip parent entry orders
                    order_type = getattr(o, "orderType", "")
                    lmt = getattr(o, "lmtPrice", 0.0) or 0.0
                    aux = getattr(o, "auxPrice", 0.0) or 0.0
                    if order_type == "LMT" and lmt > 0:
                        tp_price_live = lmt
                    elif order_type in ("STP", "TRAIL") and aux > 0:
                        sl_price_live = aux
            except Exception:
                log.warning("Bracket order scan failed", exc_info=True)

            tp_str = f"TP={tp_price_live:.2f}" if tp_price_live else "TP=N/A"
            sl_str = f"SL={sl_price_live:.2f}" if sl_price_live else "SL=N/A"
            atr_str = f"ATR={atr_value:.4f}" if atr_value else "ATR=N/A"

            try:
                # Use cached portfolio (sync) — NOT get_account_summary()
                # which calls ib.accountSummary() async and fails inside callbacks
                unrealized_pnl = 0.0
                avg_cost = 0.0
                for item in self.manager.ib.portfolio():
                    if item.contract.symbol == "CL":
                        unrealized_pnl = float(item.unrealizedPNL)
                        avg_cost = float(item.averageCost)
                        break
                # IBKR averageCost = price * multiplier (1000 for CL)
                entry_price = avg_cost / 1000.0 if avg_cost else 0.0
                log.info(
                    "[PNL] position=%d  unrealizedPnL=$%.2f  "
                    "entryPrice=%.2f  mktPrice=%.2f  %s  %s  %s  held=%d bars",
                    current_position,
                    unrealized_pnl,
                    entry_price,
                    current_price,
                    sl_str, tp_str, atr_str,
                    self._position_bars_held,
                )
            except Exception:
                # Fallback: log without account data
                log.info(
                    "[PNL] position=%d  mktPrice=%.2f  %s  %s  %s  held=%d bars",
                    current_position,
                    current_price,
                    sl_str, tp_str, atr_str,
                    self._position_bars_held,
                )
                log.warning("Portfolio lookup failed", exc_info=True)

            # Check trailing stop condition on every bar while in position
            self._check_trailing_stop()

        # 3. Delegate decision to strategy (always — needed for INFERENCE display)
        signal: TradeSignal = self.strategy.evaluate(
            features=features,
            current_price=current_price,
            atr_value=atr_value,
            current_position=effective_position,
        )

        # Shadow-replay logging: capture exact state for parity validation
        try:
            last_row = self.rolling_df.iloc[-1]
            # Prefer per-signal buy/sell probs (ensemble); fall back to direction
            if signal.buy_prob is not None or signal.sell_prob is not None:
                _prob_buy = signal.buy_prob
                _prob_sell = signal.sell_prob
            else:
                _prob_buy = signal.probability if self.strategy.direction != "SHORT" else None
                _prob_sell = signal.probability if self.strategy.direction != "LONG" else None
            self.telemetry.log_shadow_state(
                timestamp=bar_time,
                open_=float(last_row["Open"]),
                high=float(last_row["High"]),
                low=float(last_row["Low"]),
                close=float(last_row["Close"]),
                volume=float(last_row["Volume"]),
                features_dict=features.iloc[0].to_dict(),
                prob_buy=_prob_buy,
                prob_sell=_prob_sell,
                strategy_name=self.strategy.name,
            )
        except Exception:
            log.debug("Shadow state logging failed", exc_info=True)

        # Build direction-aware probability display
        direction = getattr(self.strategy, 'direction', 'LONG').upper()
        if signal.buy_prob is not None and signal.sell_prob is not None:
            # Ensemble: both probs available
            buy_prob_str = f"{signal.buy_prob:.4f}"
            sell_prob_str = f"{signal.sell_prob:.4f}"
        elif direction == "SHORT":
            buy_prob_str = "N/A"
            sell_prob_str = f"{signal.probability:.4f}"
        else:  # LONG
            buy_prob_str = f"{signal.probability:.4f}"
            sell_prob_str = "N/A"

        skip_str = f"  skip={signal.skip_reason}" if signal.skip_reason else ""
        log.info(
            "INFERENCE [%s] %s: buy_prob=%s  sell_prob=%s  "
            "signal=%s  action=%s%s",
            self.strategy.name, direction,
            buy_prob_str, sell_prob_str,
            signal.signal_label, signal.action, skip_str,
        )

        # Always log BRACKET values (computed from strategy signal)
        if signal.tp_price and signal.sl_price:
            log.info(
                "BRACKET: price=%.2f  TP=%.2f  SL=%.2f  lots=%d  ATR=%.4f",
                current_price, signal.tp_price, signal.sl_price,
                signal.lots, atr_value or 0,
            )

        # Enforce cooldown AFTER logging inference/bracket
        if in_cooldown:
            log.info(
                "COOLDOWN: %d bar(s) remaining — skipping entry",
                self._cooldown_remaining + 1,
            )
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal=signal.signal_label,
                confidence_pct=signal.confidence_pct,
                action_taken="COOLDOWN",
                current_price=current_price,
                atr_value=atr_value,
            )
            return

        # 4. Handle HOLD signals
        if signal.action == "HOLD":
            action_taken = signal.skip_reason or "HOLD"
            if signal.skip_reason == "POSITION_OPEN":
                log.info(
                    ">>> %s signal TRIGGERED (prob=%.4f) but SKIPPED: "
                    "already holding %d contracts",
                    signal.signal_label, signal.probability,
                    current_position,
                )
                action_taken = "SKIP_POSITION"
            elif signal.skip_reason == "ATR_INVALID":
                log.warning("ATR is invalid -- cannot calculate bracket levels")
                action_taken = "SKIP_ATR_INVALID"
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal=signal.signal_label,
                confidence_pct=signal.confidence_pct,
                action_taken=action_taken,
                current_price=current_price,
                atr_value=atr_value,
            )
            return

        # 5. Active signal (BUY or SELL) — bracket already logged above

        decision_timestamp_utc = bar_time.isoformat()
        signal_id = uuid.uuid4().hex
        decision_id = uuid.uuid4().hex

        # 5. Execute or dry-run
        if self.dry_run:
            log.info(
                "DRY RUN — would place bracket order %s %d CL (prob=%.2f)",
                signal.action, signal.lots, signal.probability,
            )
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal=signal.signal_label,
                confidence_pct=signal.confidence_pct,
                action_taken="DRY_RUN",
                current_price=current_price,
                atr_value=atr_value,
                tp_price=signal.tp_price,
                sl_price=signal.sl_price,
                direction=signal.action,
                signal_id=signal_id,
                decision_id=decision_id,
                decision_timestamp_utc=decision_timestamp_utc,
            )
            return

        # Place real bracket order
        if self._front_month_contract is None:
            log.error("Cannot place order: front-month contract not resolved")
            return
        try:
            # HOTFIX: Route execution to front-month contract, not continuous,
            # to prevent IBKR auto-resolution errors.
            trades = self.manager.place_bracket_order(
                contract=self._front_month_contract,
                action=signal.action,
                quantity=signal.lots,
                limit_price=current_price,
                tp_price=signal.tp_price,
                sl_price=signal.sl_price,
                entry_mode=self.entry_mode,
                adaptive_priority=self.adaptive_priority,
            )
            parent_trade = trades[0]
            order_id = parent_trade.order.orderId
            parent_order = parent_trade.order
            order_type_str = getattr(parent_order, "orderType", "???")
            algo_str = getattr(parent_order, "algoStrategy", None)
            if algo_str:
                order_type_str = f"{order_type_str}+{algo_str}"
            self._position_entry_bar_time = bar_time
            self._position_bars_held = 0
            # Capture trailing stop context at entry
            self._entry_price = current_price
            self._atr_at_entry = atr_value
            self._position_side = 1 if signal.action == "BUY" else -1
            self._trailing_activated = False
            self._highest_high = float(self.rolling_df["High"].iloc[-1])
            self._lowest_low = float(self.rolling_df["Low"].iloc[-1])
            local_sym = getattr(
                self._front_month_contract, "localSymbol", "CL"
            )
            log.info(
                "[TRADE] ENTRY: %s %d %s @ %s  "
                "TP=%.2f  SL=%.2f  (prob=%.2f, orderId=%d)",
                signal.action, signal.lots, local_sym,
                order_type_str,
                signal.tp_price, signal.sl_price, signal.probability,
                order_id,
            )
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal=signal.signal_label,
                confidence_pct=signal.confidence_pct,
                action_taken="EXECUTE",
                current_price=current_price,
                atr_value=atr_value,
                tp_price=signal.tp_price,
                sl_price=signal.sl_price,
                order_id=order_id,
                direction=signal.action,
                signal_id=signal_id,
                decision_id=decision_id,
                decision_timestamp_utc=decision_timestamp_utc,
            )
            decision_ctx = {
                "signal_id": signal_id,
                "decision_id": decision_id,
                "decision_timestamp_utc": decision_timestamp_utc,
                "current_price": current_price,
            }
            for trade in trades:
                order = getattr(trade, "order", None)
                contract = getattr(trade, "contract", None)
                if order is None:
                    continue
                child_order_id = getattr(order, "orderId", None)
                if child_order_id is not None:
                    self._last_decision_context_by_order_id[child_order_id] = decision_ctx
                event_ts = self._utc_iso_now()
                event_id = self._build_event_id(
                    event_type="ORDER_SUBMITTED",
                    event_ts=event_ts,
                    order_id=child_order_id,
                )
                self.telemetry.log_tradebook_event(
                    event_id=event_id,
                    event_type="ORDER_SUBMITTED",
                    event_timestamp_utc=event_ts,
                    order_id=child_order_id,
                    perm_id=getattr(order, "permId", None),
                    parent_order_id=getattr(order, "parentId", None),
                    account=getattr(order, "account", None),
                    symbol=getattr(contract, "symbol", None),
                    local_symbol=getattr(contract, "localSymbol", None),
                    contract_month=self._extract_contract_month(contract),
                    side=getattr(order, "action", None),
                    action=getattr(order, "action", None),
                    order_type=getattr(order, "orderType", None),
                    time_in_force=getattr(order, "tif", None),
                    status="SUBMITTED",
                    order_qty=float(getattr(order, "totalQuantity", 0) or 0),
                    limit_price=float(getattr(order, "lmtPrice", 0) or 0),
                    stop_price=float(getattr(order, "auxPrice", 0) or 0),
                    **self._base_tradebook_fields(decision_ctx=decision_ctx),
                )
        except Exception as exc:
            log.error("Failed to place bracket order: %s", exc)
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal=signal.signal_label,
                confidence_pct=signal.confidence_pct,
                action_taken=f"ERROR: {exc}",
                current_price=current_price,
                atr_value=atr_value,
                tp_price=signal.tp_price,
                sl_price=signal.sl_price,
                direction=signal.action,
                signal_id=signal_id,
                decision_id=decision_id,
                decision_timestamp_utc=decision_timestamp_utc,
            )

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    def _reconnect(self) -> bool:
        """Reconnect to IB Gateway with exponential backoff.

        Returns True if reconnection + resubscription succeeded.
        """
        delay = _RECONNECT_BASE_DELAY
        for attempt in range(1, _RECONNECT_MAX_ATTEMPTS + 1):
            if not self._running:
                return False
            log.info(
                "Reconnect attempt %d/%d (waiting %.0fs)...",
                attempt, _RECONNECT_MAX_ATTEMPTS, delay,
            )
            time.sleep(delay)
            try:
                # Ensure clean disconnect state
                try:
                    self.manager.ib.disconnect()
                except Exception:
                    pass
                # Reconnect
                self.manager.connect()
                # Re-register error handler (lost on disconnect)
                self.manager.ib.errorEvent += self._on_ib_error
                self._callbacks_registered = False
                self._register_execution_callbacks()
                # Resubscribe + backfill gaps
                self._subscriptions_lost = True
                self._resubscribe_and_backfill()
                log.info("Reconnected successfully on attempt %d", attempt)
                return True
            except Exception as exc:
                log.warning("Reconnect attempt %d failed: %s", attempt, exc)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)
        return False

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def _event_loop(self) -> None:
        """Main event loop — uses ib.sleep() to avoid blocking the async IB connection."""
        log.info("Entering event loop (poll every %.1fs) ...", _POLL_INTERVAL)
        log.info("Press Ctrl+C to stop.")

        while self._running:
            try:
                self.manager.ib.sleep(_POLL_INTERVAL)
            except KeyboardInterrupt:
                self._running = False
            except (ConnectionError, OSError) as exc:
                log.error("Connection lost: %s — attempting reconnect...", exc)
                if not self._reconnect():
                    log.error(
                        "Reconnection failed after %d attempts — "
                        "attempting full restart...",
                        _RECONNECT_MAX_ATTEMPTS,
                    )
                    # Signal that we need a full restart
                    self._running = False
                    self._needs_restart = True
            except Exception:
                log.exception("Error in event loop iteration")
                time.sleep(_POLL_INTERVAL)

        log.info("Event loop exited.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    available = ", ".join(sorted(_STRATEGY_REGISTRY.keys()))
    parser = argparse.ArgumentParser(
        description="CL Analyst — Live Execution Engine"
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="IBKR TWS/Gateway host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=4002,
        help="IBKR primary port (default: 4002 for IB Gateway; falls back to 7497 TWS)",
    )
    parser.add_argument(
        "--client-id", type=int, default=1,
        help="IBKR client ID (default: 1; overridden by config live_config.client_id)",
    )
    parser.add_argument(
        "--strategy", default=_DEFAULT_STRATEGY,
        help=f"Strategy to use (available: {available}; default: {_DEFAULT_STRATEGY})",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to a strategy JSON config file (overrides --strategy)",
    )
    parser.add_argument(
        "--db-path", default=_DEFAULT_DB_PATH,
        help="Path to the telemetry SQLite database",
    )
    parser.add_argument(
        "--quantity", type=int, default=_DEFAULT_QUANTITY,
        help="Number of CL contracts per trade (default: 1)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without placing real orders (log signals only)",
    )
    parser.add_argument(
        "--seed-path", default=_DEFAULT_SEED_PATH,
        help="Path to the immutable seed CSV (cl-5m_bk.csv)",
    )
    parser.add_argument(
        "--cache-path", default=_DEFAULT_CACHE_PATH,
        help="Path to the warm-start Parquet cache",
    )
    parser.add_argument(
        "--entry-mode", default=None,
        choices=["adaptive", "marketable_limit", "market"],
        help=(
            "Entry order type: 'adaptive' (IBKR algo, default), "
            "'marketable_limit' (limit 2 ticks through NBBO), "
            "'market' (plain MKT). Overrides live_config.entry_mode in JSON."
        ),
    )
    parser.add_argument(
        "--adaptive-priority", default=None,
        choices=["Normal", "Urgent", "Patient"],
        help="Adaptive algo urgency (default: Normal). Only used with --entry-mode adaptive.",
    )

    args = parser.parse_args()

    # Resolve strategy: --config takes priority over --strategy
    config_client_id: int | None = None
    config_entry_mode: str | None = None
    config_adaptive_priority: str | None = None
    config_exit_mode: str | None = None
    if args.config is not None:
        strategy = ConfigurableStrategy(
            config_path=args.config,
            base_quantity=args.quantity,
        )
        # Read live_config overrides from the strategy JSON
        live_cfg = strategy.config.get("live_config", {})
        config_client_id = live_cfg.get("client_id")
        config_entry_mode = live_cfg.get("entry_mode")
        config_adaptive_priority = live_cfg.get("adaptive_priority")
        config_exit_mode = live_cfg.get("exit_mode")
    else:
        strategy_key = args.strategy.upper()
        if strategy_key not in _STRATEGY_REGISTRY:
            parser.error(
                f"Unknown strategy '{args.strategy}'. "
                f"Available: {available}"
            )
        strategy_cls = _STRATEGY_REGISTRY[strategy_key]
        strategy = strategy_cls(base_quantity=args.quantity)

    # CLI --client-id takes priority; if not explicitly set (== 1 default),
    # fall back to config's live_config.client_id
    resolved_client_id = args.client_id
    if resolved_client_id == 1 and config_client_id is not None:
        resolved_client_id = config_client_id

    # ── Per-strategy isolation ────────────────────────────────────
    # Derive per-client_id cache and DB paths to prevent concurrent
    # write conflicts when running multiple LiveTrader instances.
    resolved_db_path = args.db_path
    resolved_cache_path = args.cache_path

    if resolved_client_id != 1:
        cid_suffix = f"_cid{resolved_client_id}"

        # Only override if user hasn't explicitly set a custom path
        if resolved_db_path == _DEFAULT_DB_PATH:
            resolved_db_path = str(
                _dp_data_root() / f"live_telemetry{cid_suffix}.db"
            )

        if resolved_cache_path == _DEFAULT_CACHE_PATH:
            resolved_cache_path = str(
                _dp_data_root() / "processed"
                / f"warm_start_cache{cid_suffix}.parquet"
            )

        log.info(
            "Multi-instance isolation: client_id=%d  "
            "db=%s  cache=%s",
            resolved_client_id,
            Path(resolved_db_path).name,
            Path(resolved_cache_path).name,
        )

    # ── IBKR subscription advisory ───────────────────────────────
    # Each LiveTrader instance creates 2 IBKR real-time data lines
    # (continuous + front-month). IBKR's default limit is ~100 lines.
    # With N strategies, that's 2*N lines. This is wasteful but safe
    # for < ~50 concurrent strategies.
    if resolved_client_id != 1:
        log.info(
            "NOTE: This instance (client_id=%d) creates its own IBKR "
            "data subscriptions. With many concurrent strategies, "
            "consider a shared data broadcaster.",
            resolved_client_id,
        )

    # ── Resolve entry_mode: CLI > config > default ────────────────
    resolved_entry_mode = args.entry_mode
    if resolved_entry_mode is None:
        resolved_entry_mode = config_entry_mode or "adaptive"

    resolved_adaptive_priority = args.adaptive_priority
    if resolved_adaptive_priority is None:
        resolved_adaptive_priority = config_adaptive_priority or "Normal"

    # ── Resolve exit_mode: config > default ────────────────────────
    resolved_exit_mode = config_exit_mode or "market"

    trader = LiveTrader(
        host=args.host,
        port=args.port,
        client_id=resolved_client_id,
        strategy=strategy,
        db_path=resolved_db_path,
        seed_path=args.seed_path,
        cache_path=resolved_cache_path,
        quantity=args.quantity,
        dry_run=args.dry_run,
        entry_mode=resolved_entry_mode,
        adaptive_priority=resolved_adaptive_priority,
        exit_mode=resolved_exit_mode,
    )
    # Enable persistent file logging now that client_id is resolved
    _setup_file_logging(resolved_client_id)

    trader.start()


if __name__ == "__main__":
    main()
