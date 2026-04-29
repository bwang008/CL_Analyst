"""Feature Bucket definitions for the Optuna Feature Bucket Architecture.

Categorical feature groups that Optuna can toggle on/off as hyperparameters.
The ``core`` bucket is always active; all others are searchable.

Usage::

    from src.features.feature_buckets import FEATURE_BUCKETS, get_active_features

    # In Optuna objective:
    active = {"core"}
    for bucket in TOGGLEABLE_BUCKETS:
        if trial.suggest_categorical(f"use_{bucket}", [True, False]):
            active.add(bucket)
    feature_cols = get_active_features(all_feature_cols, active)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Bucket → prefix mapping
# ---------------------------------------------------------------------------
# Each bucket maps to a list of column-name prefixes.  A feature column
# belongs to a bucket if it starts with (or exactly matches) any prefix.
# Order matters only for readability — a column is assigned to the FIRST
# matching bucket during classification.

FEATURE_BUCKETS: dict[str, list[str]] = {
    # Always-on: minimal OHLCV-derived features
    "core": ["ATR_14", "Volume_Log", "log_ret"],

    # Cyclical time encoding (hour-of-day, day-of-week)
    "time": ["Hour_", "DayOfWeek_"],

    # Bollinger bands, ATR ratios, vol-of-vol
    "volatility": ["VOL_"],

    # RSI, MACD, ADX, Spread-Adjusted RSI
    "momentum": ["MOM_"],

    # Linear regression slope, R², trend strength, Ichimoku cloud
    "trend": ["TREND_", "ICHIMOKU_"],

    # Corwin-Schultz spread, Kyle lambda (bid-ask microstructure)
    "microstructure": ["LIQ_"],

    # Candle body/wick ratios, OBV slope, VWAP distance, cross-TF ratios
    "structure": ["STRUC_", "VOLFLOW_", "CROSS_"],

    # Return distribution: skew, kurtosis, percentile rank
    "distribution": ["DIST_"],

    # Cumulative return, ATR-normalised distance, distance from high
    "exhaustion": ["EXHAUST_"],

    # Slope divergence, temporal peak offset, effort-reward ratio
    "divergence": ["EXHDIV_"],

    # AlphaFactory macro context (rolling regime indicators)
    "macro_tech": ["MACRO_"],

    # External FRED + CFTC COT data
    "macro_external": ["VIX", "OVX", "DXY", "YIELD", "COT"],
}

# Buckets that Optuna can toggle (everything except core)
TOGGLEABLE_BUCKETS: list[str] = [
    b for b in FEATURE_BUCKETS if b != "core"
]

# Minimum trials needed when bucket search is active (2^11 ≈ 2K combos)
BUCKET_MIN_TRIALS = 150


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def classify_feature(col: str) -> str | None:
    """Return the bucket name for a feature column, or None if unmatched."""
    for bucket, prefixes in FEATURE_BUCKETS.items():
        for prefix in prefixes:
            if col == prefix or col.startswith(prefix):
                return bucket
    return None


def get_active_features(
    all_feature_cols: list[str],
    active_buckets: set[str],
) -> list[str]:
    """Filter feature columns to only those in active buckets.

    Unclassified features (not matching any bucket) are always included
    to avoid silently dropping features added in future iterations.

    Args:
        all_feature_cols: Full list of feature column names.
        active_buckets: Set of bucket names that are enabled.

    Returns:
        Filtered list of feature column names.
    """
    result = []
    for col in all_feature_cols:
        bucket = classify_feature(col)
        if bucket is None:
            # Unknown feature — include to be safe
            result.append(col)
        elif bucket in active_buckets:
            result.append(col)
    return result


def get_bucket_summary(all_feature_cols: list[str]) -> dict[str, int]:
    """Return a dict of bucket_name → feature count for reporting."""
    from collections import Counter
    counts: Counter[str] = Counter()
    for col in all_feature_cols:
        bucket = classify_feature(col)
        counts[bucket or "unclassified"] += 1
    return dict(counts)
