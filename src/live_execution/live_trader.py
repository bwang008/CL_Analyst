"""
Live Event-Driven Execution Engine for CL Futures.

This module implements the live trading loop that:
1. Uses DataManager for warm-start initialization (seed CSV + IBKR backfill)
2. Subscribes to live 5-minute bars from IBKR (Two-Stream architecture)
   - Brain stream: Continuous contract for signal generation
   - Hands stream: Front-month contract for execution + raw data logging
3. Maintains a rolling window and generates features via AlphaFactory
4. Runs inference using the S_Ultimate (EXP-017) model
5. Executes bracket orders on IBKR Paper Trading
6. Logs all activity to SQLite telemetry (smoothed + raw front-month)

Usage:
    conda run -n trader python -m src.live_execution.live_trader
    conda run -n trader python -m src.live_execution.live_trader --dry-run

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
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

# Project imports
from src.features.alpha_factory import AlphaFactory
from src.LGBMLearner import LGBMLearner
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

_DEFAULT_MODEL_PATH = str(
    _PROJECT_ROOT / "models" / "registry" / "EXP-017_S_Ultimate" / "final_model.pkl"
)
_DEFAULT_CONFIG_PATH = str(
    _PROJECT_ROOT / "models" / "registry" / "EXP-017_S_Ultimate" / "config.json"
)
_DEFAULT_DB_PATH = str(_PROJECT_ROOT / "data" / "live_telemetry.db")

# AlphaFactory windows used during training (set_05/set_06)
_ALPHA_WINDOWS = [864, 2016, 4032, 10080]  # 3d, 7d, 14d, 35d in 5-min bars

# Rolling window size — must be >= largest alpha window + safety margin
_MAX_ROLLING_BARS = 11_000

# Trade parameters
_DEFAULT_QUANTITY = 1  # 1 CL contract (base lot)
_TP_ATR_MULT = 7.0   # Optimized via backtest sweep (was 2.0) — PF 2.99 at t=0.70
_SL_ATR_MULT = 1.0
_MAX_HOLD_BARS = 288  # 24 hours on 5-min bars

# Probability-based position sizing tiers (highest first)
# Maps model probability to lot count for the bracket order.
_SIZING_TIERS: list[tuple[float, int]] = [
    (0.80, 3),  # 80%+ confidence → 3 lots
    (0.70, 2),  # 70%+ confidence → 2 lots
    (0.60, 2),  # 60%+ confidence → 2 lots
    (0.50, 1),  # 50%+ confidence → 1 lot
]

# Polling interval in seconds (ib.sleep)
_POLL_INTERVAL = 5.0

# Reconnection parameters
_RECONNECT_BASE_DELAY = 5.0      # Initial delay before reconnect attempt (seconds)
_RECONNECT_MAX_DELAY = 300.0     # Max backoff delay (5 minutes)
_RECONNECT_MAX_ATTEMPTS = 50     # Max retry attempts (~2+ hours of retries)

# Default paths for DataManager
_DEFAULT_SEED_PATH = str(_PROJECT_ROOT / "data" / "raw" / "cl-5m_bk.csv")
_DEFAULT_CACHE_PATH = str(
    _PROJECT_ROOT / "data" / "processed" / "warm_start_cache.parquet"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class CLOnlyLogFilter(logging.Filter):
    """Suppress ib_insync log messages about non-CL positions/trades.

    IBKR reports historical positions, portfolio updates, executions,
    and commission reports for ALL symbols in the account, even those
    with 0 position (closed-out stocks like XOM, MSFT, V, COP).
    ib_insync logs every one of these at INFO level, cluttering the
    live trader output.  This filter drops those messages so only
    CL-related (and generic connection/warning) events get through.
    """

    _NON_CL_RE = re.compile(
        r"(?:"
        r"Stock\("
        r"|symbol='(?!CL\b)\w+"
        r")",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if self._NON_CL_RE.search(msg):
            return False  # suppress non-CL message
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
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
    Event-driven live execution engine for the S_Ultimate CL model.

    Architecture:
        IBKR → 5-min bars → rolling DataFrame → AlphaFactory →
        LGBMLearner inference → bracket order → telemetry logging
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 10,
        model_path: str = _DEFAULT_MODEL_PATH,
        config_path: str = _DEFAULT_CONFIG_PATH,
        db_path: str = _DEFAULT_DB_PATH,
        seed_path: str = _DEFAULT_SEED_PATH,
        cache_path: str = _DEFAULT_CACHE_PATH,
        quantity: int = _DEFAULT_QUANTITY,
        dry_run: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.quantity = quantity
        self.dry_run = dry_run

        # Load model
        log.info("Loading model from %s", model_path)
        self.learner = LGBMLearner.__new__(LGBMLearner)
        self.learner.load(model_path)
        self.feature_names: list[str] = self.learner.feature_names
        log.info("Model loaded: %d features", len(self.feature_names))

        # Load config for threshold
        with open(config_path) as f:
            config = json.load(f)
        self.probability_threshold: float = config.get(
            "optimized_probability_threshold", 0.45
        )
        log.info("Probability threshold: %.2f", self.probability_threshold)

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
        self._callbacks_registered = False
        self._last_decision_context_by_order_id: dict[int, dict] = {}
        self._position_entry_bar_time: Optional[pd.Timestamp] = None
        self._position_bars_held: int = 0
        self._run_id = (
            f"live-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        self._session_id = uuid.uuid4().hex
        self._hostname = socket.gethostname()
        self._process_id = os.getpid()
        self._environment = "paper" if self.port in (4002, 7497) else "live"

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def _prob_to_lots(self, probability: float) -> int:
        """Map model probability to lot count using sizing tiers.

        Returns the base quantity for probabilities below all tiers.
        """
        for min_prob, lots in _SIZING_TIERS:
            if probability >= min_prob:
                return lots
        return self.quantity  # fallback to base quantity

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect to IBKR, warm-start via DataManager, and enter the event loop."""
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

            # Step 7: Subscribe to live bars (Brain stream)
            self._subscribe()

            # Step 8: Subscribe to front-month bars (Hands stream)
            if self._front_month_contract is not None:
                self._subscribe_front_month()

            # Step 9: Enter event loop
            self._running = True
            self._event_loop()

        except Exception:
            log.exception("Fatal error in LiveTrader")
            raise
        finally:
            self._shutdown()

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
        if self._position_bars_held <= _MAX_HOLD_BARS:
            return False

        cancelled = self.manager.cancel_open_cl_orders()
        trade = self.manager.close_cl_position_market()
        log.info(
            "TIME BARRIER EXIT: held_bars=%d, cancelled_orders=%d, position=%d",
            self._position_bars_held, cancelled, current_position,
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
            order_id = getattr(order, "orderId", None)
            ctx = self._last_decision_context_by_order_id.get(order_id)
            event_ts = self._utc_iso_now()
            event_id = self._build_event_id(
                event_type="ORDER_STATUS",
                event_ts=event_ts,
                order_id=order_id,
                status=getattr(status, "status", None),
            )
            self.telemetry.log_tradebook_event(
                event_id=event_id,
                event_type="ORDER_STATUS",
                event_timestamp_utc=event_ts,
                order_id=order_id,
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
                status=getattr(status, "status", None),
                order_qty=float(getattr(order, "totalQuantity", 0) or 0),
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
            fill_price = float(getattr(execution, "price", 0) or 0)
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
                side=getattr(execution, "side", None)
                or getattr(order, "action", None),
                action=getattr(order, "action", None),
                order_type=getattr(order, "orderType", None),
                time_in_force=getattr(order, "tif", None),
                status=getattr(status, "status", None) if status else None,
                order_qty=float(getattr(order, "totalQuantity", 0) or 0),
                fill_qty=float(getattr(execution, "shares", 0) or 0),
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
        if errorCode in (1101, 1102) and self._subscriptions_lost:
            log.info("CONNECTIVITY RESTORED (Error %d) — resubscribing...", errorCode)
            self._resubscribe_and_backfill()

    def _resubscribe_and_backfill(self) -> None:
        """Re-subscribe to live bars after connectivity drop and backfill gaps."""
        try:
            # 1. Backfill any missed bars during the gap
            if self._last_bar_time is not None:
                now_utc = pd.Timestamp.utcnow()
                gap = now_utc - self._last_bar_time
                gap_minutes = gap.total_seconds() / 60

                if gap_minutes > 5:  # At least one bar could have been missed
                    log.info(
                        "Backfilling gap: %s → %s (%.1f minutes)",
                        self._last_bar_time, now_utc, gap_minutes,
                    )
                    try:
                        gap_days = max(int(gap.total_seconds() / 86400) + 1, 1)
                        duration_str = f"{gap_days} D"
                        bars = self.manager.fetch_historical_bars_by_duration(
                            duration_str=duration_str,
                            contract=self._contract,
                        )
                        if bars is not None and len(bars) > 0:
                            # Filter to only bars newer than _last_bar_time
                            new_bars = bars[bars.index > self._last_bar_time]
                            if len(new_bars) > 0:
                                self.rolling_df = pd.concat(
                                    [self.rolling_df, new_bars]
                                ).drop_duplicates(subset=["DateTime"], keep="last")
                                self.rolling_df = self.rolling_df.sort_index()
                                if len(self.rolling_df) > _MAX_ROLLING_BARS:
                                    self.rolling_df = self.rolling_df.iloc[
                                        -_MAX_ROLLING_BARS:
                                    ]
                                self._last_bar_time = self.rolling_df.index[-1]
                                # Append to warm-start cache too
                                self.data_manager.append_bar(new_bars)
                                log.info(
                                    "Backfilled %d bars, latest=%s",
                                    len(new_bars), self._last_bar_time,
                                )
                    except Exception:
                        log.exception("Gap backfill failed — continuing without")

            # 2. Cancel stale subscriptions
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

            # 3. Re-subscribe
            self._subscribe()
            if self._front_month_contract is not None:
                self._subscribe_front_month()

            self._subscriptions_lost = False
            log.info("Reconnection complete — live bars flowing again")

        except Exception:
            log.exception("Resubscription failed — will retry on next reconnect")

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
        """Run feature generation, inference, and potentially execute a trade."""
        # 1. Generate features
        features = build_live_features(self.rolling_df, self.feature_names)
        if features is None:
            log.info("Feature generation skipped (insufficient data or NaN)")
            return

        # 2. Run inference
        #    For binary models with focal loss, predict() returns logits.
        #    Apply sigmoid to get calibrated probability.
        raw_pred = self.learner.model.predict(features)
        raw_val = float(np.asarray(raw_pred).ravel()[0])

        # Determine if output is logit or probability
        if raw_val < 0 or raw_val > 1:
            probability = _sigmoid(raw_val)
        else:
            probability = raw_val

        confidence_pct = probability * 100.0
        is_buy_signal = probability >= self.probability_threshold

        current_price = float(self.rolling_df["Close"].iloc[-1])

        # Get ATR from the features
        atr_value = None
        if "ATR_14" in features.columns:
            atr_value = float(features["ATR_14"].iloc[0])

        # Enforce 24-hour time barrier on any open position
        if self._check_time_barrier(
            bar_time=bar_time,
            current_price=current_price,
            atr_value=atr_value,
        ):
            return

        log.info(
            "INFERENCE: prob=%.4f (%.1f%%)  threshold=%.2f  signal=%s",
            probability, confidence_pct,
            self.probability_threshold,
            "BUY" if is_buy_signal else "HOLD",
        )

        if not is_buy_signal:
            # Log hold signal
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal="Hold",
                confidence_pct=confidence_pct,
                action_taken="HOLD",
                current_price=current_price,
                atr_value=atr_value,
            )
            return

        # 3. Check position — only enter if flat
        current_position = self.manager.get_cl_position()
        if current_position != 0:
            log.info(
                "BUY signal ignored: already holding position (%d contracts)",
                current_position,
            )
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal="Buy",
                confidence_pct=confidence_pct,
                action_taken="SKIP_POSITION",
                current_price=current_price,
                atr_value=atr_value,
            )
            return

        # 4. Calculate bracket levels
        if atr_value is None or atr_value <= 0:
            log.warning("ATR is invalid (%.4f) — cannot calculate bracket levels", atr_value or 0)
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal="Buy",
                confidence_pct=confidence_pct,
                action_taken="SKIP_ATR_INVALID",
                current_price=current_price,
                atr_value=atr_value,
            )
            return

        tp_price = round(current_price + _TP_ATR_MULT * atr_value, 2)
        sl_price = round(current_price - _SL_ATR_MULT * atr_value, 2)

        log.info(
            "BRACKET: price=%.2f  TP=%.2f (+%.2f)  SL=%.2f (-%.2f)  ATR=%.4f",
            current_price, tp_price, _TP_ATR_MULT * atr_value,
            sl_price, _SL_ATR_MULT * atr_value, atr_value,
        )

        decision_timestamp_utc = bar_time.isoformat()
        signal_id = uuid.uuid4().hex
        decision_id = uuid.uuid4().hex

        # 5. Execute or dry-run
        if self.dry_run:
            sized_qty = self._prob_to_lots(probability)
            log.info("DRY RUN — would place bracket order BUY %d CL (prob=%.2f)", sized_qty, probability)
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal="Buy",
                confidence_pct=confidence_pct,
                action_taken="DRY_RUN",
                current_price=current_price,
                atr_value=atr_value,
                tp_price=tp_price,
                sl_price=sl_price,
                direction="BUY",
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
            sized_qty = self._prob_to_lots(probability)
            trades = self.manager.place_bracket_order(
                contract=self._front_month_contract,
                action="BUY",
                quantity=sized_qty,
                limit_price=current_price,
                tp_price=tp_price,
                sl_price=sl_price,
                use_market=True,
            )
            parent_trade = trades[0]
            order_id = parent_trade.order.orderId
            self._position_entry_bar_time = bar_time
            self._position_bars_held = 0
            log.info(
                "ORDER PLACED: orderId=%d  BUY %d CL @ MKT  TP=%.2f  SL=%.2f  (prob=%.2f)",
                order_id, sized_qty, tp_price, sl_price, probability,
            )
            self.telemetry.log_signal(
                timestamp=bar_time,
                signal="Buy",
                confidence_pct=confidence_pct,
                action_taken="EXECUTE",
                current_price=current_price,
                atr_value=atr_value,
                tp_price=tp_price,
                sl_price=sl_price,
                order_id=order_id,
                direction="BUY",
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
                signal="Buy",
                confidence_pct=confidence_pct,
                action_taken=f"ERROR: {exc}",
                current_price=current_price,
                atr_value=atr_value,
                tp_price=tp_price,
                sl_price=sl_price,
                direction="BUY",
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
                    log.error("Reconnection failed after %d attempts — shutting down.", _RECONNECT_MAX_ATTEMPTS)
                    self._running = False
            except Exception:
                log.exception("Error in event loop iteration")
                time.sleep(_POLL_INTERVAL)

        log.info("Event loop exited.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CL Analyst — Live Execution Engine (S_Ultimate)"
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
        "--client-id", type=int, default=10,
        help="IBKR client ID (default: 10)",
    )
    parser.add_argument(
        "--model-path", default=_DEFAULT_MODEL_PATH,
        help="Path to the saved model .pkl file",
    )
    parser.add_argument(
        "--config-path", default=_DEFAULT_CONFIG_PATH,
        help="Path to the model config.json",
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

    args = parser.parse_args()

    trader = LiveTrader(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        model_path=args.model_path,
        config_path=args.config_path,
        db_path=args.db_path,
        seed_path=args.seed_path,
        cache_path=args.cache_path,
        quantity=args.quantity,
        dry_run=args.dry_run,
    )
    trader.start()


if __name__ == "__main__":
    main()
