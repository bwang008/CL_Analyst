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
from src.features.macro_features import MacroFeatureEngine, StaleDataException
from src.live_execution.strategy import Strategy, TradeSignal

from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.data_manager import DataManager
from src.live_execution.interfaces.data_feed_interface import DataFeedClient
from src.live_execution.interfaces.execution_interface import ExecutionClient, StandardExecutionEvent
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
_RECONNECT_MAX_ATTEMPTS = 15     # Max retry attempts (~25 min at max backoff)

# Data farm health check after reconnect:
# IBKR fires 2103/2105 when data farms are broken, and 2104/2106 when OK.
# After TCP connect, we wait this many seconds for data farm "OK" signals
# before treating the connection as healthy.  If only "broken" signals arrive,
# the attempt is failed immediately (no point trying to resubscribe).
_DATA_FARM_BROKEN_CODES = {2103, 2105}     # Market data / HMDS farm broken
_DATA_FARM_OK_CODES = {2104, 2106}         # Market data / HMDS farm OK
_DATA_FARM_WAIT_SECONDS = 10.0             # How long to wait for farm OK after connect

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




# ---------------------------------------------------------------------------
# Feature Pipeline — moved to src.live_execution.feature_pipeline (Phase 1)
# build_live_features() is imported and re-exported above.
# ---------------------------------------------------------------------------



def _tg_escape(text: str) -> str:
    """Escape Telegram Markdown special characters in dynamic text."""
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


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
        data_client: DataFeedClient,
        exec_client: ExecutionClient,
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
        self.data_client = data_client
        self.exec_client = exec_client
        self.quantity = quantity
        self.dry_run = dry_run
        self.entry_mode = entry_mode
        self.adaptive_priority = adaptive_priority
        self.exit_mode = exit_mode
        self._open_orders = {}

        # Strategy (owns model, config, threshold, sizing, bracket math)
        self.strategy = strategy
        self.feature_names: list[str] = strategy.feature_names
        self._needs_macro: bool = any(
            f.startswith(("MACRO_VIX", "MACRO_OVX", "MACRO_DXY",
                          "MACRO_YIELD_CURVE", "MACRO_FED_FUNDS", "COT_"))
            for f in self.feature_names
        )
        self._last_macro_check_time: float = 0.0
        self._macro_daily_closes: dict[str, float] = {}

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
        # Safety Mute: blocks new entries when macro data is stale.
        # Unlike _emergency_halt (permanent), mute auto-recovers when
        # the periodic macro refresh detects fresh data.
        self._data_mute: bool = False
        self._data_mute_reason: str = ""
        self._data_mute_since: float = 0.0
        self._order_timestamps: list[float] = []
        self._last_filled_entry_order_id = None
        log.info("Strategy: %s  direction=%s", strategy.name, strategy.direction)

        # Read execution_symbol from strategy config (Brain=CL, Hands=CL or MCL)
        self._execution_symbol: str = strategy_config.get(
            "execution_symbol", "CL"
        ).upper()
        # Force lean_features to False in live trading because live models
        # generally require the full feature set (MACRO/DIST).
        # This prevents accidental missing feature errors if the config retains
        # backtest optimizations.
        self._lean_features: bool = False

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

        # DataManagers for warm-start (Two-Brain Hub)
        # Log resolved data paths loudly so cross-environment issues
        # (Windows vs WSL vs cloud) are immediately visible in logs.
        from src.data_paths import get_data_root as _get_data_root, _CL_DATA_ROOT
        log.info(
            "DATA PATHS: CL_DATA_ROOT=%s  PROJECT_ROOT=%s",
            _CL_DATA_ROOT or "(NOT SET)",
            _get_data_root().parent if _CL_DATA_ROOT else "(fallback)",
        )
        log.info("DATA PATHS: 5m seed=%s  cache=%s", seed_path, cache_path)

        self.data_manager_5m = DataManager(
            seed_path=seed_path,
            cache_path=cache_path,
            master_ledger_path=str(_get_data_root() / "processed" / "cl_continuous_master.parquet"),
            data_client=self.data_client,
            bar_size="5 mins",
            bars_per_day=288,
        )

        self.data_manager_1h = None
        if self._bar_size in ("1h", "2h", "4h"):
            # 1h models use a dedicated 1h data manager to avoid pacing limits.
            # Seed from the full historical 1H parquet (cl-1h_bk_HourSet_06.parquet)
            # which lives alongside the processed datasets in CL_DATA_ROOT/data/processed/.
            _data_root = _get_data_root()
            cache_path_1h = str(_data_root / "processed" / "warm_start_cache_1h.parquet")
            # Allow strategy config to override the seed path
            _live_cfg = strategy.config.get("live_config", {}) if getattr(strategy, "config", None) else {}
            _seed_override = _live_cfg.get("seed_path_1h")
            if _seed_override:
                # Resolve relative paths against data root
                _seed_p = Path(_seed_override)
                if not _seed_p.is_absolute():
                    _seed_p = _data_root / _seed_override
                seed_path_1h = str(_seed_p)
            else:
                seed_path_1h = str(_data_root / "processed" / "CL_raw_1h.parquet")
            log.info("DATA PATHS: 1h seed=%s  cache=%s", seed_path_1h, cache_path_1h)

            # Hard validation: the 1H seed must exist. If it doesn't, the
            # DataManager would fall back to an IBKR-only backfill that produces
            # too few bars, causing NaN features and silent inference degradation.
            _seed_1h_path = Path(seed_path_1h)
            _cache_1h_path = Path(cache_path_1h)
            if not _cache_1h_path.exists() and not _seed_1h_path.exists():
                raise FileNotFoundError(
                    f"CRITICAL: Neither 1H cache nor seed file found!\n"
                    f"  cache: {cache_path_1h}\n"
                    f"  seed:  {seed_path_1h}\n"
                    f"  CL_DATA_ROOT={_CL_DATA_ROOT}\n"
                    f"Ensure CL_DATA_ROOT points to the shared data directory "
                    f"containing data/processed/CL_HourSet_08.parquet, or copy "
                    f"the seed file to this environment."
                )

            self.data_manager_1h = DataManager(
                seed_path=seed_path_1h,
                cache_path=cache_path_1h,
                master_ledger_path=str(_data_root / "processed" / "cl_continuous_master_1h.parquet"),
                data_client=self.data_client,
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
        self._front_month_local_symbol = None
        self._front_month_str: Optional[str] = None
        self._front_month_last_close: Optional[float] = None  # Hands stream price
        self._running = False
        self._last_bar_time_5m: Optional[pd.Timestamp] = None
        self._last_bar_time_1h: Optional[pd.Timestamp] = None
        self._subscriptions_lost = False  # Track connectivity drops
        self._resubscribe_pending = False  # Prevent duplicate resubscription scheduling
        self._data_farm_ok = False         # Set True when 2104/2106 received
        self._data_farm_broken_only = False # True if only 2103/2105 received (no OK)
        self._callbacks_registered = False
        # Contract rollover state
        self._rollover_in_progress = False
        self._last_rollover_check_date = None
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
        self._environment = "unknown"

        # Telegram alerts (fire-and-forget — failures never affect trading)
        self._telegram = TelegramAlerter()
        self._bot_start_time = datetime.now(timezone.utc)
        self._last_inference_time_sec: float = 0.0
        self._last_inference_bar_time: Optional[pd.Timestamp] = None
        self._last_5m_bar_log: str = ""
        self._last_1h_bar_log: str = ""
        self._last_virtual_ledger_log: str = ""
        self._last_inference_log: str = ""
        self._last_heartbeat_payload: str = ""
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
        total_seconds = int(uptime.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        broker_status = "Connected" if (self.data_client.is_connected() and self.exec_client.is_connected()) else "Disconnected"

        current_position = 0
        unrealized_pnl = 0.0
        realized_pnl = 0.0
        net_liq = 0.0

        # Guard: only query IBKR if connected.  This method may be called
        # from the TelegramHeartbeat daemon thread, which has no asyncio
        # event loop.  Calling ensure_connected() / connect() from that
        # thread crashes with "There is no current event loop in thread".
        if (self.data_client.is_connected() and self.exec_client.is_connected()):
            try:
                acct = self.exec_client.get_account_summary(
                    symbol=self._execution_symbol,
                )
                current_position = acct["cl_position"]
                unrealized_pnl = acct["cl_unrealized_pnl"]
                realized_pnl = acct["cl_realized_pnl"]
                net_liq = acct.get("net_liquidation", 0.0)
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
            recent_logs_block = "\n*Recent Activity*\n"
            if self._last_5m_bar_log:
                recent_logs_block += f"`{self._last_5m_bar_log}`\n"
            if self._last_1h_bar_log:
                recent_logs_block += f"`{self._last_1h_bar_log}`\n"
            if self._last_virtual_ledger_log:
                recent_logs_block += f"`{self._last_virtual_ledger_log}`\n"
            if self._last_inference_log:
                recent_logs_block += f"`{self._last_inference_log}`\n"
        else:
            infer_str = "None (inference not yet computed)"
            recent_logs_block = "\n*Recent Activity*\n`Market Closed / No Data`\n"

        payload = (
            f"Uptime: `{uptime_str}` | Broker: {broker_status}\n\n"
            f"*Account Balance:*\n"
            f"Total Liq: `${net_liq:,.2f}`\n\n"
            f"*Position & PnL*\n"
            f"Position: `{current_position}`\n"
            f"Unrealized PnL: `${unrealized_pnl:,.2f}`\n"
            f"Realized PnL: `${realized_pnl:,.2f}`\n\n"
            f"*MLOps & System*\n"
            f"Last Inference Bar: {infer_str}\n"
            f"Inference Latency: `{self._last_inference_time_sec:.4f}s`\n"
            f"CPU: `{cpu_pct:.1f}%` | RAM: `{ram_pct:.1f}%` | Disk: `{disk_pct:.1f}%`\n"
            f"{recent_logs_block}"
        )

        if recent_errors:
            lines = []
            for level, msg, ts_utc in recent_errors[-5:]:
                icon = "[ERROR]" if level == "ERROR" else "[WARNING]"
                lines.append(f"{icon} `{ts_utc}` `{msg[:150]}`")
            payload += "\n\n*Recent Warnings/Errors*\n" + "\n".join(lines)

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
                if payload != self._last_heartbeat_payload:
                    self._telegram.send(f"*1-Hour Heartbeat*\n\n" + payload)
                    self._last_heartbeat_payload = payload
            except Exception as e:
                log.exception("Error in heartbeat thread: %s", e)

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
            log.info("Connecting to Data and Execution adapters...")
            self.data_client.connect()
            self.exec_client.connect()
            log.info("Connected to IBKR")

            # Step 2: Register error handler for reconnection
            self._callbacks_registered = False
            self._register_execution_callbacks()

            # Step 3: Qualify continuous contract (Brain stream) (Now handled by DataFeed)

            if self._needs_macro:
                log.info("Fetching previous daily closes for macro indices (VIX, OVX)...")
                for sym, alias in [("VIX", "VIX"), ("OVX", "OVX")]:
                    try:
                        self._macro_daily_closes[alias] = self.data_client.fetch_daily_close(sym)
                    except Exception as e:
                        log.warning("Failed to fetch daily close for %s: %s", sym, e)
                
                log.info("Loaded macro daily closes: %s", self._macro_daily_closes)

            # Step 4: Resolve front-month contract (Hands stream)
            #         Use execution_symbol from config (CL or MCL)
            try:
                self._front_month_local_symbol, self._front_month_str = (
                    self.data_client.get_front_month_contract(
                        symbol=self._execution_symbol,
                    )
                )
                log.info(
                    "Front-month contract: %s (month=%s)",
                    self._front_month_local_symbol,
                    self._front_month_str,
                )
            except Exception as exc:
                log.warning(
                    "Could not resolve front-month contract: %s. "
                    "Raw front-month logging will be disabled.",
                    exc,
                )

            # Step 4b: Pre-resolve the execution contract on the exec adapter.
            # This caches the qualified contract outside the event loop so
            # order placement from bar-update callbacks won't trigger async
            # IBKR calls that crash with "event loop already running".
            try:
                self.exec_client.resolve_contract(self._execution_symbol)
            except Exception as exc:
                log.warning(
                    "Could not pre-resolve execution contract: %s",
                    exc,
                )

            # Step 5: Print CL-only account summary
            self._print_account_summary()

            # Step 6: Pass front-month ID to DataManagers for rollover detection
            if self._front_month_local_symbol is not None:
                self.data_manager_5m.front_month_id = (
                    self._front_month_local_symbol
                )
                if self.data_manager_1h is not None:
                    self.data_manager_1h.front_month_id = (
                        self._front_month_local_symbol
                    )

            # Step 7: Refresh external macro data if stale and model needs it
            if self._needs_macro:
                log.info("Model uses external macro features — checking freshness...")
                try:
                    MacroFeatureEngine().refresh_if_stale()
                    # Also verify value-level freshness (file may be
                    # new but contain repeated data from FRED).
                    overrides = getattr(self, "_macro_daily_closes", {})
                    MacroFeatureEngine()._build_fred_features(
                        live_overrides=overrides,
                        live_time=pd.Timestamp.now()
                    )
                except StaleDataException as e:
                    self._data_mute = True
                    self._data_mute_reason = str(e)
                    self._data_mute_since = time.time()
                    log.critical(
                        "[SAFETY MUTE] ACTIVATED AT STARTUP -- "
                        "Stale FRED data, new entries BLOCKED: %s", e,
                    )
                    tg_msg = (
                        f"*[!] SAFETY MUTE ACTIVATED AT STARTUP*\n"
                        f"Stale FRED data -- new entries BLOCKED.\n"
                        f"{_tg_escape(str(e))}"
                    )
                    try:
                        self._telegram.send(tg_msg)
                    except Exception:
                        pass
                self._last_macro_check_time = time.time()

            # Step 8: Warm-start via DataManager
            self._warm_start()

            # Step 8b: Warmup inference state for continuity
            strategy_config = getattr(self.strategy, "config", {})
            warmup_bars = strategy_config.get("warmup_bars", 24)
            self._warmup_inference_state(num_bars=warmup_bars)

            # Step 7b: Recover any inherited position from the ledger
            self._recover_inherited_position()

            # Step 7c: Cancel any orphaned CL orders if we booted FLAT
            self._cancel_orphaned_orders_on_startup()

            # Step 8: Subscribe to live bars (Brain stream)
            self._subscribe()

            if self._front_month_local_symbol is not None:
                self._telegram.send(f"Front Month: `{self._front_month_local_symbol}` ({self._front_month_str})")
                self._subscribe_front_month()

            # Step 10: Enter event loop
            self._running = True

            # ── Telegram: startup confirmation ────────────────────────
            data_port = getattr(self.data_client, "port", None) or getattr(getattr(self.data_client, "manager", None), "port", "N/A")
            exec_port = getattr(self.exec_client, "port", None) or getattr(getattr(self.exec_client, "manager", None), "port", "N/A")
            startup_msg = (
                f"*LiveTrader Online*\n"
                f"Strategy: `{self.strategy.name}`\n"
                f"Environment: `{self._environment}`\n"
                f"Host: `{self._hostname}`\n"
                f"Dry-run: `{self.dry_run}`\n"
                f"Data Port: `{data_port}`\n"
                f"Exec Port: `{exec_port}`\n\n"
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
                f"[FATAL] *FATAL ERROR — LiveTrader Down*\n"
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
                self.data_client.cancel_subscription(self._live_bars_5m)
            except Exception:
                pass
        if self._live_bars_1h is not None:
            try:
                self.data_client.cancel_subscription(self._live_bars_1h)
            except Exception:
                pass
        if self._front_month_bars is not None:
            try:
                self.data_client.cancel_subscription(self._front_month_bars)
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
        self.data_client.disconnect()
        self.exec_client.disconnect()
        self.telemetry.close()
        _logging.getLogger().removeHandler(self._log_capture)
        log.info("Shutdown complete.")

    def _register_execution_callbacks(self) -> None:
        """Register IBKR execution callbacks once per connection lifecycle."""
        if self._callbacks_registered:
            return
        self.exec_client.register_order_status_callback(self._on_standard_execution_event)
        if hasattr(self.data_client, "register_error_callback"):
            self.data_client.register_error_callback(self._on_ib_error)
        if hasattr(self.exec_client, "register_error_callback"):
            self.exec_client.register_error_callback(self._on_ib_error)
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

    def _reset_position_state(self, reason: str = "CLOSED") -> None:
        if hasattr(self, '_strategy') and self._strategy is not None and self._position_side != 0:
            if hasattr(self._strategy, 'on_exit'):
                self._strategy.on_exit(self._position_side, reason, self._position_bars_held)
                
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
        self._tracked_tp_price = None
        self._tracked_sl_price = None
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
            for evt in list(self._open_orders.values()):
                if evt.symbol != self._execution_symbol:
                    continue
                order_id = evt.order_id
                if str(order_id) == str(self._pending_entry_order_id):
                    status_str = evt.status
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
            cancelled = self.exec_client.cancel_open_orders(
                symbol=self._execution_symbol,
            )
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
            for evt in list(self._open_orders.values()):
                if evt.symbol != self._execution_symbol:
                    continue
                order_id = evt.order_id
                if str(order_id) != str(self._sl_order_id):
                    continue
                # Extract raw ib_insync order to read/modify auxPrice
                raw_order = getattr(getattr(evt, "raw_event", None), "order", None)
                old_sl = getattr(raw_order, "auxPrice", 0.0) or 0.0 if raw_order else 0.0
                if raw_order is not None:
                    raw_order.auxPrice = new_sl
                if hasattr(self.exec_client, "modify_order"): self.exec_client.modify_order(evt.order_id, evt)
                log.info(
                    "TRAILING STOP: modified SL order %d: %.2f → %.2f",
                    order_id, old_sl, new_sl,
                )
                self._trailing_activated = True
                self._tracked_sl_price = new_sl  # Update local cache
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
        current_position = self.exec_client.get_position(
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
                        symbol=self._execution_symbol,
                        status="CLOSED",
                        **self._base_tradebook_fields(),
                    )
                except Exception:
                    log.debug(
                        "Failed to log OOB tradebook event", exc_info=True
                    )
                # Cancel any orphaned TP/SL orders still live on IBKR
                try:
                    cancelled = self.exec_client.cancel_open_orders(
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

        cancelled = self.exec_client.cancel_open_orders(
            symbol=self._execution_symbol,
        )
        trade = self.exec_client.close_position(
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
            self._processed_exit_order_ids.add(str(_exit_oid))
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
        ibkr_pos = self.exec_client.get_position(
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
                cancelled = self.exec_client.cancel_open_orders(
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

        # 4. Verify TP/SL orders on IBKR (query directly — self._open_orders
        #    is empty at startup before subscriptions begin)
        tp_found = False
        sl_found = False
        try:
            open_trades = self.exec_client.get_open_trades(
                symbol=self._execution_symbol,
            )
            for evt in open_trades:
                oid = evt.order_id
                if oid is not None:
                    self._open_orders[oid] = evt
                if oid is not None and str(oid) == str(tp_order_id):
                    tp_found = True
                elif oid is not None and str(oid) == str(sl_order_id):
                    sl_found = True
        except Exception:
            log.warning(
                "[RECOVERY] Failed to scan IBKR open trades",
                exc_info=True,
            )

        if tp_found and sl_found:
            self._tp_order_ids = [tp_order_id]
            self._sl_order_id = sl_order_id
            self._tracked_tp_price = tp_price
            self._tracked_sl_price = sl_price
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

        if self._front_month_local_symbol is None:
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
            self.exec_client.cancel_open_orders(
                symbol=self._execution_symbol,
            )
        except Exception:
            log.debug("[RECOVERY] cancel_open_cl_orders failed", exc_info=True)

        # Place fresh TP/SL
        exit_action = "SELL" if self._position_side == 1 else "BUY"
        try:
            child_trades = self.exec_client.place_child_orders(
                symbol=self._execution_symbol,
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

        ibkr_pos = self.exec_client.get_position(
            symbol=self._execution_symbol,
        )
        if ibkr_pos != 0:
            return  # IBKR has a position — don't cancel its protective orders

        # Bot is FLAT with no tracked orders — any CL orders on IBKR are orphans
        try:
            orphaned_evts = []
            for evt in list(self._open_orders.values()):
                if evt.symbol == self._execution_symbol:
                    orphaned_evts.append(evt)

            if not orphaned_evts:
                return

            log.warning(
                "[STARTUP SWEEP] Bot is FLAT but found %d orphaned CL order(s) "
                "on IBKR — cancelling to prevent phantom fills",
                len(orphaned_evts),
            )
            # Use the adapter's cancel_open_orders to cancel all CL orders
            try:
                cancelled = self.exec_client.cancel_open_orders(
                    symbol=self._execution_symbol,
                )
                log.info(
                    "[STARTUP SWEEP] Cancelled %d orphaned order(s)",
                    cancelled,
                )
            except Exception:
                log.exception(
                    "[STARTUP SWEEP] Failed to cancel orphaned orders"
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
        # Require decision context
        if order_id not in self._last_decision_context_by_order_id:
            log.warning(
                "BRACKET CHILDREN: no decision context for orderId=%s "
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
            child_trades = self.exec_client.place_child_orders(
                symbol=self._execution_symbol,
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
                self._tracked_tp_price = tp_price[0][1] if isinstance(tp_price, list) else tp_price
                self._tracked_sl_price = sl_price
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

    def _print_account_summary(self) -> None:
        """Print a CL-only account summary at startup."""
        w = 60  # box width
        try:
            acct = self.exec_client.get_account_summary(symbol=self._execution_symbol)
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
            # MACRO_6M needs 4320 hourly bars — enforce this at startup.
            _min_required = {"1h": 4320, "2h": 4320, "4h": 4320}
            _required = _min_required.get(self._bar_size, 0)
            if len(self.rolling_df_1h) < _required:
                err_msg = (
                    f"1H cache has only {len(self.rolling_df_1h)} bars — "
                    f"need {_required} for {self._bar_size} MACRO_6M feature warmup. "
                    f"Delete warm_start_cache_1h.parquet to trigger reseed."
                )
                log.error("CACHE VALIDATION FAILED: %s", err_msg)
                self._telegram.send(f"[WARNING] *CACHE VALIDATION FAILED*\n`{err_msg}`")
                raise RuntimeError(err_msg)

    def _warmup_inference_state(self, num_bars: int) -> None:
        """Run inference on the last N historical bars to restore state
        (e.g., consecutive signal counts, trailing tops) before live trading begins.
        """
        if num_bars <= 0:
            return

        source_df = None
        if self._bar_size in ("1h", "2h", "4h") and self.data_manager_1h is not None:
            source_df = self.data_manager_1h.get_ratio_adjusted_df()
        elif self.rolling_df_5m is not None:
            source_df = self.rolling_df_5m

        if source_df is None or source_df.empty:
            return

        N = min(num_bars, len(source_df))
        if N <= 0:
            return

        log.info("Warming up inference state on last %d bars...", N)

        # 1. Fetch real current position and average cost from IBKR
        try:
            real_current_position = self.exec_client.get_position(
                symbol=self._execution_symbol,
            )
            acct = self.exec_client.get_account_summary(symbol=self._execution_symbol)
            average_cost = float(acct.get("cl_avg_cost", 0.0))
        except Exception:
            log.warning("Warmup: failed to fetch IBKR position, defaulting to 0", exc_info=True)
            real_current_position = 0
            average_cost = 0.0

        # 2. Build features for the last N bars in one pass
        from src.live_execution.feature_pipeline import build_live_features
        try:
            warmup_features = build_live_features(
                source_df,
                self.feature_names,
                lean=self._lean_features,
                bar_size=self._bar_size,
                macro_overrides={},  # no live overrides during warmup
                return_last_n=N
            )
        except Exception as exc:
            log.warning("Warmup feature generation failed: %s", exc)
            return

        if warmup_features is None or warmup_features.empty:
            log.info("Warmup skipped (insufficient feature data).")
            return

        # 3. Pre-compute ATR arrays from raw source (parity with _on_new_bar)
        def _compute_series_atr(period: int):
            if len(source_df) >= period + 1:
                import pandas_ta as _ta
                return source_df.ta.atr(length=period)
            return None

        atr_series_long = _compute_series_atr(self._atr_period_long)
        atr_series_short = _compute_series_atr(self._atr_period_short)
        atr_series = _compute_series_atr(self._atr_period)

        # 4. Determine if entry occurred during this warmup window
        entry_crossed = False
        if real_current_position != 0 and average_cost > 0:
            for i in range(len(warmup_features)):
                dt_idx = warmup_features.index[i]
                if dt_idx in source_df.index:
                    if float(source_df.loc[dt_idx, "Low"]) <= average_cost <= float(source_df.loc[dt_idx, "High"]):
                        entry_crossed = True
                        break

        # If the position was entered BEFORE the warmup window, it won't cross the average cost.
        # In that case, we start with the mock_position already active.
        mock_position = 0 if entry_crossed else real_current_position

        # 5. Iterate over the returned feature rows and run strategy.evaluate
        # warmup_features has DateTime index matching the source_df
        for i in range(len(warmup_features)):
            row_features = warmup_features.iloc[[i]]
            dt = row_features.index[0]

            # Get raw prices for this bar
            if dt in source_df.index:
                current_price = float(source_df.loc[dt, "Close"])
                high_price = float(source_df.loc[dt, "High"])
                low_price = float(source_df.loc[dt, "Low"])
                atr_val = float(atr_series.loc[dt]) if atr_series is not None and not pd.isna(atr_series.loc[dt]) else None
                atr_long = float(atr_series_long.loc[dt]) if atr_series_long is not None and not pd.isna(atr_series_long.loc[dt]) else None
                atr_short = float(atr_series_short.loc[dt]) if atr_series_short is not None and not pd.isna(atr_series_short.loc[dt]) else None
            else:
                continue # Should not happen

            # Heuristic for Time-Travel Position Guard
            if real_current_position != 0 and mock_position == 0:
                if low_price <= average_cost <= high_price:
                    mock_position = real_current_position

            # 6. Execute isolated inference (discard output)
            _ = self.strategy.evaluate(
                features=row_features,
                current_price=current_price,
                atr_value=atr_val,
                current_position=mock_position,
                atr_value_long=atr_long,
                atr_value_short=atr_short,
            )

        log.info("Inference warmup complete.")

    # ------------------------------------------------------------------
    # Live bar subscription
    # ------------------------------------------------------------------

    def _subscribe(self) -> None:
        """Subscribe to live bars (Brain streams)."""
        log.info("Subscribing to live 5-min bars (Stream A)...")
        self._live_bars_5m = self.data_client.subscribe_live_bars(
            symbol=self._execution_symbol,
            continuous=True,
            bar_size="5 mins",
            duration_str="60 S",
        )
        self._live_bars_5m.updateEvent += self._on_bar_update_5m
        log.info("Subscribed to 5-min continuous contract live bars")

        if self._bar_size in ("1h", "2h", "4h"):
            log.info("Subscribing to live 1-hour bars (Stream B)...")
            self._live_bars_1h = self.data_client.subscribe_live_bars(
                symbol=self._execution_symbol,
                continuous=True,
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
        self._front_month_bars = self.data_client.subscribe_live_bars(
            symbol=self._execution_symbol,
            continuous=False,
            bar_size="5 mins",
            duration_str="60 S",
        )
        self._front_month_bars.updateEvent += self._on_front_month_bar_update
        log.info("Subscribed to front-month live bars")

    # ------------------------------------------------------------------
    # Contract Rollover Detection
    # ------------------------------------------------------------------

    def _check_contract_rollover(self) -> None:
        """Check if the front-month contract has rolled and transition if so.

        Called from the main event loop's 5-minute poll cycle (between
        ib.sleep() calls) and from _resubscribe_and_backfill() after
        reconnection.  Both call sites are on the main thread with the
        asyncio event loop idle, so synchronous IBKR API calls
        (reqContractDetails, placeOrder, cancelOrder) are safe.

        Gated by ``_last_rollover_check_date`` to query IBKR at most
        once per UTC calendar day.

        If a rollover is detected:
        1. Force-close any open position on the expiring contract.
        2. Update ``_front_month_local_symbol`` and ``_front_month_str``.
        3. Re-cache the execution contract.
        4. Update DataManager ``front_month_id`` for ratio tracking.
        5. Re-subscribe the Hands stream to the new contract.
        6. Send a Telegram notification.
        """
        today = datetime.now(timezone.utc).date()
        if self._last_rollover_check_date == today:
            return
        self._last_rollover_check_date = today

        # Resolve current front-month from IBKR
        try:
            new_local_sym, new_month_str = (
                self.data_client.get_front_month_contract(
                    symbol=self._execution_symbol,
                )
            )
        except Exception as exc:
            log.warning("Rollover check: failed to resolve front-month: %s", exc)
            return

        if new_local_sym == self._front_month_local_symbol:
            log.debug(
                "Rollover check: front-month unchanged (%s)", new_local_sym,
            )
            return

        # ── ROLLOVER DETECTED ─────────────────────────────────────────
        old_sym = self._front_month_local_symbol
        log.warning(
            "=" * 60 + "\n"
            "CONTRACT ROLLOVER DETECTED: %s → %s\n"
            + "=" * 60,
            old_sym, new_local_sym,
        )

        self._rollover_in_progress = True
        try:
            # 1. Force-close any open position on the expiring contract
            current_position = self.exec_client.get_position(
                symbol=self._execution_symbol,
            )
            if current_position != 0:
                log.warning(
                    "ROLLOVER FORCE-CLOSE: position=%d on %s — "
                    "cancelling brackets and closing at market",
                    current_position, old_sym,
                )
                # Cancel resting TP/SL bracket orders
                try:
                    cancelled = self.exec_client.cancel_open_orders(
                        self._execution_symbol,
                    )
                    log.info("Rollover: cancelled %d resting orders", cancelled)
                except Exception as exc:
                    log.warning("Rollover: cancel_open_orders failed: %s", exc)

                # Market-close the old position (uses pos.contract from
                # IBKR portfolio — targets the actual old-month contract,
                # not our cached reference).
                try:
                    self.exec_client.close_position(
                        symbol=self._execution_symbol,
                        exit_mode="market",
                        current_price=0.0,  # market order, price unused
                    )
                    log.info("Rollover: market close order submitted")
                except Exception as exc:
                    log.error("Rollover: close_position failed: %s", exc)

                # Reset internal position tracking
                self._reset_position_state(reason="ROLLOVER")
                self._pending_entry_order_id = None
                self._pending_entry_bar_time = None

                self._telegram.send(
                    f"*CONTRACT ROLLOVER*\n"
                    f"`{old_sym}` → `{new_local_sym}`\n\n"
                    f"[WARNING] Force-closed position ({current_position:+d}) on "
                    f"expiring contract at market.\n"
                    f"Waiting for next natural signal on `{new_local_sym}`."
                )
            else:
                self._telegram.send(
                    f"*CONTRACT ROLLOVER*\n"
                    f"`{old_sym}` → `{new_local_sym}`\n\n"
                    f"Position: FLAT — clean transition."
                )

            # 2. Update contract references
            self._front_month_local_symbol = new_local_sym
            self._front_month_str = new_month_str
            self._front_month_last_close = None  # stale — will refresh on next bar
            log.info(
                "Rollover: updated front-month to %s (%s)",
                new_local_sym, new_month_str,
            )

            # 3. Re-cache execution contract (so orders use the new month)
            try:
                self.exec_client.resolve_contract(self._execution_symbol)
                log.info("Rollover: execution contract re-cached")
            except Exception as exc:
                log.error(
                    "Rollover: failed to re-cache execution contract: %s", exc,
                )

            # 4. Update DataManager front_month_id for ratio tracking
            self.data_manager_5m.front_month_id = new_local_sym
            if self.data_manager_1h is not None:
                self.data_manager_1h.front_month_id = new_local_sym

            # 5. Re-subscribe Hands stream (front-month bars)
            if self._front_month_bars is not None:
                try:
                    self.data_client.cancel_subscription(
                        self._front_month_bars,
                    )
                except Exception:
                    pass
                self._front_month_bars = None

            try:
                self._subscribe_front_month()
                log.info("Rollover: front-month bars re-subscribed")
            except Exception as exc:
                log.error(
                    "Rollover: failed to re-subscribe front-month bars: %s",
                    exc,
                )

            log.info(
                "=" * 60 + "\n"
                "CONTRACT ROLLOVER COMPLETE: now trading %s (%s)\n"
                + "=" * 60,
                new_local_sym, new_month_str,
            )
        except Exception:
            log.exception("Unexpected error during contract rollover")
        finally:
            self._rollover_in_progress = False

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
        # Warning 2103: Market data farm connection is broken
        # Warning 2105: HMDS data farm connection is broken
        # A data farm break can silently kill keepUpToDate subscriptions
        # without ever firing error 10182.  Mark subscriptions as lost
        # proactively so the 2104/2106 "OK" handler triggers resubscription.
        if errorCode in (1100, 1101, 2103, 2105):
            log.warning("CONNECTIVITY/DATA FARM LOST (code %d: %s) — marking subscriptions as lost", errorCode, errorString)
            self._subscriptions_lost = True
            if errorCode in _DATA_FARM_BROKEN_CODES:
                self._data_farm_broken_only = True

        # Error 1102: connectivity restored, data maintained
        # Error 1101: connectivity restored, data lost
        # Warning 2104: Market data farm connection is OK
        # Warning 2106: HMDS data farm connection is OK
        if errorCode in _DATA_FARM_OK_CODES:
            self._data_farm_ok = True
            self._data_farm_broken_only = False

        if errorCode in (1101, 1102, 2104, 2106) and self._subscriptions_lost:
            if self._resubscribe_pending:
                log.debug("CONNECTIVITY RESTORED (code %d) — resubscription already scheduled, skipping", errorCode)
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
                    self.data_client.cancel_subscription(self._live_bars_5m)
                except Exception:
                    pass
            if self._live_bars_1h is not None:
                try:
                    self.data_client.cancel_subscription(self._live_bars_1h)
                except Exception:
                    pass
            if self._front_month_bars is not None:
                try:
                    self.data_client.cancel_subscription(self._front_month_bars)
                except Exception:
                    pass
            
            # 2. Re-subscribe using async API
            log.info("Subscribing to live 5-min bars (Stream A)...")
            self._live_bars_5m = await self.data_client.subscribe_live_bars_async(
                symbol=self._execution_symbol,
                continuous=True,
                bar_size="5 mins",
                duration_str="60 S",
            )
            self._live_bars_5m.updateEvent += self._on_bar_update_5m
            log.info("Subscribed to 5-min continuous contract live bars")

            if self._bar_size == "1h":
                log.info("Subscribing to live 1-hour bars (Stream B)...")
                self._live_bars_1h = await self.data_client.subscribe_live_bars_async(
                    symbol=self._execution_symbol,
                    continuous=True,
                    bar_size="1 hour",
                    duration_str="2 D",
                )
                self._live_bars_1h.updateEvent += self._on_bar_update_1h
                log.info("Subscribed to 1-hour continuous contract live bars")

            if self._front_month_local_symbol is not None:
                log.info(
                    "Subscribing to front-month bars (Hands stream: %s)...",
                    self._front_month_str,
                )
                self._front_month_bars = await self.data_client.subscribe_live_bars_async(
                    symbol=self._execution_symbol,
                    continuous=False,
                    bar_size="5 mins",
                    duration_str="60 S",
                )
                self._front_month_bars.updateEvent += self._on_front_month_bar_update
                log.info("Subscribed to front-month live bars")

            self._subscriptions_lost = False

            # 3. Backfill any gap from the disconnect period
            await self._backfill_reconnect_gap_async()

            log.info("Reconnection complete — live bars flowing again")

        except Exception:
            log.exception("Deferred resubscription failed — will retry on next reconnect")
        finally:
            self._resubscribe_pending = False

    async def _backfill_reconnect_gap_async(self) -> None:
        """Backfill bars missed during a disconnect/hibernation gap.

        After reconnecting, detects the gap between the last known bar
        timestamp and now, requests historical bars from IBKR via the
        async API, and injects them into the rolling DataFrames and
        DataManager caches.

        This prevents phantom price spikes in rolling indicators when
        bars are missed due to connectivity loss, hibernation, etc.
        """
        from src.live_execution.ibkr_client import build_cl_contract, ib_bars_to_dataframe

        now = pd.Timestamp.now()
        warmup_count = 0

        # ── 5M gap backfill ──────────────────────────────────────────
        if self._last_bar_time_5m is not None:
            gap_5m = now - self._last_bar_time_5m
            gap_5m_min = gap_5m.total_seconds() / 60

            if gap_5m_min > 10:  # Only backfill if gap > 10 minutes
                gap_days = max(1, int(gap_5m.total_seconds() / 86400) + 1)
                duration_str = f"{gap_days} D"
                log.info(
                    "RECONNECT BACKFILL (5M): gap=%.0f min (%s → %s), "
                    "requesting %s from IBKR",
                    gap_5m_min, self._last_bar_time_5m, now, duration_str,
                )

                try:
                    chunk_df = await self.data_client.fetch_historical_bars_by_duration_async(
                        duration_str=duration_str,
                        continuous=True,
                        bar_size="5 mins",
                        what_to_show="TRADES",
                        use_rth=False
                    )

                    if chunk_df is not None and not chunk_df.empty:
                        # Only keep bars newer than our last known bar
                        new_bars = chunk_df[chunk_df.index > self._last_bar_time_5m]
                        if len(new_bars) > 0:
                            self.rolling_df_5m = pd.concat([self.rolling_df_5m, new_bars])
                            # Dedup and sort
                            self.rolling_df_5m = self.rolling_df_5m[
                                ~self.rolling_df_5m.index.duplicated(keep="last")
                            ].sort_index()
                            if len(self.rolling_df_5m) > _MAX_ROLLING_BARS:
                                self.rolling_df_5m = self.rolling_df_5m.iloc[-_MAX_ROLLING_BARS:]

                            # Update DataManager cache
                            if self.data_manager_5m is not None:
                                for _, row in new_bars.iterrows():
                                    self.data_manager_5m.append_bar(row.to_frame().T)
                                self.data_manager_5m.save_cache()

                            self._last_bar_time_5m = self.rolling_df_5m.index[-1]
                            log.info(
                                "RECONNECT BACKFILL (5M): stitched %d bars, "
                                "latest=%s",
                                len(new_bars), self._last_bar_time_5m,
                            )
                            self._telegram.send(
                                f"🔄 *Reconnect Backfill (5M) Completed*\n"
                                f"Successfully stitched *{len(new_bars)}* missing 5-minute bars into cache.\n"
                                f"Latest: `{self._last_bar_time_5m}`"
                            )
                            if self._bar_size == "5m":
                                warmup_count = len(new_bars)
                        else:
                            log.info("RECONNECT BACKFILL (5M): no new bars to stitch")
                    else:
                        log.warning("RECONNECT BACKFILL (5M): IBKR returned no bars")
                except Exception:
                    log.exception("RECONNECT BACKFILL (5M) failed — continuing without backfill")
            else:
                log.info("RECONNECT BACKFILL (5M): gap < 10 min — no backfill needed")

        # ── 1H gap backfill ──────────────────────────────────────────
        if self._last_bar_time_1h is not None and self._bar_size in ("1h", "2h", "4h"):
            gap_1h = now - self._last_bar_time_1h
            gap_1h_min = gap_1h.total_seconds() / 60

            if gap_1h_min > 70:  # Only backfill if gap > 70 min (1 bar + margin)
                gap_days = max(1, int(gap_1h.total_seconds() / 86400) + 1)
                duration_str = f"{gap_days} D"
                log.info(
                    "RECONNECT BACKFILL (1H): gap=%.0f min (%s → %s), "
                    "requesting %s from IBKR",
                    gap_1h_min, self._last_bar_time_1h, now, duration_str,
                )

                try:
                    chunk_df = await self.data_client.fetch_historical_bars_by_duration_async(
                        duration_str=duration_str,
                        continuous=True,
                        bar_size="1 hour",
                        what_to_show="TRADES",
                        use_rth=False
                    )

                    if chunk_df is not None and not chunk_df.empty:
                        new_bars = chunk_df[chunk_df.index > self._last_bar_time_1h]
                        if len(new_bars) > 0:
                            self.rolling_df_1h = pd.concat([self.rolling_df_1h, new_bars])
                            self.rolling_df_1h = self.rolling_df_1h[
                                ~self.rolling_df_1h.index.duplicated(keep="last")
                            ].sort_index()
                            if len(self.rolling_df_1h) > _MAX_ROLLING_BARS:
                                self.rolling_df_1h = self.rolling_df_1h.iloc[-_MAX_ROLLING_BARS:]

                            # Update DataManager cache
                            if self.data_manager_1h is not None:
                                for _, row in new_bars.iterrows():
                                    self.data_manager_1h.append_bar(row.to_frame().T)
                                self.data_manager_1h.save_cache()

                            self._last_bar_time_1h = self.rolling_df_1h.index[-1]
                            log.info(
                                "RECONNECT BACKFILL (1H): stitched %d bars, "
                                "latest=%s",
                                len(new_bars), self._last_bar_time_1h,
                            )
                            self._telegram.send(
                                f"🔄 *Reconnect Backfill (1H) Completed*\n"
                                f"Successfully stitched *{len(new_bars)}* missing 1-hour bars into cache.\n"
                                f"Latest: `{self._last_bar_time_1h}`"
                            )
                            if self._bar_size in ("1h", "2h", "4h"):
                                warmup_count = len(new_bars)
                        else:
                            log.info("RECONNECT BACKFILL (1H): no new bars to stitch")
                    else:
                        log.warning("RECONNECT BACKFILL (1H): IBKR returned no bars")
                except Exception:
                    log.exception("RECONNECT BACKFILL (1H) failed — continuing without backfill")
            else:
                log.info("RECONNECT BACKFILL (1H): gap < 70 min — no backfill needed")

        if warmup_count > 0:
            self._warmup_inference_state(num_bars=warmup_count)

    def _resubscribe_and_backfill(self) -> None:
        """Synchronous resubscription — used by _reconnect() after a clean reconnect.

        Cancels stale subscriptions and re-subscribes using the sync API.
        This is safe to call from _reconnect() because the event loop is
        NOT running at that point (we're inside a time.sleep-based retry
        loop, not inside ib.sleep).
        """
        # 0. Check for contract rollover (reconnect after long outage may
        #    span a rollover boundary — re-resolve before resubscribing).
        self._check_contract_rollover()

        # 1. Cancel stale subscriptions
        if self._live_bars_5m is not None:
            try:
                self.data_client.cancel_subscription(self._live_bars_5m)
            except Exception:
                pass
            self._live_bars_5m = None

        if self._live_bars_1h is not None:
            try:
                self.data_client.cancel_subscription(self._live_bars_1h)
            except Exception:
                pass
            self._live_bars_1h = None
        if self._front_month_bars is not None:
            try:
                self.data_client.cancel_subscription(self._front_month_bars)
            except Exception:
                pass
            self._front_month_bars = None

        # 2. Re-subscribe using sync API (safe outside event loop)
        self._subscribe()

        if self._front_month_local_symbol is not None:
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
        # Cache close for execution pricing (used by _on_new_bar to set
        # the MARKETABLE_LIMIT price on the actual execution contract
        # rather than the Brain stream's continuous contract close).
        self._front_month_last_close = float(new_bar.close)

    def _on_bar_update_5m(self, bars, has_new_bar=False) -> None:
        """Callback fired by ib_insync when continuous 5m bars are updated."""
        if not has_new_bar or len(bars) < 2:
            return

        # bars[-1] is the new incomplete bar that just opened.
        # bars[-2] is the fully completed bar that just closed.
        new_bar = bars[-2]
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

        bar_log = (
            f"NEW 5M BAR: {bar_time}  O={new_row['Open'].iloc[0]:.2f} H={new_row['High'].iloc[0]:.2f} "
            f"L={new_row['Low'].iloc[0]:.2f} C={new_row['Close'].iloc[0]:.2f} V={new_row['Volume'].iloc[0]:.0f}"
        )
        log.info(bar_log)
        self._last_5m_bar_log = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} [INFO] {bar_log}"

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
        if not has_new_bar or len(bars) < 2:
            return

        # bars[-1] is the new incomplete bar that just opened.
        # bars[-2] is the fully completed bar that just closed.
        new_bar = bars[-2]
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

        bar_log = (
            f"NEW 1H BAR: {bar_time}  O={new_row['Open'].iloc[0]:.2f} H={new_row['High'].iloc[0]:.2f} "
            f"L={new_row['Low'].iloc[0]:.2f} C={new_row['Close'].iloc[0]:.2f} V={new_row['Volume'].iloc[0]:.0f}"
        )
        log.info(bar_log)
        self._last_1h_bar_log = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} [INFO] {bar_log}"

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
            msg = "[CRITICAL] Order rate limit exceeded (10 orders / 60s). System HALTED."
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
        if self._rollover_in_progress:
            log.debug("Skipping bar during contract rollover transition")
            return

        # 0a. Entry order TTL: cancel stale entry orders after 1 bar
        self._check_entry_order_ttl(bar_time)



        # 1. Generate features (always — needed for INFERENCE display)
        # SPLIT-BRAIN: Use ratio-adjusted data for features (model was trained
        # on ratio-adjusted continuous series). Raw data is used for execution.
        try:
            if self.data_manager_1h is not None:
                ratio_adjusted_df = self.data_manager_1h.get_ratio_adjusted_df()
            else:
                ratio_adjusted_df = rolling_df  # fallback for non-1h modes

            # Prepare macro overrides from real-time IBKR subscriptions
            macro_overrides = {}
            if self._needs_macro:
                if getattr(self, "_macro_daily_closes", None):
                    macro_overrides = self._macro_daily_closes.copy()

            features = build_live_features(
                ratio_adjusted_df, 
                self.feature_names, 
                lean=self._lean_features, 
                bar_size=stream,
                macro_overrides=macro_overrides
            )
        except StaleDataException as exc:
            if not self._data_mute:
                self._data_mute = True
                self._data_mute_reason = str(exc)
                self._data_mute_since = time.time()
                log.critical(
                    "[SAFETY MUTE] ACTIVATED -- "
                    "Stale FRED data detected, new entries BLOCKED: %s", exc,
                )
                tg_msg = (
                    f"*[!] SAFETY MUTE ACTIVATED*\n"
                    f"Stale FRED data detected -- new entries BLOCKED.\n"
                    f"{_tg_escape(str(exc))}"
                )
                try:
                    self._telegram.send(tg_msg)
                except Exception:
                    pass
            else:
                log.warning(
                    "SAFETY MUTE still active (%.0f min) — stale data persists: %s",
                    (time.time() - self._data_mute_since) / 60, exc,
                )
            # Cannot generate features with stale data — skip this bar
            return
        if features is None:
            log.info("Feature generation skipped (insufficient data or NaN)")
            return

        # WALLET: current_price for execution pricing.
        # Prefer the Hands stream (front-month close) so that the
        # MARKETABLE_LIMIT price, TP/SL offsets, and trailing stop
        # reference the actual execution contract price.  During the
        # ~2-day rollover mismatch window (when our buffer rolls before
        # the continuous contract), this avoids limit orders priced on
        # a different contract month.
        # Falls back to Brain stream (rolling_df close) if front-month
        # bars haven't arrived yet (e.g., first bar after startup).
        if self._front_month_last_close is not None:
            current_price = self._front_month_last_close
        else:
            current_price = float(rolling_df["Close"].iloc[-1])

        # Get per-side ATR for bracket sizing (parity with BacktestEngine).
        # CRITICAL: Bracket ATR must use RAW prices, NOT ratio-adjusted.
        # Using ratio-adjusted ATR would inflate historical volatility by the
        # cumulative rollover ratio multiplier, producing incorrect TP/SL levels.
        # The model's ATR_14 feature is on the ratio-adjusted price basis and
        # must NOT be used as a fallback for bracket sizing.

        def _compute_bracket_atr(period: int) -> float | None:
            """Compute bracket ATR from RAW rolling_df prices."""
            if len(rolling_df) >= period + 1:
                import pandas_ta as _ta  # noqa: F811
                _series = rolling_df.ta.atr(length=period)
                if _series is not None and not _series.empty:
                    _last = _series.iloc[-1]
                    if not np.isnan(_last):
                        return float(_last)
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
        current_position = self.exec_client.get_position(
            symbol=self._execution_symbol,
        )

        pending_cl_entry_qty = 0.0
        try:
            for evt in list(self._open_orders.values()):
                
                
                
                
                if evt.symbol != self._execution_symbol:
                    continue
                order_status = evt.status
                oid = evt.order_id
                
                # Try to extract the raw order object if available
                o = getattr(getattr(evt, "raw_event", None), "order", None)
                
                # Skip tracked TP/SL orders (they are standalone with
                # parentId==0 but are NOT entry orders)
                if oid is not None and (
                    str(oid) in map(str, self._tp_order_ids) or str(oid) == str(self._sl_order_id)
                ):
                    continue
                parent_id = getattr(o, "parentId", 0) if o else 0
                # Only count parent entry orders (parentId==0)
                if parent_id == 0 and order_status in (
                    "Submitted", "PreSubmitted", "PendingSubmit",
                ):
                    qty = float(getattr(o, "totalQuantity", 0) if o else evt.remaining_qty)
                    pending_cl_entry_qty += qty
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
        tp_price_live = None
        sl_price_live = None
        if current_position != 0:
            # Find TP/SL bracket child orders
            # Find TP/SL bracket child orders
            try:
                for evt in list(self._open_orders.values()):
                    if evt.symbol != self._execution_symbol:
                        continue
                    oid = evt.order_id
                    # Extract raw ib_insync order to read limit/stop prices
                    raw_order = getattr(getattr(evt, "raw_event", None), "order", None)
                    if oid is not None and str(oid) in map(str, getattr(self, '_tp_order_ids', [])):
                        lmt = getattr(raw_order, "lmtPrice", 0.0) or 0.0 if raw_order else 0.0
                        if lmt > 0:
                            tp_price_live = lmt
                    elif oid is not None and str(oid) == str(getattr(self, '_sl_order_id', None)):
                        aux = getattr(raw_order, "auxPrice", 0.0) or 0.0 if raw_order else 0.0
                        if aux > 0:
                            sl_price_live = aux
            except Exception:
                log.warning("Bracket order scan failed", exc_info=True)
                
            # Fallback to locally cached prices if open order lookup failed
            if tp_price_live is None and getattr(self, '_tracked_tp_price', None) is not None:
                tp_price_live = self._tracked_tp_price
            if sl_price_live is None and getattr(self, '_tracked_sl_price', None) is not None:
                sl_price_live = self._tracked_sl_price

            tp_str = f"TP={tp_price_live:.2f}" if tp_price_live else "TP=N/A"
            sl_str = f"SL={sl_price_live:.2f}" if sl_price_live else "SL=N/A"
            atr_str = f"ATR={atr_value:.4f}" if atr_value else "ATR=N/A"

            try:
                # Use cached portfolio (sync) via the execution client adapter.
                acct_summary = self.exec_client.get_account_summary(symbol=self._execution_symbol)
                unrealized_pnl = float(acct_summary.get("cl_unrealized_pnl", 0.0))
                avg_cost = float(acct_summary.get("cl_avg_cost", 0.0))
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

        # Safety Mute: block entries when macro data is stale.
        # Inference still runs (for display/telemetry) but no orders.
        if self._data_mute and signal.action in ("BUY", "SELL", "ENTER", "SHORT"):
            mute_mins = (time.time() - self._data_mute_since) / 60
            log.warning(
                "SAFETY MUTE blocking %s signal (muted %.0f min): %s",
                signal.action, mute_mins, self._data_mute_reason,
            )
            signal = TradeSignal(
                action="HOLD",
                probability=signal.probability,
                confidence_pct=signal.confidence_pct,
                signal_label="Hold",
                skip_reason="SAFETY_MUTE_STALE_DATA",
                buy_prob=signal.buy_prob,
                sell_prob=signal.sell_prob,
            )

        # Update Thread-Safe Virtual Ledger (Dual Stream Netting)
        if signal.action in ("BUY", "SELL", "ENTER", "SHORT"):
            direction = 1 if signal.action in ("BUY", "ENTER") else -1
            self._virtual_ledger[stream] = direction * signal.lots
        elif signal.action == "EXIT":
            # Dead path: strategies no longer produce EXIT signals (bracket-only exits).
            # Kept as a safety net to zero the ledger if an EXIT somehow arrives.
            self._virtual_ledger[stream] = 0
            
        net_target_position = sum(self._virtual_ledger.values())
        ledger_log = (
            f"VIRTUAL LEDGER [{stream}]: 5m={self._virtual_ledger['5m']}, 1h={self._virtual_ledger['1h']} "
            f"-> NET TARGET: {net_target_position} (Actual: {current_position})"
        )
        log.info(ledger_log)
        self._last_virtual_ledger_log = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} [INFO] {ledger_log}"
        
        # Output specifically requested formatted position information
        tp_str = f"{tp_price_live:.2f}" if tp_price_live else "N/A"
        sl_str = f"{sl_price_live:.2f}" if sl_price_live else "N/A"
        price_str = f"{current_price:.2f}" if current_price else "N/A"
        log.info(f"Symbol: {self._execution_symbol} | Position: {current_position} | Price: {price_str} | TP: {tp_str} | SL: {sl_str}")

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
        inference_log = (
            f"INFERENCE [{self.strategy.name}] {direction}: buy_prob={buy_prob_str}  sell_prob={sell_prob_str}  "
            f"signal={signal.signal_label}  action={signal.action}{skip_str}"
        )
        log.info(inference_log)
        self._last_inference_log = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} [INFO] {inference_log}"

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
            elif signal.skip_reason == "SAFETY_MUTE_STALE_DATA":
                log.warning(
                    "[SAFETY MUTE] %s signal blocked — stale macro data "
                    "(bar=%s, buy_prob=%.4f, sell_prob=%.4f)",
                    signal.signal_label,
                    bar_time,
                    signal.buy_prob or 0.0,
                    signal.sell_prob or 0.0,
                )
                action_taken = "SKIP_SAFETY_MUTE"
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
        if self._front_month_local_symbol is None:
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
            parent_trade = self.exec_client.place_bracket_order(
                symbol=self._execution_symbol,
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
            local_sym = self._front_month_local_symbol if self._front_month_local_symbol else self._execution_symbol
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
                symbol=self._execution_symbol,
                local_symbol=self._front_month_local_symbol,
                contract_month=self._front_month_str,
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

        After establishing a TCP connection, waits up to
        _DATA_FARM_WAIT_SECONDS for data farm "OK" signals (2104/2106).
        If only "broken" signals (2103/2105) arrive, the attempt is
        treated as failed immediately — there is no point trying to
        resubscribe when the gateway has no upstream data connection
        (e.g., IBKR data subscription lost to another session).
        """
        delay = _RECONNECT_BASE_DELAY
        for attempt in range(1, _RECONNECT_MAX_ATTEMPTS + 1):
            if not self._running:
                return False
            log.info(
                "Reconnect attempt %d/%d (waiting %.0fs)...",
                attempt, _RECONNECT_MAX_ATTEMPTS, delay,
            )
            # Telegram alert on FIRST attempt only
            if attempt == 1:
                try:
                    self._telegram.send(
                        f"*RECONNECT* - Connection lost, "
                        f"attempting recovery (max {_RECONNECT_MAX_ATTEMPTS} attempts)..."
                    )
                except Exception:
                    pass  # Telegram failures must never block reconnection
            # Use _stop_event.wait() instead of time.sleep() so Ctrl+C
            # (which sets _stop_event) interrupts the wait immediately
            # instead of blocking for the full backoff delay.
            if self._stop_event.wait(timeout=delay):
                return False  # shutdown requested during wait
            try:
                # Ensure clean disconnect state
                try:
                    self.data_client.disconnect()
                    self.exec_client.disconnect()
                except Exception:
                    pass
                # Reset data farm health flags before connecting
                self._data_farm_ok = False
                self._data_farm_broken_only = False
                # Reconnect
                self.data_client.connect()
                self.exec_client.connect()
                # Re-register error handler (lost on disconnect).
                # Remove first to prevent stacking — ib_insync events
                # are simple lists and += appends without dedup.
                self._callbacks_registered = False
                self._register_execution_callbacks()
                # Block the async resubscription path (_deferred_resubscribe)
                # during the data farm health check below.  Without this,
                # 2104/2106 codes fire _on_ib_error → _deferred_resubscribe
                # which races with the sync _resubscribe_and_backfill() call
                # at the end of this method, causing double subscriptions.
                self._resubscribe_pending = True

                # ── Data farm health check ─────────────────────────
                # IBKR fires 2103/2105 (broken) and/or 2104/2106 (OK)
                # immediately after connect.  Wait briefly for the
                # asyncio event loop to process these callbacks.
                waited = 0.0
                poll_step = 0.5
                while waited < _DATA_FARM_WAIT_SECONDS:
                    if hasattr(self.data_client, "sleep"):
                        self.data_client.sleep(poll_step)
                    else:
                        time.sleep(poll_step)
                    waited += poll_step
                    if self._data_farm_ok:
                        break  # At least one data farm is healthy

                if self._data_farm_broken_only and not self._data_farm_ok:
                    log.warning(
                        "Reconnect attempt %d: TCP connected but data farms "
                        "still broken (no 2104/2106 received in %.0fs). "
                        "Gateway has no upstream data — will retry.",
                        attempt, _DATA_FARM_WAIT_SECONDS,
                    )
                    # Rate-limited Telegram: only every 3rd attempt to avoid spam
                    if attempt % 3 == 0:
                        try:
                            self._telegram.send(
                                f"*RECONNECT* - Attempt {attempt}/{_RECONNECT_MAX_ATTEMPTS}: "
                                f"Gateway connected but data farms broken (no upstream data)"
                            )
                        except Exception:
                            pass  # Telegram failures must never block reconnection
                    try:
                        self.data_client.disconnect()
                        self.exec_client.disconnect()
                    except Exception:
                        pass
                    delay = min(delay * 2, _RECONNECT_MAX_DELAY)
                    continue  # Skip resubscription — it would fail anyway

                # Resubscribe + backfill gaps
                self._subscriptions_lost = True
                self._resubscribe_and_backfill()
                log.info("Reconnected successfully on attempt %d", attempt)
                try:
                    self._telegram.send(
                        f"*RECONNECTED* - Recovery successful on attempt {attempt}/{_RECONNECT_MAX_ATTEMPTS}"
                    )
                except Exception:
                    pass  # Telegram failures must never block reconnection
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
                if not (self.data_client.is_connected() and self.exec_client.is_connected()):
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
                        try:
                            self._telegram.send(
                                f"*RECONNECT FAILED* - All {_RECONNECT_MAX_ATTEMPTS} attempts exhausted. "
                                f"Triggering full restart {self._restart_count + 1}/{_RESTART_MAX_ATTEMPTS}..."
                            )
                        except Exception:
                            pass  # Telegram failures must never block reconnection
                        self._running = False
                        self._needs_restart = True
                        break
                    # Reconnect succeeded — resume normal polling
                    poll_count = 0
                    continue

                if hasattr(self.data_client, "sleep"):
                    self.data_client.sleep(_POLL_INTERVAL)
                else:
                    time.sleep(_POLL_INTERVAL)
                poll_count += 1

                # Periodic heartbeat (only when idle — no bars arriving)
                if poll_count % _HEARTBEAT_CYCLES == 0:
                    self._log_heartbeat()
                    # Contract rollover check (once per UTC day).
                    # Runs here (between ib.sleep() calls) so that
                    # synchronous IBKR API calls like reqContractDetails
                    # and placeOrder work safely — the asyncio event
                    # loop is idle at this point.
                    try:
                        self._check_contract_rollover()
                    except Exception:
                        log.warning(
                            "Rollover check failed in event loop",
                            exc_info=True,
                        )
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
                            try:
                                self._telegram.send(
                                    f"\U0001f6a8 *RECONNECT FAILED* \u2014 All {_RECONNECT_MAX_ATTEMPTS} attempts exhausted. "
                                    f"Triggering full restart {self._restart_count + 1}/{_RESTART_MAX_ATTEMPTS}\u2026"
                                )
                            except Exception:
                                pass  # Telegram failures must never block reconnection
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
                    try:
                        self._telegram.send(
                            f"*RECONNECT FAILED* - All {_RECONNECT_MAX_ATTEMPTS} attempts exhausted. "
                            f"Triggering full restart {self._restart_count + 1}/{_RESTART_MAX_ATTEMPTS}..."
                        )
                    except Exception:
                        pass  # Telegram failures must never block reconnection
                    # Signal that we need a full restart
                    self._running = False
                    self._needs_restart = True
            except Exception:
                log.exception("Error in event loop iteration")
                if hasattr(self.data_client, "sleep"):
                    self.data_client.sleep(_POLL_INTERVAL)
                else:
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
            unr_pnl, real_pnl = 0.0, 0.0
            pos = 0
            if (self.data_client.is_connected() and self.exec_client.is_connected()):
                acct = self.exec_client.get_account_summary(
                    symbol=self._execution_symbol,
                )
                pos = acct["cl_position"]
                unr_pnl = acct["cl_unrealized_pnl"]
                real_pnl = acct["cl_realized_pnl"]

                # Cache Realized PnL to prevent it from resetting to 0.0 when IBKR drops the position from the feed
                if pos != 0 or real_pnl != 0.0:
                    self._session_realized_pnl = real_pnl
                else:
                    real_pnl = getattr(self, "_session_realized_pnl", 0.0)

            pos_str = f"{pos:g} contracts" if pos != 0 else "FLAT"
            pnl_str = f" | unr_pnl=${unr_pnl:,.2f} | real_pnl=${real_pnl:,.2f}"
        except Exception:
            pos_str = "unknown"
            pnl_str = ""

        subs_status = " | subs_lost=True" if self._subscriptions_lost else ""
        mute_status = (
            f" | DATA_MUTE={int((time.time() - self._data_mute_since) / 60)}min"
            if self._data_mute else ""
        )
        log.info(
            "HEARTBEAT: alive | last_bar=%s | market=%s | position=%s%s | connected=%s%s%s",
            last_bar_str,
            market_status,
            pos_str,
            pnl_str,
            (self.data_client.is_connected() and self.exec_client.is_connected()),
            subs_status,
            mute_status,
        )

        # Naked position guardrail (kill switch)
        self._check_naked_position()

        # Periodic macro data freshness check
        if getattr(self, "_needs_macro", False):
            now = time.time()
            if now - getattr(self, "_last_macro_check_time", 0.0) >= 3600.0:
                self._last_macro_check_time = now
                from src.features.macro_features import MacroFeatureEngine, StaleDataException
                try:
                    MacroFeatureEngine().refresh_if_stale()
                    # If refresh_if_stale succeeded, test if staleness
                    # has resolved by doing a trial feature build.
                    if self._data_mute:
                        try:
                            overrides = getattr(self, "_macro_daily_closes", {})
                            MacroFeatureEngine()._build_fred_features(
                                live_overrides=overrides,
                                live_time=pd.Timestamp.now()
                            )
                            # No exception = data is fresh again
                            self._data_mute = False
                            self._data_mute_reason = ""
                            mute_mins = (time.time() - self._data_mute_since) / 60
                            self._data_mute_since = 0.0
                            log.info(
                                "[SAFETY MUTE] CLEARED -- "
                                "FRED data is fresh again after %.0f min. "
                                "New entries are now permitted.",
                                mute_mins,
                            )
                            tg_msg = (
                                f"*SAFETY MUTE CLEARED*\n"
                                f"FRED data is fresh again after {mute_mins:.0f} min.\n"
                                f"New entries are now permitted."
                            )
                            try:
                                self._telegram.send(tg_msg)
                            except Exception:
                                pass
                        except StaleDataException as e:
                            log.info(
                                "SAFETY MUTE still active — data still stale: %s", e
                            )
                except StaleDataException as e:
                    if not self._data_mute:
                        self._data_mute = True
                        self._data_mute_reason = str(e)
                        self._data_mute_since = time.time()
                        log.critical(
                            "[SAFETY MUTE] ACTIVATED during periodic refresh -- "
                            "Stale FRED data, new entries BLOCKED: %s", e,
                        )
                        tg_msg = (
                            f"*[!] SAFETY MUTE ACTIVATED*\n"
                            f"Stale FRED data detected during periodic refresh.\n"
                            f"{_tg_escape(str(e))}"
                        )
                        try:
                            self._telegram.send(tg_msg)
                        except Exception:
                            pass
                except Exception as e:
                    log.error("Macro refresh failed: %s", e, exc_info=True)

    def _check_stale_bars(self) -> bool:
        """Proactive watchdog — signal reconnect if bars are stale during market hours.

        Returns True if the caller should trigger a reconnect.

        This is the PRIMARY defense against silent subscription death.
        IBKR's keepUpToDate subscriptions can silently stop delivering
        bars without firing any error code (10182, 1100, 1101).  When
        this happens, _subscriptions_lost is never set and the reactive
        resubscription path is never triggered.

        The watchdog is purely time-based: if no bars arrive for
        _STALE_BAR_THRESHOLD_MINUTES while the market is open, force
        a full disconnect + reconnect.  Market-hours gating prevents
        false positives during weekends and daily halts.
        """
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

        subs_flag = "subs_lost=True" if self._subscriptions_lost else "subs_lost=False (silent death)"
        log.warning(
            "STALE BAR WATCHDOG: no bars for %.0f min (%s) "
            "— forcing disconnect + reconnect",
            minutes_stale, subs_flag,
        )
        try:
            self._telegram.send(
                f"*STALE BAR WATCHDOG* - No bars received for {minutes_stale:.0f}m "
                f"during market hours. Forcing reconnect..."
            )
        except Exception:
            pass  # Telegram failures must never block reconnection
        # Mark subscriptions as lost so downstream recovery paths are consistent
        self._subscriptions_lost = True
        # Disconnect first so _reconnect() starts with a clean state.
        try:
            self.data_client.disconnect()
            self.exec_client.disconnect()
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



    def _check_naked_position(self) -> None:
        """Kill switch: detect and flatten naked positions.

        A "naked" position is one where IBKR reports a non-zero position
        but the bot has no tracked SL order (self._sl_order_id is None).
        This indicates a broken state where the position is unprotected.

        When detected:
        1. Cancel all open CL orders (stale TP/SL remnants)
        2. Flatten the book with a market order
        3. Log CRITICAL and send Telegram alert
        4. Reset state to FLAT
        """
        # Only check when we think we have a position
        if self._active_trade_id is None:
            return
        # Skip if we have a pending entry that hasn't filled yet
        if self._pending_entry_order_id is not None:
            return

        if self._sl_order_id is not None:
            return  # SL is tracked — position is protected

        # Verify IBKR actually has a position (avoid false positives)
        try:
            ibkr_pos = self.exec_client.get_position(
                symbol=self._execution_symbol,
            )
        except Exception:
            log.debug("Naked position check: IBKR query failed", exc_info=True)
            return

        if ibkr_pos == 0:
            # IBKR is flat but we think we have a position — OOB close.
            # Let the normal OOB detection in _check_time_barrier handle it.
            return

        # ── NAKED POSITION DETECTED — FLATTEN ──────────────────────
        log.critical(
            "[KILL SWITCH] NAKED POSITION DETECTED: "
            "IBKR position=%d, _sl_order_id=None, "
            "active_trade=%s — FLATTENING BOOK",
            ibkr_pos, self._active_trade_id,
        )

        # 1. Cancel all open CL orders
        try:
            cancelled = self.exec_client.cancel_open_orders(
                symbol=self._execution_symbol,
            )
            log.info(
                "[KILL SWITCH] Cancelled %d open order(s)", cancelled,
            )
        except Exception:
            log.exception("[KILL SWITCH] Failed to cancel open orders")

        # 2. Flatten with market order
        try:
            current_price = 0.0
            if self.rolling_df_5m is not None and len(self.rolling_df_5m) > 0:
                current_price = float(self.rolling_df_5m["Close"].iloc[-1])
            trade = self.exec_client.close_position(
                symbol=self._execution_symbol,
                exit_mode="market",
                current_price=current_price,
            )
            close_order_id = getattr(getattr(trade, "order", None), "orderId", None)
            if close_order_id is not None:
                self._processed_exit_order_ids.add(str(close_order_id))
            log.critical(
                "[KILL SWITCH] Market close order submitted for %d contracts",
                ibkr_pos,
            )
        except Exception:
            log.exception(
                "[KILL SWITCH] FAILED to flatten position — "
                "MANUAL INTERVENTION REQUIRED"
            )

        # 3. Close ledger position
        try:
            self.telemetry.close_position(
                self._active_trade_id,
                reason="NAKED_POSITION_KILL_SWITCH",
                close_time=self._utc_iso_now(),
                bars_held=self._position_bars_held,
            )
        except Exception:
            log.debug("[KILL SWITCH] Failed to close ledger", exc_info=True)

        # 4. Telegram alert
        try:
            self._telegram.send(
                f"[CRITICAL] *NAKED POSITION DETECTED*\n"
                f"IBKR position: `{ibkr_pos}` contracts\n"
                f"SL order: `None` (MISSING)\n"
                f"Trade ID: `{self._active_trade_id}`\n"
                f"Bars held: `{self._position_bars_held}`\n\n"
                f"*ACTION: Flattening book immediately.*\n"
                f"Fix root cause before restarting."
            )
        except Exception:
            pass  # Never let Telegram failure block safety actions

        # 5. Reset state to FLAT
        self._reset_position_state(reason="NAKED_POSITION_KILL_SWITCH")
        self._pending_entry_order_id = None
        self._pending_entry_bar_time = None

    def _on_standard_execution_event(self, event: StandardExecutionEvent) -> None:
        self._open_orders[event.order_id] = event
        
        if event.status == "Filled":
            order_id = event.order_id
            avg_price = event.avg_price
            qty = event.filled_qty
            symbol_str = event.symbol
            # Extract action from raw ib_insync Trade object
            raw_trade = getattr(event, "raw_event", None)
            raw_order = getattr(raw_trade, "order", None)
            action_str = getattr(raw_order, "action", "UNKNOWN")

            # Log EXECUTION_FILL event to tradebook
            event_ts = self._utc_iso_now()
            event_id = self._build_event_id(
                event_type="EXECUTION_FILL",
                event_ts=event_ts,
                order_id=order_id,
            )
            
            # Resolve decision context using either string or int order_id
            order_id_int = None
            try:
                order_id_int = int(order_id)
            except (ValueError, TypeError):
                pass
                
            ctx = self._last_decision_context_by_order_id.get(order_id)
            if ctx is None and order_id_int is not None:
                ctx = self._last_decision_context_by_order_id.get(order_id_int)
            if ctx is None:
                ctx = {}
                
            self.telemetry.log_tradebook_event(
                event_id=event_id,
                event_type="EXECUTION_FILL",
                event_timestamp_utc=event_ts,
                order_id=order_id,
                perm_id=getattr(raw_order, "permId", None),
                parent_order_id=getattr(raw_order, "parentId", None),
                account=getattr(raw_order, "account", None),
                symbol=symbol_str,
                local_symbol=symbol_str,
                signal_id=ctx.get("signal_id"),
                decision_id=ctx.get("decision_id"),
                decision_timestamp_utc=ctx.get("decision_timestamp_utc"),
                contract_month=self._front_month_str,
                action=action_str,
                last_fill_price=avg_price,
                fill_qty=qty,
            )
            
            if hasattr(self, '_processed_exit_order_ids') and order_id in self._processed_exit_order_ids:
                return
            if hasattr(self, '_processed_entry_order_ids') and order_id in self._processed_entry_order_ids:
                return

            # TP/SL order IDs are stored as int but event.order_id is str;
            # compare both representations to avoid type-mismatch misses.
            order_id_int = None
            try:
                order_id_int = int(order_id)
            except (ValueError, TypeError):
                pass

            is_tp_fill = hasattr(self, '_tp_order_ids') and (
                order_id in self._tp_order_ids
                or (order_id_int is not None and order_id_int in self._tp_order_ids)
            )
            is_sl_fill = hasattr(self, '_sl_order_id') and (
                order_id == self._sl_order_id
                or (order_id_int is not None and order_id_int == self._sl_order_id)
            )
            
            if is_tp_fill or is_sl_fill:
                if hasattr(self, '_processed_exit_order_ids'):
                    self._processed_exit_order_ids.add(str(order_id))
                exit_reason = "TP_HIT" if is_tp_fill else "SL_HIT"
                
                # Software OCA: cancel the other resting protective order(s)
                if is_tp_fill:
                    # Cancel the SL
                    if getattr(self, '_sl_order_id', None):
                        try:
                            self.exec_client.cancel_order(str(self._sl_order_id))
                            log.info(f"[OCA] Cancelled SL order {self._sl_order_id} after TP hit")
                        except Exception as e:
                            log.warning(f"[OCA] Failed to cancel SL order {self._sl_order_id}: {e}")
                elif is_sl_fill:
                    # Cancel all TPs
                    for tp_id in getattr(self, '_tp_order_ids', []):
                        if str(tp_id) != str(order_id):
                            try:
                                self.exec_client.cancel_order(str(tp_id))
                                log.info(f"[OCA] Cancelled TP order {tp_id} after SL hit")
                            except Exception as e:
                                log.warning(f"[OCA] Failed to cancel TP order {tp_id}: {e}")
                                
                try:
                    self._telegram.send(
                        f"[CLOSED] *POSITION CLOSED* ({_tg_escape(exit_reason)})\n"
                        f"Price: `{avg_price}`\n"
                        f"Qty: `{int(qty)}`\n"
                        f"Action: `{action_str}`"
                    )
                except Exception:
                    pass
                try:
                    self.telemetry.close_position(
                        trade_id=self._active_trade_id or "unknown", 
                        reason=exit_reason, 
                        close_time=self._utc_iso_now(), 
                        bars_held=self._position_bars_held, 
                        exit_price=avg_price
                    )
                except Exception:
                    pass
                self._reset_position_state(reason=exit_reason)
            else:
                if hasattr(self, '_processed_entry_order_ids'):
                    self._processed_entry_order_ids.add(order_id)
                self._last_filled_entry_order_id = order_id
                trade_id = "trade_" + str(order_id)
                self._active_trade_id = trade_id

                # Resolve side from the raw order action
                if action_str == "BUY":
                    side_str = "LONG"
                elif action_str == "SELL":
                    side_str = "SHORT"
                else:
                    side_str = "LONG" if self._position_side == 1 else (
                        "SHORT" if self._position_side == -1 else "UNKNOWN"
                    )

                try:
                    self.telemetry.open_position(
                        trade_id=trade_id, 
                        side=side_str, 
                        quantity=int(qty), 
                        entry_price=avg_price, 
                        entry_order_id=order_id, 
                        atr_at_entry=self._atr_at_entry, 
                        entry_time=self._utc_iso_now(), 
                        entry_bar_time=self._position_entry_bar_time.isoformat() if self._position_entry_bar_time else None, 
                        trailing_atr_mult=self._trade_trailing_atr_mult, 
                        max_hold_bars=self._trade_max_hold_bars
                    )
                except Exception:
                    pass
                
                try:
                    self._telegram.send(
                        f"🚀 *ENTRY FILLED*\n"
                        f"Side: `{side_str}`\n"
                        f"Price: `{avg_price}`\n"
                        f"Qty: `{int(qty)}`"
                    )
                except Exception:
                    pass

                # Phase 2: Place TP/SL child orders on the actual fill price.
                # The decision context was stored with int keys at order
                # submission; the callback receives str order_id from IBKR.
                # Try both key types for lookup.
                ctx_key = order_id_int if order_id_int is not None else order_id
                if ctx_key not in self._last_decision_context_by_order_id:
                    ctx_key = order_id  # fallback to str
                contract = getattr(raw_trade, "contract", None)
                log.info(
                    "[TRADE] ENTRY FILLED: orderId=%s  action=%s  "
                    "fill=%.2f  qty=%d — placing TP/SL children",
                    order_id, action_str, avg_price, int(qty),
                )
                self._place_bracket_children_on_fill(
                    order_id=ctx_key,
                    fill_price=avg_price,
                    action_str=action_str,
                    qty=qty,
                    contract=contract,
                )

if __name__ == "__main__":
    from src.live_execution.cli import main
    main()
