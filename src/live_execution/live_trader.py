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
from src.live_execution.strategies.buy70_sized_manatee import Buy70SizedManatee
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.data_manager import DataManager
from src.live_execution.ibkr_client import (
    IBKRConnectionManager,
    build_cl_contract,
    ib_bars_to_dataframe,
)
from src.live_execution.telemetry import TelemetryDB
from src.live_execution.utils.telegram_alert import TelegramAlerter


# ---------------------------------------------------------------------------
# Background log capture for heartbeat diagnostics
# ---------------------------------------------------------------------------

class _TelegramLogCapture(_logging.Handler):
    """Thread-safe ring buffer that retains the last N WARNING/ERROR log records."""

    def __init__(self, maxlen: int = 8) -> None:
        super().__init__(level=_logging.WARNING)
        self._records: collections.deque = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: _logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                self._records.append((record.levelname, msg))
        except Exception:
            pass

    def drain(self) -> list:
        """Return and clear all buffered records."""
        with self._lock:
            items = list(self._records)
            self._records.clear()
        return items

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from src.data_paths import get_data_path as _dp_data_path, get_data_root as _dp_data_root

_DEFAULT_DB_PATH = str(_dp_data_path("live_telemetry.db"))

# AlphaFactory windows used during training (set_05/set_06)
_ALPHA_WINDOWS = [864, 2016, 4032, 10080]  # 3d, 7d, 14d, 35d in 5-min bars

# Extended windows for set_07 models
_ALPHA_WINDOWS_SET_07 = [288, 864, 2016, 4032, 10080]  # 1d, 3d, 7d, 14d, 35d
_MACRO_WINDOWS_SET_07 = {
    "1D": 24, "3D": 72, "1W": 168, "2W": 336,
    "1M": 840, "3M": 2160,
}

# Sentinel feature names that indicate set_07 model
_SET_07_SENTINEL_FEATURES = frozenset([
    "DIST_SKEW_288", "Time_DayOfWeek_Sin", "MOM_STOCH_K_864",
])

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

# Suppress non-CL noise from ib_insync internal logging (callbacks originate in wrapper)
logging.getLogger("ib_insync.wrapper").addFilter(CLOnlyLogFilter())


def _sigmoid(x: float) -> float:
    """Apply sigmoid to convert logit to probability."""
    return 1.0 / (1.0 + np.exp(-x))


# ---------------------------------------------------------------------------
# Feature Pipeline (replicates process_set_05/set_06 for live data)
# ---------------------------------------------------------------------------

def build_live_features(
    df: pd.DataFrame,
    feature_names: list[str],
    *,
    lean: bool = False,
    bar_size: str = "5m",
) -> Optional[pd.DataFrame]:
    """
    Generate features from a rolling OHLCV DataFrame for live inference.

    Replicates the training pipeline (process_set_05/set_06 or set_07):
    1. Add Time_Sin, Time_Cos from the DateTime index
    2. Run AlphaFactory.add_all_features(windows=_ALPHA_WINDOWS)
    3. Add Volume_Log
    4. Select the exact columns the model expects

    Automatically detects set_07 models by checking for sentinel feature
    names and switches to the extended pipeline with 288-bar window,
    expanded macro windows, and additional feature clusters.

    Args:
        df: Rolling OHLCV DataFrame with DateTime index and columns
            [Open, High, Low, Close, Volume].
        feature_names: The exact list of feature column names the model expects.
        lean: Whether to generate only momentum + time features (faster).
        bar_size: The timeframe of the input df ("5m" or "1h").

    Returns:
        Single-row DataFrame with the model's expected features,
        or None if features cannot be computed (e.g. NaN in required columns).
    """
    if bar_size == "4h":
        # 4h bars: windows are in 4h-bar units (6=1d, 18=3d, 42=7d, 84=14d, 210=35d)
        is_set_07 = True
        alpha_windows = [6, 18, 42, 84, 210]
        macro_windows = {"1D": 24, "3D": 72, "1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}
    elif bar_size == "2h":
        # 2h bars: windows are in 2h-bar units (12=1d, 36=3d, 84=7d, 168=14d, 420=35d)
        is_set_07 = True
        alpha_windows = [12, 36, 84, 168, 420]
        macro_windows = {"1D": 24, "3D": 72, "1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}
    elif bar_size == "1h":
        is_set_07 = True
        alpha_windows = [24, 72, 168, 336, 840]
        macro_windows = {"1D": 24, "3D": 72, "1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}
    else:
        # Auto-detect set_07 pipeline (ignored if lean=True)
        is_set_07 = not lean and bool(_SET_07_SENTINEL_FEATURES & set(feature_names))
        alpha_windows = _ALPHA_WINDOWS_SET_07 if is_set_07 or lean else _ALPHA_WINDOWS
        macro_windows = _MACRO_WINDOWS_SET_07

    if len(df) < alpha_windows[-1]:
        log.warning(
            "Not enough bars for feature generation: %d < %d",
            len(df), alpha_windows[-1],
        )
        return None

    # Warn if cache depth is below recommended minimum for long-window
    # features (MACRO_3M needs 2160h × 12 = 25,920 5m-bars + warmup).
    # Scale by bar_size: 26,000 for 5m, ~2,167 for 1h.
    _MIN_RECOMMENDED_BARS_5M = 26_000
    _bar_divisor = 12 if bar_size == "1h" else 1
    _min_recommended = _MIN_RECOMMENDED_BARS_5M // _bar_divisor
    if len(df) < _min_recommended:
        log.warning(
            "Cache depth %d below recommended %d — "
            "long-window features (MACRO_3M, VOL_ROC_10080) may be "
            "unreliable due to insufficient warmup history",
            len(df), _min_recommended,
        )

    # Work on a copy to avoid mutating the rolling window
    work = df.copy()

    # 1. Add cyclical time features
    minutes = work.index.hour * 60 + work.index.minute
    work["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    work["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)

    # 1b. Day-of-week encoding
    if is_set_07 or "Time_DayOfWeek_Sin" in feature_names:
        day_of_week = work.index.dayofweek
        work["Time_DayOfWeek_Sin"] = np.sin(2 * np.pi * day_of_week / 5)
        work["Time_DayOfWeek_Cos"] = np.cos(2 * np.pi * day_of_week / 5)

    # 2. Run AlphaFactory
    factory = AlphaFactory(work)
    if lean:
        # Lean path: momentum + time features only (no macro, no extended)
        # This is 6-7x faster than the full pipeline
        work = factory.add_all_features(
            windows=alpha_windows,
            include_momentum=True,
            include_macro=False,
            include_extended=False,
        )
    elif is_set_07:
        work = factory.add_all_features(
            windows=alpha_windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            macro_windows=macro_windows,
        )
    else:
        work = factory.add_all_features(
            windows=alpha_windows,
            include_momentum=True,
            include_macro=True,
        )

    # 2b. Add STOCH specifically if lean but the strategy requests it
    # (STOCH is usually part of include_extended=True, but lean turns extended off)
    if lean and any(f.startswith("MOM_STOCH_") for f in feature_names):
        for window in alpha_windows:
            factory.add_stochastic_cluster(window=window)
        work = factory.df

    # 2c. Merge external macro data (FRED + COT) if model expects it
    _has_external_macro = any(
        f.startswith(("MACRO_VIX", "MACRO_OVX", "MACRO_DXY",
                      "MACRO_YIELD_CURVE", "MACRO_FED_FUNDS", "COT_"))
        for f in feature_names
    )
    if _has_external_macro:
        try:
            work = MacroFeatureEngine().merge_all(work)
        except Exception as exc:
            log.error("CRITICAL: Error merging macro features: %s", exc, exc_info=True)
            raise RuntimeError(f"CRITICAL: Macro Feature Engine failed. Cannot generate live features. Reason: {exc}") from exc

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
    # Snapshot which cells in the last row are NaN BEFORE fill — these are
    # the features that lack sufficient warmup history.
    _pre_fill_nan = work[feature_names].iloc[-1].isna() if set(feature_names).issubset(work.columns) else None
    work.ffill(inplace=True)
    work.bfill(inplace=True)
    work.fillna(0, inplace=True)

    # Detect which features were zero-filled from NaN (cold-start warning)
    if _pre_fill_nan is not None:
        _post_fill_zero = work[feature_names].iloc[-1] == 0
        _zero_filled = _pre_fill_nan & _post_fill_zero
        if _zero_filled.any():
            _zero_cols = _zero_filled[_zero_filled].index.tolist()
            log.warning(
                "COLD START: %d features zero-filled from NaN "
                "(model never saw 0 during training): %s",
                len(_zero_cols), _zero_cols,
            )

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
        # Consecutive signal threshold: require N consecutive above-threshold
        # signals before executing a trade (0 = disabled, immediate entry).
        self._consecutive_signal_threshold: int = int(
            strategy_config.get("consecutive_signal_threshold", 0)
        )
        self._consecutive_buy_count: int = 0
        self._consecutive_sell_count: int = 0
        self._consecutive_exit_count: int = 0
        # Trailing stop config (parity with backtest engine)
        self._trailing_atr_mult: float = float(
            strategy_config.get("trailing_atr_mult", 100.0)
        )
        self._trailing_sl_atr_offset: float = float(
            strategy_config.get("trailing_sl_atr_offset", 0.25)
        )
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
        log.info("Strategy: %s  direction=%s", strategy.name, strategy.direction)

        # Read execution_symbol from strategy config (Brain=CL, Hands=CL or MCL)
        self._execution_symbol: str = strategy_config.get(
            "execution_symbol", "CL"
        ).upper()
        # Whether to use the lean (momentum-only) feature path
        self._lean_features: bool = bool(
            strategy_config.get("lean_features", False)
        )
        log.info(
            "Entry mode: %s  adaptive_priority=%s  max_hold_bars=%d  "
            "tp_cooldown=%d  sl_cooldown=%d  trailing_atr_mult=%.2f  "
            "trailing_sl_offset=%.2f  exit_mode=%s  max_position=%d  "
            "execution_symbol=%s  lean_features=%s",
            entry_mode, adaptive_priority, self._max_hold_bars,
            self._tp_cooldown_bars, self._sl_cooldown_bars,
            self._trailing_atr_mult,
            self._trailing_sl_atr_offset, self._exit_mode,
            self._max_position_size,
            self._execution_symbol, self._lean_features,
        )

        # Extract designated primary stream from config (e.g. "1h" or "5m")
        self._bar_size: str = strategy_config.get("bar_size", "5m").lower()

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
            # Seed from the full historical 1H parquet (cl-1h_bk_HourSet_02.parquet)
            # which lives alongside the processed datasets in CL_DATA_ROOT/data/processed/.
            from src.data_paths import get_data_root as _get_data_root
            _data_root = _get_data_root()
            cache_path_1h = str(_data_root / "processed" / "warm_start_cache_1h.parquet")
            seed_path_1h = str(_data_root / "processed" / "cl-1h_bk_HourSet_02.parquet")
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
        self._tp_order_id: Optional[int] = None
        self._sl_order_id: Optional[int] = None
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
        try:
            current_position = self.manager.get_cl_position(symbol=self._execution_symbol)
        except Exception:
            pass

        unrealized_pnl = 0.0
        realized_pnl = 0.0
        try:
            for item in self.manager.ib.portfolio():
                if item.contract.symbol == "CL":
                    unrealized_pnl = float(item.unrealizedPNL)
                    realized_pnl = float(item.realizedPNL)
                    break
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
            for level, msg in recent_errors[-5:]:
                icon = "🚨" if level == "ERROR" else "⚠️"
                lines.append(f"{icon} `{msg[:180]}`")
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
            _needs_macro = any(
                f.startswith(("MACRO_VIX", "MACRO_OVX", "MACRO_DXY",
                              "MACRO_YIELD_CURVE", "MACRO_FED_FUNDS", "COT_"))
                for f in self.feature_names
            )
            if _needs_macro:
                log.info("Model uses external macro features — checking freshness...")
                try:
                    MacroFeatureEngine().refresh_if_stale()
                except Exception as exc:
                    log.warning("Macro data refresh failed: %s (will use existing data)", exc)

            # Step 8: Warm-start via DataManager
            self._warm_start()

            # Step 7b: Recover any inherited position from the ledger
            self._recover_inherited_position()

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
        log.info("Received signal %d — shutting down gracefully...", signum)
        self._running = False

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
        self._tp_order_id = None
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
                )
            except Exception:
                log.debug("Failed to close ledger position", exc_info=True)
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
                    self._position_bars_held = max(
                        0, int(delta_minutes / 5)
                    )
                    log.info(
                        "[RECOVERY] Estimated %d bars held since entry at %s",
                        self._position_bars_held,
                        self._position_entry_bar_time,
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
            self._tp_order_id = tp_order_id
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
                # Detect TP/SL fill by tracked order IDs (no parentId linkage)
                is_tp_fill = (order_id is not None and order_id in self._tp_order_ids)
                is_sl_fill = (order_id is not None and order_id == self._sl_order_id)
                if is_tp_fill or is_sl_fill:
                    # Exit order filled — log and apply software-side OCA
                    exit_type = "TP HIT" if is_tp_fill else "SL HIT"
                    exit_icon = "🟢" if is_tp_fill else "🔴"
                    log.info(
                        "[TRADE] EXIT: %s %.0f %s @ %.2f (%s)",
                        action_str, qty, symbol_str, avg_price, exit_type,
                    )
                    # ── Telegram: trade exit alert ────────────────────
                    self._telegram.send(
                        f"{exit_icon} *Trade Exit — {exit_type}*\n"
                        f"{action_str} {qty:.0f} `{symbol_str}` @ `{avg_price:.2f}`",
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
                            close_reason = "TP_HIT" if is_tp_fill else "SL_HIT"
                            try:
                                self.telemetry.close_position(
                                    self._active_trade_id,
                                    reason=close_reason,
                                    close_time=self._utc_iso_now(),
                                    bars_held=self._position_bars_held,
                                )
                            except Exception:
                                log.debug(
                                    "Failed to close ledger position",
                                    exc_info=True,
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
                        self._reset_position_state()
                else:
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
            self._last_bar_time_1h = self.rolling_df_1h.index[-1]
            log.info(
                "1h rolling window initialized: %d bars, latest=%s",
                len(self.rolling_df_1h), self._last_bar_time_1h,
            )

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

    def _on_bar_update_5m(self, bars, has_new_bar) -> None:
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

    def _on_bar_update_1h(self, bars, has_new_bar) -> None:
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

    def _on_new_bar(self, bar_time: pd.Timestamp, rolling_df: pd.DataFrame, stream: str) -> None:
        """Run feature generation, strategy evaluation, update ledger, and net execution."""
        # 0a. Entry order TTL: cancel stale entry orders after 1 bar
        self._check_entry_order_ttl(bar_time)

        # 0b. Track cooldown state
        in_cooldown = False
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            in_cooldown = True

        # 1. Generate features (always — needed for INFERENCE display)
        # Use stream for bar_size to inform feature engineering scale
        features = build_live_features(
            rolling_df, self.feature_names, lean=self._lean_features, bar_size=stream
        )
        if features is None:
            log.info("Feature generation skipped (insufficient data or NaN)")
            return

        current_price = float(rolling_df["Close"].iloc[-1])

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
        current_position = self.manager.get_cl_position(
            symbol=self._execution_symbol,
        )

        # Engine-level hard position cap (defense-in-depth)
        if abs(current_position) >= self._max_position_size:
            if abs(current_position) > self._max_position_size:
                log.warning(
                    "POSITION CAP BREACH: abs(position)=%d > max=%d — "
                    "blocking ALL new entries",
                    abs(current_position), self._max_position_size,
                )
            # Already at or above max — don't even run strategy eval
            # for new entries (still log PNL and run trailing stop)
            pass  # will be blocked by strategy's position guard

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
                oid = getattr(o, "orderId", None)
                # Skip tracked TP/SL orders (they are standalone with
                # parentId==0 but are NOT entry orders)
                if oid is not None and (
                    oid == self._tp_order_id or oid == self._sl_order_id
                ):
                    continue
                parent_id = getattr(o, "parentId", 0) or 0
                # Only count parent entry orders (parentId==0)
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
                    oid = getattr(o, "orderId", None)
                    if oid is not None and oid == self._tp_order_id:
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
        )
        self._last_inference_time_sec = time.perf_counter() - t0
        self._last_inference_bar_time = bar_time  # track last successful inference

        # Update Thread-Safe Virtual Ledger (Dual Stream Netting)
        if signal.action == "ENTER":
            self._virtual_ledger[stream] = signal.direction * signal.lots
        elif signal.action == "EXIT":
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
            # No active signal — reset consecutive counters
            if self._consecutive_signal_threshold > 0:
                self._consecutive_buy_count = 0
                self._consecutive_sell_count = 0
                self._consecutive_exit_count = 0
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

        # 4b. Consecutive signal filter (parity with backtest engine)
        if self._consecutive_signal_threshold > 0:
            if signal.action in ("BUY", "ENTER"):
                self._consecutive_buy_count += 1
                self._consecutive_sell_count = 0
                self._consecutive_exit_count = 0
                if self._consecutive_buy_count < self._consecutive_signal_threshold:
                    log.info(
                        "CONSECUTIVE FILTER: BUY signal %d/%d — waiting for more",
                        self._consecutive_buy_count,
                        self._consecutive_signal_threshold,
                    )
                    self.telemetry.log_signal(
                        timestamp=bar_time,
                        signal=signal.signal_label,
                        confidence_pct=signal.confidence_pct,
                        action_taken="CONSECUTIVE_WAIT",
                        current_price=current_price,
                        atr_value=atr_value,
                    )
                    return
                # Threshold met — reset counter and proceed to execute
                log.info(
                    "CONSECUTIVE FILTER: BUY threshold met (%d/%d) — executing",
                    self._consecutive_buy_count,
                    self._consecutive_signal_threshold,
                )
                self._consecutive_buy_count = 0
            elif signal.action in ("SELL", "SHORT"):
                self._consecutive_sell_count += 1
                self._consecutive_buy_count = 0
                self._consecutive_exit_count = 0
                if self._consecutive_sell_count < self._consecutive_signal_threshold:
                    log.info(
                        "CONSECUTIVE FILTER: SELL signal %d/%d — waiting for more",
                        self._consecutive_sell_count,
                        self._consecutive_signal_threshold,
                    )
                    self.telemetry.log_signal(
                        timestamp=bar_time,
                        signal=signal.signal_label,
                        confidence_pct=signal.confidence_pct,
                        action_taken="CONSECUTIVE_WAIT",
                        current_price=current_price,
                        atr_value=atr_value,
                    )
                    return
                log.info(
                    "CONSECUTIVE FILTER: SELL threshold met (%d/%d) — executing",
                    self._consecutive_sell_count,
                    self._consecutive_signal_threshold,
                )
                self._consecutive_sell_count = 0
            elif signal.action == "EXIT":
                self._consecutive_exit_count += 1
                self._consecutive_buy_count = 0
                self._consecutive_sell_count = 0
                if self._consecutive_exit_count < self._consecutive_signal_threshold:
                    log.info(
                        "CONSECUTIVE FILTER: EXIT netting signal %d/%d — waiting for more",
                        self._consecutive_exit_count,
                        self._consecutive_signal_threshold,
                    )
                    self.telemetry.log_signal(
                        timestamp=bar_time,
                        signal=signal.signal_label,
                        confidence_pct=signal.confidence_pct,
                        action_taken="CONSECUTIVE_WAIT",
                        current_price=current_price,
                        atr_value=atr_value,
                    )
                    return
                log.info(
                    "CONSECUTIVE FILTER: EXIT threshold met (%d/%d) — executing",
                    self._consecutive_exit_count,
                    self._consecutive_signal_threshold,
                )
                self._consecutive_exit_count = 0

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
        # Compute TP/SL offsets (ATR * mult) as dollar amounts.
        # These are stored in the decision context so the fill callback
        # can compute bracket prices from any fill price.
        tp_offset = abs(signal.tp_price - current_price)
        sl_offset = abs(signal.sl_price - current_price)

        try:
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
            self._atr_at_entry = atr_value
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

        # Heartbeat: log status every ~5 minutes (60 cycles × 5s) when
        # no new bars arrive, so the user knows the trader is alive.
        _HEARTBEAT_CYCLES = 60  # 60 × 5s = 300s = 5 minutes
        poll_count = 0

        while self._running:
            try:
                self.manager.ib.sleep(_POLL_INTERVAL)
                poll_count += 1

                # Periodic heartbeat (only when idle — no bars arriving)
                if poll_count % _HEARTBEAT_CYCLES == 0:
                    self._log_heartbeat()
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

        log.info(
            "HEARTBEAT: alive | last_bar=%s | market=%s | position=%s%s | connected=%s",
            last_bar_str,
            market_status,
            pos_str,
            pnl_str,
            self.manager.ib.isConnected() if getattr(self.manager, "ib", None) else False,
        )

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
# Legacy cache migration
# ---------------------------------------------------------------------------

def _merge_legacy_cid_caches(shared_cache_path: str) -> None:
    """Merge any per-client_id warm-start caches into the shared cache.

    Prior versions created separate caches per client_id
    (warm_start_cache_cid18.parquet, etc.). This function detects those
    files, merges new bars into the shared cache, and renames the old
    files so they aren't re-processed.
    """
    import glob as _glob

    shared = Path(shared_cache_path)
    cache_dir = shared.parent
    pattern = str(cache_dir / "warm_start_cache_cid*.parquet")
    legacy_files = sorted(_glob.glob(pattern))

    if not legacy_files:
        return

    log.info(
        "Found %d legacy per-client cache(s) — merging into shared cache",
        len(legacy_files),
    )

    # Load the shared cache if it exists
    if shared.exists():
        shared_df = pd.read_parquet(shared, engine="pyarrow")
        if "DateTime" in shared_df.columns:
            shared_df["DateTime"] = pd.to_datetime(shared_df["DateTime"])
    else:
        shared_df = pd.DataFrame(
            columns=["DateTime", "Open", "High", "Low", "Close", "Volume"]
        )

    # Merge bars from each legacy file
    merged_count = 0
    for legacy_path in legacy_files:
        try:
            ldf = pd.read_parquet(legacy_path, engine="pyarrow")
            if "DateTime" in ldf.columns:
                ldf["DateTime"] = pd.to_datetime(ldf["DateTime"])
            before = len(shared_df)
            shared_df = pd.concat([shared_df, ldf], ignore_index=True)
            shared_df = shared_df.drop_duplicates(subset="DateTime", keep="last")
            new_bars = len(shared_df) - before
            merged_count += new_bars
            log.info(
                "  Merged %s: %d bars (%d new)",
                Path(legacy_path).name, len(ldf), new_bars,
            )
            # Rename legacy file so it isn't re-processed
            backup = Path(legacy_path).with_suffix(".parquet.migrated")
            Path(legacy_path).rename(backup)
            log.info("  Renamed -> %s", backup.name)
        except Exception as e:
            log.warning("  Failed to merge %s: %s", legacy_path, e)

    if merged_count > 0:
        shared_df = shared_df.sort_values("DateTime").reset_index(drop=True)
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared_df.to_parquet(shared, engine="pyarrow", index=False)
        log.info(
            "Shared cache updated: %d total bars (%d new from migration)",
            len(shared_df), merged_count,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    available = ", ".join(sorted(_STRATEGY_REGISTRY.keys()))
    default_host = os.environ.get("IBKR_HOST", "127.0.0.1")
    default_port = int(os.environ.get("IBKR_PORT", "4002"))
    parser = argparse.ArgumentParser(
        description="CL Analyst — Live Execution Engine"
    )
    parser.add_argument(
        "--host", default=default_host,
        help="IBKR TWS/Gateway host (default: IBKR_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=default_port,
        help=(
            "IBKR primary port (default: IBKR_PORT or 4002 for IB Gateway; "
            "falls back to 7497 TWS)"
        ),
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
    # Telemetry DB is per-client (contains strategy-specific signals,
    # predictions, trades). OHLCV warm-start cache is SHARED — all
    # strategies receive the same CL continuous bars.
    resolved_db_path = args.db_path
    resolved_cache_path = args.cache_path  # always shared (no cid suffix)

    if resolved_client_id != 1:
        cid_suffix = f"_cid{resolved_client_id}"

        # Only override DB path if user hasn't explicitly set a custom path
        if resolved_db_path == _DEFAULT_DB_PATH:
            resolved_db_path = str(
                _dp_data_root() / f"live_telemetry{cid_suffix}.db"
            )

        # Merge any existing per-client caches into the shared cache
        # so no historical bars are lost from prior per-cid runs.
        _merge_legacy_cid_caches(resolved_cache_path)

        log.info(
            "Multi-instance isolation: client_id=%d  "
            "db=%s  cache=%s (shared)",
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
