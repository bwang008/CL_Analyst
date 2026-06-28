"""
Live Feature Pipeline for CL Futures Inference.

Extracted from live_trader.py (Phase 1 modularization).
Replicates the training pipeline (process_set_05/set_06/set_07)
for real-time feature generation.

This module is the single source of truth for live feature engineering.
Both the live trader and parity validation scripts consume it.
"""

from __future__ import annotations

import collections
import logging
from typing import Optional

import numpy as np
import pandas as pd

from src.features.alpha_factory import AlphaFactory
from src.features.macro_features import MacroFeatureEngine, StaleDataException

log = logging.getLogger("LiveTrader")

# ---------------------------------------------------------------------------
# Feature Pipeline Constants
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Inference-Level Feature Variance Monitor
# ---------------------------------------------------------------------------

# Rolling buffer of recent feature snapshots for variance monitoring.
# Detects features stuck at constant values across inference cycles,
# catching issues that pass the FRED-level circuit breaker (e.g.,
# a feature "changing" but stuck at an extreme constant like Amihud).
_FEATURE_HISTORY: collections.deque = collections.deque(maxlen=48)

# Minimum number of inferences before variance checks activate.
_VARIANCE_MIN_HISTORY = 24

# Feature name patterns to monitor for zero-variance.
_VARIANCE_CRITICAL_PATTERNS = ("CHG_1D", "CHG_3D")


def _check_inference_feature_variance(
    features_row: pd.DataFrame,
    feature_names: list[str],
) -> None:
    """Log warnings if critical features show zero variance over recent inferences.

    This is a **warning-only** monitor — it does not raise exceptions or
    block entries.  Warnings surface in the live trader log and Telegram
    heartbeat messages, enabling early detection without trade disruption.

    Called once per inference cycle with the single-row feature DataFrame.
    """
    snapshot = features_row[feature_names].iloc[0].to_dict()
    _FEATURE_HISTORY.append(snapshot)

    if len(_FEATURE_HISTORY) < _VARIANCE_MIN_HISTORY:
        return

    history_df = pd.DataFrame(list(_FEATURE_HISTORY))
    critical_cols = [
        c for c in history_df.columns
        if any(p in c for p in _VARIANCE_CRITICAL_PATTERNS)
    ]

    for col in critical_cols:
        if col not in history_df.columns:
            continue
        col_std = history_df[col].std()
        if col_std == 0:
            log.warning(
                "FEATURE VARIANCE ALERT: %s has been constant (%.4f) "
                "for %d consecutive inferences",
                col, history_df[col].iloc[-1], len(history_df),
            )


# ---------------------------------------------------------------------------
# Feature Pipeline (replicates process_set_05/set_06 for live data)
# ---------------------------------------------------------------------------

def build_live_features(
    df: pd.DataFrame,
    feature_names: list[str],
    lean: bool = True,
    bar_size: str = "5m",
    macro_overrides: dict[str, float] | None = None,
    return_last_n: int = 1,
) -> pd.DataFrame | None:
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
        macro_windows = {"1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}
    elif bar_size == "2h":
        # 2h bars: windows are in 2h-bar units (12=1d, 36=3d, 84=7d, 168=14d, 420=35d)
        is_set_07 = True
        alpha_windows = [12, 36, 84, 168, 420]
        macro_windows = {"1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}
    elif bar_size == "1h":
        is_set_07 = True
        alpha_windows = [24, 72, 168, 336, 840]
        macro_windows = {"1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}
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
    # features (MACRO_3M needs 2160h of data).
    # Convert the 5m threshold to the current bar size:
    #   5m  → divisor 1   → 26,000 bars
    #   1h  → divisor 12  → ~2,167 bars
    #   2h  → divisor 24  → ~1,083 bars
    #   4h  → divisor 48  → ~542 bars
    _MIN_RECOMMENDED_BARS_5M = 26_000
    _bar_divisor = {"5m": 1, "1h": 12, "2h": 24, "4h": 48}.get(bar_size, 1)
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
    # bars_per_hour determines the bar-count equivalent of rolling windows in
    # add_macro_context (e.g. MACRO_3M = 2160 hours × bars_per_hour bars).
    # Default of 12 is for 5m bars; must be 1 for 1h/2h/4h to avoid
    # 12× over-sized macro windows that silently underestimate context range.
    _bars_per_hour = {"5m": 12, "1h": 1, "2h": 0.5, "4h": 0.25}.get(bar_size, 12)
    _has_ichimoku = any(f.startswith("ICHIMOKU_") for f in feature_names)
    _has_dma = any(f.startswith("TREND_DMA_") for f in feature_names)
    _has_exh_div = any(f.startswith("EXHDIV_") for f in feature_names)
    _has_term_structure = any(f.startswith("TS_") for f in feature_names)
    _has_internal_macro = any(f.startswith(("MACRO_POS_", "MACRO_WIDTH_")) for f in feature_names)

    factory = AlphaFactory(work, bars_per_hour=_bars_per_hour)
    if lean:
        # Lean path: momentum + time features only (no macro, no extended)
        # This is 6-7x faster than the full pipeline
        work = factory.add_all_features(
            windows=alpha_windows,
            include_momentum=True,
            include_macro=_has_internal_macro,
            include_extended=False,
            include_ichimoku=_has_ichimoku,
            include_dma=_has_dma,
            include_exhaustion_divergence=_has_exh_div,
            include_term_structure=_has_term_structure,
        )
    elif is_set_07:
        work = factory.add_all_features(
            windows=alpha_windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            macro_windows=macro_windows,
            include_ichimoku=_has_ichimoku,
            include_dma=_has_dma,
            include_exhaustion_divergence=_has_exh_div,
            include_term_structure=_has_term_structure,
        )
    else:
        work = factory.add_all_features(
            windows=alpha_windows,
            include_momentum=True,
            include_macro=True,
            include_ichimoku=_has_ichimoku,
            include_dma=_has_dma,
            include_exhaustion_divergence=_has_exh_div,
            include_term_structure=_has_term_structure,
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
            live_time = work.index[-1] if not work.empty else None
            work = MacroFeatureEngine().merge_all(
                work, 
                live_overrides=macro_overrides, 
                live_time=live_time
            )
        except StaleDataException:
            # Let staleness exceptions propagate directly so the live
            # trader can catch them and enter Safety Mute mode.
            raise
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

    # 5. Replace inf with NaN, then forward-fill only.
    #    ffill is standard for timeseries gaps (carries last valid value forward).
    #    bfill and fillna(0) are REMOVED — they fabricate values the model never
    #    saw during training and silently mask warm-up deficits.
    work.replace([np.inf, -np.inf], np.nan, inplace=True)
    work.ffill(inplace=True)

    # 6. Extract the last rows with the model's expected columns
    missing_cols = set(feature_names) - set(work.columns)
    if missing_cols:
        log.error("Missing feature columns: %s", missing_cols)
        return None

    last_rows = work[feature_names].iloc[-return_last_n:]

    # 7. HARD NaN GUARD — if ANY model feature is still NaN after ffill,
    #    the cache lacks sufficient warmup history.  Return None to force
    #    live_trader to skip this bar rather than infer on fabricated data.
    #    We check only the oldest returned row (if it has no NaNs, the newer ones won't either due to ffill)
    nan_count = int(last_rows.iloc[0].isna().sum())
    if nan_count > 0:
        nan_cols = last_rows.columns[last_rows.iloc[0].isna()].tolist()
        log.error(
            "HARD NaN GUARD FAILED: cache lacks sufficient warmup history. "
            "Found NaNs in features: %s", nan_cols[:5]
        )
        return None

    # 7. Inference-level feature variance monitoring (warning-only).
    #    Tracks recent feature vectors and warns if any critical CHG
    #    feature has zero variance — catches stuck features that pass
    #    the FRED-level circuit breaker.
    try:
        _check_inference_feature_variance(last_rows.iloc[[-1]], feature_names)
    except Exception:
        pass  # Never let monitoring crash inference

    # 8. Downcast to float32 to match the precision of parquet-stored
    #    training features.  LightGBM tree splits are exact comparisons;
    #    float64 values near split thresholds may route to different leaves
    #    than the float32 values the model was trained on.
    last_rows = last_rows.astype(np.float32)

    return last_rows
