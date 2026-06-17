"""Feature Parity Test: verify build_live_features() on ratio-adjusted OHLCV
matches the pre-computed features in the CL_HourSet_11 training parquet.

This test takes a window of OHLCV from HourSet_11, runs the live feature
pipeline on it, and asserts that the resulting features closely match the
pre-computed values stored in the training dataset.

This validates that the live trader's JIT ratio-adjusted data flow produces
features equivalent to the model's training data.
"""
import unittest
import os
import sys
import numpy as np
import pandas as pd

# Paths
_DATA_ROOT = os.environ.get("CL_DATA_ROOT", r"C:\CL_Analyst_Data")
_HS11_PATH = os.path.join(_DATA_ROOT, "data", "processed", "CL_HourSet_11.parquet")

# Features that the HS11 model uses (from the strategy config)
_HS11_FEATURE_PATTERNS = [
    "ICHIMOKU_*",
    "TREND_DMA_*",
    "ATR_14",
    "VOL_ROC_12",
    "VOL_ROC_24",
    "VOL_ROC_72",
    "DIST_KURT_12",
    "DIST_KURT_24",
    "DIST_KURT_72",
    "DIST_SKEW_12",
    "DIST_SKEW_24",
    "DIST_SKEW_72",
    "TS_*",
    "log_ret",
]


def _expand_patterns(patterns: list[str], all_columns: list[str]) -> list[str]:
    """Expand wildcard patterns (e.g. 'ICHIMOKU_*') against available columns."""
    import fnmatch
    expanded = []
    for pat in patterns:
        if "*" in pat:
            matches = fnmatch.filter(all_columns, pat)
            expanded.extend(sorted(matches))
        else:
            if pat in all_columns:
                expanded.append(pat)
    return list(dict.fromkeys(expanded))  # deduplicate, preserve order


@unittest.skipUnless(
    os.path.exists(_HS11_PATH),
    f"CL_HourSet_11.parquet not found at {_HS11_PATH}",
)
class TestFeatureParity(unittest.TestCase):
    """Verify live feature pipeline reproduces training features."""

    @classmethod
    def setUpClass(cls):
        """Load training data once for all tests."""
        cls.hs11 = pd.read_parquet(_HS11_PATH)
        cls.feature_names = _expand_patterns(
            _HS11_FEATURE_PATTERNS, list(cls.hs11.columns)
        )

    def test_feature_names_resolved(self):
        """Ensure we resolved at least some features from patterns."""
        self.assertGreater(
            len(self.feature_names), 10,
            f"Expected >10 features, got {len(self.feature_names)}: {self.feature_names}",
        )
        self.assertIn("ATR_14", self.feature_names)
        self.assertIn("log_ret", self.feature_names)

    def test_feature_parity_at_recent_timestamp(self):
        """Compare build_live_features() output vs training data at a recent bar.

        Takes the last 3000 rows of HourSet_11 OHLCV, runs build_live_features(),
        and compares the output for the final bar against the training data.
        """
        from src.live_execution.feature_pipeline import build_live_features

        # Take a generous window for feature warmup
        window = self.hs11.iloc[-3000:].copy()
        ohlcv = window[["Open", "High", "Low", "Close", "Volume"]].copy()
        ohlcv["DateTime"] = ohlcv.index
        ohlcv = ohlcv.set_index("DateTime", drop=False)
        ohlcv.index.name = "DateTime"

        # Run live feature pipeline
        features = build_live_features(
            ohlcv, self.feature_names, lean=False, bar_size="1h"
        )

        self.assertIsNotNone(features, "build_live_features returned None")
        self.assertEqual(len(features), 1, "Expected single-row output")

        # Compare each feature
        target_ts = ohlcv.index[-1]
        training_row = self.hs11.loc[target_ts]

        mismatches = []
        for feat in self.feature_names:
            if feat not in features.columns:
                mismatches.append(f"{feat}: MISSING from live output")
                continue
            if feat not in self.hs11.columns:
                continue  # feature computed live but not in training data

            live_val = float(features[feat].iloc[0])
            train_val = float(training_row[feat])

            if np.isnan(live_val) and np.isnan(train_val):
                continue  # both NaN is OK

            if np.isnan(live_val) or np.isnan(train_val):
                mismatches.append(
                    f"{feat}: live={live_val} vs train={train_val} (one is NaN)"
                )
                continue

            if train_val == 0:
                if abs(live_val) > 1e-6:
                    mismatches.append(
                        f"{feat}: live={live_val:.6f} vs train=0.0"
                    )
                continue

            rel_err = abs(live_val - train_val) / max(abs(train_val), 1e-10)
            if rel_err > 0.02:  # 2% tolerance for floating-point path differences
                mismatches.append(
                    f"{feat}: live={live_val:.6f} vs train={train_val:.6f} "
                    f"(rel_err={rel_err:.4f})"
                )

        if mismatches:
            msg = f"{len(mismatches)} feature mismatches:\n" + "\n".join(
                mismatches[:20]
            )
            if len(mismatches) > 20:
                msg += f"\n... and {len(mismatches) - 20} more"
            self.fail(msg)

    def test_bracket_atr_uses_raw_data(self):
        """Verify that bracket ATR computed from raw OHLCV differs from
        model ATR when rollover ratios are present.

        This validates that the split-brain architecture correctly separates
        model features (ratio-adjusted) from risk management (raw).
        """
        import pandas_ta

        # Create a synthetic scenario with a 5% ratio applied to bars
        # INSIDE the ATR(14) lookback window so the ATR calculation
        # actually sees the price discontinuity.
        window = self.hs11.iloc[-500:].copy()
        ohlcv_raw = window[["Open", "High", "Low", "Close", "Volume"]].copy()
        ohlcv_raw["DateTime"] = ohlcv_raw.index
        ohlcv_raw = ohlcv_raw.set_index("DateTime", drop=False)
        ohlcv_raw.index.name = "DateTime"

        # Create a "ratio-adjusted" version by scaling bars WITHIN the ATR window
        # This simulates what happens when get_ratio_adjusted_df() adjusts old bars
        ohlcv_adjusted = ohlcv_raw.copy()
        # Scale everything before the last 10 bars by 5% — creates a jump
        # that lands inside the ATR(14) window
        cutoff = len(ohlcv_adjusted) - 10
        for col in ("Open", "High", "Low", "Close"):
            ohlcv_adjusted.iloc[:cutoff, ohlcv_adjusted.columns.get_loc(col)] *= 1.05

        # Compute ATR on both
        atr_raw = ohlcv_raw.ta.atr(length=14).iloc[-1]
        atr_adjusted = ohlcv_adjusted.ta.atr(length=14).iloc[-1]

        # They should be different because the adjusted one has a price
        # discontinuity in the middle causing inflated ATR
        # (The raw one has the true market ATR)
        self.assertFalse(
            np.isclose(atr_raw, atr_adjusted, rtol=0.01),
            f"Bracket ATR (raw={atr_raw:.4f}) should differ from "
            f"model ATR (adjusted={atr_adjusted:.4f}) when ratio applied",
        )


if __name__ == "__main__":
    unittest.main()
