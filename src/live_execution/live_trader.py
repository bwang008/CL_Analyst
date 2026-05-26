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

# Suppress Intel Fortran runtime Ctrl+C interception on Windows.
# The Fortran runtime (libifcoremd.dll, loaded by NumPy/MKL) intercepts
# console events before Python's signal handler, causing
# "forrtl: error (200): program aborting due to control-C event".
import os as _os
_os.environ.setdefault("FOR_DISABLE_CONSOLE_CTRL_HANDLER", "1")

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
import threading
import time
import uuid
import collections
import logging as _logging
import psutil
import shutil
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
from src.features.macro_features import MacroFeatureEngine
from src.live_execution.strategy import Strategy, TradeSignal

from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.data_manager import DataManager
from src.live_execution.ibkr_client import (
    IBKRConnectionManager,
    build_cl_contract,
    ib_bars_to_dataframe,
)
from src.live_execution.telemetry import TelemetryDB
from src.live_execution.utils.telegram_alert import TelegramAlerter
# Phase 1 modularization: extracted modules
from src.live_execution.feature_pipeline import (  # noqa: F401
    build_live_features,
    _ALPHA_WINDOWS,
    _ALPHA_WINDOWS_SET_07,
    _MACRO_WINDOWS_SET_07,
    _SET_07_SENTINEL_FEATURES,
)
from src.live_execution.log_config import (  # noqa: F401
    _TelegramLogCapture,
    CLOnlyLogFilter,
    _setup_file_logging,
    _LOG_DIR,
    _LOG_FORMAT,
    _LOG_DATE_FORMAT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from src.data_paths import get_data_path as _dp_data_path, get_data_root as _dp_data_root

_DEFAULT_DB_PATH = str(_dp_data_path("live_telemetry.db"))


# Rolling window size — must be >= largest seed lookback (150 days × 288 bars/day)
# plus margin for IBKR backfill and live bars.
# Parity note (2026-03-08): 52/80 features diverged >2σ when window was too small.
# Features with long lookbacks (MACRO_3M, VOL_ROC_10080) need the full history.
_MAX_ROLLING_BARS = 44_000

# Trade parameters (engine-level safety rails)
_DEFAULT_QUANTITY = 1  # 1 CL contract (base lot)
_MAX_HOLD_BARS = 288  # 24 hours on 5-min bars

# Strategy registry — maps CLI names to strategy classes
_STRATEGY_REGISTRY: dict[str, type[Strategy]] = {}
_DEFAULT_STRATEGY = None

# Polling interval in seconds (ib.sleep)
_POLL_INTERVAL = 5.0

# Reconnection parameters
_RECONNECT_BASE_DELAY = 5.0      # Initial delay before reconnect attempt (seconds)
_RECONNECT_MAX_DELAY = 300.0     # Max backoff delay (5 minutes)
_RECONNECT_MAX_ATTEMPTS = 50     # Max retry attempts (~2+ hours of retries)

# Stale bar watchdog: force reconnect when bars stop arriving
_STALE_BAR_THRESHOLD_MINUTES = 15  # Minutes without a bar before forcing reconnect

# Auto-restart parameters (process-level recovery)
_RESTART_MAX_ATTEMPTS = 5        # Max full restart attempts
_RESTART_DELAY = 300.0           # Delay between restart attempts (5 minutes)

# Default paths for DataManager (CL_DATA_ROOT primary, repo-local fallback)
_DEFAULT_SEED_PATH = str(_dp_data_path("raw/cl-5m_bk.csv"))
_DEFAULT_CACHE_PATH = str(
    _dp_data_root() / "processed" / "warm_start_cache.parquet"
)

# ---------------------------------------------------------------------------
# Logging (CLOnlyLogFilter, _setup_file_logging moved to log_config.py)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATE_FORMAT,
)
log = logging.getLogger("LiveTrader")

# Suppress non-CL noise from ib_insync internal logging (callbacks originate in wrapper)
logging.getLogger("ib_insync.wrapper").addFilter(CLOnlyLogFilter())



# ---------------------------------------------------------------------------
# Feature Pipeline — moved to src.live_execution.feature_pipeline (Phase 1)
# build_live_features() is imported and re-exported above.
# ---------------------------------------------------------------------------


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
        self._needs_macro: bool = any(
            f.startswith(("MACRO_VIX", "MACRO_OVX", "MACRO_DXY",
                          "MACRO_YIELD_CURVE", "MACRO_FED_FUNDS", "COT_"))
            for f in self.feature_names
        )
        self._last_macro_check_time: float = 0.0

        # Read max_hold_bars from strategy config (keeps backtest & live in sync)
        strategy_config = getattr(strategy, "config", {})

        # Parse config through centralized StrategyConfig dataclass
        # to ensure parity with BacktestEngine.from_config()
        from src.live_execution.strategy_config import StrategyConfig
        _sc = StrategyConfig.from_dict(strategy_config)

        self._max_hold_bars: int = _sc.max_hold_bars

        # Trailing stop config (parity with backtest engine)
        self._trailing_atr_mult: float = _sc.trailing_atr_mult
        # SL placement offset after trailing stop activation.
        # Supports both new key (trailing_sl_atr_offset) and legacy key
        # (trailing_activation_mult) via StrategyConfig.from_dict().
        self._trailing_sl_atr_offset: float = _sc.trailing_sl_atr_offset
        self._trailing_sl_atr_offset_long: float = _sc.long.trailing_sl_atr_offset
        self._trailing_sl_atr_offset_short: float = _sc.short.trailing_sl_atr_offset
        # Exit mode for time-barrier exits (separate from entry_mode)
        self._exit_mode: str = exit_mode
        # Engine-level hard position cap (defense-in-depth)
        # Computed from the highest lot size across all tiers, or from
        # max_concurrent * quantity.  This prevents position accumulation
        # regardless of strategy-level guards.
        max_lots_from_tiers = quantity
        for tier_list_key in ("long", "short"):
            tier_list = strategy_config.get(tier_list_key, {})
            if isinstance(tier_list, dict):
                for tier in tier_list.get("tiers", []):
                    tier_lots = int(tier.get("lots", 1))
                    if tier_lots > max_lots_from_tiers:
                        max_lots_from_tiers = tier_lots
        sizing_tiers = strategy_config.get("sizing_tiers", {})
        for _, lots_val in sizing_tiers.items():
            if int(lots_val) > max_lots_from_tiers:
                max_lots_from_tiers = int(lots_val)
        self._max_position_size: int = int(
            strategy_config.get("max_position_size", max_lots_from_tiers)
        )
        self._emergency_halt: bool = False
        self._order_timestamps: list[float] = []
        self._last_filled_entry_order_id = None
        log.info("Strategy: %s  direction=%s", strategy.name, strategy.direction)

        # Read execution_symbol from strategy config (Brain=CL, Hands=CL or MCL)
        self._execution_symbol: str = strategy_config.get(
            "execution_symbol", "CL"
        ).upper()
        # Whether to use the lean (momentum-only) feature path
        self._lean_features: bool = bool(
            strategy_config.get("lean_features", False)
        )

        # Extract designated primary stream from config (e.g. "1h" or "5m")
        self._bar_size: str = strategy_config.get("bar_size", "5m").lower()

        # ATR period for bracket sizing (separate from ATR_14 model feature).
        # The model always uses ATR_14 as a feature, but bracket placement
        # (TP/SL/trailing) can use a different ATR period found by optimizer.
        self._atr_period: int = _sc.atr_period
        # Per-side ATR periods (parity with BacktestEngine)
        # The optimizer may find different optimal ATR periods for long vs short.
        # Both must be pre-computed as rolling columns on the DataFrame.
        self._atr_period_long: int = _sc.long.atr_period
        self._atr_period_short: int = _sc.short.atr_period

        log.info(
            "Entry mode: %s  adaptive_priority=%s  max_hold_bars=%d  "
            "trailing_atr_mult=%.2f  "
            "trailing_sl_offset=%.2f  exit_mode=%s  max_position=%d  "
            "execution_symbol=%s  lean_features=%s  atr_period=%d  "
            "atr_period_long=%d  atr_period_short=%d",
            entry_mode, adaptive_priority, self._max_hold_bars,
            self._trailing_atr_mult,
            self._trailing_sl_atr_offset, self._exit_mode,
            self._max_position_size,
            self._execution_symbol, self._lean_features,
            self._atr_period, self._atr_period_long, self._atr_period_short,
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

        # DataManagers for warm-start (Two-Brain Hub)
        self.data_manager_5m = DataManager(
            seed_path=seed_path,
            cache_path=cache_path,
            ibkr_manager=self.manager,
            bar_size="5 mins",
            bars_per_day=288,
        )

        self.data_manager_1h = None
        if self._bar_size in ("1h", "2h", "4h"):
            # 1h models use a dedicated 1h data manager to avoid pacing limits.
            # Seed from the full historical 1H parquet (cl-1h_bk_HourSet_06.parquet)
            # which lives alongside the processed datasets in CL_DATA_ROOT/data/processed/.
            from src.data_paths import get_data_root as _get_data_root
            _data_root = _get_data_root()
            cache_path_1h = str(_data_root / "processed" / "warm_start_cache_1h.parquet")
            seed_path_1h = str(_data_root / "processed" / "CL_HourSet_08.parquet")
            self.data_manager_1h = DataManager(
                seed_path=seed_path_1h,
                cache_path=cache_path_1h,
                ibkr_manager=self.manager,
                bar_size="1 hour",
                bars_per_day=24,
            )

        # Thread-Safe Virtual Ledger State
        self._ledger_lock = threading.Lock()
        self._virtual_ledger = {
            "5m": 0,
            "1h": 0,
        }

        # State
        self.rolling_df_5m: Optional[pd.DataFrame] = None
        self.rolling_df_1h: Optional[pd.DataFrame] = None
        self._live_bars_5m = None
        self._live_bars_1h = None
        self._front_month_bars = None  # Two-Stream: raw front-month
        self._contract = None
        self._front_month_contract = None
        self._front_month_str: Optional[str] = None
        self._running = False
        self._last_bar_time_5m: Optional[pd.Timestamp] = None
        self._last_bar_time_1h: Optional[pd.Timestamp] = None
        self._subscriptions_lost = False  # Track connectivity drops
        self._resubscribe_pending = False  # Prevent duplicate resubscription scheduling
        self._callbacks_registered = False
        self._last_decision_context_by_order_id: dict[int, dict] = {}
        self._position_entry_bar_time: Optional[pd.Timestamp] = None
        self._position_bars_held: int = 0
        # Entry order TTL: cancel unfilled entry orders after 1 bar
        self._pending_entry_order_id: Optional[int] = None
        self._pending_entry_bar_time: Optional[pd.Timestamp] = None
        # Trailing stop state (parity with backtest engine _on_in_position)
        self._trailing_activated: bool = False
        self._entry_price: Optional[float] = None
        self._atr_at_entry: Optional[float] = None
        self._position_side: int = 0  # +1 long, -1 short
        self._highest_high: float = 0.0
        self._lowest_low: float = float("inf")
        # Per-trade overrides (reset each trade via _reset_position_state)
        self._trade_trailing_atr_mult: Optional[float] = None
        self._trade_max_hold_bars: Optional[int] = None
        # TP/SL order tracking for software-side OCA (no parentId linkage)
        self._tp_order_ids: list[int] = []
        self._sl_order_id: Optional[int] = None
        # Persistent set of order IDs already processed as TP/SL exits.
        # Intentionally NOT cleared by _reset_position_state() so that a
        # duplicate IBKR Filled callback arriving after the state reset cannot
        # misidentify the same exit order as a new entry fill.
        self._processed_exit_order_ids: set[int] = set()
        self._processed_entry_order_ids: set[int] = set()
        # Active trade ID for position ledger tracking (OOB close detection)
        self._active_trade_id: Optional[str] = None
        self._run_id = (
            f"live-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        self._session_id = uuid.uuid4().hex
        self._hostname = socket.gethostname()
        self._process_id = os.getpid()
        self._environment = "paper" if self.port in (4002, 7497) else "live"

        # Telegram alerts (fire-and-forget — failures never affect trading)
        self._telegram = TelegramAlerter()
        self._bot_start_time = datetime.now(timezone.utc)
        self._last_inference_time_sec: float = 0.0
        self._last_inference_bar_time: Optional[pd.Timestamp] = None
        self._heartbeat_stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        # Event set on SIGINT/SIGTERM — used to interrupt interruptible sleeps
        self._stop_event = threading.Event()

        # Attach log capture handler to root logger
        self._log_capture = _TelegramLogCapture(maxlen=8)
        self._log_capture.setFormatter(
            _logging.Formatter("%(levelname)s [%(name)s] %(message)s")
        )
        _logging.getLogger().addHandler(self._log_capture)

    # (_prob_to_lots moved to Strategy subclasses)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _build_heartbeat_payload(self, recent_errors: list | None = None) -> str:
        """Format the heartbeat payload for Telegram."""
        uptime = datetime.now(timezone.utc) - self._bot_start_time
        uptime_str = str(uptime).split('.')[0]
        broker_status = "🟢 Connected" if self.manager.ib.isConnected() else "🔴 Disconnected"

        current_position = 0
        unrealized_pnl = 0.0
        realized_pnl = 0.0

        # Guard: only query IBKR if connected.  This method may be called
        # from the TelegramHeartbeat daemon thread, which has no asyncio
        # event loop.  Calling ensure_connected() / connect() from that
        # thread crashes with "There is no current event loop in thread".
        if self.manager.ib.isConnected():
            try:
                current_position = self.manager.get_cl_position(symbol=self._execution_symbol)
            except Exception:
                pass

            try:
                for item in self.manager.ib.portfolio():
                    if item.contract.symbol == "CL":
                        unrealized_pnl = float(item.unrealizedPNL)
                        realized_pnl = float(item.realizedPNL)
                        break

                # If position is flat, IBKR drops the contract from portfolio().
                # Fall back to the daily account-level Realized PnL to prevent resetting to $0.
                if current_position == 0:
                    for av in self.manager.ib.accountValues():
                        if av.tag == "RealizedPnL" and av.currency == "USD":
                            realized_pnl = float(av.value)
            except Exception:
                pass

        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            ram_pct = ram.percent
            disk = shutil.disk_usage("/")
            disk_pct = (disk.used / disk.total) * 100
        except Exception:
            cpu_pct = ram_pct = disk_pct = 0.0

        if self._last_inference_bar_time is not None:
            infer_str = f"`{self._last_inference_bar_time}`"
        else:
            infer_str = "❌ None (inference not yet computed)"

        payload = (
            f"⏱️ Uptime: `{uptime_str}` | Broker: {broker_status}\n\n"
            f"📈 *Position & PnL*\n"
            f"Position: `{current_position}`\n"
            f"Unrealized PnL: `${unrealized_pnl:,.2f}`\n"
            f"Realized PnL: `${realized_pnl:,.2f}`\n\n"
            f"🧠 *MLOps & System*\n"
            f"Last Inference Bar: {infer_str}\n"
            f"Inference Latency: `{self._last_inference_time_sec:.4f}s`\n"
            f"CPU: `{cpu_pct:.1f}%` | RAM: `{ram_pct:.1f}%` | Disk: `{disk_pct:.1f}%`"
        )

        if recent_errors:
            lines = []
            for level, msg, ts_utc in recent_errors[-5:]:
                icon = "🚨" if level == "ERROR" else "⚠️"
                lines.append(f"{icon} `{ts_utc}` `{msg[:150]}`")
            payload += "\n\n📝 *Recent Warnings/Errors*\n" + "\n".join(lines)

        return payload

    def _start_heartbeat_thread(self) -> None:
        """Start the background 1-hour heartbeat thread."""
        self._heartbeat_stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="TelegramHeartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        log.info("Heartbeat thread started (interval=3600s).")

    def _heartbeat_loop(self) -> None:
        """Daemon thread: send a Telegram health-check pulse every 60 minutes."""
        _INTERVAL = 3600  # seconds
        while not self._heartbeat_stop_event.wait(timeout=_INTERVAL):
            try:
                recent_errors = self._log_capture.drain()
                payload = self._build_heartbeat_payload(recent_errors=recent_errors)
                self._telegram.send(f"💓 *1-Hour Heartbeat*\n\n" + payload)
            except Exception:
                pass  # Never let heartbeat crash the thread

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
            #         Use execution_symbol from config (CL or MCL)
            try:
                self._front_month_contract, self._front_month_str = (
                    self.manager.get_front_month_contract(
                        symbol=self._execution_symbol,
                    )
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

            # Step 6: Pass front-month ID to DataManagers for rollover detection
            if self._front_month_contract is not None:
                self.data_manager_5m.front_month_id = (
                    self._front_month_contract.localSymbol
                )
                if self.data_manager_1h is not None:
                    self.data_manager_1h.front_month_id = (
                        self._front_month_contract.localSymbol
                    )

            # Step 7: Refresh external macro data if stale and model needs it
            if self._needs_macro:
                log.info("Model uses external macro features — checking freshness...")
                MacroFeatureEngine().refresh_if_stale()
                self._last_macro_check_time = time.time()

            # Step 8: Warm-start via DataManager
            self._warm_start()

            # Step 7b: Recover any inherited position from the ledger
            self._recover_inherited_position()

            # Step 7c: Cancel any orphaned CL orders if we booted FLAT
            self._cancel_orphaned_orders_on_startup()

            # Step 8: Subscribe to live bars (Brain stream)
            self._subscribe()

            # Step 9: Subscribe to front-month bars (Hands stream)
            if self._front_month_contract is not None:
                self._subscribe_front_month()

            # Step 10: Enter event loop
            self._running = True

            # ── Telegram: startup confirmation ────────────────────────
            startup_msg = (
                f"🚀 *LiveTrader Online*\n"
                f"Strategy: `{self.strategy.name}`\n"
                f"Environment: `{self._environment}`\n"
                f"Host: `{self._hostname}`\n"
                f"Dry-run: `{self.dry_run}`\n\n"
            )
            startup_msg += self._build_heartbeat_payload()
            self._telegram.send(startup_msg)

            # Start clock-driven heartbeat (independent of inference)
            self._start_heartbeat_thread()

            self._event_loop()

        except Exception as _fatal_exc:
            log.exception("Fatal error in LiveTrader")
            # ── Telegram: fatal exception alert ───────────────────────
            self._telegram.send(
                f"🚨 *FATAL ERROR — LiveTrader Down*\n"
                f"Strategy: `{self.strategy.name}`\n"
                f"Error: `{type(_fatal_exc).__name__}: {str(_fatal_exc)[:200]}`\n"
                f"Host: `{self._hostname}`",
            )
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
                self._live_bars_5m = None
                self._live_bars_1h = None
                self._front_month_bars = None
                self.start()  # recursive restart
            else:
                log.error(
                    "AUTO-RESTART exhausted all %d attempts — "
                    "shutting down permanently.",
                    _RESTART_MAX_ATTEMPTS,
                )

    def _signal_handler(self, signum, frame) -> None:
        log.info("Received signal %d — stopping (hard kill in 5s if needed)", signum)
        self._running = False
        self._stop_event.set()  # wake any interruptible sleeps immediately

        # Hard-kill watchdog: if shutdown hasn't finished in 5 seconds, force-exit.
        # This prevents the process from hanging during reconnect waits or
        # slow IB disconnect calls.
        def _force_kill():
            import time as _time
            _time.sleep(5)
            log.warning("Shutdown timed out — forcing exit (os._exit).")
            _os._exit(1)

        _kill_thread = threading.Thread(target=_force_kill, name="ShutdownWatchdog", daemon=True)
        _kill_thread.start()

    def _shutdown(self) -> None:
        log.info("Shutting down...")
        if self._live_bars_5m is not None:
            try:
                self.manager.cancel_subscription(self._live_bars_5m)
            except Exception:
                pass
        if self._live_bars_1h is not None:
            try:
                self.manager.cancel_subscription(self._live_bars_1h)
            except Exception:
                pass
        if self._front_month_bars is not None:
            try:
                self.manager.cancel_subscription(self._front_month_bars)
            except Exception:
                pass
        # Save warm-start caches on shutdown
        try:
            self.data_manager_5m.save_cache()
            if self.data_manager_1h is not None:
                self.data_manager_1h.save_cache()
        except Exception:
            log.warning("Failed to save warm-start cache on shutdown.")
        # Stop heartbeat thread
        self._heartbeat_stop_event.set()
        self.manager.disconnect()
        self.telemetry.close()
        _logging.getLogger().removeHandler(self._log_capture)
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
        # Reset per-trade overrides (back to global config defaults)
        self._trade_trailing_atr_mult = None
        self._trade_max_hold_bars = None
        # Clear TP/SL order tracking
        self._tp_order_ids = []
        self._sl_order_id = None
        self._active_trade_id: Optional[str] = None

    def _check_entry_order_ttl(self, bar_time: pd.Timestamp) -> None:
        """Cancel stale entry orders that haven't filled after 1 bar.

        If an Adaptive/Limit entry order was placed on the previous bar
        and is still pending (PreSubmitted/Submitted), cancel it and all
        bracket children so the position guard unblocks for new signals.
        """
        if self._pending_entry_order_id is None:
            return
        if self._pending_entry_bar_time is None:
            return
        # Only cancel if at least 1 bar has elapsed
        if bar_time <= self._pending_entry_bar_time:
            return

        # Check if the parent entry order is still open on IBKR
        still_pending = False
        try:
            for t in self.manager.ib.openTrades():
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                s = getattr(t, "orderStatus", None)
                if o is None or c is None:
                    continue
                if getattr(c, "symbol", None) != "CL":
                    continue
                order_id = getattr(o, "orderId", None)
                if order_id == self._pending_entry_order_id:
                    status_str = getattr(s, "status", "") if s else ""
                    if status_str in (
                        "Submitted", "PreSubmitted", "PendingSubmit",
                    ):
                        still_pending = True
                    break
        except Exception:
            log.debug("TTL check: failed to query open trades", exc_info=True)
            return

        if not still_pending:
            # Order already filled or cancelled — clear pending state
            self._pending_entry_order_id = None
            self._pending_entry_bar_time = None
            return

        # Cancel the stale entry + bracket children
        log.info(
            "ENTRY TTL: cancelling unfilled entry order %d "
            "(placed at %s, now %s — 1 bar TTL expired)",
            self._pending_entry_order_id,
            self._pending_entry_bar_time,
            bar_time,
        )
        try:
            cancelled = self.manager.cancel_open_cl_orders()
            log.info(
                "ENTRY TTL: cancelled %d CL order(s)", cancelled,
            )
        except Exception:
            log.exception("ENTRY TTL: failed to cancel stale orders")

        self._pending_entry_order_id = None
        self._pending_entry_bar_time = None
        self._reset_position_state()

    def _snapshot_decision_state(self, event_type: str) -> None:
        """Capture and persist the current FSM state for parity auditing.

        Call at three points:
        1. ENTRY — after entry order is placed
        2. BRACKET_PLACED — after TP/SL children are attached
        3. TRAILING_ACTIVATED — when trailing SL modifies the bracket
        """
        trade_id = self._active_trade_id
        if trade_id is None:
            return
        try:
            self.telemetry.log_decision_state(
                trade_id=trade_id,
                event_type=event_type,
                event_timestamp_utc=self._utc_iso_now(),
                entry_price=self._entry_price,
                position_side=self._position_side,
                atr_at_entry=self._atr_at_entry,
                bracket_atr=self._atr_at_entry,
                tp_price=None,  # filled at BRACKET_PLACED
                sl_price=None,  # filled at BRACKET_PLACED
                trailing_atr_mult=(
                    self._trade_trailing_atr_mult
                    if self._trade_trailing_atr_mult is not None
                    else self._trailing_atr_mult
                ),
                trailing_sl_atr_offset=(
                    self._trailing_sl_atr_offset_long if self._position_side == 1
                    else self._trailing_sl_atr_offset_short
                ),
                trailing_activated=self._trailing_activated,
                highest_high=self._highest_high,
                lowest_low=self._lowest_low,
                bars_held=self._position_bars_held,
            )
        except Exception:
            log.debug("Failed to snapshot decision state", exc_info=True)

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
        last_bar = self.rolling_df_5m.iloc[-1]
        bar_high = float(last_bar["High"])
        bar_low = float(last_bar["Low"])
        self._highest_high = max(self._highest_high, bar_high)
        self._lowest_low = min(self._lowest_low, bar_low)

        # Check trailing trigger condition
        # Use per-trade override if set, otherwise global config
        effective_trailing = (
            self._trade_trailing_atr_mult
            if self._trade_trailing_atr_mult is not None
            else self._trailing_atr_mult
        )
        triggered = False
        if self._position_side == 1:  # Long
            if self._highest_high >= (
                self._entry_price
                + effective_trailing * self._atr_at_entry
            ):
                triggered = True
        elif self._position_side == -1:  # Short
            if self._lowest_low <= (
                self._entry_price
                - effective_trailing * self._atr_at_entry
            ):
                triggered = True

        if not triggered:
            return

        # Calculate new SL price — route to the correct per-side offset
        effective_offset = (
            self._trailing_sl_atr_offset_long if self._position_side == 1
            else self._trailing_sl_atr_offset_short
        )
        offset = effective_offset * self._atr_at_entry
        if self._position_side == 1:
            new_sl = self._entry_price + offset
        else:
            new_sl = self._entry_price - offset
        new_sl = round(new_sl, 2)

        log.info(
            "TRAILING STOP: activated — entry=%.2f  ATR=%.4f  "
            "trigger=%.2f×ATR  offset=%.2f×ATR  new_SL=%.2f",
            self._entry_price, self._atr_at_entry,
            effective_trailing, effective_offset,
            new_sl,
        )

        # Find and modify the SL order on IBKR by tracked order ID
        try:
            if self._sl_order_id is None:
                log.warning(
                    "TRAILING STOP: triggered but _sl_order_id is None "
                    "— SL order may not have been placed"
                )
                return
            for t in self.manager.ib.openTrades():
                c = getattr(t, "contract", None)
                o = getattr(t, "order", None)
                if c is None or o is None:
                    continue
                if getattr(c, "symbol", None) != "CL":
                    continue
                order_id = getattr(o, "orderId", None)
                if order_id != self._sl_order_id:
                    continue
                old_sl = getattr(o, "auxPrice", 0.0) or 0.0
                o.auxPrice = new_sl
                self.manager.ib.placeOrder(c, o)
                log.info(
                    "TRAILING STOP: modified SL order %d: %.2f → %.2f",
                    order_id, old_sl, new_sl,
                )
                self._trailing_activated = True
                # Persist new SL price to ledger
                if self._active_trade_id is not None:
                    try:
                        self.telemetry.update_position_sl(
                            self._active_trade_id,
                            new_sl_price=new_sl,
                            sl_order_id=order_id,
                        )
                    except Exception:
                        log.debug("Failed to update ledger SL", exc_info=True)
                # Snapshot decision state at trailing activation
                try:
                    self.telemetry.log_decision_state(
                        trade_id=self._active_trade_id,
                        event_type="TRAILING_ACTIVATED",
                        event_timestamp_utc=self._utc_iso_now(),
                        entry_price=self._entry_price,
                        position_side=self._position_side,
                        atr_at_entry=self._atr_at_entry,
                        bracket_atr=self._atr_at_entry,
                        sl_price=new_sl,
                        trailing_atr_mult=effective_trailing,
                        trailing_sl_atr_offset=effective_offset,
                        trailing_activated=True,
                        highest_high=self._highest_high,
                        lowest_low=self._lowest_low,
                        bars_held=self._position_bars_held,
                    )
                except Exception:
                    log.debug("Failed to snapshot TRAILING_ACTIVATED state", exc_info=True)
                return
            log.warning(
                "TRAILING STOP: triggered but SL order %d not found in "
                "open trades (may have already filled or been cancelled)",
                self._sl_order_id,
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
        current_position = self.manager.get_cl_position(
            symbol=self._execution_symbol,
        )
        if current_position == 0:
            # Detect out-of-band close (manual TWS close, external system, etc.)
            if self._active_trade_id is not None:
                log.info(
                    "[TRADE] EXIT: OUT-OF-BAND close detected — position went "
                    "flat while trade %s was still tracked (held %d bars)",
                    self._active_trade_id, self._position_bars_held,
                )
                try:
                    self.telemetry.close_position(
                        self._active_trade_id,
                        reason="CLOSED_OOB",
                        close_time=self._utc_iso_now(),
                        bars_held=self._position_bars_held,
                        exit_price=current_price,
                    )
                except Exception:
                    log.debug(
                        "Failed to close ledger position (OOB)", exc_info=True
                    )
                # Log a tradebook event for auditability
                event_ts = self._utc_iso_now()
                event_id = self._build_event_id(
                    event_type="POSITION_CLOSED_OOB",
                    event_ts=event_ts,
                )
                try:
                    self.telemetry.log_tradebook_event(
                        event_id=event_id,
                        event_type="POSITION_CLOSED_OOB",
                        event_timestamp_utc=event_ts,
                        symbol="CL",
                        status="CLOSED",
                        **self._base_tradebook_fields(),
                    )
                except Exception:
                    log.debug(
                        "Failed to log OOB tradebook event", exc_info=True
                    )
                # Cancel any orphaned TP/SL orders still live on IBKR
                try:
                    cancelled = self.manager.cancel_open_cl_orders(
                        symbol=self._execution_symbol,
                    )
                    if cancelled > 0:
                        log.info(
                            "OOB CLEANUP: cancelled %d orphaned CL order(s)",
                            cancelled,
                        )
                except Exception:
                    log.debug(
                        "OOB CLEANUP: cancel_open_cl_orders failed",
                        exc_info=True,
                    )
            self._reset_position_state()
            return False

        if self._position_entry_bar_time is None:
            # First bar after detecting a position.
            self._position_entry_bar_time = bar_time
            self._position_bars_held = 0
            return False

        self._position_bars_held += 1
        # Use per-trade override if set, otherwise global config
        effective_max_hold = (
            self._trade_max_hold_bars
            if self._trade_max_hold_bars is not None
            else self._max_hold_bars
        )
        if self._position_bars_held <= effective_max_hold:
            return False

        cancelled = self.manager.cancel_open_cl_orders(
            symbol=self._execution_symbol,
        )
        trade = self.manager.close_cl_position(
            symbol=self._execution_symbol,
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
        # Close position in ledger
        if self._active_trade_id is not None:
            try:
                self.telemetry.close_position(
                    self._active_trade_id,
                    reason="TIME_BARRIER",
                    close_time=self._utc_iso_now(),
                    bars_held=self._position_bars_held,
                    exit_price=current_price,
                )
            except Exception:
                log.debug("Failed to close ledger position", exc_info=True)
        # Register the exit order ID so the async fill callback recognises it
        # as a known exit rather than triggering PHANTOM FILL BLOCKED.
        _exit_oid = getattr(getattr(trade, "order", None), "orderId", None)
        if _exit_oid is not None:
            self._processed_exit_order_ids.add(_exit_oid)
        self._reset_position_state()
        return True

    def _recover_inherited_position(self) -> None:
        """Recover position state from the persistent ledger on startup.

        Reads the `active_positions` table to restore in-memory state
        (entry price, TP/SL order IDs, etc.) and verifies that the
        IBKR portfolio matches.  If TP/SL orders are missing from IBKR
        (e.g. cancelled during downtime), they are re-placed using the
        stored prices from the ledger.
        """
        # 1. Check ledger for an open position
        ledger_pos = self.telemetry.get_open_position()
        ibkr_pos = self.manager.get_cl_position(
            symbol=self._execution_symbol,
        )

        if ledger_pos is None and ibkr_pos == 0:
            log.info("[RECOVERY] No open position in ledger or IBKR — clean start")
            return

        if ledger_pos is None and ibkr_pos != 0:
            # IBKR has a position but the ledger doesn't know about it.
            # This can happen if the position was opened before the ledger
            # was implemented, or from a different client_id.
            log.warning(
                "[RECOVERY] IBKR has position=%d but no open trade in ledger. "
                "Position is UNTRACKED — no TP/SL will be placed. "
                "Consider manually closing or adding a ledger entry.",
                ibkr_pos,
            )
            return

        # Ledger has an open position
        trade_id = ledger_pos["trade_id"]
        side = ledger_pos["side"]
        entry_price = ledger_pos["entry_price"]
        quantity = ledger_pos["quantity"]
        tp_order_id = ledger_pos.get("tp_order_id")
        sl_order_id = ledger_pos.get("sl_order_id")
        tp_price = ledger_pos.get("tp_price")
        sl_price = ledger_pos.get("sl_price")
        atr_at_entry = ledger_pos.get("atr_at_entry")
        entry_bar_time_str = ledger_pos.get("entry_bar_time")
        trailing_atr_mult = ledger_pos.get("trailing_atr_mult")
        max_hold_bars = ledger_pos.get("max_hold_bars")

        # 2. Verify IBKR position exists
        if ibkr_pos == 0:
            # Position was closed while we were offline
            log.info(
                "[RECOVERY] Ledger trade %s shows OPEN but IBKR is flat "
                "— marking as CLOSED (filled out-of-band)",
                trade_id,
            )
            self.telemetry.close_position(
                trade_id,
                reason="CLOSED_OOB",
                close_time=self._utc_iso_now(),
            )
            # Clean up any orphaned TP/SL orders still resting on IBKR
            # (e.g. TP filled offline → software OCA never cancelled the SL)
            try:
                cancelled = self.manager.cancel_open_cl_orders(
                    symbol=self._execution_symbol,
                )
                if cancelled > 0:
                    log.info(
                        "[RECOVERY] Cancelled %d orphaned CL order(s) "
                        "after OOB close",
                        cancelled,
                    )
            except Exception:
                log.debug(
                    "[RECOVERY] cancel_open_cl_orders failed",
                    exc_info=True,
                )
            return

        # 3. IBKR confirms position exists — restore in-memory state
        log.info(
            "[RECOVERY] Restoring position from ledger: "
            "trade_id=%s  side=%s  entry=%.2f  qty=%d",
            trade_id, side, entry_price, quantity,
        )
        self._active_trade_id = trade_id
        self._entry_price = entry_price
        self._atr_at_entry = atr_at_entry
        self._position_side = 1 if side == "LONG" else -1
        self._trade_trailing_atr_mult = trailing_atr_mult
        self._trade_max_hold_bars = (
            int(max_hold_bars) if max_hold_bars is not None else None
        )

        # Restore entry bar time and estimate bars held
        if entry_bar_time_str:
            try:
                self._position_entry_bar_time = pd.Timestamp(entry_bar_time_str)
                # Estimate bars held from entry time to now
                if self.rolling_df_5m is not None and len(self.rolling_df_5m) > 0:
                    last_bar = self.rolling_df_5m.index[-1]
                    delta_minutes = (
                        last_bar - self._position_entry_bar_time
                    ).total_seconds() / 60.0
                    # Use bar_size to compute correct bar duration
                    _bar_minutes = {
                        "5m": 5, "1h": 60, "2h": 120, "4h": 240,
                    }
                    bar_dur = _bar_minutes.get(self._bar_size, 5)
                    self._position_bars_held = max(
                        0, int(delta_minutes / bar_dur)
                    )
                    log.info(
                        "[RECOVERY] Estimated %d bars held since entry at %s "
                        "(bar_size=%s, delta=%.0f min)",
                        self._position_bars_held,
                        self._position_entry_bar_time,
                        self._bar_size,
                        delta_minutes,
                    )
            except Exception:
                log.debug("Failed to parse entry_bar_time", exc_info=True)
                self._position_entry_bar_time = (
                    self.rolling_df_5m.index[-1]
                    if self.rolling_df_5m is not None and len(self.rolling_df_5m) > 0
                    else None
                )
                self._position_bars_held = 0
        else:
            # No bar time saved — set conservative defaults
            self._position_entry_bar_time = (
                self.rolling_df_5m.index[-1]
                if self.rolling_df_5m is not None and len(self.rolling_df_5m) > 0
                else None
            )
            self._position_bars_held = 0

        # Init trailing stop tracking from current data
        if self.rolling_df_5m is not None and len(self.rolling_df_5m) > 0:
            self._highest_high = float(self.rolling_df_5m["High"].iloc[-1])
            self._lowest_low = float(self.rolling_df_5m["Low"].iloc[-1])

        # 4. Verify TP/SL orders on IBKR
        tp_found = False
        sl_found = False
        try:
            for t in self.manager.ib.openTrades():
                c = getattr(t, "contract", None)
                o = getattr(t, "order", None)
                if c is None or o is None:
                    continue
                if getattr(c, "symbol", None) != "CL":
                    continue
                oid = getattr(o, "orderId", None)
                if oid is not None and oid == tp_order_id:
                    tp_found = True
                elif oid is not None and oid == sl_order_id:
                    sl_found = True
        except Exception:
            log.warning(
                "[RECOVERY] Failed to scan IBKR open trades",
                exc_info=True,
            )

        if tp_found and sl_found:
            # Both orders exist — just restore the IDs
            self._tp_order_ids = [tp_order_id]
            self._sl_order_id = sl_order_id
            log.info(
                "[RECOVERY] TP/SL verified on IBKR: "
                "TP orderId=%s (%.2f)  SL orderId=%s (%.2f)",
                tp_order_id, tp_price or 0.0,
                sl_order_id, sl_price or 0.0,
            )
            return

        # 5. One or both TP/SL orders missing — re-place them
        if tp_price is None or sl_price is None:
            log.warning(
                "[RECOVERY] TP/SL orders missing and no stored prices "
                "in ledger — cannot re-place protective orders. "
                "Position is UNPROTECTED."
            )
            return

        if self._front_month_contract is None:
            log.warning(
                "[RECOVERY] Cannot re-place TP/SL — "
                "front-month contract not resolved"
            )
            return

        # Cancel any stale orders that partially exist
        if tp_found and not sl_found:
            log.info("[RECOVERY] SL order missing — re-placing both TP/SL")
        elif sl_found and not tp_found:
            log.info("[RECOVERY] TP order missing — re-placing both TP/SL")
        else:
            log.info("[RECOVERY] Both TP/SL orders missing — placing fresh")

        # Cancel any remaining stale CL exit orders before re-placing
        try:
            self.manager.cancel_open_cl_orders()
        except Exception:
            log.debug("[RECOVERY] cancel_open_cl_orders failed", exc_info=True)

        # Place fresh TP/SL
        exit_action = "SELL" if self._position_side == 1 else "BUY"
        try:
            child_trades = self.manager.place_child_orders(
                contract=self._front_month_contract,
                parent_order_id=0,  # no parent — standalone
                action=exit_action,
                quantity=quantity,
                tp_price=tp_price,
                sl_price=sl_price,
            )
            if len(child_trades) >= 2:
                tp_trade_objs = child_trades[:-1]
                sl_trade_obj = child_trades[-1]

                self._tp_order_ids = []
                for t in tp_trade_objs:
                    oid = getattr(getattr(t, "order", None), "orderId", None)
                    if oid is not None:
                        self._tp_order_ids.append(oid)
                        
                self._sl_order_id = getattr(
                    getattr(sl_trade_obj, "order", None), "orderId", None
                )
                # Update ledger with new order IDs (use first TP as reference proxy)
                self.telemetry.update_position_brackets(
                    trade_id,
                    tp_order_id=self._tp_order_ids[0] if self._tp_order_ids else None,
                    sl_order_id=self._sl_order_id,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )
                log.info(
                    "[RECOVERY] TP/SL RE-PLACED: "
                    "TP orderIds=%s (avg %.2f)  SL orderId=%s (%.2f)",
                    self._tp_order_ids, tp_price,
                    self._sl_order_id, sl_price,
                )
            else:
                log.warning(
                    "[RECOVERY] place_child_orders returned "
                    "%d trades (expected 2)",
                    len(child_trades),
                )
        except Exception:
            log.exception(
                "[RECOVERY] Failed to re-place TP/SL orders"
            )

    def _cancel_orphaned_orders_on_startup(self) -> None:
        """Cancel any orphaned CL orders on IBKR if the bot considers itself FLAT.

        Runs once at startup, *after* _recover_inherited_position().
        Catches stale entry or exit orders left over from a prior session
        that the bot no longer tracks (e.g. an unfilled BUY limit from
        before a restart).  Without this, such orders could fill after
        startup and trigger PHANTOM FILL BLOCKED — leaving an unprotected
        position.
        """
        # Only sweep if the bot has no position and no tracked pending entry
        if self._active_trade_id is not None:
            return  # Position is being tracked — don't touch orders
        if self._pending_entry_order_id is not None:
            return  # Entry order is being tracked — don't touch orders

        ibkr_pos = self.manager.get_cl_position(
            symbol=self._execution_symbol,
        )
        if ibkr_pos != 0:
            return  # IBKR has a position — don't cancel its protective orders

        # Bot is FLAT with no tracked orders — any CL orders on IBKR are orphans
        try:
            open_cl_orders = []
            for t in self.manager.ib.openTrades():
                c = getattr(t, "contract", None)
                if c is not None and getattr(c, "symbol", None) == self._execution_symbol:
                    o = getattr(t, "order", None)
                    if o is not None:
                        open_cl_orders.append(o)

            if not open_cl_orders:
                return

            log.warning(
                "[STARTUP SWEEP] Bot is FLAT but found %d orphaned CL order(s) "
                "on IBKR — cancelling to prevent phantom fills",
                len(open_cl_orders),
            )
            for order in open_cl_orders:
                try:
                    self.manager.ib.cancelOrder(order)
                    log.info(
                        "[STARTUP SWEEP] Cancelled orphaned order: "
                        "orderId=%s action=%s type=%s lmt=%.2f aux=%.2f",
                        getattr(order, "orderId", "?"),
                        getattr(order, "action", "?"),
                        getattr(order, "orderType", "?"),
                        float(getattr(order, "lmtPrice", 0) or 0),
                        float(getattr(order, "auxPrice", 0) or 0),
                    )
                except Exception:
                    log.exception(
                        "[STARTUP SWEEP] Failed to cancel orderId=%s",
                        getattr(order, "orderId", "?"),
                    )
        except Exception:
            log.exception("[STARTUP SWEEP] Failed to scan for orphaned orders")

    def _place_bracket_children_on_fill(
        self,
        *,
        order_id: int,
        fill_price: float,
        action_str: str,
        qty: float,
        contract,
    ) -> None:
        """Place TP/SL child orders after entry fill, using actual fill price.

        Phase 2 of two-phase order placement.  Computes bracket prices
        from the fill price + stored ATR offsets so the SL/TP are always
        correctly positioned relative to the real entry.
        """
        ctx = self._last_decision_context_by_order_id.get(order_id)
        if ctx is None:
            log.warning(
                "BRACKET CHILDREN: no decision context for orderId=%d "
                "— cannot place TP/SL children",
                order_id,
            )
            return

        tp_offset = ctx.get("tp_offset")
        sl_offset = ctx.get("sl_offset")
        entry_action = ctx.get("entry_action")
        lots = int(ctx.get("lots", qty))

        tiered_tp_offsets = ctx.get("tiered_tp_offsets")

        if (tp_offset is None and not tiered_tp_offsets) or sl_offset is None or entry_action is None:
            log.warning(
                "BRACKET CHILDREN: missing tp_offset/sl_offset/entry_action "
                "in context for orderId=%d — cannot place children",
                order_id,
            )
            return

        # Compute bracket prices from fill price
        exit_action = "SELL" if entry_action == "BUY" else "BUY"
        if entry_action == "BUY":
            sl_price = round(fill_price - sl_offset, 2)
            if tiered_tp_offsets:
                tp_price = []
                rem_lots = lots
                for i, (pct, off) in enumerate(tiered_tp_offsets):
                    t_lots = rem_lots if i == len(tiered_tp_offsets) - 1 else max(1, int(round(lots * pct)))
                    t_lots = min(t_lots, rem_lots)
                    if t_lots > 0:
                        tp_price.append((t_lots, round(fill_price + off, 2)))
                    rem_lots -= t_lots
            else:
                tp_price = round(fill_price + tp_offset, 2)
        else:  # SELL (short)
            sl_price = round(fill_price + sl_offset, 2)
            if tiered_tp_offsets:
                tp_price = []
                rem_lots = lots
                for i, (pct, off) in enumerate(tiered_tp_offsets):
                    t_lots = rem_lots if i == len(tiered_tp_offsets) - 1 else max(1, int(round(lots * pct)))
                    t_lots = min(t_lots, rem_lots)
                    if t_lots > 0:
                        tp_price.append((t_lots, round(fill_price - off, 2)))
                    rem_lots -= t_lots
            else:
                tp_price = round(fill_price - tp_offset, 2)

        log.info(
            "[TRADE] BRACKET CHILDREN: fill=%.2f  TPs=%s  SL=%.2f  "
            "(array=%s, sl_offset=%.4f)",
            fill_price, tp_price, sl_price, bool(tiered_tp_offsets), sl_offset,
        )

        try:
            child_trades = self.manager.place_child_orders(
                contract=contract,
                parent_order_id=order_id,
                action=exit_action,
                quantity=lots,
                tp_price=tp_price,
                sl_price=sl_price,
            )
            # Update entry price to actual fill (for trailing stop)
            self._entry_price = fill_price

            # Store TP and SL order IDs for software-side OCA and
            if len(child_trades) >= 2:
                tp_trade_objs = child_trades[:-1]
                sl_trade_obj = child_trades[-1]
                
                self._tp_order_ids = []
                for tp_trade_obj in tp_trade_objs:
                    oid = getattr(getattr(tp_trade_obj, "order", None), "orderId", None)
                    if oid is not None:
                        self._tp_order_ids.append(oid)
                        
                self._sl_order_id = getattr(
                    getattr(sl_trade_obj, "order", None), "orderId", None
                )
                log.info(
                    "[TRADE] BRACKET CHILDREN: TP orderIds=%s  SL orderId=%s  "
                    "(standalone orders, software OCA active)",
                    self._tp_order_ids, self._sl_order_id,
                )
                # Persist TP/SL order IDs and prices to ledger
                if self._active_trade_id is not None:
                    try:
                        self.telemetry.update_position_brackets(
                            self._active_trade_id,
                            tp_order_id=self._tp_order_ids[0] if self._tp_order_ids else None,
                            sl_order_id=self._sl_order_id,
                            # Dummy array aggregation if multiple 
                            tp_price=tp_price if not isinstance(tp_price, list) else tp_price[0][1],
                            sl_price=sl_price,
                        )
                    except Exception:
                        log.debug(
                            "Failed to update ledger brackets", exc_info=True
                        )
                    # Snapshot decision state after bracket placement
                    try:
                        # Overwrite tp/sl in the snapshot with actual bracket prices
                        _effective_tp = tp_price if not isinstance(tp_price, list) else tp_price[0][1]
                        self.telemetry.log_decision_state(
                            trade_id=self._active_trade_id,
                            event_type="BRACKET_PLACED",
                            event_timestamp_utc=self._utc_iso_now(),
                            entry_price=fill_price,
                            position_side=self._position_side,
                            atr_at_entry=self._atr_at_entry,
                            bracket_atr=self._atr_at_entry,
                            tp_price=_effective_tp,
                            sl_price=sl_price,
                            trailing_atr_mult=(
                                self._trade_trailing_atr_mult
                                if self._trade_trailing_atr_mult is not None
                                else self._trailing_atr_mult
                            ),
                            trailing_sl_atr_offset=(
                                self._trailing_sl_atr_offset_long if self._position_side == 1
                                else self._trailing_sl_atr_offset_short
                            ),
                            trailing_activated=False,
                            highest_high=self._highest_high,
                            lowest_low=self._lowest_low,
                            bars_held=self._position_bars_held,
                        )
                    except Exception:
                        log.debug("Failed to snapshot BRACKET_PLACED state", exc_info=True)

            # Log tradebook events for the children
            decision_ctx = ctx
            for child_trade in child_trades:
                child_order = getattr(child_trade, "order", None)
                child_contract = getattr(child_trade, "contract", None)
                if child_order is None:
                    continue
                child_order_id = getattr(child_order, "orderId", None)
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
                    perm_id=getattr(child_order, "permId", None),
                    parent_order_id=getattr(child_order, "parentId", None),
                    account=getattr(child_order, "account", None),
                    symbol=getattr(child_contract, "symbol", None),
                    local_symbol=getattr(child_contract, "localSymbol", None),
                    contract_month=self._extract_contract_month(child_contract),
                    side=getattr(child_order, "action", None),
                    action=getattr(child_order, "action", None),
                    order_type=getattr(child_order, "orderType", None),
                    time_in_force=getattr(child_order, "tif", None),
                    status="SUBMITTED",
                    order_qty=float(getattr(child_order, "totalQuantity", 0) or 0),
                    limit_price=float(getattr(child_order, "lmtPrice", 0) or 0),
                    stop_price=float(getattr(child_order, "auxPrice", 0) or 0),
                    **self._base_tradebook_fields(decision_ctx=decision_ctx),
                )
        except Exception:
            log.exception(
                "BRACKET CHILDREN: failed to place TP/SL children "
                "for orderId=%d (fill=%.2f)",
                order_id, fill_price,
            )

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
                # Extract order_id BEFORE the TP/SL check (was previously
                # only assigned in the else branch, causing UnboundLocalError)
                order_id = getattr(order, "orderId", None)

                # FIX: Duplicate-exit guard.
                # IBKR routinely fires orderStatusEvent twice for the same fill.
                # After _reset_position_state() clears _tp_order_ids / _sl_order_id,
                # the second callback can no longer identify the order as an exit
                # and falls through to the entry-fill branch, opening a phantom
                # position and placing fresh TP/SL children — causing an
                # exponential cascade.  Check the persistent set first.
                if order_id is not None and order_id in self._processed_exit_order_ids:
                    log.info(
                        "Ignoring duplicate Filled event for already-processed "
                        "exit order %d (SL/TP cascade guard active)",
                        order_id,
                    )
                    return

                # Detect TP/SL fill by tracked order IDs (no parentId linkage)
                is_tp_fill = (order_id is not None and order_id in self._tp_order_ids)
                is_sl_fill = (order_id is not None and order_id == self._sl_order_id)
                if is_tp_fill or is_sl_fill:
                    # Immediately register in the persistent set so any subsequent
                    # duplicate callback for this same order is blocked above.
                    if order_id is not None:
                        self._processed_exit_order_ids.add(order_id)
                        # Trim to prevent unbounded growth (cap at 500 entries).
                        if len(self._processed_exit_order_ids) > 500:
                            self._processed_exit_order_ids = set(
                                list(self._processed_exit_order_ids)[-500:]
                            )
                    # Calculate PnL for the Telegram alert
                    pnl_str = ""
                    pnl_val = 0.0
                    if getattr(self, "_entry_price", None) is not None and self._entry_price > 0:
                        try:
                            mult_str = getattr(contract, "multiplier", "1000")
                            multiplier = float(mult_str) if mult_str else 1000.0
                        except Exception:
                            multiplier = 1000.0

                        if action_str == "SELL":  # closing a long
                            pnl_val = (avg_price - self._entry_price) * qty * multiplier
                        elif action_str == "BUY": # closing a short
                            pnl_val = (self._entry_price - avg_price) * qty * multiplier
                        
                        sign = "" if pnl_val >= 0 else "-"
                        pnl_str = f"\n  • PnL: `{sign}${abs(pnl_val):.2f}`"

                    # Determine exit type and icon
                    if is_sl_fill and getattr(self, "_trailing_activated", False):
                        exit_type = "TRAILING SL HIT"
                        exit_icon = "🟢" if pnl_val >= 0 else "🟡"
                    else:
                        exit_type = "TP HIT" if is_tp_fill else "SL HIT"
                        exit_icon = "🟢" if is_tp_fill else "🔴"

                    # Exit order filled — log and apply software-side OCA
                    log.info(
                        "[TRADE] EXIT: %s %.0f %s @ %.2f (%s) PnL=%.2f",
                        action_str, qty, symbol_str, avg_price, exit_type, pnl_val
                    )
                    # ── Telegram: trade exit alert ────────────────────
                    entry_str = f"{self._entry_price:.2f}" if getattr(self, "_entry_price", None) else "Unknown"
                    self._telegram.send(
                        f"{exit_icon} *Trade Exit — {exit_type}*\n"
                        f"{action_str} {qty:.0f} `{symbol_str}` @ `{avg_price:.2f}`\n"
                        f"  • Entry: `{entry_str}`{pnl_str}",
                    )

                    if is_sl_fill:
                        # Global SL hit — cancel all pending TPs
                        for tp_id in self._tp_order_ids:
                            try:
                                for t in self.manager.ib.openTrades():
                                    o2 = getattr(t, "order", None)
                                    if o2 is not None and getattr(o2, "orderId", None) == tp_id:
                                        self.manager.ib.cancelOrder(o2)
                                        log.info("OCA: cancelled pending TP tranche %d after SL HIT", tp_id)
                                        break
                            except Exception:
                                log.exception("OCA: failed to cancel pending TP tranche %d", tp_id)
                        is_final_exit = True
                    else:
                        # TP fill (partial or full)
                        self._tp_order_ids.remove(order_id)
                        if not self._tp_order_ids:
                            # Last TP filled — cancel the SL
                            if self._sl_order_id is not None:
                                try:
                                    for t in self.manager.ib.openTrades():
                                        o2 = getattr(t, "order", None)
                                        if o2 is not None and getattr(o2, "orderId", None) == self._sl_order_id:
                                            self.manager.ib.cancelOrder(o2)
                                            log.info("OCA: cancelled opposite SL %d after final TP HIT", self._sl_order_id)
                                            break
                                except Exception:
                                    log.exception("OCA: failed to cancel opposite SL %d", self._sl_order_id)
                            is_final_exit = True
                        else:
                            # Fractional TP filled — dynamically downgrade SL order size!
                            if self._sl_order_id is not None:
                                try:
                                    for t in self.manager.ib.openTrades():
                                        o2 = getattr(t, "order", None)
                                        if o2 is not None and getattr(o2, "orderId", None) == self._sl_order_id:
                                            # We just sold `qty` contracts
                                            current_qty = float(getattr(o2, "totalQuantity", 0) or 0)
                                            new_qty = current_qty - qty
                                            if new_qty > 0:
                                                o2.totalQuantity = float(new_qty)
                                                self.manager.ib.placeOrder(t.contract, o2)
                                                log.info("OCA: reduced SL %d quantity from %.0f to %.0f after partial TP", self._sl_order_id, current_qty, new_qty)
                                            else:
                                                self.manager.ib.cancelOrder(o2)
                                                log.info("OCA: cancelled SL %d (quantity exhausted)", self._sl_order_id)
                                            break
                                except Exception:
                                    log.exception("OCA: failed to modify SL %d quantity", self._sl_order_id)
                            is_final_exit = False
                            
                    if is_final_exit:
                        # Close position in ledger
                        if self._active_trade_id is not None:
                            if is_sl_fill and getattr(self, "_trailing_activated", False):
                                close_reason = "TRAILING_SL"
                            elif is_sl_fill:
                                close_reason = "SL_HIT"
                            else:
                                close_reason = "TP_HIT"
                            try:
                                self.telemetry.close_position(
                                    self._active_trade_id,
                                    reason=close_reason,
                                    close_time=self._utc_iso_now(),
                                    bars_held=self._position_bars_held,
                                    exit_price=avg_price,
                                )
                            except Exception:
                                log.debug(
                                    "Failed to close ledger position",
                                    exc_info=True,
                                )

                        self._reset_position_state()
                else:
                    if parent_id != 0:
                        log.warning(
                            "Untracked child exit order filled (orderId=%d, parentId=%d, type=%s)! "
                            "Skipping entry logic to prevent bracket cascade.",
                            order_id, parent_id, order_type
                        )
                    elif order_id is not None and order_id in self._processed_entry_order_ids:
                        log.info("Ignoring duplicate Filled event for parent order %s", order_id)
                    else:
                        # an external/manual order or a stale exit order that slipped
                        # past the duplicate-exit guard above.
                        _entry_ctx = self._last_decision_context_by_order_id.get(order_id)
                        if _entry_ctx is None:
                            log.warning(
                                "PHANTOM FILL BLOCKED: orderId=%d has no decision context "
                                "and is not a tracked TP/SL — likely a stale exit callback. "
                                "Ignoring to prevent phantom position accumulation.",
                                order_id,
                            )
                            return

                        # Defense-in-depth Fix B: callback-level position cap.
                        # Check the live IBKR position to detect overexposure.
                        _cb_position = self.manager.get_cl_position(
                            symbol=self._execution_symbol
                        )
                        if abs(_cb_position) > self._max_position_size:
                            log.warning(
                                "CALLBACK POSITION CAP BREACH: position=%d > max=%d "
                                "after fill for orderId=%d. "
                                "Processing fill anyway to ensure TP/SL protection.",
                                abs(_cb_position), self._max_position_size, order_id,
                            )

                        self._last_filled_entry_order_id = order_id
                        if order_id is not None:
                            self._processed_entry_order_ids.add(order_id)
                            if len(self._processed_entry_order_ids) > 500:
                                self._processed_entry_order_ids = set(list(self._processed_entry_order_ids)[-500:])
                        # Parent entry order filled — clear TTL tracking
                        log.info(
                            "[TRADE] FILLED: %s %.0f %s @ %.2f",
                            action_str, qty, symbol_str, avg_price,
                        )
                        # ── Telegram: trade completely filled alert ────────────────────
                        dctx = self._last_decision_context_by_order_id.get(order_id, {})
                        prob_buy = dctx.get("buy_prob_str", "N/A")
                        prob_sell = dctx.get("sell_prob_str", "N/A")
                        bar_str = dctx.get("bar_str", "N/A")

                        self._telegram.send(
                            f"✅ *Trade Filled*\n"
                            f"{action_str} {qty:.0f} `{symbol_str}` @ `{avg_price:.2f}`\n"
                            f"Prob (B/S): `{prob_buy}` / `{prob_sell}`\n"
                            f"Bar: {bar_str}"
                        )
                        self._pending_entry_order_id = None
                        self._pending_entry_bar_time = None
                        # Open position in the persistent ledger
                        trade_id = uuid.uuid4().hex
                        self._active_trade_id = trade_id
                        side = "LONG" if action_str == "BOT" or action_str == "BUY" else "SHORT"
                        try:
                            self.telemetry.open_position(
                                trade_id=trade_id,
                                side=side,
                                quantity=int(qty),
                                entry_price=avg_price,
                                entry_order_id=order_id,
                                atr_at_entry=self._atr_at_entry,
                                entry_time=self._utc_iso_now(),
                                entry_bar_time=(
                                    self._position_entry_bar_time.isoformat()
                                    if self._position_entry_bar_time is not None
                                    else None
                                ),
                                trailing_atr_mult=self._trade_trailing_atr_mult,
                                max_hold_bars=self._trade_max_hold_bars,
                            )
                            log.info(
                                "[LEDGER] OPEN: trade_id=%s  side=%s  qty=%d  "
                                "entry=%.2f  ATR=%.4f",
                                trade_id, side, int(qty), avg_price,
                                self._atr_at_entry or 0.0,
                            )
                        except Exception:
                            log.exception("Failed to write OPEN to position ledger")
                        # Snapshot decision state at entry
                        self._snapshot_decision_state("ENTRY")
                        # Phase 2: place TP/SL as standalone orders from actual fill price
                        if order_id is not None:
                            self._place_bracket_children_on_fill(
                                order_id=order_id,
                                fill_price=avg_price,
                                action_str=action_str,
                                qty=qty,
                                contract=contract,
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
        log.info("Warm-start: initializing 5m DataManager...")
        self.rolling_df_5m = self.data_manager_5m.initialize()

        if len(self.rolling_df_5m) == 0:
            raise RuntimeError(
                "Warm-start failed: no data available for 5m stream."
            )

        # Ensure DateTime index
        if "DateTime" in self.rolling_df_5m.columns and not isinstance(
            self.rolling_df_5m.index, pd.DatetimeIndex
        ):
            self.rolling_df_5m = self.rolling_df_5m.set_index("DateTime", drop=False)

        self._last_bar_time_5m = self.rolling_df_5m.index[-1]
        log.info(
            "5m rolling window initialized: %d bars, latest=%s",
            len(self.rolling_df_5m), self._last_bar_time_5m,
        )

        if self._bar_size in ("1h", "2h", "4h") and self.data_manager_1h is not None:
            log.info("Warm-start: initializing 1h DataManager...")
            self.rolling_df_1h = self.data_manager_1h.initialize()
            if len(self.rolling_df_1h) == 0:
                raise RuntimeError("Warm-start failed: no data available for 1h stream.")
            if "DateTime" in self.rolling_df_1h.columns and not isinstance(
                self.rolling_df_1h.index, pd.DatetimeIndex
            ):
                self.rolling_df_1h = self.rolling_df_1h.set_index("DateTime", drop=False)
            # Guard against cache contamination (e.g., 5m rows accidentally
            # written into warm_start_cache_1h.parquet by legacy runs).
            # A valid 1h cache should have ~1 hour median spacing.
            if len(self.rolling_df_1h) >= 3:
                _dt_deltas = self.rolling_df_1h.index.to_series().diff().dropna()
                _dt_deltas = _dt_deltas[_dt_deltas > pd.Timedelta(0)]
                if len(_dt_deltas) > 0:
                    _median_delta = _dt_deltas.median()
                    _min_hourly = pd.Timedelta(minutes=45)
                    _max_hourly = pd.Timedelta(hours=2, minutes=30)
                    if _median_delta < _min_hourly or _median_delta > _max_hourly:
                        log.warning(
                            "1h cache cadence invalid (median delta=%s). "
                            "Deleting %s and rebuilding from 1h seed.",
                            _median_delta,
                            self.data_manager_1h.cache_path,
                        )
                        try:
                            if self.data_manager_1h.cache_path.exists():
                                self.data_manager_1h.cache_path.unlink()
                        except Exception as exc:
                            raise RuntimeError(
                                f"Failed to delete invalid 1h cache at "
                                f"{self.data_manager_1h.cache_path}: {exc}"
                            ) from exc
                        self.rolling_df_1h = self.data_manager_1h.initialize()
                        if "DateTime" in self.rolling_df_1h.columns and not isinstance(
                            self.rolling_df_1h.index, pd.DatetimeIndex
                        ):
                            self.rolling_df_1h = self.rolling_df_1h.set_index("DateTime", drop=False)
            self._last_bar_time_1h = self.rolling_df_1h.index[-1]
            log.info(
                "1h rolling window initialized: %d bars, latest=%s",
                len(self.rolling_df_1h), self._last_bar_time_1h,
            )
            
            # Post-load validation: ensure 1H cache meets minimum requirements
            _min_required = {"1h": 840, "2h": 840, "4h": 840}
            _required = _min_required.get(self._bar_size, 0)
            if len(self.rolling_df_1h) < _required:
                err_msg = (
                    f"1H cache has only {len(self.rolling_df_1h)} bars — "
                    f"need {_required} for {self._bar_size} inference. "
                    f"Delete warm_start_cache_1h.parquet to trigger reseed."
                )
                log.error("CACHE VALIDATION FAILED: %s", err_msg)
                self._telegram.send(f"⚠️ *CACHE VALIDATION FAILED*\n`{err_msg}`")
                raise RuntimeError(err_msg)

    # ------------------------------------------------------------------
    # Live bar subscription
    # ------------------------------------------------------------------

    def _subscribe(self) -> None:
        """Subscribe to live bars (Brain streams)."""
        log.info("Subscribing to live 5-min bars (Stream A)...")
        self._live_bars_5m = self.manager.subscribe_live_bars(
            self._contract,
            bar_size="5 mins",
            duration_str="60 S",
        )
        self._live_bars_5m.updateEvent += self._on_bar_update_5m
        log.info("Subscribed to 5-min continuous contract live bars")

        if self._bar_size in ("1h", "2h", "4h"):
            log.info("Subscribing to live 1-hour bars (Stream B)...")
            self._live_bars_1h = self.manager.subscribe_live_bars(
                self._contract,
                bar_size="1 hour",
                duration_str="2 D",
            )
            self._live_bars_1h.updateEvent += self._on_bar_update_1h
            log.info("Subscribed to 1-hour continuous contract live bars")

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

        # Error 1100: Connectivity between IBKR and TWS has been lost
        # Error 1101: Connectivity restored, data lost
        if errorCode in (1100, 1101):
            log.warning("CONNECTIVITY LOST (code %d) — marking subscriptions as lost", errorCode)
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
            if self._live_bars_5m is not None:
                try:
                    self.manager.cancel_subscription(self._live_bars_5m)
                except Exception:
                    pass
            if self._live_bars_1h is not None:
                try:
                    self.manager.cancel_subscription(self._live_bars_1h)
                except Exception:
                    pass
            if self._front_month_bars is not None:
                try:
                    self.manager.cancel_subscription(self._front_month_bars)
                except Exception:
                    pass

            # 2. Re-subscribe using async API
            log.info("Subscribing to live 5-min bars (Stream A)...")
            self._live_bars_5m = await self.manager.subscribe_live_bars_async(
                self._contract,
                bar_size="5 mins",
                duration_str="60 S",
            )
            self._live_bars_5m.updateEvent += self._on_bar_update_5m
            log.info("Subscribed to 5-min continuous contract live bars")

            if self._bar_size == "1h":
                log.info("Subscribing to live 1-hour bars (Stream B)...")
                self._live_bars_1h = await self.manager.subscribe_live_bars_async(
                    self._contract,
                    bar_size="1 hour",
                    duration_str="2 D",
                )
                self._live_bars_1h.updateEvent += self._on_bar_update_1h
                log.info("Subscribed to 1-hour continuous contract live bars")

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
        if self._live_bars_5m is not None:
            try:
                self.manager.cancel_subscription(self._live_bars_5m)
            except Exception:
                pass
            self._live_bars_5m = None

        if self._live_bars_1h is not None:
            try:
                self.manager.cancel_subscription(self._live_bars_1h)
            except Exception:
                pass
            self._live_bars_1h = None
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

    def _on_front_month_bar_update(self, bars, has_new_bar=False) -> None:
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

    def _on_bar_update_5m(self, bars, has_new_bar=False) -> None:
        """Callback fired by ib_insync when continuous 5m bars are updated."""
        if not has_new_bar or not bars:
            return

        new_bar = bars[-1]
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
        new_row = new_row.set_index(pd.DatetimeIndex(new_row["DateTime"]), drop=False)
        new_row.index.name = "DateTime"
        bar_time = new_row.index[0]

        if self._last_bar_time_5m is not None and bar_time <= self._last_bar_time_5m:
            return
        self._last_bar_time_5m = bar_time

        log.info(
            "NEW 5M BAR: %s  O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            bar_time, new_row["Open"].iloc[0], new_row["High"].iloc[0],
            new_row["Low"].iloc[0], new_row["Close"].iloc[0], new_row["Volume"].iloc[0],
        )

        self.rolling_df_5m = pd.concat([self.rolling_df_5m, new_row])
        if len(self.rolling_df_5m) > _MAX_ROLLING_BARS:
            self.rolling_df_5m = self.rolling_df_5m.iloc[-_MAX_ROLLING_BARS:]

        self.data_manager_5m.append_bar(new_row)

        self.telemetry.log_bar(
            timestamp=bar_time, open_=new_row["Open"].iloc[0],
            high=new_row["High"].iloc[0], low=new_row["Low"].iloc[0],
            close=new_row["Close"].iloc[0], volume=new_row["Volume"].iloc[0],
        )

        if self._bar_size == "5m":
            with self._ledger_lock:
                self._on_new_bar(bar_time, self.rolling_df_5m, "5m")

    def _on_bar_update_1h(self, bars, has_new_bar=False) -> None:
        """Callback fired by ib_insync when continuous 1h bars are updated."""
        if not has_new_bar or not bars:
            return

        new_bar = bars[-1]
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
        new_row = new_row.set_index(pd.DatetimeIndex(new_row["DateTime"]), drop=False)
        new_row.index.name = "DateTime"
        bar_time = new_row.index[0]

        if self._last_bar_time_1h is not None and bar_time <= self._last_bar_time_1h:
            return
        self._last_bar_time_1h = bar_time

        log.info(
            "NEW 1H BAR: %s  O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            bar_time, new_row["Open"].iloc[0], new_row["High"].iloc[0],
            new_row["Low"].iloc[0], new_row["Close"].iloc[0], new_row["Volume"].iloc[0],
        )

        self.rolling_df_1h = pd.concat([self.rolling_df_1h, new_row])
        if len(self.rolling_df_1h) > _MAX_ROLLING_BARS:
            self.rolling_df_1h = self.rolling_df_1h.iloc[-_MAX_ROLLING_BARS:]

        self.data_manager_1h.append_bar(new_row)

        if self._bar_size == "1h":
            with self._ledger_lock:
                self._on_new_bar(bar_time, self.rolling_df_1h, "1h")
        elif self._bar_size in ("2h", "4h"):
            # Resample 1h → 2h/4h on-the-fly and dispatch on boundary
            resample_hours = 2 if self._bar_size == "2h" else 4
            if bar_time.hour % resample_hours == (resample_hours - 1):
                df_resampled = self.rolling_df_1h.resample(
                    f"{resample_hours}h"
                ).agg({
                    "Open": "first", "High": "max",
                    "Low": "min", "Close": "last",
                    "Volume": "sum",
                }).dropna(subset=["Close"])
                if len(df_resampled) > 0:
                    log.info(
                        "RESAMPLED %s BAR: %s  (%d bars from 1H stream)",
                        self._bar_size.upper(), bar_time, len(df_resampled),
                    )
                    with self._ledger_lock:
                        self._on_new_bar(bar_time, df_resampled, self._bar_size)

    # ------------------------------------------------------------------
    # Inference + Execution
    # ------------------------------------------------------------------

    def _check_order_rate_limit(self) -> bool:
        """Check if we are exceeding 10 orders per 60 seconds."""
        if self._emergency_halt:
            return False
            
        now = time.time()
        # Keep only timestamps within the last 60 seconds
        self._order_timestamps = [ts for ts in self._order_timestamps if now - ts <= 60.0]
        
        if len(self._order_timestamps) >= 10:
            self._emergency_halt = True
            msg = "🚨 CRITICAL: Order rate limit exceeded (10 orders / 60s). System HALTED."
            log.critical(msg)
            try:
                self._telegram.send(msg)
            except Exception:
                pass
            return False
            
        self._order_timestamps.append(now)
        return True

    def _on_new_bar(self, bar_time: pd.Timestamp, rolling_df: pd.DataFrame, stream: str) -> None:
        """Run feature generation, strategy evaluation, update ledger, and net execution."""
        if self._emergency_halt:
            log.warning("EMERGENCY HALT ACTIVE — ignoring new bar")
            return

        # 0a. Entry order TTL: cancel stale entry orders after 1 bar
        self._check_entry_order_ttl(bar_time)



        # 1. Generate features (always — needed for INFERENCE display)
        # Use stream for bar_size to inform feature engineering scale
        features = build_live_features(
            rolling_df, self.feature_names, lean=self._lean_features, bar_size=stream
        )
        if features is None:
            log.info("Feature generation skipped (insufficient data or NaN)")
            return

        current_price = float(rolling_df["Close"].iloc[-1])

        # Get per-side ATR for bracket sizing (parity with BacktestEngine).
        # ATR_14 is always computed inside build_live_features() as a MODEL
        # FEATURE (LightGBM was trained with it).  For bracket placement
        # (TP/SL/trailing), we use the config's per-side atr_period which may
        # differ (e.g. optimizer found atr_period_long=18, atr_period_short=26).
        # This keeps model parity while allowing independent bracket ATR tuning.

        def _compute_bracket_atr(period: int) -> float | None:
            """Compute a single rolling ATR value for a given period."""
            if period == 14 and "ATR_14" in features.columns:
                return float(features["ATR_14"].iloc[0])
            if len(rolling_df) >= period + 1:
                import pandas_ta as _ta  # noqa: F811
                _series = rolling_df.ta.atr(length=period)
                if _series is not None and not _series.empty:
                    _last = _series.iloc[-1]
                    if not np.isnan(_last):
                        return float(_last)
            # Fallback: use ATR_14 if available
            if "ATR_14" in features.columns:
                log.warning(
                    "Bracket ATR(%d) computation failed — falling back to ATR_14",
                    period,
                )
                return float(features["ATR_14"].iloc[0])
            return None

        if self._atr_period_long == self._atr_period_short:
            # Same period for both sides — compute once, share
            atr_value_long = _compute_bracket_atr(self._atr_period_long)
            atr_value_short = atr_value_long
        else:
            atr_value_long = _compute_bracket_atr(self._atr_period_long)
            atr_value_short = _compute_bracket_atr(self._atr_period_short)

        # Legacy single atr_value: use global period for trailing stop checks
        # on existing positions (side already known at that point).
        # For new entries, the side-specific value is used.
        atr_value = _compute_bracket_atr(self._atr_period)

        # Enforce 24-hour time barrier on any open position (engine safety rail)
        if self._check_time_barrier(
            bar_time=bar_time,
            current_price=current_price,
            atr_value=atr_value,
        ):
            return

        # 2. Position guard: check both filled position AND pending orders
        #    to prevent duplicate entries when Adaptive Algo is still working
        current_position = self.manager.get_cl_position(
            symbol=self._execution_symbol,
        )

        pending_cl_entry_qty = 0.0
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
                oid = getattr(o, "orderId", None)
                # Skip tracked TP/SL orders (they are standalone with
                # parentId==0 but are NOT entry orders)
                if oid is not None and (
                    oid in self._tp_order_ids or oid == self._sl_order_id
                ):
                    continue
                parent_id = getattr(o, "parentId", 0) or 0
                # Only count parent entry orders (parentId==0)
                if parent_id == 0 and order_status in (
                    "Submitted", "PreSubmitted", "PendingSubmit",
                ):
                    pending_cl_entry_qty += float(getattr(o, "totalQuantity", 0) or 0)
        except Exception:
            log.debug("Failed to check pending orders", exc_info=True)

        # Total exposure accounting for unfilled orders
        total_exposure = abs(current_position) + pending_cl_entry_qty

        # Engine-level hard position cap (defense-in-depth)
        hard_blocked_by_engine = False
        if total_exposure >= self._max_position_size:
            if total_exposure > self._max_position_size:
                log.warning(
                    "POSITION CAP BREACH: abs(position)=%d + pending=%.0f > max=%d — "
                    "blocking ALL new entries",
                    abs(current_position), pending_cl_entry_qty, self._max_position_size,
                )
            hard_blocked_by_engine = True

        # Treat pending entry orders as an effective position to block duplicates
        effective_position = current_position
        if pending_cl_entry_qty > 0 and current_position == 0:
            effective_position = int(pending_cl_entry_qty)  # non-zero → blocks entry
            log.info(
                "POSITION GUARD: portfolio=0 but %.0f pending CL entry qty "
                "— treating as position=%d",
                pending_cl_entry_qty, effective_position,
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
                    oid = getattr(o, "orderId", None)
                    if oid is not None and oid in self._tp_order_ids:
                        lmt = getattr(o, "lmtPrice", 0.0) or 0.0
                        if lmt > 0:
                            tp_price_live = lmt
                    elif oid is not None and oid == self._sl_order_id:
                        aux = getattr(o, "auxPrice", 0.0) or 0.0
                        if aux > 0:
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
        t0 = time.perf_counter()
        signal: TradeSignal = self.strategy.evaluate(
            features=features,
            current_price=current_price,
            atr_value=atr_value,
            current_position=effective_position,
            atr_value_long=atr_value_long,
            atr_value_short=atr_value_short,
        )
        self._last_inference_time_sec = time.perf_counter() - t0
        self._last_inference_bar_time = bar_time  # track last successful inference

        if hard_blocked_by_engine and signal.action in ("BUY", "SELL", "ENTER", "SHORT"):
            log.warning("Engine overriding strategy signal %s to HOLD due to position cap", signal.action)
            signal = TradeSignal(
                action="HOLD",
                probability=signal.probability,
                confidence_pct=signal.confidence_pct,
                signal_label="Hold",
                skip_reason="HARD_POSITION_CAP",
                buy_prob=signal.buy_prob,
                sell_prob=signal.sell_prob,
            )

        # Update Thread-Safe Virtual Ledger (Dual Stream Netting)
        if signal.action == "ENTER":
            self._virtual_ledger[stream] = signal.direction * signal.lots
        elif signal.action == "EXIT":
            # Dead path: strategies no longer produce EXIT signals (bracket-only exits).
            # Kept as a safety net to zero the ledger if an EXIT somehow arrives.
            self._virtual_ledger[stream] = 0
            
        net_target_position = sum(self._virtual_ledger.values())
        log.info(
            "VIRTUAL LEDGER [%s]: 5m=%d, 1h=%d -> NET TARGET: %d (Actual: %d)",
            stream, self._virtual_ledger["5m"], self._virtual_ledger["1h"],
            net_target_position, current_position,
        )

        # Shadow-replay logging: capture exact state for parity validation
        try:
            last_row = rolling_df.iloc[-1]
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
        except Exception as e:
            log.error("Shadow state logging failed: %s", e, exc_info=True)
            raise RuntimeError(f"CRITICAL: Failed to write shadow state to telemetry DB: {e}") from e

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
            elif signal.skip_reason == "EXECUTION_GUARD":
                log.warning(
                    "[EXECUTION GUARD] new entries blocked "
                    "(bar=%s, buy_prob=%.4f, sell_prob=%.4f)",
                    bar_time,
                    signal.buy_prob or 0.0,
                    signal.sell_prob or 0.0,
                )
                action_taken = "SKIP_EXECUTION_GUARD"
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal=signal.signal_label,
                confidence_pct=signal.confidence_pct,
                action_taken=action_taken,
                current_price=current_price,
                atr_value=atr_value,
            )
            return

        # 5. Active signal (BUY or SELL)

        decision_timestamp_utc = bar_time.isoformat()
        signal_id = uuid.uuid4().hex
        decision_id = uuid.uuid4().hex

        # Safety: strategy should never return EXIT (bracket-only exit rule).
        # If it does, log a warning and treat as HOLD.
        if signal.action == "EXIT":
            log.warning(
                "Strategy returned EXIT signal — this should not happen under "
                "bracket-only exit rules. Treating as HOLD. "
                "buy_prob=%.4f  sell_prob=%.4f",
                signal.buy_prob or 0.0, signal.sell_prob or 0.0,
            )
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal="Hold",
                confidence_pct=signal.confidence_pct,
                action_taken="EXIT_BLOCKED",
                current_price=current_price,
                atr_value=atr_value,
            )
            return

        # 6. Execute or dry-run (BUY / SELL)
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
        # Compute TP/SL offsets (ATR * mult) as dollar amounts.
        # These are stored in the decision context so the fill callback
        # can compute bracket prices from any fill price.
        tp_offset = abs(signal.tp_price - current_price)
        sl_offset = abs(signal.sl_price - current_price)

        try:
            if not self._check_order_rate_limit():
                return
                
            # Phase 1: Submit entry order only (no TP/SL children).
            # TP/SL are placed in Phase 2 from _on_order_status fill callback
            # using the actual fill price to avoid SL/TP mispricing.
            parent_trade = self.manager.place_entry_order(
                contract=self._front_month_contract,
                action=signal.action,
                quantity=signal.lots,
                limit_price=current_price,
                entry_mode=self.entry_mode,
                adaptive_priority=self.adaptive_priority,
            )
            order_id = parent_trade.order.orderId
            parent_order = parent_trade.order
            order_type_str = getattr(parent_order, "orderType", "???")
            algo_str = getattr(parent_order, "algoStrategy", None)
            if algo_str:
                order_type_str = f"{order_type_str}+{algo_str}"
            self._position_entry_bar_time = bar_time
            self._position_bars_held = 0
            # Track pending entry for TTL cancellation
            self._pending_entry_order_id = order_id
            self._pending_entry_bar_time = bar_time
            # Capture trailing stop context at entry (will be updated on fill)
            self._entry_price = current_price
            # Use the side-specific ATR from evaluate() for trailing stop parity
            self._atr_at_entry = signal.atr_at_entry if signal.atr_at_entry is not None else atr_value
            self._position_side = 1 if signal.action == "BUY" else -1
            self._trailing_activated = False
            self._highest_high = float(rolling_df["High"].iloc[-1])
            self._lowest_low = float(rolling_df["Low"].iloc[-1])
            # Store per-trade overrides from tier matching (None = use global)
            self._trade_trailing_atr_mult = signal.trailing_atr_mult
            self._trade_max_hold_bars = signal.max_hold_bars
            local_sym = getattr(
                self._front_month_contract, "localSymbol", "CL"
            )
            log.info(
                "[TRADE] ENTRY: %s %d %s @ %s  "
                "TP=%.2f  SL=%.2f  (prob=%.2f, orderId=%d)  "
                "[offsets: tp=%.4f sl=%.4f — children placed on fill]",
                signal.action, signal.lots, local_sym,
                order_type_str,
                signal.tp_price, signal.sl_price, signal.probability,
                order_id, tp_offset, sl_offset,
            )
            # ── Telegram: trade entry alert ────────────────────────
            try:
                lr = rolling_df.iloc[-1]
                bar_str = f"O:`{float(lr['Open']):.2f}` H:`{float(lr['High']):.2f}` L:`{float(lr['Low']):.2f}` C:`{float(lr['Close']):.2f}` V:`{float(lr['Volume']):.0f}`"
            except Exception:
                bar_str = "N/A"

            self._telegram.send(
                f"📊 *Trade Entry*\n"
                f"{signal.action} {signal.lots} `{local_sym}`\n"
                f"Price: `{current_price:.2f}`\n"
                f"TP: `{signal.tp_price:.2f}` / SL: `{signal.sl_price:.2f}`\n"
                f"Prob (B/S): `{buy_prob_str}` / `{sell_prob_str}`\n"
                f"{bar_str}"
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
            # Store decision context with offsets for fill callback
            decision_ctx = {
                "signal_id": signal_id,
                "decision_id": decision_id,
                "decision_timestamp_utc": decision_timestamp_utc,
                "current_price": current_price,
                "tp_offset": tp_offset,
                "sl_offset": sl_offset,
                "entry_action": signal.action,
                "lots": signal.lots,
                "buy_prob_str": buy_prob_str,
                "sell_prob_str": sell_prob_str,
                "bar_str": bar_str,
            }
            self._last_decision_context_by_order_id[order_id] = decision_ctx
            # Log tradebook event for parent entry
            event_ts = self._utc_iso_now()
            event_id = self._build_event_id(
                event_type="ORDER_SUBMITTED",
                event_ts=event_ts,
                order_id=order_id,
            )
            self.telemetry.log_tradebook_event(
                event_id=event_id,
                event_type="ORDER_SUBMITTED",
                event_timestamp_utc=event_ts,
                order_id=order_id,
                perm_id=getattr(parent_order, "permId", None),
                parent_order_id=getattr(parent_order, "parentId", None),
                account=getattr(parent_order, "account", None),
                symbol=getattr(self._front_month_contract, "symbol", None),
                local_symbol=getattr(self._front_month_contract, "localSymbol", None),
                contract_month=self._extract_contract_month(self._front_month_contract),
                side=getattr(parent_order, "action", None),
                action=getattr(parent_order, "action", None),
                order_type=getattr(parent_order, "orderType", None),
                time_in_force=getattr(parent_order, "tif", None),
                status="SUBMITTED",
                order_qty=float(getattr(parent_order, "totalQuantity", 0) or 0),
                limit_price=float(getattr(parent_order, "lmtPrice", 0) or 0),
                stop_price=float(getattr(parent_order, "auxPrice", 0) or 0),
                **self._base_tradebook_fields(decision_ctx=decision_ctx),
            )
        except Exception as exc:
            log.error("Failed to place entry order: %s", exc)
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

        # ── 1-Hour Heartbeat ───────────────────────────────────────────
        if stream == self._bar_size and bar_time.minute == 0:
            self._telegram.send(f"💓 *1-Hour Heartbeat*\n\n" + self._build_heartbeat_payload())

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
            # Use _stop_event.wait() instead of time.sleep() so Ctrl+C
            # (which sets _stop_event) interrupts the wait immediately
            # instead of blocking for the full backoff delay.
            if self._stop_event.wait(timeout=delay):
                return False  # shutdown requested during wait
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

        # Heartbeat: log status every ~5 minutes (60 cycles × 5s) when
        # no new bars arrive, so the user knows the trader is alive.
        _HEARTBEAT_CYCLES = 60  # 60 × 5s = 300s = 5 minutes
        poll_count = 0

        while self._running:
            try:
                # Proactive disconnect detection:
                # ib.sleep() on a disconnected client does NOT raise —
                # it silently calls asyncio.sleep().  So we must check
                # the connection state explicitly and route to _reconnect()
                # ourselves.  Without this, a disconnect leaves the bot
                # in a zombie state (loop running but no data flowing).
                if not self.manager.ib.isConnected():
                    log.warning(
                        "DISCONNECT DETECTED in event loop — "
                        "attempting reconnect..."
                    )
                    if not self._reconnect():
                        log.error(
                            "Reconnection failed after %d attempts — "
                            "attempting full restart...",
                            _RECONNECT_MAX_ATTEMPTS,
                        )
                        self._running = False
                        self._needs_restart = True
                        break
                    # Reconnect succeeded — resume normal polling
                    poll_count = 0
                    continue

                self.manager.ib.sleep(_POLL_INTERVAL)
                poll_count += 1

                # Periodic heartbeat (only when idle — no bars arriving)
                if poll_count % _HEARTBEAT_CYCLES == 0:
                    self._log_heartbeat()
                    # Stale bar watchdog: if bars stopped arriving while
                    # subscriptions are marked lost, force a reconnect.
                    if self._check_stale_bars():
                        log.info(
                            "Stale bar watchdog triggered reconnect — "
                            "entering recovery path..."
                        )
                        if not self._reconnect():
                            log.error(
                                "Reconnection failed after %d attempts — "
                                "attempting full restart...",
                                _RECONNECT_MAX_ATTEMPTS,
                            )
                            self._running = False
                            self._needs_restart = True
                            break
                        poll_count = 0
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

    def _log_heartbeat(self) -> None:
        """Log a periodic heartbeat so the user knows the trader is alive."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Time since last bar
        if getattr(self, "_last_bar_time_5m", None) is not None:
            delta = now - self._last_bar_time_5m
            hours = delta.total_seconds() / 3600
            last_bar_str = f"{hours:.1f}h ago ({self._last_bar_time_5m})"
        else:
            last_bar_str = "no bars received yet"

        # Market hours check (CL: Sun 18:00 ET → Fri 17:00 ET)
        market_status = self._get_market_status(now)

        # Position and PNL lookup
        try:
            # Pnl accumulation for the execution symbol
            unr_pnl, real_pnl = 0.0, 0.0
            pos = 0.0
            if getattr(self.manager, "ib", None) and self.manager.ib.isConnected():
                for item in self.manager.ib.portfolio():
                    if getattr(item.contract, "symbol", "") == self._execution_symbol:
                        pos += getattr(item, "position", 0.0)
                        unr_pnl += getattr(item, "unrealizedPNL", 0.0) or 0.0
                        real_pnl += getattr(item, "realizedPNL", 0.0) or 0.0
            
            pos_str = f"{pos:g} contracts" if pos != 0 else "FLAT"
            pnl_str = f" | unr_pnl=${unr_pnl:,.2f} | real_pnl=${real_pnl:,.2f}"
        except Exception:
            pos_str = "unknown"
            pnl_str = ""

        subs_status = " | subs_lost=True ⚠️" if self._subscriptions_lost else ""
        log.info(
            "HEARTBEAT: alive | last_bar=%s | market=%s | position=%s%s | connected=%s%s",
            last_bar_str,
            market_status,
            pos_str,
            pnl_str,
            self.manager.ib.isConnected() if getattr(self.manager, "ib", None) else False,
            subs_status,
        )

        # Periodic macro data freshness check
        if getattr(self, "_needs_macro", False):
            now = time.time()
            if now - getattr(self, "_last_macro_check_time", 0.0) >= 3600.0:
                self._last_macro_check_time = now
                from src.features.macro_features import MacroFeatureEngine
                MacroFeatureEngine().refresh_if_stale()

    def _check_stale_bars(self) -> bool:
        """Proactive watchdog — signal reconnect if bars are stale during market hours.

        Returns True if the caller should trigger a reconnect.

        Closes the gap between reactive resubscription (waits for IBKR
        restore event) and socket-level reconnect (waits for socket to
        die).  Without this, the system can sit in a zombie state for
        30-90 minutes with the socket alive but all data farms severed.
        """
        # Only act when we KNOW subscriptions are broken
        if not self._subscriptions_lost:
            return False

        # Don't force reconnect outside market hours (no bars expected)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        market_status = self._get_market_status(now)
        if market_status != "OPEN":
            return False

        # Check how long since the last bar
        last_bar_time = getattr(self, "_last_bar_time_5m", None)
        if last_bar_time is None:
            return False  # No bars received yet — warm start still in progress

        minutes_stale = (now - last_bar_time).total_seconds() / 60
        if minutes_stale < _STALE_BAR_THRESHOLD_MINUTES:
            return False  # Not stale enough yet

        log.warning(
            "STALE BAR WATCHDOG: no bars for %.0f min with "
            "_subscriptions_lost=True — forcing disconnect + reconnect",
            minutes_stale,
        )
        # Disconnect first so _reconnect() starts with a clean state.
        try:
            self.manager.ib.disconnect()
        except Exception:
            pass  # disconnect() can fail if already broken — that's fine
        return True  # Caller should invoke _reconnect()

    @staticmethod
    def _get_market_status(utc_now: datetime) -> str:
        """Return human-readable CL market status based on UTC time.

        CL futures trade Sunday 18:00 ET → Friday 17:00 ET with a
        daily maintenance halt 17:00-18:00 ET (Mon-Thu).

        Args:
            utc_now: Current time in UTC (tz-naive).

        Returns:
            String like "OPEN", "CLOSED (weekend)", or "CLOSED (daily halt)".
        """
        import pytz
        et = pytz.timezone("America/New_York")
        et_now = utc_now.replace(tzinfo=pytz.utc).astimezone(et)
        weekday = et_now.weekday()  # 0=Mon … 6=Sun
        hour = et_now.hour

        # Saturday: always closed
        if weekday == 5:
            return "CLOSED (weekend — opens Sun 6pm ET)"
        # Sunday before 18:00 ET: closed
        if weekday == 6 and hour < 18:
            return "CLOSED (weekend — opens Sun 6pm ET)"
        # Friday after 17:00 ET: closed
        if weekday == 4 and hour >= 17:
            return "CLOSED (weekend — opens Sun 6pm ET)"
        # Mon-Thu 17:00-18:00 ET: daily maintenance halt
        if 0 <= weekday <= 3 and hour == 17:
            return "CLOSED (daily halt 5-6pm ET)"
        return "OPEN"



# ---------------------------------------------------------------------------
# CLI entry point — moved to src.live_execution.cli (Phase 1)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    from src.live_execution.cli import main
    main()
