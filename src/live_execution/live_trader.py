"""
Live Event-Driven Execution Engine for CL Futures.

This module implements the live trading loop that:
1. Fetches historical bars on startup (cold start)
2. Subscribes to live 5-minute bars from IBKR
3. Maintains a rolling window and generates features via AlphaFactory
4. Runs inference using the S_Ultimate (EXP-017) model
5. Executes bracket orders on IBKR Paper Trading
6. Logs all activity to SQLite telemetry

Usage:
    conda run -n trader python -m src.live_execution.live_trader
    conda run -n trader python -m src.live_execution.live_trader --dry-run

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Project imports
from src.features.alpha_factory import AlphaFactory
from src.LGBMLearner import LGBMLearner
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
_DEFAULT_QUANTITY = 1  # 1 CL contract
_TP_ATR_MULT = 2.0
_SL_ATR_MULT = 1.0

# How many days of history to fetch on cold start
_COLD_START_DAYS = 5

# Polling interval in seconds (ib.sleep)
_POLL_INTERVAL = 5.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("LiveTrader")


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

        # State
        self.rolling_df: Optional[pd.DataFrame] = None
        self._live_bars = None
        self._contract = None
        self._running = False
        self._last_bar_time: Optional[pd.Timestamp] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect to IBKR, do cold start, and enter the event loop."""
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

            # Step 2: Qualify contract
            self._contract = build_cl_contract(continuous=True)
            self._contract = self.manager.qualify_contract(self._contract)
            log.info("Qualified CL contract: %s", self._contract)

            # Step 3: Cold start — fetch historical bars
            self._cold_start()

            # Step 4: Subscribe to live bars
            self._subscribe()

            # Step 5: Enter event loop
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
        self.manager.disconnect()
        self.telemetry.close()
        log.info("Shutdown complete.")

    # ------------------------------------------------------------------
    # Cold start
    # ------------------------------------------------------------------

    def _cold_start(self) -> None:
        """Fetch historical bars to populate the rolling window."""
        log.info(
            "Cold start: fetching %d days of 5-min bars...",
            _COLD_START_DAYS,
        )
        df = self.manager.fetch_historical_bars(
            days_back=_COLD_START_DAYS,
            continuous=True,
        )
        n_bars = len(df)
        log.info("Cold start: received %d bars", n_bars)

        if n_bars == 0:
            raise RuntimeError(
                "Cold start failed: no historical bars received from IBKR."
            )

        # Ensure DateTime index
        if "DateTime" in df.columns and not isinstance(df.index, pd.DatetimeIndex):
            df = df.set_index("DateTime", drop=False)

        self.rolling_df = df
        self._last_bar_time = df.index[-1]
        log.info(
            "Rolling window initialized: %d bars, latest=%s",
            len(self.rolling_df), self._last_bar_time,
        )

        # Log all cold-start bars to telemetry
        for _, row in df.iterrows():
            ts = row.get("DateTime", row.name)
            self.telemetry.log_bar(
                timestamp=ts,
                open_=row["Open"],
                high=row["High"],
                low=row["Low"],
                close=row["Close"],
                volume=row["Volume"],
            )
        log.info("Logged %d cold-start bars to telemetry", n_bars)

    # ------------------------------------------------------------------
    # Live bar subscription
    # ------------------------------------------------------------------

    def _subscribe(self) -> None:
        """Subscribe to live 5-min bars with keepUpToDate."""
        log.info("Subscribing to live 5-min bars...")
        self._live_bars = self.manager.subscribe_live_bars(
            self._contract,
            bar_size="5 mins",
            duration_str="60 S",
        )
        self._live_bars.updateEvent += self._on_bar_update
        log.info("Subscribed to live bars")

    def _on_bar_update(self, bars, has_new_bar) -> None:
        """Callback fired by ib_insync when bars are updated."""
        if not has_new_bar or not bars:
            return

        # Convert the latest bar to a row
        new_bar = bars[-1]
        new_row = pd.DataFrame(
            [{
                "DateTime": pd.Timestamp(new_bar.date),
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

        # Log bar to telemetry
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

        # 5. Execute or dry-run
        if self.dry_run:
            log.info("DRY RUN — would place bracket order BUY %d CL", self.quantity)
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
            )
            return

        # Place real bracket order
        try:
            trades = self.manager.place_bracket_order(
                contract=self._contract,
                action="BUY",
                quantity=self.quantity,
                limit_price=current_price,
                tp_price=tp_price,
                sl_price=sl_price,
                use_market=True,
            )
            parent_trade = trades[0]
            order_id = parent_trade.order.orderId
            log.info(
                "ORDER PLACED: orderId=%d  BUY %d CL @ MKT  TP=%.2f  SL=%.2f",
                order_id, self.quantity, tp_price, sl_price,
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
            )

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

    args = parser.parse_args()

    trader = LiveTrader(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        model_path=args.model_path,
        config_path=args.config_path,
        db_path=args.db_path,
        quantity=args.quantity,
        dry_run=args.dry_run,
    )
    trader.start()


if __name__ == "__main__":
    main()
