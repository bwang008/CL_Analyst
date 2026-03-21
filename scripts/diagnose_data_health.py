"""
Data Health Diagnostic — Detects feature drift, cache corruption, and rollover issues.

Compares current cache/telemetry feature values against known-good baseline
ranges. Reports PASS/WARN/FAIL for each check.

Usage:
    python scripts/diagnose_data_health.py
    python scripts/diagnose_data_health.py --telemetry  # also check shadow_log
    python scripts/diagnose_data_health.py --verbose     # show detailed feature values
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_paths import get_data_root  # noqa: E402
from src.features.alpha_factory import AlphaFactory  # noqa: E402

log = logging.getLogger("diagnose_data_health")

# ---------------------------------------------------------------------------
# Baseline feature ranges (from pre-rollover known-good data, March 2026)
# If the current value is outside [lo, hi], the check flags WARN/FAIL.
# These are approximate ranges for the TAIL (latest bar) of the cache.
# ---------------------------------------------------------------------------

# Features that are most sensitive to back-adjustment mixing
_CRITICAL_FEATURES = {
    # feature_name: (reasonable_lo, reasonable_hi, description)
    "ATR_14":          (0.05,   2.50,  "14-bar ATR — should reflect real volatility"),
    "Volume_Log":      (0.00,  12.00,  "log1p(Volume) — should be consistent"),
}

# Features to compare in telemetry (shadow_log) if available
_DRIFT_FEATURES = [
    "ATR_14", "Volume_Log",
    "MOM_RSI_14", "MOM_ADX_14",
    "DIST_ZSCORE",
    "VOLFLOW_OBV_SLOPE_288",
]

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

class CheckResult:
    """Result of a single diagnostic check."""
    def __init__(self, name: str, status: str, detail: str):
        self.name = name
        self.status = status   # PASS, WARN, FAIL
        self.detail = detail

    def __str__(self):
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(self.status, "?")
        return f"  {icon} [{self.status}] {self.name}: {self.detail}"


def check_cache_exists() -> CheckResult:
    """Check that the warm-start cache file exists."""
    cache_path = get_data_root() / "processed" / "warm_start_cache.parquet"
    if cache_path.exists():
        size_mb = cache_path.stat().st_size / (1024 * 1024)
        return CheckResult("Cache File", "PASS", f"{cache_path.name} ({size_mb:.1f} MB)")
    return CheckResult("Cache File", "FAIL", f"Not found: {cache_path}")


def check_cache_integrity() -> tuple[CheckResult, pd.DataFrame | None]:
    """Load cache and validate basic integrity."""
    cache_path = get_data_root() / "processed" / "warm_start_cache.parquet"
    if not cache_path.exists():
        return CheckResult("Cache Integrity", "FAIL", "Cache file missing"), None

    try:
        df = pd.read_parquet(str(cache_path))
    except Exception as exc:
        return CheckResult("Cache Integrity", "FAIL", f"Failed to load: {exc}"), None

    # Ensure DateTime index
    if "DateTime" in df.columns and not isinstance(df.index, pd.DatetimeIndex):
        df = df.set_index("DateTime")
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception:
            pass

    # Check required columns
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        return CheckResult("Cache Integrity", "FAIL", f"Missing columns: {missing}"), None

    # Check for NaN/inf in OHLCV
    ohlcv_nans = df[list(required)].isna().sum().sum()
    ohlcv_infs = np.isinf(df[list(required)].select_dtypes(include=[np.number])).sum().sum()

    n_bars = len(df)
    date_range = f"{df.index.min()} → {df.index.max()}"

    issues = []
    if ohlcv_nans > 0:
        issues.append(f"{ohlcv_nans} NaN")
    if ohlcv_infs > 0:
        issues.append(f"{ohlcv_infs} inf")

    if issues:
        return CheckResult(
            "Cache Integrity", "WARN",
            f"{n_bars} bars, {date_range} — issues: {', '.join(issues)}"
        ), df

    return CheckResult(
        "Cache Integrity", "PASS",
        f"{n_bars} bars, {date_range}"
    ), df


def check_price_continuity(df: pd.DataFrame) -> CheckResult:
    """Check for sudden price jumps that indicate splice errors."""
    if df is None or len(df) < 2:
        return CheckResult("Price Continuity", "FAIL", "Insufficient data")

    close = df["Close"].values
    returns = np.abs(np.diff(close) / close[:-1])

    # Flag bars with > 5% single-bar return (abnormal for CL 5-min)
    large_jumps = np.where(returns > 0.05)[0]

    if len(large_jumps) > 0:
        worst_idx = large_jumps[np.argmax(returns[large_jumps])]
        worst_pct = returns[worst_idx] * 100
        worst_time = df.index[worst_idx + 1]
        return CheckResult(
            "Price Continuity", "WARN",
            f"{len(large_jumps)} bars with >5% jump. "
            f"Worst: {worst_pct:.2f}% at {worst_time}"
        )

    max_return = returns.max() * 100
    return CheckResult(
        "Price Continuity", "PASS",
        f"Max single-bar return: {max_return:.3f}%"
    )


def check_feature_ranges(df: pd.DataFrame, verbose: bool = False) -> list[CheckResult]:
    """Generate features on the cache and validate ranges."""
    results = []

    if df is None or len(df) < 300:
        results.append(CheckResult(
            "Feature Generation", "FAIL",
            "Insufficient cache data for feature generation"
        ))
        return results

    try:
        # Generate features the same way live_trader does
        work = df.copy()

        # Time encoding
        minute_of_day = work.index.hour * 60 + work.index.minute
        work["Time_Sin"] = np.sin(2 * np.pi * minute_of_day / 1440)
        work["Time_Cos"] = np.cos(2 * np.pi * minute_of_day / 1440)

        # AlphaFactory
        work = AlphaFactory(work).add_all_features(
            include_momentum=True,
            include_macro=True,
            include_extended=True,
        )

        # ATR_14
        if "ATR_14" not in work.columns:
            import pandas_ta as ta
            atr_series = work.ta.atr(length=14)
            if atr_series is not None:
                work["ATR_14"] = atr_series

        # Volume_Log
        work["Volume_Log"] = np.log1p(work["Volume"])

        # Clean
        work.replace([np.inf, -np.inf], np.nan, inplace=True)
        work.ffill(inplace=True)
        work.bfill(inplace=True)
        work.fillna(0, inplace=True)

        results.append(CheckResult(
            "Feature Generation", "PASS",
            f"{len(work.columns)} features generated"
        ))

    except Exception as exc:
        results.append(CheckResult(
            "Feature Generation", "FAIL", f"Error: {exc}"
        ))
        return results

    # Check critical features on the last bar
    last = work.iloc[-1]
    for feat, (lo, hi, desc) in _CRITICAL_FEATURES.items():
        if feat not in work.columns:
            results.append(CheckResult(f"Feature: {feat}", "WARN", "Not found"))
            continue

        val = float(last[feat])

        if lo <= val <= hi:
            status = "PASS"
        elif val < lo * 0.5 or val > hi * 2.0:
            status = "FAIL"
        else:
            status = "WARN"

        detail = f"{val:.4f}  (expected {lo:.2f}–{hi:.2f}) — {desc}"
        results.append(CheckResult(f"Feature: {feat}", status, detail))

    # Show recent feature stats if verbose
    if verbose:
        recent = work.tail(288)  # last 1 day
        print("\n  📊 Recent Feature Stats (last 288 bars):")
        for feat in _DRIFT_FEATURES:
            if feat in recent.columns:
                vals = recent[feat]
                print(f"     {feat:30s}  mean={vals.mean():10.4f}  "
                      f"std={vals.std():10.4f}  "
                      f"min={vals.min():10.4f}  max={vals.max():10.4f}")

    return results


def check_telemetry_signals() -> list[CheckResult]:
    """Check recent shadow_log entries for signal health."""
    results = []

    db_path = get_data_root() / "live_telemetry_cid18.db"
    if not db_path.exists():
        results.append(CheckResult(
            "Telemetry DB", "WARN", f"Not found: {db_path}"
        ))
        return results

    try:
        conn = sqlite3.connect(str(db_path))

        # Count total entries
        total = conn.execute("SELECT COUNT(*) FROM shadow_log").fetchone()[0]
        results.append(CheckResult(
            "Telemetry DB", "PASS", f"{total} shadow_log entries"
        ))

        # Get recent probabilities
        rows = conn.execute(
            "SELECT timestamp, prob_buy, prob_sell, strategy_name "
            "FROM shadow_log ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()

        if not rows:
            results.append(CheckResult(
                "Recent Signals", "WARN", "No recent shadow_log entries"
            ))
            conn.close()
            return results

        buy_probs = [r[1] for r in rows if r[1] is not None]
        sell_probs = [r[2] for r in rows if r[2] is not None]
        actions = [r[3] for r in rows if r[3] is not None]

        max_buy = max(buy_probs) if buy_probs else 0
        max_sell = max(sell_probs) if sell_probs else 0

        # Check if probabilities are stuck below threshold
        if max_buy < 0.55 and max_sell < 0.55:
            results.append(CheckResult(
                "Signal Health", "FAIL",
                f"ALL probabilities below 0.55 in last 50 bars "
                f"(max buy={max_buy:.3f}, max sell={max_sell:.3f}). "
                f"Possible feature drift / data issue!"
            ))
        elif max_buy < 0.60 and max_sell < 0.60:
            results.append(CheckResult(
                "Signal Health", "WARN",
                f"No probabilities above 0.60 in last 50 bars "
                f"(max buy={max_buy:.3f}, max sell={max_sell:.3f}). "
                f"May be normal during quiet markets."
            ))
        else:
            n_actionable = sum(1 for r in rows
                               if (r[1] and r[1] > 0.60) or (r[2] and r[2] > 0.60))
            results.append(CheckResult(
                "Signal Health", "PASS",
                f"max buy={max_buy:.3f}, max sell={max_sell:.3f}, "
                f"{n_actionable}/50 bars above 0.60"
            ))

        # Check feature drift: compare first-half vs second-half of shadow log
        feat_rows = conn.execute(
            "SELECT features_json FROM shadow_log "
            "WHERE features_json IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 100"
        ).fetchall()

        if len(feat_rows) >= 20:
            try:
                recent_feats = [json.loads(r[0]) for r in feat_rows[:10]]
                older_feats = [json.loads(r[0]) for r in feat_rows[-10:]]

                drift_warnings = []
                for feat in _DRIFT_FEATURES:
                    recent_vals = [f.get(feat) for f in recent_feats if feat in f]
                    older_vals = [f.get(feat) for f in older_feats if feat in f]

                    if recent_vals and older_vals:
                        recent_mean = np.mean(recent_vals)
                        older_mean = np.mean(older_vals)

                        if older_mean != 0:
                            pct_change = abs(recent_mean - older_mean) / abs(older_mean) * 100
                            if pct_change > 200:
                                drift_warnings.append(
                                    f"{feat}: {pct_change:.0f}% drift "
                                    f"({older_mean:.4f} → {recent_mean:.4f})"
                                )

                if drift_warnings:
                    results.append(CheckResult(
                        "Feature Drift", "FAIL",
                        "Large drift detected:\n" +
                        "\n".join(f"       • {w}" for w in drift_warnings)
                    ))
                else:
                    results.append(CheckResult(
                        "Feature Drift", "PASS",
                        "No significant drift in last 100 shadow_log entries"
                    ))
            except (json.JSONDecodeError, TypeError):
                results.append(CheckResult(
                    "Feature Drift", "WARN", "Could not parse features_json"
                ))

        conn.close()

    except Exception as exc:
        results.append(CheckResult(
            "Telemetry DB", "FAIL", f"Error: {exc}"
        ))

    return results


def check_roll_metadata() -> CheckResult:
    """Check roll metadata for consistency."""
    meta_path = get_data_root() / "processed" / ".roll_metadata.json"
    if not meta_path.exists():
        return CheckResult("Roll Metadata", "WARN", "No roll metadata file found")

    try:
        with open(meta_path) as f:
            meta = json.load(f)

        fm = meta.get("last_front_month", "unknown")
        delta = meta.get("cumulative_delta", 0)
        history = meta.get("roll_history", [])

        return CheckResult(
            "Roll Metadata", "PASS",
            f"front_month={fm}  cumulative_delta=${delta:.4f}  "
            f"rolls={len(history)}"
        )
    except Exception as exc:
        return CheckResult("Roll Metadata", "FAIL", f"Error reading metadata: {exc}")


def check_cache_backup() -> CheckResult:
    """Check that cache backups exist in the repo."""
    backup_dir = _PROJECT_ROOT / "data" / "cache_backups"
    if not backup_dir.exists():
        return CheckResult("Cache Backups", "WARN", "No backup directory found")

    backups = list(backup_dir.glob("*.parquet"))
    if not backups:
        return CheckResult("Cache Backups", "WARN", "No backup parquet files found")

    latest = max(backups, key=lambda p: p.stat().st_mtime)
    total_mb = sum(b.stat().st_size for b in backups) / (1024 * 1024)

    return CheckResult(
        "Cache Backups", "PASS",
        f"{len(backups)} backup(s), {total_mb:.1f} MB total. "
        f"Latest: {latest.name}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Data Health Diagnostic")
    parser.add_argument("--telemetry", action="store_true",
                        help="Also check telemetry shadow_log for signal health")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed feature statistics")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    print("=" * 64)
    print("  DATA HEALTH DIAGNOSTIC")
    print("=" * 64)

    all_results: list[CheckResult] = []

    # 1. Cache exists
    print("\n📁 Cache & Metadata")
    r = check_cache_exists()
    all_results.append(r)
    print(r)

    # 2. Cache integrity
    r, df = check_cache_integrity()
    all_results.append(r)
    print(r)

    # 3. Price continuity
    r = check_price_continuity(df)
    all_results.append(r)
    print(r)

    # 4. Roll metadata
    r = check_roll_metadata()
    all_results.append(r)
    print(r)

    # 5. Cache backups
    r = check_cache_backup()
    all_results.append(r)
    print(r)

    # 6. Feature generation + range checks
    print("\n🔬 Feature Health")
    feat_results = check_feature_ranges(df, verbose=args.verbose)
    all_results.extend(feat_results)
    for r in feat_results:
        print(r)

    # 7. Telemetry (optional)
    if args.telemetry:
        print("\n📡 Telemetry & Signals")
        tel_results = check_telemetry_signals()
        all_results.extend(tel_results)
        for r in tel_results:
            print(r)

    # Summary
    n_pass = sum(1 for r in all_results if r.status == "PASS")
    n_warn = sum(1 for r in all_results if r.status == "WARN")
    n_fail = sum(1 for r in all_results if r.status == "FAIL")

    print("\n" + "=" * 64)
    print(f"  SUMMARY: {n_pass} PASS, {n_warn} WARN, {n_fail} FAIL")

    if n_fail > 0:
        print("  🚨 ACTION REQUIRED: Fix FAIL items before running live trader")
        sys.exit(1)
    elif n_warn > 0:
        print("  ⚠️  Review WARN items but likely safe to proceed")
        sys.exit(0)
    else:
        print("  ✅ All checks passed — data is healthy")
        sys.exit(0)

    print("=" * 64)


if __name__ == "__main__":
    main()
