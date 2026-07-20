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
import math
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Load .env file (CL_DATA_ROOT, etc.) before reading env-based constants
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Project imports
from src.core.instrument_master import round_to_tick
from src.features.alpha_factory import AlphaFactory
from src.features.macro_features import (
    MacroFeatureEngine,
    StaleDataException,
    has_external_macro_features,
    validate_external_macro_features,
)
from src.live_execution.strategy import Strategy, TradeSignal

from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.instrument_context import resolve_instrument_context
from src.live_execution.data_manager import (
    REQUIRED_1H_BARS,
    ROLL_SEAM_RESOLVED,
    DataManager,
    derive_data_paths,
)
# T5: session calendars (leaf module — stdlib + pytz + instrument_master).
# Aliased so the local `market_status` variables in the heartbeat/watchdog
# never shadow the imported callable.
from src.live_execution.session_calendar import (
    market_status as _calendar_market_status,
    session_open_anchor as _session_open_anchor,
)
# A-5 activity grace shared with the :06 fleet monitor (fleet_health is a
# stdlib-only leaf module — no import cycle).
from src.live_execution.fleet_health import FILL_PRICE_GRACE_MINUTES
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

from src.data_paths import get_data_path as _dp_data_path

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

# Console heartbeat: wall-clock anchored — each child fires when the SHARED
# system clock crosses (t - offset) % interval == 0, so a fleet of children
# reports at fixed spacing in a stable order with zero runtime coordination
# (the fleet runner assigns each child's phase via --heartbeat-offset).
# Replaces the poll-count gate (60 x 5s cycles) whose phase was an accident
# of startup duration, drifted with per-cycle work, and re-phased on every
# reconnect (poll_count reset).
_HEARTBEAT_INTERVAL = 300.0
# Floor for the deadline-shortened sleep: never busy-spin, but small enough
# that firing jitter stays well under the 5s inter-child spacing.
_HEARTBEAT_MIN_SLEEP = 0.05


def _initial_heartbeat_deadline(now: float, offset: float,
                                interval: float = _HEARTBEAT_INTERVAL) -> float:
    """First tick strictly after `now` on the (offset mod interval) grid."""
    return (math.floor((now - offset) / interval) + 1) * interval + offset


def _advance_heartbeat_deadline(deadline: float, now: float,
                                interval: float = _HEARTBEAT_INTERVAL) -> float:
    """Next on-grid tick strictly after `now`. A late fire (backfill or
    reconnect stall) SKIPS the missed ticks instead of bursting, so the
    child rejoins the fleet rotation at its own slot."""
    missed = math.ceil((now - deadline) / interval + 1e-9)
    return deadline + interval * max(1, missed)


def _heartbeat_sleep(now: float, deadline: float,
                     poll_interval: float = _POLL_INTERVAL) -> float:
    """Poll sleep, shortened so the loop wakes AT the deadline instead of
    up to a full poll late — firing jitter must stay far below the 5s
    spacing between children for the rotation order to hold."""
    return min(poll_interval, max(_HEARTBEAT_MIN_SLEEP, deadline - now))

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
# 15 -> 30 per the explicit 2026-07-06 user directive
# (watchdog-telegram-throttle_07062026_0007): thin holiday Globex sessions
# made the 15-min watchdog cycle spam recovery machinery. Accepted
# trade-off: the blind window before recovery starts doubles — bracket
# TP/SL orders rest server-side on IBKR, so open positions stay protected.
_STALE_BAR_THRESHOLD_MINUTES = 30  # Minutes without a bar before forcing reconnect
# T7: hourly-only instances (enable_5m_stream=false) watch the 1h stream
# instead. 1h bars are open-time stamped and delivered at T+60, so normal
# staleness oscillates 60→120 min — 135 = 120 max normal + the legacy
# 15-min margin (design-time value; the 5m constant above is now 30).
_STALE_BAR_THRESHOLD_MINUTES_1H = 135
# Consecutive stale-bar-watchdog firings with NO new brain-stream bar in
# between before escalating to a process exit (SystemExit).  fleet_runner
# only restarts CRASHED children — a child churning through fruitless
# reconnect cycles ("Reconnected successfully" but zero bars) looks healthy
# to it, so the child must crash itself to get a fresh start (the startup
# subscription path demonstrably works).
_MAX_FRUITLESS_RECONNECTS = 3

# Deferred-resubscription retry timer
# (resubscribe-retry-blindness_07062026_0640): the 2026-07-06 incident —
# an IBKR *website* login invalidated the Gateway data session; farm-OK
# (2106) fired while the conflict still held, every child's resubscribe
# failed with error-162, and the old "will retry on next reconnect" path
# never fired again (no new farm-OK ever came). Children sat alive-but-
# blind until a manual restart. Retry on an event-loop timer instead:
# 60s, 120s, 240s, then capped at 300s, for at most 5 attempts — beyond
# that the stale-bar watchdog (30 min / SystemExit) is the backstop.
_RESUBSCRIBE_RETRY_BASE_SECONDS = 60
_RESUBSCRIBE_RETRY_CAP_SECONDS = 300
_MAX_RESUBSCRIBE_RETRIES = 5

# TIME BARRIER exit confirmation (exit-fill-unverified_07152026_1855): the
# confirmed-flat gate defers an UNFILLED exit to the next bar rather than
# booking a fabricated price and disarming both safety nets. Bound the
# cross-bar retries by ATTEMPTS (never a sleep — a sleep blocks the live event
# loop); on exhaustion escalate LOUD and keep the position TRACKED so
# housekeeping's HEAL branch (not the detect-only UNTRACKED branch) owns it.
# The 5-minute kill switch, armed for free by the deferral, is the real net —
# this ceiling only bounds how long we retry the exit quietly before shouting.
_MAX_TIME_BARRIER_EXIT_ATTEMPTS = 6

# Hourly order housekeeping (hourly-order-housekeeping_07072026_0435):
# in-child broker-vs-ledger sweep at ~:15 wall clock — after the :00
# signal bar and the :06 read-only fleet monitor, so each hour composes
# detect (:06) -> clean (:15) -> verify (next :06).
_HOUSEKEEPING_MINUTE = 15
# A-3: a sweep of local-cache reads should take well under a second;
# anything slower is flagged (never aborted — the work is already done).
_HOUSEKEEPING_BUDGET_SECONDS = 10.0
# A-1(b): the ONLY close reasons whose exit_price/close_reason may be
# overwritten by a proven broker fill. TP_HIT/SL_HIT rows carry real
# prices and are NEVER touched.
_HOUSEKEEPING_OVERWRITE_REASONS = ("CLOSED_OOB", "CLOSED_OOB_UNRECOVERED")


def _price_decimals(tick_size) -> int:
    """Decimal places needed to render one tick of an instrument.

    0.001 (NG) -> 3, 0.01 (CL) -> 2, 0.1 (MGC) -> 1, 0.25 (ES) -> 2,
    1 -> 0. A hardcoded %.2f hides half a tick on NG — $50 per contract
    (telemetry-fill-commission_07062026_0640 R3).
    """
    from decimal import Decimal
    exponent = Decimal(str(tick_size)).normalize().as_tuple().exponent
    return max(0, -exponent)

# Watchdog-family Telegram throttle
# (watchdog-telegram-throttle_07062026_0007, user directive #2): at most
# ONE watchdog-family Telegram per instance per hour — STALE BAR WATCHDOG,
# WATCHDOG ESCALATION, *RECONNECT* (first attempt + farms-broken) and
# *RECONNECTED* route through LiveTrader._send_watchdog_telegram.
# Log lines are NEVER throttled (directive #3). Patchable seam: tests set
# this to 0 to disable the throttle (suppression uses strict <).
_WATCHDOG_TG_COOLDOWN_SECONDS = 3600

# Auto-restart parameters (process-level recovery)
_RESTART_MAX_ATTEMPTS = 5        # Max full restart attempts
_RESTART_DELAY = 300.0           # Delay between restart attempts (5 minutes)

# Pending roll-seam capture (jit-roll-ratio-empty_07102026_1453 Stage 2):
# a pending roll unresolved past this deadline escalates LOUDLY
# (log.critical + Telegram) on every retry attempt — never a silent skip.
_PENDING_ROLL_ESCALATION_DEADLINE = timedelta(days=3)
# Sentinel for the lazily-initialized retry gate (None is a legitimate
# DataManager.last_timestamp value on an empty cache, so it cannot mark
# "never attempted").
_PENDING_ROLL_GATE_UNSET = object()

# DataManager default paths are derived per symbol via
# data_manager.derive_data_paths(ctx.brain_symbol) — T2 removed the
# module-level CL constants (_DEFAULT_SEED_PATH/_DEFAULT_CACHE_PATH).

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
        seed_path: Optional[str] = None,
        cache_path: Optional[str] = None,
        quantity: int = _DEFAULT_QUANTITY,
        dry_run: bool = False,
        entry_mode: str = "adaptive",
        adaptive_priority: str = "Normal",
        exit_mode: str = "market",
        client_id: Optional[int] = None,
        heartbeat_offset: float = 0.0,
    ) -> None:
        self.data_client = data_client
        self.exec_client = exec_client
        self.quantity = quantity
        self.dry_run = dry_run
        self.entry_mode = entry_mode
        self.adaptive_priority = adaptive_priority
        self.exit_mode = exit_mode
        # Console-heartbeat phase on the shared wall clock (seconds). The
        # fleet runner passes 5s * manifest index so children report in a
        # fixed rotation; 0.0 = standalone run (no rotation to join).
        self._heartbeat_offset = float(heartbeat_offset)
        self._open_orders = {}

        # Strategy (owns model, config, threshold, sizing, bracket math)
        self.strategy = strategy
        self.feature_names: list[str] = strategy.feature_names
        # T4: helper-based external-macro detection (instrument-independent,
        # so it can run before context resolution). Extensionally identical
        # to the legacy 6-prefix rule for CL/ES feature sets, adds exactly
        # MACRO_GVZ_* for GC, and keeps AlphaFactory-internal
        # MACRO_WIDTH_*/MACRO_POS_* excluded (D1).
        self._needs_macro: bool = has_external_macro_features(
            self.feature_names
        )
        self._last_macro_check_time: float = 0.0
        self._macro_daily_closes: dict[str, float] = {}

        # Read max_hold_bars from strategy config (keeps backtest & live in sync)
        strategy_config = getattr(strategy, "config", {})

        # Parse config through centralized StrategyConfig dataclass
        # to ensure parity with BacktestEngine.from_config()
        from src.live_execution.strategy_config import StrategyConfig
        _sc = StrategyConfig.from_dict(strategy_config)

        # BACKTEST-ONLY GUARD (ticket trailing-stop-ladder_07132026_1745):
        # the live trailing path implements only the single one-shot rung.
        # Refuse to start on a multi-rung ladder config until the live
        # implementation + /validate-parity land (blueprint Phase 3).
        for _lside in ("long", "short"):
            _ladder = getattr(_sc, _lside).trailing_ladder
            if _ladder is not None and len(_ladder) > 1:
                raise RuntimeError(
                    f"Config has a multi-rung {_lside}.trailing_ladder "
                    f"({len(_ladder)} rungs) but live execution only supports "
                    f"the single legacy trailing rung — trailing_ladder is "
                    f"backtest-only until the Phase-3 live implementation ships."
                )

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

        # Resolve + validate the instrument (raises on missing/unknown/
        # mismatched symbol — no silent CL default). Stored for T2-T5 to
        # consume; T1 wires nothing else through it.
        self._instrument_context = resolve_instrument_context(strategy_config)
        self._execution_symbol: str = self._instrument_context.execution_symbol
        # T4 (D3): the model's feature_names are the ultimate contract —
        # refuse to start when the model requires external macro/COT
        # features this brain instrument cannot build (e.g. an ES config
        # with MACRO_OVX_*). Raises HERE in __init__, before connect() /
        # any network side-effect (connect happens in start()). Gated on
        # _needs_macro so non-macro configs never consult the instrument's
        # macro metadata.
        if self._needs_macro:
            validate_external_macro_features(
                self.feature_names, self._instrument_context.brain_instrument
            )
        # T2 (D3): all live data artifacts are keyed by the BRAIN symbol —
        # the cached/ledgered series IS the brain-stream continuous series,
        # so an MCL config legitimately shares CL's file set. CL derives
        # byte-identical legacy names. Explicit seed/cache args win.
        _paths = derive_data_paths(self._instrument_context.brain_symbol)
        seed_path = seed_path or str(_paths.seed_5m)
        cache_path = cache_path or str(_paths.cache_5m)
        # Force lean_features to False in live trading because live models
        # generally require the full feature set (MACRO/DIST).
        # This prevents accidental missing feature errors if the config retains
        # backtest optimizations.
        self._lean_features: bool = False

        # Extract designated primary stream from config (e.g. "1h" or "5m")
        self._bar_size: str = strategy_config.get("bar_size", "5m").lower()

        # T7 (t7-es-ops-runway): hourly-only mode. live_config.enable_5m_stream
        # is OPTIONAL and DEFAULTS TRUE — every config without the key (the
        # whole CL fleet) constructs byte-identically to HEAD. When false, the
        # brain 5m artifacts (DataManager/seed/warm-start/subscription) are
        # skipped entirely; the front-month Hands stream STAYS (order-pricing-
        # critical). Not a silent fork: the resolved mode is loudly logged and
        # misuse (false + 5m inference stream) hard-crashes here, before any
        # network side-effect.
        _live_cfg_all = strategy_config.get("live_config", {}) or {}
        self._enable_5m_stream: bool = bool(
            _live_cfg_all.get("enable_5m_stream", True)
        )
        if not self._enable_5m_stream and self._bar_size not in ("1h", "2h", "4h"):
            raise ValueError(
                f"live_config.enable_5m_stream=false requires an hourly "
                f"bar_size (got {self._bar_size!r}) — the 5m stream IS the "
                f"inference stream for 5m configs."
            )

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
        # client_id present (fleet/CLI path) -> identity-bound telemetry on
        # the SHARED fleet DB: writes stamped (symbol, client_id), reads
        # scoped to this bot. Absent (tests, livetest) -> legacy single-bot
        # DB, byte-identical behavior.
        self.client_id = client_id
        if client_id is not None:
            self.telemetry = TelemetryDB(
                db_path,
                symbol=self._instrument_context.brain_symbol,
                client_id=client_id,
            )
        else:
            self.telemetry = TelemetryDB(db_path)
        log.info("Telemetry DB: %s", db_path)

        # Watchdog-family Telegram throttle state
        # (watchdog-telegram-throttle_07062026_0007). client_id present
        # (fleet/CLI) -> ONE cid-keyed JSON sidecar BESIDE the shared
        # telemetry DB (R1: db_path is the ONE shared fleet_telemetry.db —
        # never derive from the db stem alone) so the hourly budget
        # survives the R4 SystemExit -> fleet_runner restart cycle
        # (restarted child re-resolves the same cid -> same file).
        # client_id None (livetest, tests, object.__new__ stubs) -> None ->
        # _send_watchdog_telegram runs in-memory-only with ZERO disk I/O.
        if client_id is not None:
            self._watchdog_tg_state_path = Path(db_path).with_name(
                f"watchdog_tg_cid{self.client_id}.json"
            )
        else:
            self._watchdog_tg_state_path = None

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
        # C2 (T2 impact review) — RESOLVED IN T5: roll metadata is now
        # namespaced per EXECUTION symbol (last_front_month_by_symbol +
        # startswith ownership) inside DataManager, so concurrent CL + MCL
        # instances sharing the brain file no longer ping-pong. bars_per_day
        # is fed from the registry (CL keeps its legacy 288/24 literals by
        # construction — pinned).
        # T7: hourly-only instances (enable_5m_stream=false) never construct
        # the 5m manager — data_manager_5m is None, the design's sentinel
        # (every downstream 5m touchpoint is None-guarded or flag-gated).
        if self._enable_5m_stream:
            log.info("DATA PATHS: 5m seed=%s  cache=%s", seed_path, cache_path)
            self.data_manager_5m = DataManager(
                symbol=self._instrument_context.brain_symbol,
                seed_path=seed_path,
                cache_path=cache_path,
                master_ledger_path=str(_paths.ledger_5m),
                roll_metadata_path=str(_paths.roll_metadata),
                data_client=self.data_client,
                bar_size="5 mins",
                bars_per_day=self._instrument_context.brain_instrument.bars_per_day_5m,
                execution_symbol=self._instrument_context.execution_symbol,
                # seedless-5m-live-stream_07052026_0546: only 5m MODELS
                # consume deep 5m features (parity note at _MAX_ROLLING_BARS)
                # — hourly models may shallow-bootstrap a seedless 5m window
                # from IBKR. 5m models keep the hard seed requirement.
                allow_shallow_bootstrap=(self._bar_size in ("1h", "2h", "4h")),
            )
        else:
            self.data_manager_5m = None
            log.warning(
                "HOURLY-ONLY MODE: enable_5m_stream=false — 5m DataManager/"
                "seed/subscription disabled; trailing evaluates on 1h bars; "
                "the front-month hands stream stays subscribed."
            )

        self.data_manager_1h = None
        if self._bar_size in ("1h", "2h", "4h"):
            # 1h models use a dedicated 1h data manager to avoid pacing limits.
            # Seed from the full historical 1H parquet ({SYM}_raw_1h.parquet)
            # which lives alongside the processed datasets in CL_DATA_ROOT/data/processed/.
            _data_root = _get_data_root()
            cache_path_1h = str(_paths.cache_1h)
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
                seed_path_1h = str(_paths.seed_1h)
            log.info("DATA PATHS: 1h seed=%s  cache=%s", seed_path_1h, cache_path_1h)

            # Hard validation: the 1H seed must exist. If it doesn't, the
            # DataManager would fall back to an IBKR-only backfill that produces
            # too few bars, causing NaN features and silent inference degradation.
            _seed_1h_path = Path(seed_path_1h)
            _cache_1h_path = Path(cache_path_1h)
            if not _cache_1h_path.exists() and not _seed_1h_path.exists():
                raise FileNotFoundError(
                    f"CRITICAL: Neither 1H cache nor seed file found for "
                    f"{self._instrument_context.brain_symbol}!\n"
                    f"  cache: {cache_path_1h}\n"
                    f"  seed:  {seed_path_1h}\n"
                    f"  CL_DATA_ROOT={_CL_DATA_ROOT}\n"
                    f"Ensure CL_DATA_ROOT points to the shared data directory "
                    f"containing the 1H seed parquet, or copy "
                    f"the seed file to this environment."
                )

            # Same roll-metadata file as the 5m manager — preserves the
            # intra-symbol 5m+1h sharing (same execution_symbol, so the T5
            # ownership filter keeps their sharing intact).
            self.data_manager_1h = DataManager(
                symbol=self._instrument_context.brain_symbol,
                seed_path=seed_path_1h,
                cache_path=cache_path_1h,
                master_ledger_path=str(_paths.ledger_1h),
                roll_metadata_path=str(_paths.roll_metadata),
                data_client=self.data_client,
                bar_size="1 hour",
                bars_per_day=self._instrument_context.brain_instrument.bars_per_day_1h,
                execution_symbol=self._instrument_context.execution_symbol,
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
        # seedless-5m-live-stream_07052026_0546: latched by _warm_start when
        # the 5m window was founded by the shallow IBKR bootstrap (read by
        # the startup Telegram Mode stamp — 3-surface disclosure).
        self._shallow_5m_bootstrap: bool = False
        self._last_bar_time_5m: Optional[pd.Timestamp] = None
        self._last_bar_time_1h: Optional[pd.Timestamp] = None
        self._subscriptions_lost = False  # Track connectivity drops
        self._resubscribe_pending = False  # Prevent duplicate resubscription scheduling
        self._data_farm_ok = False         # Set True when 2104/2106 received
        self._data_farm_broken_only = False # True if only 2103/2105 received (no OK)
        # Health-event emission is an explicit live-path opt-in (cli.main
        # sets True): unit tests driving watchdog/resubscribe paths must
        # never write into the production error queue.
        self._health_events_enabled = False
        self._callbacks_registered = False
        # Contract rollover state
        self._rollover_in_progress = False
        self._last_rollover_check_date = None
        # Hourly housekeeping latch: (date, hour) of the last swept slot
        # (A-7 — wall-clock gated, never poll-count gated).
        self._last_housekeeping_slot = None
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
        # TIME BARRIER exit confirmation state (exit-fill-unverified_07152026_1855):
        # the exit is confirmed against a settled broker snapshot before the
        # ledger is booked; these bound the cross-bar retry and remember the
        # last pending exit order id. Both are CLEARED by _reset_position_state.
        self._time_barrier_exit_attempts: int = 0
        self._pending_exit_order_id: Optional[int] = None
        # Persistent set of order IDs already processed as TP/SL exits.
        # Intentionally NOT cleared by _reset_position_state() so that a
        # duplicate IBKR Filled callback arriving after the state reset cannot
        # misidentify the same exit order as a new entry fill.
        self._processed_exit_order_ids: set[int] = set()
        self._processed_entry_order_ids: set[int] = set()
        # Registry of order IDs submitted as ENTRY orders (str form). The fill
        # handler only books a new trade for IDs in this set — decision context
        # alone cannot discriminate entries because it is also stored under
        # child TP/SL order IDs for telemetry traceability.
        self._entry_order_ids: set = set()
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
        self._telegram = TelegramAlerter(prefix=self._execution_symbol)

        class _SymbolPrefixFilter(logging.Filter):
            def __init__(self, symbol: str):
                super().__init__()
                self.symbol = symbol

            def filter(self, record: logging.LogRecord) -> bool:
                if not getattr(record, "_symbol_prefixed", False):
                    # Pad to width 3 so the tag column aligns across children
                    # ("[CL ]", "[MES]", "[NG ]") — otherwise 2- vs 3-char
                    # symbols shift every field after them by a character.
                    record.msg = f"[{self.symbol:<3}] {record.msg}"
                    record._symbol_prefixed = True
                return True

        # Prevent duplicate filters in test environments with many instantiations
        log.filters = [f for f in log.filters if not isinstance(f, _SymbolPrefixFilter)]
        self._symbol_filter = _SymbolPrefixFilter(self._execution_symbol)
        log.addFilter(self._symbol_filter)
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
        init_margin = 0.0
        maint_margin = 0.0
        excess_liq = 0.0

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
                init_margin = acct.get("init_margin_req", 0.0)
                maint_margin = acct.get("maint_margin_req", 0.0)
                excess_liq = acct.get("excess_liquidity", 0.0)
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
            f"Total Liq: `${net_liq:,.2f}`\n"
            f"Init Margin (acct): `${init_margin:,.2f}`\n"
            f"Maint Margin (acct): `${maint_margin:,.2f}`\n"
            f"Free Cushion (Excess Liq): `${excess_liq:,.2f}`\n\n"
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
                # T4 (D2): instrument-derived index fetch list, ordered
                # ["VIX"] + [vol] so CL keeps today's exact ["VIX", "OVX"]
                # byte-order (ES/ZC/ZS/SI -> ["VIX"], GC -> ["VIX", "GVZ"]).
                # The symbol doubles as the _macro_daily_closes key AND the
                # FRED column label _build_fred_features' live_overrides
                # match against (registry invariant:
                # live_vol_index == volatility_index.replace("CLS", "")).
                vol = self._brain_instrument.live_vol_index
                index_syms = ["VIX"] + ([vol] if vol != "VIX" else [])
                log.info(
                    "Fetching previous daily closes for macro indices (%s)...",
                    ", ".join(index_syms),
                )
                for sym in index_syms:
                    try:
                        self._macro_daily_closes[sym] = self.data_client.fetch_daily_close(sym)
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
                if self.data_manager_5m is not None:  # T7: None in hourly-only
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
                    MacroFeatureEngine(
                        instrument=self._brain_instrument
                    ).refresh_if_stale()
                    # Also verify value-level freshness (file may be
                    # new but contain repeated data from FRED).
                    overrides = getattr(self, "_macro_daily_closes", {})
                    MacroFeatureEngine(
                        instrument=self._brain_instrument
                    )._build_fred_features(
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
                f"Exec Port: `{exec_port}`\n"
            )
            # T7: stamp the resolved stream mode (no silent forks)
            if not self._enable_5m_stream:
                startup_msg += (
                    "Mode: `HOURLY-ONLY (enable_5m_stream=false)`\n"
                )
            # seedless-5m-live-stream_07052026_0546: stamp the shallow 5m
            # bootstrap mode (3-surface disclosure, mirrors HOURLY-ONLY).
            if getattr(self, "_shallow_5m_bootstrap", False):
                startup_msg += (
                    "Mode: `5M SHALLOW BOOTSTRAP (no 5m seed — IBKR-fetched "
                    "window)`\n"
                )
            startup_msg += "\n"
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
        # Save warm-start caches on shutdown. T7 (impact_review C2): each
        # save gets its OWN None-guard + try/except — the former SHARED try
        # let a 5m-side failure (e.g. the None manager in hourly-only mode)
        # swallow the AttributeError and silently SKIP the 1h cache save.
        if self.data_manager_5m is not None:
            try:
                self.data_manager_5m.save_cache()
            except Exception:
                log.warning("Failed to save warm-start cache on shutdown.")
        if self.data_manager_1h is not None:
            try:
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
        # Commission reports carry the broker-side commission and realized
        # PnL per fill — without them the telemetry DB can never reconcile
        # against IBKR (telemetry-fill-commission_07062026_0640). Exec
        # session only: the data client's session receives duplicates.
        if hasattr(self.exec_client, "register_commission_callback"):
            self.exec_client.register_commission_callback(self._on_commission_event)
        if hasattr(self.data_client, "register_error_callback"):
            self.data_client.register_error_callback(self._on_ib_error)
        if hasattr(self.exec_client, "register_error_callback"):
            self.exec_client.register_error_callback(self._on_ib_error)
        self._callbacks_registered = True

    def _on_commission_event(self, evt) -> None:
        """Persist a broker commission report as a COMMISSION tradebook row.

        The deterministic event_id (execId-keyed) makes the INSERT OR
        IGNORE idempotent across sessions and restarts. Telemetry failure
        must never propagate into the broker event loop.
        """
        try:
            self.telemetry.log_tradebook_event(
                event_id=f"COMMISSION_{evt.exec_id}",
                event_type="COMMISSION",
                event_timestamp_utc=self._utc_iso_now(),
                order_id=evt.order_id,
                broker_execution_id=evt.exec_id,
                symbol=evt.symbol,
                commission=evt.commission,
                realized_pnl=evt.realized_pnl,
            )
        except Exception:
            log.warning(
                "COMMISSION telemetry write failed (execId=%s)",
                getattr(evt, "exec_id", "?"), exc_info=True,
            )

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
        # TIME BARRIER exit confirmation state — cleared on any real close so a
        # fresh position starts with a clean retry budget and no stale pending
        # exit id (exit-fill-unverified_07152026_1855).
        self._time_barrier_exit_attempts = 0
        self._pending_exit_order_id = None

    def _clear_pending_entry(self) -> None:
        """Clear ONLY the pending-entry record — never in-position state.

        Entry-cancellation paths (TTL, rollover, kill-switch) call this
        for orders that NEVER filled: no trade existed, so no
        strategy.on_exit and no cooldown may fire (D2.4). Real closes of
        FILLED positions still go through _reset_position_state (A2).
        """
        self._pending_entry_order_id = None
        self._pending_entry_bar_time = None

    def _pending_entry_filled_qty(self) -> float:
        """Broker-reported filled quantity on the tracked pending entry.

        A1 discriminator: a partially-filled "pending" order is NOT
        never-filled — contracts exist broker-side. Reads the cached
        order event (filled_qty, falling back to raw orderStatus);
        0 when the order is unknown to the cache.
        """
        if self._pending_entry_order_id is None:
            return 0.0
        evt = self._open_orders.get(str(self._pending_entry_order_id))
        if evt is None:
            evt = self._open_orders.get(self._pending_entry_order_id)
        if evt is None:
            return 0.0
        filled = getattr(evt, "filled_qty", None)
        if filled is None:
            status = getattr(getattr(evt, "raw_event", None), "orderStatus", None)
            filled = getattr(status, "filled", 0)
        return float(filled or 0)

    def _check_entry_order_ttl(self, bar_time: pd.Timestamp) -> None:
        """Cancel stale entry orders that haven't filled after 1 bar.

        If an Adaptive/Limit entry order was placed on the previous bar
        and is still pending (PreSubmitted/Submitted), cancel it and all
        bracket children so the position guard unblocks for new signals.

        A never-filled entry is NOT a trade: only the pending record is
        cleared (D2.4). The old _reset_position_state() here fired
        strategy.on_exit with an SL-flavored cooldown for a trade that
        never existed.
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
            self._clear_pending_entry()
            return

        # A1: a partial fill means contracts EXIST broker-side — silently
        # cancelling and clearing would hide a live position. Alert loudly
        # and leave the state for the fill callback / kill switch.
        filled_qty = self._pending_entry_filled_qty()
        if filled_qty > 0:
            log.error(
                "ENTRY TTL: pending entry order %s is PARTIALLY FILLED "
                "(filled=%.0f) — NOT cancelling/clearing; a position exists "
                "broker-side and must be adjudicated by the fill path",
                self._pending_entry_order_id, filled_qty,
            )
            try:
                self._telegram.send(
                    f"[CRITICAL] *PARTIAL FILL ON PENDING ENTRY*\n"
                    f"Order: `{self._pending_entry_order_id}` "
                    f"filled `{filled_qty:.0f}`\n"
                    f"TTL cancel SKIPPED — position exists broker-side. "
                    f"Verify brackets/fill handling."
                )
            except Exception:
                pass  # Never let Telegram failure block safety logic
            return

        # Cancel the stale entry + bracket children
        log.info(
            "ENTRY TTL: cancelling unfilled entry order %s "
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

        # Never-filled: pending record only — no on_exit, no cooldown (D2.4)
        self._clear_pending_entry()

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
        # D2.3: hard-gate on CONFIRMED in-position state before ANY work —
        # the extremes update included. Both signals are fill-time-only:
        # _active_trade_id is set by the entry-fill callback (or ledger
        # recovery), _sl_order_id by bracket-children placement. A pending
        # unfilled entry carries neither, so the old repeating
        # "_sl_order_id is None" warning (NG order 19, 2026-07-06) is
        # structurally impossible — not merely suppressed. The tracked SL
        # order alone still counts as confirmation: pinned S6 seams
        # (test_tick_order_pricing) evidence the fill via the SL order.
        if self._active_trade_id is None and self._sl_order_id is None:
            return
        if self._trailing_activated:
            return
        if self._entry_price is None or self._atr_at_entry is None:
            return
        if self._atr_at_entry <= 0:
            return

        # Update bar extremes from the latest bar. T7 (impact_review C4):
        # the extremes frame is selected ONCE, by PRESENCE — the 5m frame
        # whenever it exists (every 5m-enabled instance, including the
        # parity harness's populated 5m mirror), else the 1h frame
        # (hourly-only instances, where rolling_df_5m is None by
        # construction). Deliberately NOT a flag read — frame presence IS
        # the mode, declared loudly at startup. Extremes stay the same
        # monotonic max/min accumulators.
        extremes_df = (
            self.rolling_df_5m
            if self.rolling_df_5m is not None
            else self.rolling_df_1h
        )
        last_bar = extremes_df.iloc[-1]
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
        # S6 (T3): snap to the instrument grid (CL: bit-identical to the
        # legacy round(new_sl, 2) via the power-of-ten fast path).
        new_sl = round_to_tick(new_sl, self._tick_size)

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
                # TRANSMIT-THEN-COMMIT: transmit FIRST inside a targeted
                # try/except — the generic handler below must never swallow
                # a transmit failure after the auxPrice mutation above. On
                # failure: restore the cached order, log at ERROR, and
                # commit NOTHING (no latch, no tracked price, no ledger
                # write, no snapshot) — the trigger is not latched, so the
                # next bar re-fires and retries naturally.
                try:
                    self.exec_client.modify_order(evt.order_id, evt)
                except Exception:
                    if raw_order is not None:
                        raw_order.auxPrice = old_sl  # un-poison the cached Trade
                    log.exception(
                        "TRAILING STOP: SL modify transmit FAILED for order "
                        "%s (SL remains %.2f at broker) — will retry on "
                        "next bar",
                        order_id, old_sl,
                    )
                    return
                # --- success only below this line ---
                log.info(
                    "TRAILING STOP: modified SL order %s: %.2f → %.2f",
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
                        # LOUD, not swallowed (ticket
                        # unprotected-leg-verification_07082026_0315): the
                        # broker SL is already at the new trailed price (the
                        # modify above committed), but the ledger now holds a
                        # STALE sl_price. That matters because the protective-
                        # leg heal re-places from the ledger's sl_price — a
                        # silent staleness here would re-place the ORIGINAL
                        # (looser) stop. Do NOT roll back the broker modify;
                        # surface it for ledger repair.
                        detail = (
                            f"trailing SL modified at the broker to "
                            f"{new_sl:.4f} (order {order_id}) but the ledger "
                            f"sl_price persist FAILED — ledger is stale, a "
                            f"future re-place could use the original stop"
                        )
                        log.error(
                            "[TRAILING STOP] %s", detail, exc_info=True)
                        self._emit_health_event(
                            "housekeeping-ledger-persist-failed", detail)
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
                "TRAILING STOP: triggered but SL order %s not found in "
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
        """Enforce 24-hour (288-bar) exit to match backtests.

        SUBMIT-AND-DEFER (settle-confirm-event-loop_07202026_0713). This runs
        INSIDE the ib_insync bar-update callback (_on_bar_update_* -> _on_new_bar
        -> here), i.e. from an already-RUNNING asyncio loop. It may therefore
        only SUBMIT orders (non-blocking placeOrder/cancelOrder) and RECORD
        intent — it must NEVER call the settled read: _confirm_settled_position
        -> get_position_settled does self.ib.run() == loop.run_until_complete(),
        which re-enters the running loop and raises 'This event loop is already
        running'. Every settled-based confirm/book/re-arm decision is deferred to
        _reconcile_pending_position_state, which runs on a genuinely-idle
        main-loop tick.
        """
        # BINDING CONDITION 2 — re-entrancy guard. While a TIME BARRIER exit is
        # already outstanding, the idle-loop reconciler owns its resolution; a
        # second bar callback must NOT submit another exit or read settled (a
        # repeat settled read would re-crash in-loop). Defer immediately.
        if self._pending_exit_order_id is not None:
            return False

        current_position = self.exec_client.get_position(
            symbol=self._execution_symbol,
        )
        if current_position == 0:
            if self._active_trade_id is not None:
                # We think we hold a position but read flat. Right after a
                # reconnect this can be a stale/empty ib_insync cache. DEFER:
                # do NOT confirm settled here (that does self.ib.run() and
                # re-enters the running callback loop) and do NOT book an
                # out-of-band close off an unconfirmed flat — the idle-loop
                # reconciler's flat-read branch owns the settled confirm +
                # OOB-close booking (reconnect-false-flat-oob, relocated).
                log.info(
                    "[TIME BARRIER] flat read for tracked trade %s — deferring "
                    "the settled confirm + any out-of-band-close decision to "
                    "the idle-loop reconciler (no in-callback settled read)",
                    self._active_trade_id,
                )
                return False
            # Genuinely flat + untracked — routine flat bar.
            self._reset_position_state(reason="CLOSED_OOB")
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
        # :1679 just cancelled the resting SL/TP on the broker. Reflect that in
        # memory NOW: the in-memory ids no longer point at live orders, and
        # clearing them ARMS the 5-minute kill switch (its guards at :5776/:5782
        # fire on _active_trade_id set + _sl_order_id None) to cover any
        # deferral window below. The tracked PRICES survive so A3 can re-arm.
        self._sl_order_id = None
        self._tp_order_ids = []

        trade = self.exec_client.close_position(
            symbol=self._execution_symbol,
            exit_mode=self._exit_mode,
            current_price=current_price,
        )
        _exit_oid = getattr(getattr(trade, "order", None), "orderId", None)
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
            order_id=_exit_oid,
        )

        # A0 — the exit was NEVER SUBMITTED: close_position returned None (the
        # close_cl_position:825 no-match return) or an order object carrying no
        # orderId. A missing orderId is a HARD failure (no silent-None default)
        # — we can neither confirm nor cancel an exit that does not exist. Do
        # NOT book, do NOT reset; re-arm protection (no live exit exists, so
        # re-arming is safe here) and keep the trade tracked to retry next bar.
        if trade is None or _exit_oid is None:
            log.critical(
                "[TIME BARRIER] exit order was NEVER SUBMITTED for trade %s "
                "(close_position returned %r) — NOT booking; re-arming "
                "protection and keeping the position tracked to retry next bar",
                self._active_trade_id, trade,
            )
            self._rearm_time_barrier_protection(current_position)
            self._pending_exit_order_id = None
            self._note_time_barrier_deferral(_exit_oid)
            return False

        # The exit WAS submitted — register its id so the async fill callback
        # recognises it as a known exit (not a PHANTOM FILL), record it as the
        # pending exit, and DEFER. The settled confirm/book/re-arm decision runs
        # in _reconcile_pending_position_state on the next genuinely-idle tick
        # (this callback runs inside the running loop; a settled read here would
        # raise 'This event loop is already running'). Until the reconciler
        # clears _pending_exit_order_id the re-entrancy guard above blocks a
        # second exit, and _sl_order_id is None so the 5-minute kill switch
        # covers the deferral window.
        self._processed_exit_order_ids.add(str(_exit_oid))
        self._pending_exit_order_id = _exit_oid
        return False

    def _reconcile_pending_position_state(self) -> bool:
        """Idle-loop settled-based reconciler (settle-confirm-event-loop).

        Owns ALL settled-based confirmation for a tracked trade. It MUST run on
        a genuinely-idle main-loop tick (wired into _event_loop immediately
        BEFORE _run_hourly_housekeeping): the settled read it depends on does
        self.ib.run() == loop.run_until_complete(), which raises 'This event
        loop is already running' if reached from inside the ib_insync bar-update
        callback. It is therefore NEVER called from _check_time_barrier, which
        now only submits the exit + records intent and defers here.

        Two independently-triggered branches, in order:
          * Pending-exit (_pending_exit_order_id set): resolve the TIME BARRIER
            exit _check_time_barrier submitted-and-deferred — the a1464d2
            A1/A2/route settled decision, relocated byte-for-byte to this idle
            context (the loop has now turned since submission, so the fill is
            reflected and the settled snapshot is authoritative). Returns True
            when the exit is booked/completed, False when deferred.
          * Flat-read (a tracked trade with NO pending exit whose cached
            position reads flat): the reconnect-false-flat / out-of-band-close
            case — confirm settled and, on a CONFIRMED flat, book the OOB close.
            Its trigger is the flat cache read itself, NOT the pending exit.

        Never raises into the event loop (the housekeeping/rollover pattern): an
        internal failure logs and DEFERS to the next idle tick — it must NEVER
        book or re-arm on an unconfirmed / guessed value (no cheap fix).
        """
        try:
            # --- Pending-exit branch (owns BINDING CONDITION 2's deferral) ----
            if self._pending_exit_order_id is not None:
                exit_oid = self._pending_exit_order_id
                current_position = self.exec_client.get_position(
                    symbol=self._execution_symbol,
                )
                # A1 — gate on broker truth. The loop is idle and has turned
                # since the exit was submitted in the bar callback, so the fill
                # is reflected and this settled snapshot is authoritative (same
                # main-thread, event-loop-idle contract self.ib.run() needs).
                settled = self._confirm_settled_position(self._execution_symbol)
                if settled is None:
                    # Unconfirmed -> fail closed (the :1593-1601 precedent). The
                    # exit is still live and can still fill: no ledger write, no
                    # reset, keep the position tracked, do NOT re-arm and do NOT
                    # cancel it away (BINDING CONDITION 1). Retry next idle tick.
                    log.error(
                        "[TIME BARRIER] exit %s for trade %s could not be "
                        "confirmed (settled snapshot failed) — no book, no "
                        "reset, retaining the position + live exit "
                        "(fail-closed); retrying next idle tick",
                        exit_oid, self._active_trade_id,
                    )
                    self._note_time_barrier_deferral(exit_oid)
                    return False
                if settled == 0:
                    # Flat: the exit filled. Book the PROVEN price and finish.
                    return self._book_time_barrier_flat(exit_oid)

                # settled != 0 — the incident: the exit did not (yet) fill. A2:
                # retire the stranded GTC exit BEFORE touching protection —
                # leaving it resting would let it double-fill against a re-armed
                # stop.
                cancel_count = self.exec_client.cancel_orders_by_ids([exit_oid])
                if cancel_count == 0:
                    # Not open — a filled order has already left openTrades(), so
                    # the cancel was a silent no-op. No live exit can fire =>
                    # route on a fresh settled read.
                    return self._route_retired_time_barrier_exit(
                        exit_oid, current_position,
                    )
                # BINDING CONDITION 1 — cancel_count >= 1: the exit is only
                # cancel-REQUESTED, not dead. ib_insync fires cancelOrder
                # fire-and-forget (ibkr_execution.py:298-313) and a fast fill can
                # still cross at the exchange (the race documented at
                # ibkr_client.py:1583-1588). NEVER re-arm while it can still
                # fill: re-scan the open book, and only once the exit has LEFT it
                # may the settled read be taken (the ordering is load-bearing — a
                # settled snapshot pre-dating the fill would re-arm a stop onto a
                # flat book = a naked reversal).
                open_trades = self.exec_client.get_open_trades(
                    self._execution_symbol,
                ) or []
                exit_still_open = any(
                    str(getattr(evt, "order_id", None)) == str(exit_oid)
                    for evt in open_trades
                )
                if exit_still_open:
                    # Still live -> defer: stay tracked, _sl_order_id None so the
                    # 5-minute kill switch covers the gap, no re-arm, no ledger
                    # write. Retry next tick (bounded by bars/attempts, never a
                    # sleep).
                    log.warning(
                        "[TIME BARRIER] exit %s cancel-requested but still "
                        "resting — deferring: no re-arm (would double-fill), "
                        "position stays tracked and the kill switch covers the "
                        "gap; retrying next idle tick", exit_oid,
                    )
                    self._note_time_barrier_deferral(exit_oid)
                    return False
                # The exit has left the book -> STRICTLY AFTER that, route on
                # settled.
                return self._route_retired_time_barrier_exit(
                    exit_oid, current_position,
                )

            # --- Flat-read branch (BINDING CONDITION 3) -----------------------
            # A tracked trade with NO pending exit whose cached position reads
            # flat is the reconnect-false-flat / out-of-band-close case (the
            # :1657 site, relocated to the idle context). Independently
            # triggered by the flat cache read — NOT gated on a pending exit.
            if self._active_trade_id is not None:
                current_position = self.exec_client.get_position(
                    symbol=self._execution_symbol,
                )
                if current_position != 0:
                    # Healthy / non-flat -> nothing to reconcile. Do NOT take a
                    # settled snapshot (an ib.run per poll) for a healthy
                    # position every idle tick.
                    return False
                # Flat cache read -> confirm with a settled snapshot before
                # booking an out-of-band close (idle here, so self.ib.run() is
                # safe).
                settled = self._confirm_settled_position(self._execution_symbol)
                if settled is None:
                    # Unconfirmed -> fail closed: retain position + protective
                    # orders, defer (the :1658-1666 precedent, relocated).
                    log.error(
                        "[RECONCILE] flat read for tracked trade %s could not "
                        "be confirmed (settled snapshot failed) — retaining "
                        "position + protective orders this tick (fail-closed)",
                        self._active_trade_id,
                    )
                    return False
                if settled != 0:
                    # False flat — the position is genuinely still open; the
                    # cache read was stale. Nothing to book; steady-state
                    # management resumes on the next bar.
                    return False
                # CONFIRMED flat -> book the out-of-band close.
                self._book_out_of_band_close()
                return False

            return False
        except Exception:
            # Never-raises boundary (like housekeeping/rollover): DEFER on
            # failure — log and retry next idle tick. This is the ONLY permitted
            # catch and it must NEVER book or re-arm on an unconfirmed / guessed
            # value.
            log.error(
                "[RECONCILE] pending-position reconciliation failed — deferring "
                "to the next idle tick (no book, no re-arm on an unconfirmed "
                "value)",
                exc_info=True,
            )
            return False

    def _book_out_of_band_close(self) -> None:
        """Book a CONFIRMED out-of-band close for the tracked trade (the
        in-callback :1668-1725 block, relocated to the idle reconciler). The
        exit price is an explicit unknown (None) — the idle reconciler has no bar
        price and NEVER fabricates one (the honest-unknown convention); the
        NAKED_POSITION kill switch / housekeeping own any priced flatten."""
        log.info(
            "[TRADE] EXIT: OUT-OF-BAND close detected — position CONFIRMED flat "
            "while trade %s was still tracked (held %d bars)",
            self._active_trade_id, self._position_bars_held,
        )
        try:
            self.telemetry.close_position(
                self._active_trade_id,
                reason="CLOSED_OOB",
                close_time=self._utc_iso_now(),
                bars_held=self._position_bars_held,
                exit_price=None,
            )
        except Exception:
            log.debug("Failed to close ledger position (OOB)", exc_info=True)
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
            log.debug("Failed to log OOB tradebook event", exc_info=True)
        # Cancel any orphaned TP/SL orders still live on IBKR
        try:
            cancelled = self.exec_client.cancel_open_orders(
                symbol=self._execution_symbol,
            )
            if cancelled > 0:
                log.info(
                    "OOB CLEANUP: cancelled %d orphaned order(s)", cancelled,
                )
        except Exception:
            log.debug("OOB CLEANUP: cancel_open_orders failed", exc_info=True)
        # CLOSED_OOB: an exit whose true reason was lost. on_exit() only fires
        # when _position_side != 0, i.e. exactly the OOB case.
        self._reset_position_state(reason="CLOSED_OOB")

    def _route_retired_time_barrier_exit(
        self, exit_oid, current_position,
    ) -> bool:
        """Route a TIME BARRIER exit once it is provably no longer live (cancel
        count 0, or count>=1 then gone from the open book). Re-confirm settled
        STRICTLY AFTER retirement and branch: flat -> book the proven price;
        still-open -> re-arm protection (A3) and stay tracked; unconfirmed ->
        fail closed (no re-arm). Returns _check_time_barrier's value."""
        settled = self._confirm_settled_position(self._execution_symbol)
        if settled is None:
            log.error(
                "[TIME BARRIER] exit %s retired but the settled snapshot could "
                "not be confirmed — no book, no reset, no re-arm (fail-closed); "
                "retrying next bar", exit_oid,
            )
            self._note_time_barrier_deferral(exit_oid)
            return False
        if settled == 0:
            # The exit filled after all — book the proven price and finish.
            return self._book_time_barrier_flat(exit_oid)
        # Still open and the exit is provably dead -> A3: safe to re-arm.
        self._rearm_time_barrier_protection(current_position)
        log.warning(
            "[TIME BARRIER] exit %s died without filling; re-armed protection "
            "and kept trade %s tracked — retrying exit next bar",
            exit_oid, self._active_trade_id,
        )
        self._note_time_barrier_deferral(exit_oid)
        return False

    def _book_time_barrier_flat(self, exit_oid) -> bool:
        """settled == 0: the exit filled. Book the ledger CLOSED with the PROVEN
        execution price (NULL when no execution matches the exit order id — an
        explicit unknown, never the fabricated current_price; the :2305-2313
        precedent), then reset with reason='TIME_BARRIER' (the backtest flavors
        TIME_BARRIER exits as SL for sl_cooldown_bars parity) and report a
        completed exit."""
        exit_price = self._resolve_exit_fill_price(exit_oid)
        if self._active_trade_id is not None:
            try:
                self.telemetry.close_position(
                    self._active_trade_id,
                    reason="TIME_BARRIER",
                    close_time=self._utc_iso_now(),
                    bars_held=self._position_bars_held,
                    exit_price=exit_price,
                )
            except Exception:
                log.debug("Failed to close ledger position", exc_info=True)
        self._reset_position_state(reason="TIME_BARRIER")
        return True

    def _resolve_exit_fill_price(self, exit_oid) -> Optional[float]:
        """Return the PROVEN fill price for the exit order id from broker
        executions, or None when no execution matches (str/int-robust — ledger
        ids are ints, execution records carry str order ids; the same join key
        as _recover_oob_close:2294-2303). NEVER fabricates a price from the
        stale bar close."""
        try:
            executions = self.exec_client.get_executions(
                self._execution_symbol,
            ) or []
        except Exception:
            log.error(
                "[TIME BARRIER] get_executions failed — booking NULL exit price "
                "for order %s (never a fabricated price)", exit_oid,
                exc_info=True,
            )
            return None
        for rec in executions:
            if str(rec.get("order_id")) == str(exit_oid):
                return rec.get("price")
        return None

    def _rearm_time_barrier_protection(self, current_position) -> None:
        """Re-place SL/TP from the ledger's stored prices after an unfilled
        TIME BARRIER exit was retired (A3), or when the exit was never
        submitted (A0). The now-dead order ids were cleared when :1679 cancelled
        them; the tracked PRICES survive for exactly this re-arm."""
        self._verify_and_heal_protective_legs(
            trade_id=self._active_trade_id,
            tp_order_id=None,
            sl_order_id=None,
            tp_price=self._tracked_tp_price,
            sl_price=self._tracked_sl_price,
            quantity=abs(current_position) or 1,
            position_side=self._position_side,
        )

    def _note_time_barrier_deferral(self, exit_oid) -> None:
        """Count one deferred TIME BARRIER exit attempt (A4). On exhaustion,
        escalate LOUD (log.critical + Telegram + health event) while keeping the
        position TRACKED so housekeeping's HEAL branch (:2813) owns it rather
        than the detect-only UNTRACKED branch (:2757). Bounded by attempts,
        never a sleep — the 5-minute kill switch is the real net."""
        self._time_barrier_exit_attempts += 1
        if self._time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS:
            return
        detail = (
            f"TIME BARRIER exit for trade {self._active_trade_id} still "
            f"unconfirmed after {self._time_barrier_exit_attempts} attempts "
            f"(last exit order {exit_oid}) — position stays TRACKED for "
            f"housekeeping heal / the kill switch; needs a human if it persists"
        )
        log.critical("[TIME BARRIER] %s", detail)
        try:
            self._emit_health_event("time-barrier-exit-unconfirmed", detail)
        except Exception:
            log.debug(
                "emit_health_event failed (time-barrier-exit)", exc_info=True,
            )
        try:
            if getattr(self, "_telegram", None) is not None:
                self._telegram.send(
                    f"*TIME BARRIER EXIT UNCONFIRMED* — "
                    f"{self._execution_symbol}\n\n{detail}"
                )
        except Exception:
            log.debug(
                "Telegram send failed (time-barrier-exit)", exc_info=True,
            )

    def _bars_since(self, ts: object) -> Optional[int]:
        """Count brain-stream bars strictly AFTER ``ts`` (gap-immune).

        Restart-recovery replacement for the old wall-clock estimate
        (``int(delta_minutes / bar_dur)``), which counted weekend (~49h) and
        daily-halt gaps as phantom bars and fired spurious TIME_BARRIER exits
        after a weekend restart (ticket
        recovery-barsheld-wallclock_07092026_1239).

        Counts rows in the rolling frame matching ``self._bar_size`` with a
        strictly-greater comparison, matching the steady-state counter's
        semantics exactly (the entry/close bar itself reads 0; +1 per later
        bar). If ``ts`` predates the seeded window the count is a lower
        bound — errs toward HOLDING, never toward a spurious close.

        Returns None when it cannot count honestly (reviewer C1/C2):
        unsupported bar size (2h/4h brains are RESAMPLED from 1h rows — raw
        row counting would over-count 2-4x), missing/empty frame, or a
        malformed ``ts``. Callers keep their conservative default; this
        helper never raises (recovery must never crash startup).
        """
        try:
            if self._bar_size == "1h":
                df = self.rolling_df_1h
            elif self._bar_size == "5m":
                df = self.rolling_df_5m
            else:
                log.warning(
                    "[RECOVERY] _bars_since: unsupported bar_size %r (2h/4h "
                    "brains are resampled from 1h rows) — cannot count bars "
                    "honestly, caller keeps its conservative default",
                    self._bar_size,
                )
                return None
            if df is None or len(df) == 0:
                return None
            ts_parsed = pd.Timestamp(ts)
            if pd.isna(ts_parsed):  # None/NaT parse silently → refuse
                return None
            return int((df.index > ts_parsed).sum())
        except Exception:
            log.debug("[RECOVERY] _bars_since failed for ts=%r", ts, exc_info=True)
            return None

    def _seed_restart_cooldown(
        self, side_int: int, reason: object, close_time: object,
    ) -> None:
        """Re-arm the strategy's post-exit re-entry cooldown after a restart.

        The cooldown gate (ConfigurableStrategy) reads in-memory state that a
        fresh process resets to "no recent exit", so ``sl_cooldown_bars`` is
        silently dropped across restarts (ticket
        cooldown-not-restored-on-restart_07082026_0230). This seeds it from a
        real exit:

        - ``close_time is None`` — the exit just happened (startup OOB
          recovery) → full cooldown window, degenerating exactly to the
          mid-session ``on_exit(-1)`` path.
        - ``close_time=<ts>`` (ledger reconstruction) — measured from the
          ACTUAL exit bar, so an exit that already aged past its window is
          inert (no over-block). If no bar time is available to measure
          against, we stay inert rather than risk over-blocking.

        Parity: seeding ``_last_exit_bars_ago = bars_elapsed - 1`` reproduces
        the counter a continuously-running bot would hold N bars after the
        close (the gate's pre-increment then reads the honest bars_elapsed),
        matching the BacktestEngine. TP_HIT_OOB is excluded from the SL
        cooldown tuple upstream, so a recovered take-profit applies no SL
        cooldown.
        """
        strat = getattr(self, "_strategy", None)
        if strat is None or not hasattr(strat, "on_exit"):
            return
        if side_int not in (1, -1) or reason is None:
            return

        if close_time is None:
            bars_elapsed = 0  # exit just happened → enforce the full window
        else:
            # Count ACTUAL brain bars since the exit bar (gap-immune): the
            # old wall-clock division over-aged cooldowns across weekend and
            # halt gaps (ticket recovery-barsheld-wallclock_07092026_1239).
            _bars = self._bars_since(close_time)
            if _bars is None:
                return  # cannot measure staleness honestly → stay inert
            bars_elapsed = _bars

        strat.on_exit(side_int, reason, getattr(self, "_position_bars_held", 0))
        # on_exit hard-sets bars_ago=-1 ("just exited"); for a historical exit
        # overwrite to the honest elapsed count so the gate ages it correctly.
        if bars_elapsed > 0:
            if side_int == 1 and hasattr(strat, "_last_exit_bars_ago_long"):
                strat._last_exit_bars_ago_long = bars_elapsed - 1
            elif side_int == -1 and hasattr(strat, "_last_exit_bars_ago_short"):
                strat._last_exit_bars_ago_short = bars_elapsed - 1

    def _reconstruct_cooldown_from_ledger(self) -> None:
        """On a flat restart, re-seed each side's re-entry cooldown from the
        most recent CLOSED ledger row for THAT side, so ``sl_cooldown_bars``
        survives a restart even when the stop happened before shutdown.

        Per-side (a recent long exit and a recent short exit are both honored).
        Best-effort: a ledger-query failure must never block startup recovery.
        """
        try:
            closed = self.telemetry.get_recent_closed_positions()
        except Exception:
            log.warning(
                "[RECOVERY] cooldown reconstruction skipped — ledger query "
                "failed", exc_info=True,
            )
            return
        if not closed:
            return
        seen: set = set()
        for row in closed:  # close_time DESC → first per side is most recent
            side = row.get("side")
            side_int = 1 if side == "LONG" else (-1 if side == "SHORT" else 0)
            if side_int == 0 or side_int in seen:
                continue
            seen.add(side_int)  # the most-recent CLOSED row for this side
            reason = row.get("close_reason")
            if reason is not None:
                self._seed_restart_cooldown(
                    side_int, reason, close_time=row.get("close_time"),
                )
            if len(seen) == 2:
                break

    def _confirm_settled_position(self, symbol) -> Optional[int]:
        """Confirm net position from a freshly-SETTLED broker snapshot.

        Guards the cancel/close-on-zero paths against a stale/empty ib_insync
        position cache right after a (re)connect (the reconnect-false-flat OOB
        bug): a single ``get_position()==0`` may be an unpopulated cache, not
        a real flat. Returns the settled net position, or **None** if it
        could NOT be confirmed (settle timeout/error) — on None the caller
        MUST fail CLOSED (retain protective orders; never treat as flat).

        Adapters without a settled read fall back to the plain read, which is
        authoritative for them (e.g. the simulation).
        """
        try:
            return self.exec_client.get_position_settled(symbol=symbol)
        except NotImplementedError:
            return self.exec_client.get_position(symbol=symbol)
        except Exception as exc:
            detail = (
                f"settled position snapshot could not be confirmed for "
                f"{symbol} ({type(exc).__name__}: {exc}) — retaining "
                f"protection, deferring any out-of-band-close decision"
            )
            log.error("[POSITION] %s", detail, exc_info=True)
            self._emit_health_event("position-flat-unconfirmed", detail)
            try:
                if getattr(self, "_telegram", None) is not None:
                    self._telegram.send(
                        f"*SETTLED POSITION UNCONFIRMED* — {symbol}\n\n"
                        f"A flat broker read could not be confirmed by a "
                        f"settled snapshot; retaining protective orders and "
                        f"deferring the out-of-band-close decision "
                        f"(fail-closed)."
                    )
            except Exception:
                log.debug(
                    "Telegram send failed (position-flat-unconfirmed)",
                    exc_info=True,
                )
            return None

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
            # Re-seed the re-entry cooldown from the ledger so a stop-out that
            # happened before this restart still blocks same-side re-entry
            # (the in-memory gate would otherwise reset to "no recent exit").
            self._reconstruct_cooldown_from_ledger()
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

        # 2. Verify IBKR position exists. A single read of 0 right after a
        #    (re)connect can be a STALE/empty ib_insync position cache (the
        #    account-update stream has not arrived yet), NOT a real flat — so
        #    NEVER cancel the protective legs / mark an out-of-band close on an
        #    unconfirmed 0. Force a SETTLED snapshot and re-verify first
        #    (reconnect-false-flat-oob: the SI child cancelled a live short's
        #    SL/TP on exactly this false flat).
        if ibkr_pos == 0:
            settled = self._confirm_settled_position(self._execution_symbol)
            if settled == 0:
                # CONFIRMED flat — position was closed while offline (OOB).
                log.info(
                    "[RECOVERY] Ledger trade %s shows OPEN but IBKR is "
                    "CONFIRMED flat — resolving out-of-band close",
                    trade_id,
                )
                reason, _price = self._recover_oob_close(
                    trade_id=trade_id,
                    tp_order_id=tp_order_id,
                    sl_order_id=sl_order_id,
                )
                # Arm the re-entry cooldown from the recovered exit reason. The
                # mid-session housekeeping path does this via
                # _reset_position_state, but that no-ops here (_position_side==0
                # at startup), so seed on_exit DIRECTLY with the LEDGER side.
                # The OOB close just happened → full sl_cooldown window,
                # matching mid-session behavior.
                self._seed_restart_cooldown(
                    1 if side == "LONG" else -1, reason, close_time=None,
                )
                return
            if settled is None:
                # Could NOT confirm (settle timeout/error) → FAIL CLOSED: keep
                # the position + its resting legs, restore in-memory tracking
                # so the child keeps managing, and defer the OOB decision to
                # the next sweep. Do NOT cancel, do NOT mark closed. (A loud
                # position-flat-unconfirmed health event + Telegram already
                # fired inside _confirm_settled_position.)
                log.error(
                    "[RECOVERY] Ledger trade %s shows OPEN and the first IBKR "
                    "read was flat, but a settled snapshot could NOT confirm it "
                    "— retaining position + protective legs (fail-closed), "
                    "deferring out-of-band-close decision.",
                    trade_id,
                )
            else:
                # Settled snapshot shows the position is really still open —
                # the initial flat read was a stale post-reconnect cache.
                log.warning(
                    "[RECOVERY] Initial IBKR read for trade %s was flat but a "
                    "settled snapshot shows position=%d — restoring (stale "
                    "post-reconnect cache, NOT an out-of-band close).",
                    trade_id, settled,
                )
            # settled is None (fail-closed) or nonzero (stale cache): fall
            # through to restore the position and verify/heal its legs
            # (the heal is idempotent — both legs resting is a no-op).

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

        # Restore entry bar time and COUNT bars held from received brain
        # bars (gap-immune _bars_since): the old wall-clock division counted
        # weekend (~49h) / daily-halt gaps as phantom bars and fired spurious
        # TIME_BARRIER exits right after a weekend restart (ticket
        # recovery-barsheld-wallclock_07092026_1239).
        if entry_bar_time_str:
            try:
                self._position_entry_bar_time = pd.Timestamp(entry_bar_time_str)
                _bars = self._bars_since(self._position_entry_bar_time)
                if _bars is not None:
                    self._position_bars_held = _bars
                    log.info(
                        "[RECOVERY] Counted %d bars held since entry at %s "
                        "(bar_size=%s, gap-immune)",
                        self._position_bars_held,
                        self._position_entry_bar_time,
                        self._bar_size,
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

        # 4-5. Verify TP/SL rest on the broker; re-place both if a leg is
        #      missing (shared with the hourly housekeeping heal).
        self._verify_and_heal_protective_legs(
            trade_id=trade_id,
            tp_order_id=tp_order_id,
            sl_order_id=sl_order_id,
            tp_price=tp_price,
            sl_price=sl_price,
            quantity=quantity,
            position_side=self._position_side,
        )

    def _verify_and_heal_protective_legs(
        self, *, trade_id, tp_order_id, sl_order_id, tp_price, sl_price,
        quantity, position_side,
    ) -> str:
        """Verify the position's TP/SL rest on the broker; if a leg is missing,
        cancel stale legs and re-place BOTH from the ledger's current prices.

        Shared by startup recovery (`_recover_inherited_position`) and the
        hourly housekeeping heal. Idempotent: both-resting is a no-op, so a
        healthy position is never churned. `place_child_orders` has no
        single-leg mode, so a missing SL re-cycles a resting TP too — accepted
        (operator decision 2026-07-08, ticket
        unprotected-leg-verification_07082026_0315).

        Returns: "verified" (both resting, no action), "healed" (re-placed),
        "no-prices"/"no-front-month" (cannot re-place — UNPROTECTED),
        "partial"/"place-failed" (placement problem).
        """
        # Verify TP/SL orders on IBKR (query directly — self._open_orders
        # is empty at startup before subscriptions begin).
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
            return "verified"

        # One or both TP/SL orders missing — re-place them
        if tp_price is None or sl_price is None:
            log.warning(
                "[RECOVERY] TP/SL orders missing and no stored prices "
                "in ledger — cannot re-place protective orders. "
                "Position is UNPROTECTED."
            )
            return "no-prices"

        if self._front_month_local_symbol is None:
            log.warning(
                "[RECOVERY] Cannot re-place TP/SL — "
                "front-month contract not resolved"
            )
            return "no-front-month"

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

        # R2 (T3): snap re-placed ledger prices to the instrument grid —
        # identity for rows written by the (tick-snapped) S7 path; protects
        # the recovery path (whose whole job is preventing naked positions)
        # against off-grid ledger rows (pre-T3 non-CL shapes, manual DB
        # edits) drawing Error 110.
        tp_price = round_to_tick(tp_price, self._tick_size)
        sl_price = round_to_tick(sl_price, self._tick_size)

        # Place fresh TP/SL
        exit_action = "SELL" if position_side == 1 else "BUY"
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
                return "healed"
            else:
                log.warning(
                    "[RECOVERY] place_child_orders returned "
                    "%d trades (expected 2)",
                    len(child_trades),
                )
                return "partial"
        except Exception:
            log.exception(
                "[RECOVERY] Failed to re-place TP/SL orders"
            )
            return "place-failed"

    def _recover_oob_close(self, *, trade_id, tp_order_id, sl_order_id):
        """Resolve a ledger-OPEN / broker-flat trade (startup D1 + hourly
        housekeeping drift recovery).

        2026-07-06 incident: the symbol-scoped sweep silently missed a GTC
        TP resting on the OLD contract symbol after an MGC->GC instance
        reconfiguration (naked-short trap a human had to defuse), and the
        close was written without an exit price. Order of operations:
        cancel the ledger row's exact bracket ids with the symbol-blind
        targeted primitive FIRST, then the bulk sweep as belt-and-braces;
        recover the true exit leg/price from broker executions; anything
        still unaccounted is a live orphan hazard — ERROR + Telegram,
        never a debug-swallow.

        Returns ``(reason, price)``: ("TP_HIT_OOB"/"SL_HIT_OOB", float)
        on a matched execution, ("CLOSED_OOB_UNRECOVERED", None)
        otherwise — the housekeeping drift branch resets position state
        with the TRUTHFUL reason (cooldown parity with the legacy path).
        """
        expected_ids = [
            oid for oid in (tp_order_id, sl_order_id) if oid is not None
        ]

        # (a) Targeted symbol-blind cancel of the exact protective ids.
        cancelled_by_id = 0
        try:
            if expected_ids:
                cancelled_by_id = self.exec_client.cancel_orders_by_ids(
                    expected_ids,
                )
                if cancelled_by_id:
                    log.info(
                        "[RECOVERY] Cancelled %d protective order(s) by id "
                        "after OOB close: %s",
                        cancelled_by_id, expected_ids,
                    )
        except Exception:
            log.error(
                "[RECOVERY] Targeted cancel of protective orders %s FAILED",
                expected_ids, exc_info=True,
            )

        # Belt-and-braces: the existing symbol-scoped sweep (A8: the bulk
        # primitive and its other call sites are untouched).
        bulk_cancelled = 0
        try:
            bulk_cancelled = self.exec_client.cancel_open_orders(
                symbol=self._execution_symbol,
            )
            if bulk_cancelled > 0:
                log.info(
                    "[RECOVERY] Cancelled %d orphaned %s order(s) "
                    "after OOB close",
                    bulk_cancelled, self._execution_symbol,
                )
        except Exception:
            log.error(
                "[RECOVERY] Symbol-scoped sweep failed after OOB close",
                exc_info=True,
            )

        # (b) Match broker executions to the protective order ids to learn
        # which leg actually filled. symbol=None on purpose: after a
        # contract reconfiguration the fill may live on the OLD symbol —
        # order ids are the join key (str/int-robust: ledger ids are ints,
        # execution records carry str order ids).
        executions = []
        try:
            executions = self.exec_client.get_executions() or []
        except Exception:
            log.error(
                "[RECOVERY] get_executions failed — cannot recover the OOB "
                "exit price", exc_info=True,
            )

        exit_reason = "CLOSED_OOB_UNRECOVERED"
        exit_price = None
        matched = []
        for rec in executions:
            rec_oid = str(rec.get("order_id"))
            if tp_order_id is not None and rec_oid == str(tp_order_id):
                exit_reason = "TP_HIT_OOB"
                exit_price = rec.get("price")
                matched.append(rec)
            elif sl_order_id is not None and rec_oid == str(sl_order_id):
                exit_reason = "SL_HIT_OOB"
                exit_price = rec.get("price")
                matched.append(rec)

        if exit_reason == "CLOSED_OOB_UNRECOVERED":
            # (c) Day boundary / no matching execution: exit price stays
            # NULL — an explicit unknown, never a fabricated price.
            log.warning(
                "[RECOVERY] No broker execution matches trade %s brackets "
                "(tp=%s sl=%s) — closing CLOSED_OOB_UNRECOVERED with NULL "
                "exit price",
                trade_id, tp_order_id, sl_order_id,
            )
        else:
            log.info(
                "[RECOVERY] OOB exit recovered for trade %s: %s @ %s",
                trade_id, exit_reason, exit_price,
            )
        self.telemetry.close_position(
            trade_id,
            reason=exit_reason,
            close_time=self._utc_iso_now(),
            exit_price=exit_price,
        )

        self._book_recovered_executions(matched)

        # An expected protective order neither found open (cancelled) nor
        # provably done (matched execution) is a live orphan hazard — the
        # exact 2026-07-06 failure was silent by construction (success
        # logged only if cancelled > 0, except -> log.debug).
        unaccounted = (
            len(expected_ids) - len(matched) - cancelled_by_id - bulk_cancelled
        )
        if unaccounted > 0:
            log.error(
                "[RECOVERY] %d protective order(s) of trade %s UNACCOUNTED "
                "(tp=%s sl=%s): neither found open nor matched to a broker "
                "execution — possible live orphan on another contract. "
                "Verify and cancel manually in TWS.",
                unaccounted, trade_id, tp_order_id, sl_order_id,
            )
            try:
                self._telegram.send(
                    f"[CRITICAL] *ORPHANED PROTECTIVE ORDER RISK*\n"
                    f"Trade: `{trade_id}` (closed out-of-band)\n"
                    f"TP order: `{tp_order_id}` / SL order: `{sl_order_id}`\n"
                    f"`{unaccounted}` order(s) neither cancelled nor filled "
                    f"— check TWS for orders resting on an OLD contract."
                )
            except Exception:
                pass  # Never let Telegram failure block recovery

        return exit_reason, exit_price

    def _book_recovered_executions(self, records) -> None:
        """Book recovered broker fills as tradebook rows.

        Shared by startup OOB recovery and the hourly housekeeping
        ledger repair. A5: event_ids are keyed on broker execId /
        order-id+permId — NOT the timestamp-based _build_event_id — so
        repeated restarts AND hourly sweeps dedupe via INSERT OR IGNORE
        (byte-stable across wall clocks).
        """
        for rec in records:
            exec_id = rec.get("exec_id")
            perm_id = rec.get("perm_id")
            fill_event_id = (
                f"EXECUTION_FILL_{exec_id}" if exec_id
                else f"EXECUTION_FILL_{rec.get('order_id')}_{perm_id}"
            )
            try:
                self.telemetry.log_tradebook_event(
                    event_id=fill_event_id,
                    event_type="EXECUTION_FILL",
                    event_timestamp_utc=str(
                        rec.get("time") or self._utc_iso_now()
                    ),
                    order_id=rec.get("order_id"),
                    perm_id=perm_id,
                    broker_execution_id=exec_id,
                    symbol=rec.get("symbol"),
                    local_symbol=rec.get("symbol"),
                    contract_month=self._front_month_str,
                    side=rec.get("side"),
                    action=rec.get("side"),
                    status="FILLED",
                    avg_fill_price=rec.get("price"),
                    last_fill_price=rec.get("price"),
                    fill_qty=rec.get("qty"),
                    **self._base_tradebook_fields(),
                )
            except Exception:
                log.warning(
                    "[RECOVERY] EXECUTION_FILL tradebook write failed "
                    "(order %s)", rec.get("order_id"), exc_info=True,
                )
            report = rec.get("commission_report")
            if report is not None and exec_id:
                commission = (
                    report.get("commission") if isinstance(report, dict)
                    else getattr(report, "commission", None)
                )
                realized = (
                    report.get("realizedPNL") if isinstance(report, dict)
                    else getattr(report, "realizedPNL", None)
                )
                try:
                    # Same COMMISSION_<execId> event id as the live
                    # commission bridge (_on_commission_event) — a later
                    # live report for the same execId dedupes.
                    self.telemetry.log_tradebook_event(
                        event_id=f"COMMISSION_{exec_id}",
                        event_type="COMMISSION",
                        event_timestamp_utc=self._utc_iso_now(),
                        order_id=rec.get("order_id"),
                        broker_execution_id=exec_id,
                        symbol=rec.get("symbol"),
                        commission=commission,
                        realized_pnl=realized,
                    )
                except Exception:
                    log.warning(
                        "[RECOVERY] COMMISSION tradebook write failed "
                        "(execId=%s)", exec_id, exc_info=True,
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
        if ibkr_pos == 0:
            # About to treat any resting orders as orphans and cancel them. A
            # stale post-reconnect cache could read flat while a real position
            # (with legit protective orders) exists — CONFIRM settled before
            # cancelling, and fail closed if it can't be confirmed
            # (reconnect-false-flat-oob).
            settled = self._confirm_settled_position(self._execution_symbol)
            if settled is None:
                log.error(
                    "[STARTUP SWEEP] flat read could not be confirmed (settled "
                    "snapshot failed) — NOT cancelling any orders (fail-closed)."
                )
                return
            ibkr_pos = settled
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

    # ------------------------------------------------------------------
    # Hourly order housekeeping (hourly-order-housekeeping_07072026_0435)
    # ------------------------------------------------------------------

    def _run_hourly_housekeeping(self) -> None:
        """Hourly broker-vs-ledger housekeeping sweep (~:15 wall clock).

        Invoked UNCONDITIONALLY on every event-loop poll and self-gated
        here (A-7): fires once per (date, hour) slot at minute >= 15 —
        after the :00 signal bar and the :06 read-only fleet monitor.
        Never raises: a housekeeping failure must leave trading
        untouched (same boundary contract as emit_crash_event); any
        internal failure surfaces as a housekeeping-error health event.
        """
        try:
            now = datetime.now(timezone.utc)
            if now.minute < _HOUSEKEEPING_MINUTE:
                # Latch untouched: consuming the slot early would skip
                # this hour's real :15 sweep.
                return
            slot = (now.date(), now.hour)
            if slot == self._last_housekeeping_slot:
                return
            # Latch BEFORE the sweep body (same pattern as
            # _last_rollover_check_date): a crashing sweep runs once per
            # slot instead of hot-looping on every 5s poll.
            self._last_housekeeping_slot = slot

            # Silent skips — states where a broker/ledger comparison lies.
            if not self.exec_client.is_connected():
                return
            if self._rollover_in_progress or self._emergency_halt:
                return

            start = time.monotonic()
            with self._ledger_lock:
                actions, alerts, aborted = self._housekeeping_sweep(now)
            elapsed = time.monotonic() - start

            if elapsed > _HOUSEKEEPING_BUDGET_SECONDS:
                # A-3: the sweep shares the event loop with bar handling —
                # a slow sweep is a health problem even when it succeeds.
                self._emit_health_event(
                    "housekeeping-error",
                    f"slow housekeeping sweep: {elapsed:.1f}s (budget "
                    f"{_HOUSEKEEPING_BUDGET_SECONDS:.0f}s) — the event "
                    f"loop was blocked for the duration",
                )

            if alerts:
                # A-3: ALL findings of one sweep batch into ONE send.
                self._telegram.send(
                    f"*HOUSEKEEPING ({self._execution_symbol})* — "
                    f"{len(alerts)} finding(s) need a human:\n"
                    + "\n".join(f"- {a}" for a in alerts)
                )

            log.info(
                "[HOUSEKEEPING] %02d:%02d sweep%s: %d auto-clean "
                "action(s), %d detect-only alert(s)",
                now.hour, now.minute,
                " ABORTED (mid-sweep disconnect)" if aborted else "",
                len(actions), len(alerts),
            )
        except Exception as exc:
            self._emit_health_event(
                "housekeeping-error",
                f"housekeeping sweep failed: {exc!r}",
            )
            log.exception("[HOUSEKEEPING] sweep failed")

    def _housekeeping_sweep(self, now) -> tuple:
        """One housekeeping pass (caller holds ``_ledger_lock``).

        A-2: broker access is restricted to LOCAL-CACHE primitives
        (get_cached_position / get_open_trades / get_executions) plus
        the targeted cancel_orders_by_ids — an ensure_connected-routed
        call here could block on a reconnect under _ledger_lock and
        deadlock the event loop via a re-entrant bar callback.
        is_connected() is re-checked before EACH broker touch; any
        mid-sweep disconnect abandons the remainder. The one sanctioned
        exception is _recover_oob_close (drift recovery reuses the
        startup path so reason/price recovery stays single-sourced).

        Returns ``(actions, alerts, aborted)``: auto-clean summaries,
        detect-only alert lines (batched into one Telegram by the
        caller), and whether the sweep aborted on a disconnect.
        """
        actions: list = []
        alerts: list = []
        aborted = (actions, alerts, True)

        now_naive = now.replace(tzinfo=None)

        if not self.exec_client.is_connected():
            return aborted
        position = int(
            self.exec_client.get_cached_position(self._execution_symbol)
        )

        if not self.exec_client.is_connected():
            return aborted
        # symbol=None on purpose (A-10): orphans can rest on an OLD
        # contract symbol after an instrument reconfiguration.
        resting = list(self.exec_client.get_open_trades(None) or [])

        if not self.exec_client.is_connected():
            return aborted
        executions = list(self.exec_client.get_executions() or [])

        # Ledger reads are local SQLite — no broker connection involved.
        open_row = self.telemetry.get_open_position()
        recent_closed = list(
            self.telemetry.get_recent_closed_positions() or []
        )

        pending_id = self._pending_entry_order_id
        # A-4: ids tracked in LIVE state are never cancellable no matter
        # which CLOSED row they match — TWS id-sequence resets reuse ids.
        live_ids = {str(oid) for oid in self._tp_order_ids if oid is not None}
        if self._sl_order_id is not None:
            live_ids.add(str(self._sl_order_id))
        if pending_id is not None:
            live_ids.add(str(pending_id))

        # str/int-robust join keys: ledger ids are ints, broker events
        # carry str order ids.
        closed_bracket_ids = {}
        for row in recent_closed:
            for key in ("tp_order_id", "sl_order_id"):
                oid = row.get(key)
                if oid is not None:
                    closed_bracket_ids.setdefault(str(oid), row)

        # ── (a) Orphaned protective orders — auto-clean ──────────────
        # ALL preconditions required: provably flat everywhere AND the
        # resting id matches a recent CLOSED row's own bracket ids.
        if (self._active_trade_id is None and pending_id is None
                and position == 0):
            orphan_ids = [
                evt.order_id for evt in resting
                if str(evt.order_id) not in live_ids
                and str(evt.order_id) in closed_bracket_ids
            ]
            if orphan_ids:
                if not self.exec_client.is_connected():
                    return aborted
                cancelled = self.exec_client.cancel_orders_by_ids(orphan_ids)
                detail = (
                    f"cancelled {cancelled} orphaned protective order(s) "
                    f"matching recent CLOSED brackets: {orphan_ids} — "
                    f"this class previously rested until the next restart"
                )
                self._emit_health_event(
                    "housekeeping-orphan-cancelled", detail)
                actions.append(detail)
                log.info("[HOUSEKEEPING] %s", detail)

        # ── (b) Broker-vs-ledger drift — auto-clean with A-5 grace ───
        if open_row is not None and position == 0:
            drift_trade_id = open_row.get("trade_id")
            row_ids = {
                str(open_row.get(k))
                for k in ("entry_order_id", "tp_order_id", "sl_order_id")
                if open_row.get(k) is not None
            }
            grace = timedelta(minutes=FILL_PRICE_GRACE_MINUTES)
            recent_activity = False
            for rec in executions:
                if str(rec.get("order_id")) not in row_ids:
                    continue
                rec_time = self._naive_utc_exec_time(rec.get("time"))
                if rec_time is not None and now_naive - rec_time <= grace:
                    recent_activity = True
                    break
            if recent_activity:
                # A-5: a fill callback may be in flight — acting now
                # would double-close. Note it; next sweep acts if the
                # drift persists.
                detail = (
                    f"ledger trade {drift_trade_id} OPEN while broker "
                    f"cache is flat, but its orders show fill activity "
                    f"within the last {FILL_PRICE_GRACE_MINUTES} min — "
                    f"detect-only this sweep"
                )
                self._emit_health_event(
                    "housekeeping-drift-detected", detail)
                log.warning("[HOUSEKEEPING] %s", detail)
            else:
                detail = (
                    f"ledger trade {drift_trade_id} OPEN while broker "
                    f"cache is flat — recovering via the OOB close path"
                )
                self._emit_health_event(
                    "housekeeping-drift-detected", detail)
                log.warning("[HOUSEKEEPING] %s", detail)
                reason, price = self._recover_oob_close(
                    trade_id=drift_trade_id,
                    tp_order_id=open_row.get("tp_order_id"),
                    sl_order_id=open_row.get("sl_order_id"),
                )
                # Truthful recovered reason → strategy cooldown parity
                # with the legacy broker-flat path.
                self._reset_position_state(reason=reason)
                detail = (
                    f"drift on trade {drift_trade_id} recovered: "
                    f"{reason} @ {price}"
                )
                actions.append(detail)
                log.info("[HOUSEKEEPING] %s", detail)

        # ── (c) Ledger repair — A-1(b) whitelist only ────────────────
        exec_by_oid: dict = {}
        for rec in executions:
            exec_by_oid.setdefault(str(rec.get("order_id")), []).append(rec)

        for row in recent_closed:
            if row.get("close_reason") not in _HOUSEKEEPING_OVERWRITE_REASONS:
                continue  # TP_HIT/SL_HIT/... rows are NEVER touched
            matched = []
            upgraded_reason = None
            # Only the row's OWN bracket ids may prove its exit.
            for key, upgraded in (("tp_order_id", "TP_HIT_OOB"),
                                  ("sl_order_id", "SL_HIT_OOB")):
                oid = row.get(key)
                if oid is None:
                    continue
                recs = exec_by_oid.get(str(oid))
                if recs:
                    matched = recs
                    upgraded_reason = upgraded
                    break
            if not matched:
                continue  # no proof → leave as-is, never synthesize
            proven_price = matched[0].get("price")
            updated = self.telemetry.repair_closed_position(
                row.get("trade_id"),
                exit_price=proven_price,
                reason=upgraded_reason,
                allow_overwrite_reasons=_HOUSEKEEPING_OVERWRITE_REASONS,
            )
            if not updated:
                continue  # row already truthful — idempotent re-run
            self._book_recovered_executions(matched)
            detail = (
                f"repaired CLOSED trade {row.get('trade_id')}: "
                f"{row.get('close_reason')} @ {row.get('exit_price')} -> "
                f"{upgraded_reason} @ {proven_price} (proven broker fill)"
            )
            self._emit_health_event("housekeeping-ledger-repaired", detail)
            actions.append(detail)
            log.info("[HOUSEKEEPING] %s", detail)

        # ── Protective-leg verify + heal (operator-authorized 2026-07-08,
        #    ticket unprotected-leg-verification_07082026_0315) ──────────
        # A genuinely-missing SL/TP is re-placed from the ledger's current
        # prices; a healthy position is never touched (idempotent). Fail-closed
        # guards keep the auto-placement bounded: an empty broker cache
        # (possible post-reconnect staleness), an active rate-limit halt, or a
        # mid-sweep disconnect all defer to detect-only rather than churn.
        # UNTRACKED (position with no ledger row) stays human-only.
        if position != 0 and pending_id is None:
            if open_row is None:
                detail = (
                    f"UNTRACKED position: broker position {position:+d} "
                    f"with no ledger trade — needs human adjudication"
                )
                self._emit_health_event(
                    "housekeeping-untracked-position", detail)
                alerts.append(detail)
                # An untracked position cannot be healed (no ledger prices);
                # if it also has no resting stop it is doubly a human concern.
                if not any(self._event_order_type(evt) == "STP"
                           for evt in resting):
                    naked = (
                        f"NAKED position: broker position {position:+d} with "
                        f"no resting stop order — protection needs a human"
                    )
                    self._emit_health_event(
                        "housekeeping-naked-position", naked)
                    alerts.append(naked)
            else:
                resting_ids = {str(evt.order_id) for evt in resting}
                sl_id = open_row.get("sl_order_id")
                tp_id = open_row.get("tp_order_id")
                sl_ok = sl_id is not None and str(sl_id) in resting_ids
                tp_ok = tp_id is not None and str(tp_id) in resting_ids
                trade_id = open_row.get("trade_id")

                if sl_ok and tp_ok:
                    pass  # both legs resting on the broker — nothing to do
                elif not resting:
                    # Broker shows ZERO open orders while we hold a position —
                    # the openTrades cache may be transiently stale (e.g. right
                    # after a reconnect). Do NOT churn; re-check next sweep.
                    detail = (
                        f"protective leg(s) missing for {trade_id} but broker "
                        f"shows ZERO open orders — possible stale cache, "
                        f"deferring heal (needs a human if it persists)"
                    )
                    self._emit_health_event("housekeeping-naked-position", detail)
                    alerts.append(detail)
                elif getattr(self, "_emergency_halt", False):
                    detail = (
                        f"protective leg(s) missing for {trade_id} but the "
                        f"order rate-limit HALT is active — cannot heal, "
                        f"needs a human"
                    )
                    self._emit_health_event("housekeeping-naked-position", detail)
                    alerts.append(detail)
                elif not self.exec_client.is_connected():
                    detail = (
                        f"protective leg(s) missing for {trade_id} but the "
                        f"broker session is disconnected — deferring heal"
                    )
                    self._emit_health_event("housekeeping-naked-position", detail)
                    alerts.append(detail)
                else:
                    status = self._verify_and_heal_protective_legs(
                        trade_id=trade_id,
                        tp_order_id=tp_id,
                        sl_order_id=sl_id,
                        tp_price=open_row.get("tp_price"),
                        sl_price=open_row.get("sl_price"),
                        quantity=abs(position),
                        position_side=1 if position > 0 else -1,
                    )
                    if status == "healed":
                        detail = (
                            f"HEALED protective legs for {trade_id} "
                            f"(pos {position:+d}): re-placed TP/SL from ledger "
                            f"— new SL id={self._sl_order_id}"
                        )
                        self._emit_health_event(
                            "housekeeping-protective-leg-healed", detail)
                        actions.append(detail)
                        log.error("[HOUSEKEEPING] %s", detail)
                    elif status == "verified":
                        pass  # heal's own re-query found both — snapshot was stale
                    else:
                        detail = (
                            f"protective leg(s) missing for {trade_id} and the "
                            f"heal could NOT re-place them ({status}) — position "
                            f"UNPROTECTED, needs a human"
                        )
                        self._emit_health_event(
                            "housekeeping-naked-position", detail)
                        alerts.append(detail)

        if pending_id is not None:
            filled = self._pending_entry_filled_qty()
            if filled > 0:
                # A1 (039208d): a partial fill means contracts EXIST
                # broker-side — cancelling would hide a live position.
                detail = (
                    f"AMBIGUOUS pending entry {pending_id}: partially "
                    f"filled ({filled:.0f}) — leaving for the fill path"
                )
                self._emit_health_event("housekeeping-ambiguous", detail)
                alerts.append(detail)

        # A-6: unknown bot-origin orders while provably flat. Live ids
        # (including the pending entry — entry orders legitimately rest
        # while flat) and recent CLOSED brackets (the orphan class,
        # handled above) are excluded.
        if (self._active_trade_id is None and open_row is None
                and position == 0):
            unknown_ids = [
                str(evt.order_id) for evt in resting
                if str(evt.order_id) not in live_ids
                and str(evt.order_id) not in closed_bracket_ids
            ]
            if unknown_ids:
                detail = (
                    f"UNKNOWN resting order(s) while flat: "
                    f"{', '.join(unknown_ids)} — matching neither live "
                    f"state, recent CLOSED brackets, nor a pending entry; "
                    f"detect-only (possibly human-placed)"
                )
                self._emit_health_event(
                    "housekeeping-unknown-order", detail)
                alerts.append(detail)

        return actions, alerts, False

    @staticmethod
    def _event_order_type(evt):
        """Broker order type ("LMT"/"STP"/...) off a StandardExecutionEvent."""
        order = getattr(getattr(evt, "raw_event", None), "order", None)
        return getattr(order, "orderType", None)

    @staticmethod
    def _naive_utc_exec_time(value):
        """Coerce a get_executions record ``time`` to naive-UTC.

        The record contract delivers naive-UTC datetimes; tz-aware and
        ISO-string forms are tolerated. Unknown shapes → None (the A-5
        grace then treats the record as non-recent, matching the legacy
        broker-flat path's behavior when no timing evidence exists).
        """
        # Local import: the module-level `datetime` name is the
        # injectable clock seam — isinstance needs the real class.
        from datetime import datetime as _dt
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = _dt.fromisoformat(value)
            except ValueError:
                return None
        if not isinstance(value, _dt):
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

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

        ctx = self._last_decision_context_by_order_id[order_id]
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

        # Compute bracket prices from fill price.
        # S7 (T3): snap every child price to the instrument grid — the
        # naked-stop site (a rejected TP/SL child after a filled entry is a
        # naked position). CL stays bit-identical to the legacy
        # round(fill ± offset, 2) via the power-of-ten fast path.
        tick = self._tick_size
        exit_action = "SELL" if entry_action == "BUY" else "BUY"
        if entry_action == "BUY":
            sl_price = round_to_tick(fill_price - sl_offset, tick)
            if tiered_tp_offsets:
                tp_price = []
                rem_lots = lots
                for i, (pct, off) in enumerate(tiered_tp_offsets):
                    t_lots = rem_lots if i == len(tiered_tp_offsets) - 1 else max(1, int(round(lots * pct)))
                    t_lots = min(t_lots, rem_lots)
                    if t_lots > 0:
                        tp_price.append((t_lots, round_to_tick(fill_price + off, tick)))
                    rem_lots -= t_lots
            else:
                tp_price = round_to_tick(fill_price + tp_offset, tick)
        else:  # SELL (short)
            sl_price = round_to_tick(fill_price + sl_offset, tick)
            if tiered_tp_offsets:
                tp_price = []
                rem_lots = lots
                for i, (pct, off) in enumerate(tiered_tp_offsets):
                    t_lots = rem_lots if i == len(tiered_tp_offsets) - 1 else max(1, int(round(lots * pct)))
                    t_lots = min(t_lots, rem_lots)
                    if t_lots > 0:
                        tp_price.append((t_lots, round_to_tick(fill_price - off, tick)))
                    rem_lots -= t_lots
            else:
                tp_price = round_to_tick(fill_price - tp_offset, tick)

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
            # A3: _entry_price is seeded from the fill by the caller
            # (_on_standard_execution_event) BEFORE children are placed —
            # state must not be contingent on bracket-children success.

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
        """Print a per-symbol account summary at startup.

        T6 m3 cosmetic: banner TEXT is derived from the execution symbol
        (byte-identical for CL). The account-summary dict KEYS stay cl_*
        for every symbol pending the m2 rename micro-ticket.
        """
        w = 60  # box width
        try:
            sym = self._execution_symbol
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
            f"ACCOUNT SUMMARY ({sym} Only)".center(w),
            "=" * w,
            f"  Account:           {acct['account'] or 'N/A'}",
            f"  Net Liquidation:   ${acct['net_liquidation']:>14,.2f}",
            f"  Available Funds:   ${acct['available_funds']:>14,.2f}",
            "-" * w,
            f"  {sym} Position:       {pos_str}",
            f"  {sym} Market Value:   ${acct['cl_market_value']:>14,.2f}",
            f"  {sym} Avg Cost:       ${acct['cl_avg_cost']:>14,.2f}",
            f"  {sym} Unrealized PnL: ${acct['cl_unrealized_pnl']:>14,.2f}",
            f"  {sym} Realized PnL:   ${acct['cl_realized_pnl']:>14,.2f}",
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
        # T7: hourly-only instances skip the 5m warm start ENTIRELY — no 5m
        # seed/cache is required (rolling_df_5m / _last_bar_time_5m stay
        # None). The getattr default mirrors the flag's default-true
        # semantics for object.__new__ test stubs that predate the flag.
        if getattr(self, "_enable_5m_stream", True):
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

            # seedless-5m-live-stream_07052026_0546: latch + loudly banner
            # the shallow bootstrap (log surface of the 3-surface
            # discipline; the startup Telegram Mode stamp reads this flag).
            self._shallow_5m_bootstrap = bool(
                getattr(self.data_manager_5m, "shallow_bootstrapped", False)
            )
            if self._shallow_5m_bootstrap:
                log.warning(
                    "SHALLOW 5M MODE: no 5m seed/cache existed — window "
                    "bootstrapped from IBKR (%d bars). Run 2+ warm-starts "
                    "from the saved cache.",
                    len(self.rolling_df_5m),
                )
        else:
            log.info(
                "HOURLY-ONLY MODE: 5m warm start skipped "
                "(enable_5m_stream=false — no 5m seed required)."
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
            _min_required = {
                "1h": REQUIRED_1H_BARS,
                "2h": REQUIRED_1H_BARS,
                "4h": REQUIRED_1H_BARS,
            }
            _required = _min_required.get(self._bar_size, 0)
            if len(self.rolling_df_1h) < _required:
                # T5: cache-name-derived so non-CL messages name the REAL
                # cache file. CL's cache IS warm_start_cache_1h.parquet ->
                # byte-identical legacy text (pinned).
                err_msg = (
                    f"1H cache has only {len(self.rolling_df_1h)} bars — "
                    f"need {_required} for {self._bar_size} MACRO_6M feature warmup. "
                    f"Delete {self.data_manager_1h.cache_path.name} to trigger reseed."
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
                return_last_n=N,
                # T4: instrument only resolved when the feature list needs
                # external macro data (non-macro configs never touch the
                # instrument seam here).
                instrument=self._brain_instrument if self._needs_macro else None,
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

    @property
    def _brain_symbol(self) -> str:
        """Brain-stream symbol (T2).

        Prefers the resolved InstrumentContext (always set by __init__).
        Falls back to the SAME structural derivation the resolver uses
        (micro -> parent contract, outright -> itself) for test stubs
        built via object.__new__ that set only _execution_symbol (e.g.
        tests/test_cooldown.py). This is structural derivation, NOT a
        silent CL default — unknown symbols raise via get_instrument.
        """
        ctx = getattr(self, "_instrument_context", None)
        if ctx is not None:
            return ctx.brain_symbol
        from src.core.instrument_master import get_instrument
        return get_instrument(self._execution_symbol).micro_of or self._execution_symbol

    @property
    def _brain_instrument(self) -> "Instrument":
        """Brain-stream registry Instrument (T4).

        Drives the live macro path: the startup index daily-close fetch
        list, the MacroFeatureEngine's per-symbol FRED/COT files, and the
        buildable MACRO_*/COT_* name set (an MCL config uses CL's macro
        set — macro features feed the MODEL, which is brain-keyed).

        Prefers the resolved InstrumentContext (always set by __init__).
        Falls back to the SAME structural derivation the resolver uses
        (micro -> parent contract, outright -> itself, via _brain_symbol)
        for test stubs built with object.__new__ that set only
        _execution_symbol. This is structural derivation, NOT a silent CL
        default — unknown symbols raise ValueError via get_instrument and
        a missing seam raises AttributeError naming _execution_symbol.
        """
        ctx = getattr(self, "_instrument_context", None)
        if ctx is not None:
            return ctx.brain_instrument
        from src.core.instrument_master import get_instrument
        return get_instrument(self._brain_symbol)

    @property
    def _execution_instrument(self) -> "Instrument":
        """Execution-instrument registry entry (T6 m1 display seam).

        Prefers the resolved InstrumentContext (always set by __init__).
        Falls back to the registry via _execution_symbol for test stubs
        built with object.__new__ — the same structural derivation as
        _tick_size/_brain_instrument, NOT a silent default: unknown
        symbols raise via get_instrument and a missing seam raises
        AttributeError naming _execution_symbol.
        """
        ctx = getattr(self, "_instrument_context", None)
        if ctx is not None:
            return ctx.execution_instrument
        from src.core.instrument_master import get_instrument
        return get_instrument(self._execution_symbol)

    @property
    def _tick_size(self) -> float:
        """Execution-instrument tick size for order-price snapping (T3).

        Prefers the resolved InstrumentContext (always set by __init__).
        Orders are placed on the EXECUTION contract, so this is the
        execution instrument's tick, never the brain's (micros share the
        parent tick in the registry, so MCL/MES are unaffected either way).
        Falls back to the registry via _execution_symbol for test stubs
        built with object.__new__ — the same structural derivation as
        _brain_symbol, NOT a silent default: unknown symbols raise via
        get_instrument and a missing seam raises AttributeError.
        """
        ctx = getattr(self, "_instrument_context", None)
        if ctx is not None:
            return ctx.execution_instrument.tick_size
        from src.core.instrument_master import get_instrument
        return get_instrument(self._execution_symbol).tick_size

    def _subscribe(self) -> None:
        """Subscribe to live bars (Brain streams).

        T2 (constraint 3): Brain streams subscribe the BRAIN symbol's
        continuous contract (an MCL config's brain is CL); the Hands
        stream (_subscribe_front_month) stays on the execution symbol.
        """
        # T7: hourly-only instances never subscribe the continuous 5m brain
        # stream (the front-month Hands stream is untouched — it is a
        # separate, seed-free subscription via _subscribe_front_month).
        if getattr(self, "_enable_5m_stream", True):
            log.info("Subscribing to live 5-min bars (Stream A)...")
            self._live_bars_5m = self.data_client.subscribe_live_bars(
                symbol=self._brain_symbol,
                continuous=True,
                bar_size="5 mins",
                duration_str="60 S",
            )
            self._live_bars_5m.updateEvent += self._on_bar_update_5m
            log.info("Subscribed to 5-min continuous contract live bars")
        else:
            log.info(
                "HOURLY-ONLY MODE: skipping 5m brain subscription "
                "(enable_5m_stream=false)."
            )

        if self._bar_size in ("1h", "2h", "4h"):
            log.info("Subscribing to live 1-hour bars (Stream B)...")
            self._live_bars_1h = self.data_client.subscribe_live_bars(
                symbol=self._brain_symbol,
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

                # Reset internal position tracking — a REAL close of a
                # filled position: full reset incl. strategy.on_exit (A2).
                self._reset_position_state(reason="ROLLOVER")
                self._clear_pending_entry()

                self._telegram.send(
                    f"*CONTRACT ROLLOVER*\n"
                    f"`{old_sym}` → `{new_local_sym}`\n\n"
                    f"[WARNING] Force-closed position ({current_position:+d}) on "
                    f"expiring contract at market.\n"
                    f"Waiting for next natural signal on `{new_local_sym}`."
                )
            else:
                # D2.4/A2 scoping: a pending never-filled entry rests on the
                # EXPIRING contract — the same old-contract-orphan class as
                # the OOB incident. Cancel it while the tracked symbol still
                # matches and clear ONLY pending state (no fill ever existed,
                # so no cooldown may fire).
                if self._pending_entry_order_id is not None:
                    filled_qty = self._pending_entry_filled_qty()
                    if filled_qty > 0:
                        # A1: partially filled — contracts exist broker-side;
                        # never silently discard them.
                        log.error(
                            "Rollover: pending entry order %s is PARTIALLY "
                            "FILLED (filled=%.0f) — NOT clearing; the fill "
                            "path must adjudicate the broker-side position",
                            self._pending_entry_order_id, filled_qty,
                        )
                        try:
                            self._telegram.send(
                                f"[CRITICAL] *PARTIAL FILL AT ROLLOVER*\n"
                                f"Order: `{self._pending_entry_order_id}` "
                                f"filled `{filled_qty:.0f}` on expiring "
                                f"contract — manual verification required."
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            cancelled = self.exec_client.cancel_open_orders(
                                symbol=self._execution_symbol,
                            )
                            log.info(
                                "Rollover: cancelled %d pending entry "
                                "order(s) on expiring contract", cancelled,
                            )
                        except Exception as exc:
                            log.error(
                                "Rollover: pending-entry cancel failed: %s",
                                exc,
                            )
                        self._clear_pending_entry()
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
            if self.data_manager_5m is not None:  # T7: None in hourly-only
                self.data_manager_5m.front_month_id = new_local_sym
            if self.data_manager_1h is not None:
                self.data_manager_1h.front_month_id = new_local_sym

            # 4b. Persist the roll seam FIRST (crash-safe), then attempt
            # immediate resolution (jit-roll-ratio-empty_07102026_1453
            # Stage 2). The old handler updated front_month_id in memory
            # and persisted NOTHING — a restart between IBKR's CONTFUT
            # lead flip and the next startup lost the seam forever
            # (post-roll vs post-roll ratio ≈ 1 → tolerance-swallowed).
            if self.data_manager_1h is not None:
                try:
                    self.data_manager_1h.set_pending_roll(
                        old_sym, new_local_sym,
                    )
                except Exception:
                    log.critical(
                        "Rollover: FAILED to persist pending roll %s → %s — "
                        "the seam will be LOST if this process dies before "
                        "resolution succeeds.",
                        old_sym, new_local_sym, exc_info=True,
                    )
                # Force an immediate attempt: the new-1h-bar retry gate may
                # still hold the current bar from an earlier lifecycle.
                self._pending_roll_last_attempt_bar = _PENDING_ROLL_GATE_UNSET
                self._attempt_pending_roll_resolution()

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

    def _attempt_pending_roll_resolution(self) -> None:
        """Try to resolve the persisted pending roll seam, if one exists.

        jit-roll-ratio-empty_07102026_1453 (Stage 2): invoked from the
        event-loop poll body (OUTSIDE the heartbeat gate — that gate
        fires only every _HEARTBEAT_INTERVAL and skips missed ticks
        after a stall) and immediately after rollover
        detection. Self-gates to one resolve attempt per NEW 1h bar via
        data_manager_1h.last_timestamp (the CONTFUT basis only ever flips
        with new bars, so sub-bar retries are pure IBKR-pacing waste).

        Outcomes:
          - RESOLVED → clear the pending record; when a 5m manager exists
            (shared metadata file, same execution symbol) its seam is
            resolved too — the 5m cache otherwise keeps a silent basis
            break.
          - RETRY / ESCALATE → keep the pending record for the next 1h
            bar; quiet within the escalation deadline, then log.critical +
            Telegram with an operator remedy on every attempt (the record
            is NOT cleared — an operator must act).

        Gate state is lazily initialized (getattr default) and the whole
        body is defensive: this method must never raise into the poll
        loop.
        """
        try:
            dm_1h = getattr(self, "data_manager_1h", None)
            if dm_1h is None:
                return
            rec = dm_1h.get_pending_roll()
            if rec is None:
                return

            # Retry gate: one attempt per new 1h bar. Lazily initialized —
            # the sanctioned test harness builds via object.__new__ and
            # sets no private attrs.
            current_bar = dm_1h.last_timestamp
            last_attempt_bar = getattr(
                self, "_pending_roll_last_attempt_bar",
                _PENDING_ROLL_GATE_UNSET,
            )
            if (
                last_attempt_bar is not _PENDING_ROLL_GATE_UNSET
                and last_attempt_bar == current_bar
            ):
                return
            self._pending_roll_last_attempt_bar = current_bar

            outcome = dm_1h.resolve_roll_seam(
                from_contract=rec["from"],
                to_contract=rec["to"],
                detected_at=rec["detected_at"],
            )
            if outcome == ROLL_SEAM_RESOLVED:
                dm_5m = getattr(self, "data_manager_5m", None)
                if dm_5m is not None:
                    # Shared metadata file, same execution symbol — the 5m
                    # cache's seam must be captured too.
                    try:
                        outcome_5m = dm_5m.resolve_roll_seam(
                            from_contract=rec["from"],
                            to_contract=rec["to"],
                            detected_at=rec["detected_at"],
                        )
                        log.info(
                            "Pending roll: 5m manager seam scan → %s",
                            outcome_5m,
                        )
                    except Exception:
                        log.exception(
                            "Pending roll: 5m manager seam resolution "
                            "failed (1h seam already recorded)"
                        )
                dm_1h.clear_pending_roll()
                log.warning(
                    "PENDING ROLL RESOLVED: %s → %s (ratio recorded, "
                    "record cleared).", rec["from"], rec["to"],
                )
                return

            # RETRY / ESCALATE: keep the pending record for the next bar.
            log.info(
                "Pending roll %s → %s unresolved (outcome=%s) — will "
                "retry on the next 1h bar.",
                rec["from"], rec["to"], outcome,
            )
            # detected_at is a NAIVE local ISO stamp (DataManager
            # convention) — keep the deadline math naive-vs-naive.
            detected_dt = datetime.fromisoformat(rec["detected_at"])
            age = datetime.now() - detected_dt
            if age > _PENDING_ROLL_ESCALATION_DEADLINE:
                log.critical(
                    "PENDING ROLL UNRESOLVED past deadline: %s → %s "
                    "detected %s (%.1f h ago), latest outcome=%s. The roll "
                    "seam is still UNRECORDED — features are drifting onto "
                    "a broken price basis. Operator remedy: check the IBKR "
                    "CONTFUT stream; if the basis never flips, derive the "
                    "ratio manually from an expired-contract overlap fetch "
                    "and append it to roll_history, then clear the "
                    "pending_roll entry.",
                    rec["from"], rec["to"], rec["detected_at"],
                    age.total_seconds() / 3600.0, outcome,
                )
                try:
                    self._telegram.send(
                        f"[CRITICAL] *PENDING ROLL UNRESOLVED*\n"
                        f"`{rec['from']}` → `{rec['to']}` detected "
                        f"`{rec['detected_at']}` — unresolved for "
                        f"{age.days}d. The roll seam is still unrecorded; "
                        f"features are drifting onto a broken price basis.\n"
                        f"Remedy: verify the IBKR CONTFUT basis flip; if "
                        f"it never comes, record the ratio manually in "
                        f"roll\\_history and clear pending\\_roll."
                    )
                except Exception:
                    log.warning(
                        "Pending-roll escalation Telegram failed",
                        exc_info=True,
                    )
        except Exception:
            log.exception(
                "Pending-roll resolution attempt failed (record kept for "
                "the next retry)"
            )

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
        retry_scheduled = False
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
            # T2 (constraint 3): Brain streams = brain symbol continuous;
            # the Hands (front-month) stream below stays execution symbol.
            # T7: hourly-only instances skip the 5m brain resubscribe too.
            if getattr(self, "_enable_5m_stream", True):
                log.info("Subscribing to live 5-min bars (Stream A)...")
                self._live_bars_5m = await self.data_client.subscribe_live_bars_async(
                    symbol=self._brain_symbol,
                    continuous=True,
                    bar_size="5 mins",
                    duration_str="60 S",
                )
                self._live_bars_5m.updateEvent += self._on_bar_update_5m
                log.info("Subscribed to 5-min continuous contract live bars")
            else:
                log.info(
                    "HOURLY-ONLY MODE: skipping 5m brain resubscription "
                    "(enable_5m_stream=false)."
                )

            if self._bar_size == "1h":
                log.info("Subscribing to live 1-hour bars (Stream B)...")
                self._live_bars_1h = await self.data_client.subscribe_live_bars_async(
                    symbol=self._brain_symbol,
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
            self._resubscribe_retry_count = 0

            # 3. Backfill any gap from the disconnect period
            await self._backfill_reconnect_gap_async()

            log.info("Reconnection complete — live bars flowing again")

        except Exception:
            # 2026-07-06 incident: waiting for the "next reconnect" (a new
            # farm-OK event) left every child blind when the OK had already
            # fired during an IP-session conflict. Retry on a timer instead
            # (resubscribe-retry-blindness_07062026_0640).
            retry_count = getattr(self, "_resubscribe_retry_count", 0) + 1
            self._resubscribe_retry_count = retry_count
            if retry_count <= _MAX_RESUBSCRIBE_RETRIES:
                delay = min(
                    _RESUBSCRIBE_RETRY_BASE_SECONDS * (2 ** (retry_count - 1)),
                    _RESUBSCRIBE_RETRY_CAP_SECONDS,
                )
                log.exception(
                    "Deferred resubscription failed (attempt %d/%d) — "
                    "retrying in %ds (timer-based; no farm-OK event required)",
                    retry_count, _MAX_RESUBSCRIBE_RETRIES, delay,
                )
                self._schedule_resubscribe_retry(delay)
                retry_scheduled = True
            else:
                log.exception(
                    "Deferred resubscription failed — %d retries exhausted; "
                    "the stale-bar watchdog is the remaining backstop",
                    _MAX_RESUBSCRIBE_RETRIES,
                )
                self._emit_health_event(
                    "resubscribe-retries-exhausted",
                    f"Deferred resubscription failed {retry_count} times "
                    f"(base {_RESUBSCRIBE_RETRY_BASE_SECONDS}s, cap "
                    f"{_RESUBSCRIBE_RETRY_CAP_SECONDS}s) — child is blind "
                    f"until the stale-bar watchdog escalates.",
                )
        finally:
            # The guard stays up while a retry timer is outstanding so a
            # racing farm-OK event cannot double-schedule; cleared otherwise.
            self._resubscribe_pending = retry_scheduled

    def _schedule_resubscribe_retry(self, delay_seconds) -> None:
        """Arm an event-loop timer that re-enters _deferred_resubscribe.

        This is the seam the retry tests mock; kept tiny on purpose.
        """
        import asyncio
        loop = asyncio.get_event_loop()
        loop.call_later(
            delay_seconds,
            lambda: asyncio.ensure_future(self._deferred_resubscribe()),
        )

    def _emit_health_event(self, kind, detail) -> None:
        """Queue a non-crash health event into the fleet error queue.

        Alive-but-degraded states (stale-bar watchdog firings, exhausted
        resubscribe retries) were invisible to the hourly monitor — only
        process crashes reached the queue. Emission failure must never
        affect trading.
        """
        if not getattr(self, "_health_events_enabled", False):
            return  # live-path opt-in — see __init__
        try:
            from src.live_execution.fleet_error_events import (
                emit_child_health_event,
            )
            model = (
                getattr(getattr(self, "strategy", None), "name", None)
                or getattr(self, "_brain_symbol", None)
                or "unknown"
            )
            client_id = getattr(
                getattr(self, "telemetry", None), "client_id", None,
            )
            emit_child_health_event(
                model_name=model, client_id=client_id,
                kind=kind, detail=detail,
            )
        except Exception:
            log.warning(
                "Health-event emission failed (kind=%s)", kind, exc_info=True,
            )

    async def _backfill_reconnect_gap_async(self) -> None:
        """Backfill bars missed during a disconnect/hibernation gap.

        After reconnecting, detects the gap between the last known bar
        timestamp and now, requests historical bars from IBKR via the
        async API, and injects them into the rolling DataFrames and
        DataManager caches.

        This prevents phantom price spikes in rolling indicators when
        bars are missed due to connectivity loss, hibernation, etc.

        The fetch below carries NO symbol kwarg — the symbol is bound at
        adapter construction (T2, D1), which keeps this call compatible
        with the untouched SimulatedDataFeed.
        """
        # Gap reference must be tz-naive UTC — _last_bar_time_5m/_1h are
        # normalized tz-naive UTC, so pd.Timestamp.now() (LOCAL wall clock)
        # computes gap ≈ real_gap - utc_offset on non-UTC hosts and the
        # backfill never fires.  Matches the stale-bar watchdog
        # (_check_stale_bars).  Note: pd.Timestamp.utcnow() is tz-AWARE
        # (pandas 1.5.3) and would raise on comparison with the tz-naive
        # last-bar timestamps — the tz must be stripped.
        now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
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
                        # Drop the still-forming tail bar: an empty endDateTime
                        # makes IBKR return the currently-forming bar as the last
                        # row, and the stitch filter below only drops OLDER bars.
                        # Completeness is measured from the FETCH-literal bar-size
                        # ("5 min") — NOT self._bar_size (which may be 2h/4h and
                        # would mis-size the test) — against the already-computed
                        # tz-naive UTC `now`, so a not-yet-closed bar never enters
                        # the rolling window / cache nor advances _last_bar_time_5m
                        # (the live stream redelivers it when it completes).
                        chunk_df = chunk_df[(chunk_df.index + pd.Timedelta("5 min")) <= now]
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
                                f"*Reconnect Backfill (5M) Completed*\n"
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
                        # Drop the still-forming tail bar: an empty endDateTime
                        # makes IBKR return the currently-forming bar as the last
                        # row, and the stitch filter below only drops OLDER bars.
                        # Completeness is measured from the FETCH-literal bar-size
                        # ("1 hour") — NOT self._bar_size (2h/4h would mis-size the
                        # test) — against the already-computed tz-naive UTC `now`,
                        # so a not-yet-closed bar never enters the rolling window /
                        # cache nor advances _last_bar_time_1h (the live stream
                        # redelivers it as NEW 1H BAR + inference when it completes).
                        chunk_df = chunk_df[(chunk_df.index + pd.Timedelta("1 hour")) <= now]
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
                                f"*Reconnect Backfill (1H) Completed*\n"
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
        # A live brain-stream bar arrived — the watchdog's fruitless-
        # reconnect escalation counter resets to zero (R4).
        self._fruitless_reconnect_count = 0

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

        with self._ledger_lock:
            self._check_trailing_stop()

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
        # A live brain-stream bar arrived — the watchdog's fruitless-
        # reconnect escalation counter resets to zero (R4).
        self._fruitless_reconnect_count = 0

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

        # Check trailing stop on every 1h bar — bar-size agnostic.
        # In production, 5m bars already check via _on_bar_update_5m().
        # This ensures 1h-only paths (livetest, future bar sizes) also check.
        with self._ledger_lock:
            self._check_trailing_stop()

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
                macro_overrides=macro_overrides,
                # T4: instrument only resolved when the feature list needs
                # external macro data (non-macro configs never touch the
                # instrument seam here).
                instrument=self._brain_instrument if self._needs_macro else None,
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

        # Enforce 24-hour time barrier on any open position (engine safety
        # rail). On a barrier exit do NOT return early: the exit bar must
        # still be evaluated — the backtest evaluates every bar including
        # exit bars, and consecutive-signal/opposite-side-entry parity
        # depends on it (B(b)+F ticket, human-authorized 2026-07-03). The
        # exited side is gated by its own cooldown (exit-bar counter reads 0).
        self._check_time_barrier(
            bar_time=bar_time,
            current_price=current_price,
            atr_value=atr_value,
        )

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

            dp = _price_decimals(self._tick_size)
            tp_str = f"TP={tp_price_live:.{dp}f}" if tp_price_live else "TP=N/A"
            sl_str = f"SL={sl_price_live:.{dp}f}" if sl_price_live else "SL=N/A"
            atr_str = f"ATR={atr_value:.4f}" if atr_value else "ATR=N/A"

            try:
                # Use cached portfolio (sync) via the execution client adapter.
                acct_summary = self.exec_client.get_account_summary(symbol=self._execution_symbol)
                unrealized_pnl = float(acct_summary.get("cl_unrealized_pnl", 0.0))
                avg_cost = float(acct_summary.get("cl_avg_cost", 0.0))
                ibkr_mark = float(acct_summary.get("cl_market_price", 0.0))
                # Entry: OUR actual fill is the single source of truth.
                # IBKR averageCost includes commission (misleading on a
                # per-price display) — used only as a recovery fallback
                # when no in-memory fill is known. avgCost = price *
                # contract multiplier (registry-driven).
                _entry_fill = getattr(self, "_entry_price", None)
                if _entry_fill is not None:
                    entry_price = _entry_fill
                elif avg_cost:
                    entry_price = avg_cost / self._execution_instrument.multiplier
                else:
                    entry_price = 0.0
                # Mkt: IBKR's live mark (same source as unrealizedPnL) so
                # the line is internally consistent; bar close only as a
                # fallback, labeled so a frozen bar can't masquerade as a
                # live quote (telemetry-fill-commission_07062026_0640 R3).
                if ibkr_mark > 0:
                    mkt_price, mkt_src = ibkr_mark, "IBKR"
                else:
                    mkt_price, mkt_src = current_price, "bar"
                log.info(
                    "[PNL] position=%d  unrealizedPnL=$%.2f  "
                    "entryPrice=%.*f  mktPrice=%.*f (%s)  %s  %s  %s  held=%d bars",
                    current_position,
                    unrealized_pnl,
                    dp, entry_price,
                    dp, mkt_price, mkt_src,
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
                "DRY RUN — would place bracket order %s %d %s (prob=%.2f)",
                signal.action, signal.lots, self._execution_symbol,
                signal.probability,
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
            # D2.1: PLACEMENT stores a pending-entry record ONLY. In-position
            # state (_entry_price, _atr_at_entry, _position_side, extremes,
            # _position_entry_bar_time) belongs to the confirmed FILL: the
            # pre-fill state here ran trailing math off the submission price
            # on every trade and re-fired the trailing warning bar after bar
            # for unfilled GTC entries (NG order 19, 2026-07-06). Signal-time
            # ATR and per-trade overrides travel in the decision context
            # below; the fill callback seeds all in-position state from them.
            self._pending_entry_order_id = order_id
            self._pending_entry_bar_time = bar_time
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
                f"*Trade Entry*\n"
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
                # D2.1: fill-time seeding inputs — the side-specific ATR
                # from evaluate() (trailing-stop parity) and per-trade
                # overrides from tier matching (None = use global).
                "atr_at_entry": (
                    signal.atr_at_entry
                    if signal.atr_at_entry is not None else atr_value
                ),
                "trailing_atr_mult": signal.trailing_atr_mult,
                "max_hold_bars": signal.max_hold_bars,
            }
            self._last_decision_context_by_order_id[order_id] = decision_ctx
            # Register as a known ENTRY order so the fill handler routes it to
            # the entry branch (see UNRECOGNIZED FILL guard).
            self._entry_order_ids.add(str(order_id))
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
            self._telegram.send(f"*1-Hour Heartbeat*\n\n" + self._build_heartbeat_payload())

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
            # Telegram alert on FIRST attempt only — throttled
            # (watchdog-telegram-throttle_07062026_0007); the helper never
            # raises, so it can never block reconnection.
            if attempt == 1:
                self._send_watchdog_telegram(
                    f"*RECONNECT* - Connection lost, "
                    f"attempting recovery (max {_RECONNECT_MAX_ATTEMPTS} attempts)..."
                )
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
                # Block the async resubscription path (_deferred_resubscribe)
                # BEFORE connecting.  IBKR delivers 2104/2106 DURING the
                # connect handshake; with _subscriptions_lost=True those
                # fire _on_ib_error → _deferred_resubscribe, which races
                # the sync _resubscribe_and_backfill() call at the end of
                # this method — both paths subscribe all streams, cancel
                # each other's in-flight requests, and clobber the
                # _live_bars_* references.  The guard must already be True
                # when data_client.connect() runs; it is cleared at the end
                # of _resubscribe_and_backfill() on success and on the
                # failure paths below (so a later legitimate 2104 can still
                # schedule _deferred_resubscribe after _reconnect gives up).
                self._resubscribe_pending = True
                # Reconnect
                self.data_client.connect()
                self.exec_client.connect()
                # Re-register error handler (lost on disconnect).
                # Remove first to prevent stacking — ib_insync events
                # are simple lists and += appends without dedup.
                self._callbacks_registered = False
                self._register_execution_callbacks()

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
                    # Rate-limited Telegram: only every 3rd attempt to avoid
                    # spam, PLUS the hourly watchdog-family throttle
                    # (watchdog-telegram-throttle_07062026_0007); the helper
                    # never raises, so it can never block reconnection.
                    if attempt % 3 == 0:
                        self._send_watchdog_telegram(
                            f"*RECONNECT* - Attempt {attempt}/{_RECONNECT_MAX_ATTEMPTS}: "
                            f"Gateway connected but data farms broken (no upstream data)"
                        )
                    try:
                        self.data_client.disconnect()
                        self.exec_client.disconnect()
                    except Exception:
                        pass
                    # Failed attempt — release the guard so it cannot leak
                    # True past the loop and block the deferred 2104 path.
                    self._resubscribe_pending = False
                    delay = min(delay * 2, _RECONNECT_MAX_DELAY)
                    continue  # Skip resubscription — it would fail anyway

                # Resubscribe + backfill gaps
                self._subscriptions_lost = True
                self._resubscribe_and_backfill()
                log.info("Reconnected successfully on attempt %d", attempt)
                # Throttled send (watchdog-telegram-throttle_07062026_0007);
                # the helper never raises, so it can never block reconnection.
                self._send_watchdog_telegram(
                    f"*RECONNECTED* - Recovery successful on attempt {attempt}/{_RECONNECT_MAX_ATTEMPTS}"
                )
                return True
            except Exception as exc:
                log.warning("Reconnect attempt %d failed: %s", attempt, exc)
                # Failed attempt — release the guard so it cannot leak
                # True past the loop and block the deferred 2104 path.
                self._resubscribe_pending = False
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)
        return False

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def _event_loop(self) -> None:
        """Main event loop — uses ib.sleep() to avoid blocking the async IB connection."""
        log.info("Entering event loop (poll every %.1fs) ...", _POLL_INTERVAL)
        log.info("Press Ctrl+C to stop.")

        # Heartbeat: wall-clock anchored ticks every _HEARTBEAT_INTERVAL at
        # this child's phase offset (see the constant's comment). getattr:
        # Strict-Locked loop tests construct bare object.__new__ traders
        # without the attribute; 0.0 matches the standalone default.
        hb_offset = float(getattr(self, "_heartbeat_offset", 0.0))
        next_heartbeat = _initial_heartbeat_deadline(time.time(), hb_offset)

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
                    # Reconnect succeeded — resume normal polling. The
                    # heartbeat deadline is wall-clock anchored, so the
                    # child keeps its fleet-rotation slot across reconnects
                    # (the old poll_count reset re-phased it arbitrarily).
                    continue

                # Sleep the normal poll interval, shortened to wake AT the
                # heartbeat deadline (sub-poll firing precision).
                sleep_for = _heartbeat_sleep(time.time(), next_heartbeat)
                if hasattr(self.data_client, "sleep"):
                    self.data_client.sleep(sleep_for)
                else:
                    time.sleep(sleep_for)

                # Idle-loop settled reconciler (settle-confirm-event-loop). MUST
                # run BEFORE housekeeping (BINDING CONDITION 1 ordering): if the
                # OOB-closer / 5-min kill switch acts on a pending TIME BARRIER
                # exit first, the NULL-price row persists — just sooner. Runs
                # here in the genuinely-idle slot (between ib.sleep() calls) so
                # its settled read (self.ib.run()) is safe — it CANNOT run in the
                # bar-update callback, which is inside the running loop. It is
                # the never-raise boundary for all settled-based confirmation.
                self._reconcile_pending_position_state()

                # Hourly housekeeping sweep — invoked EVERY poll and
                # self-gated on the wall clock (A-7), deliberately NOT
                # inside the heartbeat gate: that gate fires only every
                # _HEARTBEAT_INTERVAL and SKIPS missed ticks after a stall,
                # so a heartbeat-gated schedule can starve. Runs here
                # (between ib.sleep() calls) for the same sync-API safety
                # as the rollover check; never raises.
                self._run_hourly_housekeeping()

                # Pending roll-seam retry (jit-roll-ratio-empty_07102026_1453
                # Stage 2) — like housekeeping (A-7), deliberately OUTSIDE
                # the heartbeat gate (same starvation reasoning). Cadence
                # self-gates on the new-1h-bar check inside; never raises.
                self._attempt_pending_roll_resolution()

                # Periodic heartbeat: fires when the shared wall clock
                # crosses this child's next on-phase tick. The deadline is
                # advanced BEFORE the work so an exception below can never
                # re-fire the same tick.
                now = time.time()
                if now >= next_heartbeat:
                    next_heartbeat = _advance_heartbeat_deadline(
                        next_heartbeat, now
                    )
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

        # Time since last bar. Right-padded to a fixed width so the "bar="
        # column aligns across children (fleet_health._HEARTBEAT_RE parses
        # the numeric "bar=<N>h" token — keep that shape).
        if getattr(self, "_last_bar_time_5m", None) is not None:
            delta = now - self._last_bar_time_5m
            hours = delta.total_seconds() / 3600
            bar_str = f"{hours:5.1f}h"
        else:
            bar_str = "  n/a "

        # Market hours check (CL: Sun 18:00 ET → Fri 17:00 ET). Parked LAST in
        # the line on purpose: it is the one variable-length field (CLOSED
        # strings are byte-fenced verbose), so trailing it keeps the numeric
        # columns above aligned instead of being pushed around by its length.
        market_status = self._get_market_status(now)

        connected = (
            self.data_client.is_connected() and self.exec_client.is_connected()
        )

        # Position and PNL lookup.
        #   loc_real_pnl : our restart-surviving cumulative, summed from the
        #                  per-fill CommissionReport.realizedPNL we persist in
        #                  the tradebook DB (scoped to this bot's client_id).
        try:
            unr_pnl = 0.0
            pos = 0
            if connected:
                acct = self.exec_client.get_account_summary(
                    symbol=self._execution_symbol,
                )
                pos = acct["cl_position"]
                unr_pnl = acct["cl_unrealized_pnl"]

            # DB read, independent of the broker connection — never zeroes out.
            try:
                loc_real_pnl = self.telemetry.realized_pnl_total()
            except Exception:
                loc_real_pnl = 0.0

            # Fixed-width, right-aligned so pos/unr/real form clean columns
            # across children. FLAT renders as 0 (a padded number aligns; the
            # word "FLAT" would not).
            pos_str = f"{int(pos):>3d}"
            pnl_str = (
                f" | unr=${unr_pnl:>10,.2f}"
                f" | real=${loc_real_pnl:>11,.2f}"
            )
        except Exception:
            pos_str = "  ?"
            pnl_str = ""

        subs_status = " | subs_lost=True" if self._subscriptions_lost else ""
        mute_status = (
            f" | DATA_MUTE={int((time.time() - self._data_mute_since) / 60)}min"
            if self._data_mute else ""
        )
        # Layout (parsed by fleet_health._HEARTBEAT_RE — preserve the
        # "alive |", "bar=", "pos=", and "subs_lost=" tokens if you edit this):
        #   alive | bar=<age>h | pos=<n> | unr=$<x> | real=$<x> | conn=T/F | <market>
        log.info(
            "alive | bar=%s | pos=%s%s | conn=%s%s%s | %s",
            bar_str,
            pos_str,
            pnl_str,
            "T" if connected else "F",
            subs_status,
            mute_status,
            market_status,
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
                    MacroFeatureEngine(
                        instrument=self._brain_instrument
                    ).refresh_if_stale()
                    # If refresh_if_stale succeeded, test if staleness
                    # has resolved by doing a trial feature build.
                    if self._data_mute:
                        try:
                            overrides = getattr(self, "_macro_daily_closes", {})
                            MacroFeatureEngine(
                                instrument=self._brain_instrument
                            )._build_fred_features(
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

    def _send_watchdog_telegram(self, msg: str) -> None:
        """Send a watchdog-family Telegram alert, throttled to 1/hour/instance.

        watchdog-telegram-throttle_07062026_0007 (user directive #2):
        during thin sessions the watchdog/reconnect cycle spammed Telegram
        every ~30-45 min. This helper throttles ONLY the Telegram sends —
        every log line stays full-fidelity (directive #3), and the recovery
        machinery itself (return values, disconnects, SystemExit
        escalation) is untouched.

        Semantics:
          - SUPPRESS when elapsed-since-last-attempt is strictly <
            _WATCHDOG_TG_COOLDOWN_SECONDS (patching the constant to 0
            disables the throttle cleanly): count it, best-effort persist,
            and log the FULL suppressed message text at INFO.
          - SEND otherwise, appending a "+N suppressed" consolidation
            suffix when alerts were swallowed in the closed window.
          - ATTEMPT CONSUMES BUDGET: the timestamp is recorded whether or
            not the send succeeds — a Telegram outage must not become
            per-fire retry spam.
          - NEVER raises: the send and each persistence I/O are separately
            try/except-wrapped.

        State lives in-memory (getattr seams — object.__new__ stubs are
        structural, not silent config defaults) and, when
        _watchdog_tg_state_path is set (client_id present), in a cid-keyed
        JSON sidecar hydrated lazily ONCE so the budget survives the R4
        escalation restart. state path None/missing -> zero disk I/O.
        """
        now = datetime.now(timezone.utc)
        state_path = getattr(self, "_watchdog_tg_state_path", None)

        def _persist() -> None:
            # Best-effort: an unwritable sidecar degrades to
            # in-memory-only and must never block or delay the caller
            # (the escalation path SystemExits right after this helper).
            if state_path is None:
                return
            try:
                last = self._watchdog_tg_last_send_utc
                Path(state_path).write_text(json.dumps({
                    "last_send_utc": last.isoformat() if last else None,
                    "suppressed_count": self._watchdog_tg_suppressed_count,
                }))
            except Exception:
                pass

        # Lazy one-time hydration from the sidecar (corrupt / missing /
        # unreadable -> treated as no-state, in-memory-only).
        if not getattr(self, "_watchdog_tg_hydrated", False):
            self._watchdog_tg_hydrated = True
            self._watchdog_tg_last_send_utc = None
            self._watchdog_tg_suppressed_count = 0
            if state_path is not None:
                try:
                    payload = json.loads(Path(state_path).read_text())
                    raw = payload.get("last_send_utc")
                    if raw:
                        parsed = datetime.fromisoformat(str(raw))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        self._watchdog_tg_last_send_utc = parsed
                    self._watchdog_tg_suppressed_count = int(
                        payload.get("suppressed_count", 0)
                    )
                except Exception:
                    self._watchdog_tg_last_send_utc = None
                    self._watchdog_tg_suppressed_count = 0

        last_send = self._watchdog_tg_last_send_utc
        if last_send is not None:
            elapsed = (now - last_send).total_seconds()
            if elapsed < _WATCHDOG_TG_COOLDOWN_SECONDS:
                self._watchdog_tg_suppressed_count += 1
                _persist()
                log.info(
                    "TELEGRAM SUPPRESSED (watchdog-family cooldown, "
                    "%.0fm remaining, %d suppressed this window): %s",
                    (_WATCHDOG_TG_COOLDOWN_SECONDS - elapsed) / 60.0,
                    self._watchdog_tg_suppressed_count,
                    msg,
                )
                return

        suppressed = self._watchdog_tg_suppressed_count
        if suppressed > 0:
            msg = (
                f"{msg}\n(+{suppressed} watchdog-family alerts suppressed "
                f"in the last hour — see log)"
            )
        # ATTEMPT CONSUMES BUDGET: record before the send outcome is known.
        self._watchdog_tg_last_send_utc = now
        self._watchdog_tg_suppressed_count = 0
        try:
            self._telegram.send(msg)
        except Exception:
            pass  # Telegram failures must never block watchdog machinery
        _persist()

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

        # Check how long since the last bar. T7: hourly-only instances
        # (enable_5m_stream=false) have NO 5m stream — anchor the 1h stream
        # against the 135-min threshold instead. 5m-enabled instances keep
        # the byte-identical 15-min/_last_bar_time_5m behavior (a stale 1h
        # anchor with a fresh 5m stream is a pre-existing, deferred gap —
        # C6). The getattr default-true mirrors the function's own
        # _last_bar_time_5m seam for object.__new__ watchdog stubs.
        if getattr(self, "_enable_5m_stream", True):
            last_bar_time = getattr(self, "_last_bar_time_5m", None)
            stale_threshold = _STALE_BAR_THRESHOLD_MINUTES
        else:
            last_bar_time = getattr(self, "_last_bar_time_1h", None)
            stale_threshold = _STALE_BAR_THRESHOLD_MINUTES_1H
        if last_bar_time is None:
            return False  # No bars received yet — warm start still in progress

        # T5 reopen grace: restart the stale clock at the most recent
        # session open so a halt-old last bar cannot trigger a reconnect
        # storm at every reopen. All three calendars anchor now — GLOBEX
        # joined grains/equity via cl-watchdog-reopen-grace_07052026_0001
        # (the former anchor=None false-fired the watchdog at every daily
        # 17:00 CT reopen, confirmed 2026-07-06/07).
        anchor = _session_open_anchor(self._brain_instrument, now)
        reference = (
            last_bar_time
            if anchor is None or anchor <= last_bar_time
            else anchor
        )
        minutes_stale = (now - reference).total_seconds() / 60
        if minutes_stale < stale_threshold:
            return False  # Not stale enough yet

        # R4: count CONSECUTIVE fruitless firings.  Any new brain-stream
        # bar (_on_bar_update_5m/_on_bar_update_1h) resets this to zero;
        # if the watchdog fires _MAX_FRUITLESS_RECONNECTS times with no
        # bar in between, the reconnect loop is churning without ever
        # recovering data — crash the process so fleet_runner restarts
        # the child fresh.  The getattr default mirrors the
        # _enable_5m_stream / _last_bar_time_5m seams above for
        # object.__new__ test stubs (a structural seam, not a silent
        # config default).
        fruitless_count = getattr(self, "_fruitless_reconnect_count", 0) + 1
        self._fruitless_reconnect_count = fruitless_count
        if fruitless_count >= _MAX_FRUITLESS_RECONNECTS:
            log.critical(
                "STALE BAR WATCHDOG: %d consecutive reconnect cycles "
                "produced NO bars (last bar %s, %.0f min stale) — "
                "escalating to process exit so fleet_runner restarts "
                "this child fresh",
                fruitless_count, last_bar_time, minutes_stale,
            )
            # Throttled send (watchdog-telegram-throttle_07062026_0007):
            # the helper — INCLUDING its sidecar persistence — completes
            # before the SystemExit below, and it never raises, so a
            # Telegram/persistence failure cannot block the escalation.
            self._send_watchdog_telegram(
                f"*WATCHDOG ESCALATION* - {fruitless_count} consecutive "
                f"reconnects produced no bars ({minutes_stale:.0f}m "
                f"stale). Terminating process for a fresh restart..."
            )
            raise SystemExit(
                f"stale-bar watchdog: {fruitless_count} consecutive "
                f"fruitless reconnects — exiting for fleet_runner restart"
            )

        subs_flag = "subs_lost=True" if self._subscriptions_lost else "subs_lost=False (silent death)"
        log.warning(
            "STALE BAR WATCHDOG: no bars for %.0f min (%s) "
            "— forcing disconnect + reconnect",
            minutes_stale, subs_flag,
        )
        # Throttled send (watchdog-telegram-throttle_07062026_0007) — the
        # helper never raises, so the old try/except moved inside it.
        self._send_watchdog_telegram(
            f"*STALE BAR WATCHDOG* - No bars received for {minutes_stale:.0f}m "
            f"during market hours. Forcing reconnect..."
        )
        # Surface the firing to the error queue: alive-but-blind states used
        # to leave only log/Telegram traces, invisible to the hourly monitor
        # (resubscribe-retry-blindness_07062026_0640 R2).
        self._emit_health_event(
            "stale-bars-watchdog",
            f"STALE BAR WATCHDOG: no bars for {minutes_stale:.0f} min "
            f"({subs_flag}) during market hours — forcing disconnect + "
            f"reconnect",
        )
        # Mark subscriptions as lost so downstream recovery paths are consistent
        self._subscriptions_lost = True
        # Disconnect first so _reconnect() starts with a clean state.
        try:
            self.data_client.disconnect()
            self.exec_client.disconnect()
        except Exception:
            pass  # disconnect() can fail if already broken — that's fine
        return True  # Caller should invoke _reconnect()

    def _get_market_status(self, utc_now: datetime) -> str:
        """Return human-readable market status for the BRAIN instrument.

        T5: delegates to src.live_execution.session_calendar (the legacy
        CL body moved there VERBATIM as the GLOBEX calendar — CL strings
        byte-identical, sweep-pinned; grains get their own calendar).
        Bars are the BRAIN stream, so status follows _brain_instrument
        (the T4 seam also serves object.__new__ test stubs that set only
        _execution_symbol).

        Args:
            utc_now: Current time in UTC (tz-naive).

        Returns:
            "OPEN" or a "CLOSED (...)" string.
        """
        return _calendar_market_status(self._brain_instrument, utc_now)



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

        # 5. Reset state to FLAT — a REAL close of a filled position (A2:
        # full reset incl. strategy.on_exit); the pending record is cleared
        # cooldown-free via _clear_pending_entry.
        self._reset_position_state(reason="NAKED_POSITION_KILL_SWITCH")
        self._clear_pending_entry()

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
                avg_fill_price=avg_price,
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
                
                # Software OCA: cancel the remaining resting protective order(s).
                # cancel_open_orders is a bulk, symbol-scoped cancel; at exit time
                # exactly one bracket is live, so this clears only the sibling leg(s).
                try:
                    cancelled = self.exec_client.cancel_open_orders(symbol=self._execution_symbol)
                    log.info(f"[OCA] cancelled {cancelled} resting protective order(s) after {exit_reason}")
                except Exception as e:
                    log.warning(f"[OCA] Failed to cancel resting protective orders after {exit_reason}: {e}")
                                
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
                # Guard: only orders registered at submission may book a NEW
                # trade. A fill that is neither a tracked TP/SL exit nor a
                # known entry (e.g. an orphaned protective order, or a replayed
                # fill after restart) must NOT be treated as an entry — doing
                # so places bracket children around an exit and corrupts
                # position tracking. Decision context is no discriminator: it
                # is also stored under child order IDs for telemetry.
                _known_entries = getattr(self, "_entry_order_ids", set())
                if str(order_id) not in _known_entries:
                    log.error(
                        "[TRADE] UNRECOGNIZED FILL: orderId=%s  action=%s  "
                        "fill=%.2f  qty=%d — not a tracked entry or TP/SL "
                        "order; ignoring (position state unchanged; OOB "
                        "detection will reconcile)",
                        order_id, action_str, avg_price, int(qty),
                    )
                    return
                if hasattr(self, '_processed_entry_order_ids'):
                    self._processed_entry_order_ids.add(order_id)
                self._last_filled_entry_order_id = order_id
                trade_id = "trade_" + str(order_id)
                self._active_trade_id = trade_id

                # D2.2: a recognized entry fill MUST resolve its stored
                # pending context (signal-time ATR, per-trade overrides).
                # A miss means the seeding inputs are gone — raise loudly,
                # fabricate nothing (bracket children cannot be placed; the
                # kill switch protects the naked position).
                if not ctx:
                    log.error(
                        "[TRADE] ENTRY FILL orderId=%s has NO stored decision "
                        "context — ATR/trailing overrides unrecoverable and "
                        "TP/SL children cannot be placed; kill switch will "
                        "flatten the naked position",
                        order_id,
                    )
                    try:
                        self._telegram.send(
                            f"[CRITICAL] *ENTRY FILL WITHOUT CONTEXT*\n"
                            f"Order: `{order_id}` fill: `{avg_price}`\n"
                            f"No stored decision context — TP/SL cannot be "
                            f"placed; expect kill-switch flatten."
                        )
                    except Exception:
                        pass  # Never let Telegram failure block fill handling

                # D2.2 + A3: ALL in-position state is seeded HERE, from the
                # FILL — never at submission. The position exists broker-side
                # from this moment even if bracket children fail below, so
                # seeding precedes _place_bracket_children_on_fill.
                if action_str == "BUY":
                    self._position_side = 1
                elif action_str == "SELL":
                    self._position_side = -1
                else:
                    _ctx_action = ctx.get("entry_action")
                    self._position_side = (
                        1 if _ctx_action == "BUY"
                        else -1 if _ctx_action == "SELL" else 0
                    )
                side_str = (
                    "LONG" if self._position_side == 1
                    else "SHORT" if self._position_side == -1 else "UNKNOWN"
                )
                self._entry_price = avg_price  # A3: fill, not submission price
                self._trailing_activated = False
                self._position_bars_held = 0
                # Signal-time ATR / per-trade overrides from the pending
                # context (always written at submission — D2.1; .get keeps
                # legacy-shaped contexts from crashing the broker callback,
                # and the ctx-miss ERROR above already covers the loud path).
                self._atr_at_entry = ctx.get("atr_at_entry")
                self._trade_trailing_atr_mult = ctx.get("trailing_atr_mult")
                self._trade_max_hold_bars = ctx.get("max_hold_bars")
                # Fill-time bar seeding: presence-selected frame — the same
                # rule as _check_trailing_stop (T7/C4). Extremes are
                # RE-SEEDED from the fill-time bar, not accumulated from
                # submission (an unfilled GTC's pre-fill extremes poisoned
                # the trailing trigger).
                extremes_df = getattr(self, "rolling_df_5m", None)
                if extremes_df is None:
                    extremes_df = getattr(self, "rolling_df_1h", None)
                if extremes_df is not None and len(extremes_df) > 0:
                    fill_bar = extremes_df.iloc[-1]
                    self._highest_high = float(fill_bar["High"])
                    self._lowest_low = float(fill_bar["Low"])
                    self._position_entry_bar_time = extremes_df.index[-1]
                else:
                    # No frame to seed from (fill before warm data) — say so
                    # loudly; extremes stay at the accumulator identities.
                    log.error(
                        "[TRADE] ENTRY FILL orderId=%s: no rolling frame "
                        "available to seed fill-time extremes/entry bar",
                        order_id,
                    )
                # D2.2: the pending slot is consumed by this fill — clear it
                # eagerly so TTL cannot re-arm against a live trade.
                if str(getattr(self, "_pending_entry_order_id", None)) == str(order_id):
                    self._clear_pending_entry()

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

                # Stamp the actual fill onto the decision-ledger row — the
                # ledger recorded only decision intent until now; every
                # historical fill_price was NULL
                # (telemetry-fill-commission_07062026_0640 R2).
                try:
                    self.telemetry.update_fill(
                        order_id_int if order_id_int is not None else order_id,
                        avg_price,
                    )
                except Exception:
                    log.warning(
                        "update_fill failed for order %s", order_id,
                        exc_info=True,
                    )
                
                try:
                    self._telegram.send(
                        f"*ENTRY FILLED*\n"
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
