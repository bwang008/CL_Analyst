"""
Macro Feature Engineering for CL Analyst.

Merges external macro data (FRED, CFTC COT) into bar-level DataFrames
to create features that capture regime context beyond pure OHLCV signals.

All features are derived from daily/weekly data that is forward-filled
to bar resolution (5-min or 1-hour). No lookahead bias — each bar only
sees data that was already published at that point in time.

Feature naming convention:
    MACRO_{SOURCE}_{SIGNAL}_{WINDOW}
    COT_{SIGNAL}_{WINDOW}

Usage:
    from src.features.macro_features import MacroFeatureEngine
    engine = MacroFeatureEngine()
    df = engine.merge_all(df)  # df must have a DateTime index
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.data_paths import get_data_path

log = logging.getLogger(__name__)

# Maximum file age before auto-refresh (seconds)
_FRED_MAX_AGE_SECONDS = 24 * 3600      # 1 day
_COT_MAX_AGE_SECONDS = 7 * 24 * 3600   # 7 days

# Change windows (in trading days)
CHANGE_WINDOWS = [1, 3, 7, 14, 35]

# Percentile windows (in trading days) — floor at 14 for meaningful ranks
PCTILE_WINDOWS = [14, 35, 60]

# Series that must change between consecutive trading days.
# If any of these have identical values for >= the per-series threshold
# consecutive days at the tail, StaleDataException is raised.
#
# Per-series thresholds reflect real-world FRED publication lags:
#   DXY  (DTWEXBGS) — Broad Dollar Index: FRED lags 3-5 business days.
#                     Tightened to 5 (was 7): the old threshold allowed
#                     5-6 days of CHG_1D=0.0 without triggering mute.
#                     With weekend ffill producing 2 identical values and
#                     FRED's 3-5 day lag, 5 catches extended stale periods
#                     while still tolerating normal long-weekend gaps (3-4).
#   VIX  (VIXCLS)   — Published daily by CBOE, but FRED sometimes lags
#                     1-2 days. Use 3 to avoid false weekend mutes.
#   OVX  (OVXCLS)   — Same as VIX. Use 3.
_STALE_THRESHOLDS: dict[str, int] = {
    "DXY": 5,   # FRED Broad Dollar Index: 3-5 day lag (tightened from 7)
    "VIX": 3,   # CBOE VIX via FRED: 1-2 day lag, use 3 for weekend safety
    "OVX": 3,   # CBOE OVX via FRED: same as VIX
}

# Derived features that must not be stuck at zero for extended periods.
# These are computed as pct_change(1) of critical series.  Even if raw
# values pass the _STALE_THRESHOLDS check, FRED publication lag can
# produce identical raw values (2-4 repeats, below threshold) whose
# pct_change is 0.0 — a value the model rarely saw during training.
_FEATURE_STALE_THRESHOLD = 3  # consecutive trading days of CHG_1D == 0.0
_FEATURE_STALE_CRITICAL = [
    "MACRO_DXY_CHG_1D",
    "MACRO_VIX_CHG_1D",
    "MACRO_OVX_CHG_1D",
]


class StaleDataException(RuntimeError):
    """Raised when FRED macro data contains stale (repeated) values.

    This means the FRED CSV file passed the file-age freshness check
    but contains identical daily values for critical series (DXY, VIX,
    OVX) — producing zero-valued CHG_1D features the model has never
    seen during training.

    The live trader should catch this and enter Safety Mute mode,
    blocking new entries until the data resolves.

    Attributes:
        stale_series: Dict mapping series name to the repeated value.
        repeat_count: Number of consecutive identical values detected.
    """

    def __init__(
        self,
        stale_series: dict[str, float],
        repeat_count: int,
        message: str = "",
    ):
        self.stale_series = stale_series
        self.repeat_count = repeat_count
        if not message:
            names = ", ".join(
                f"{k}={v:.4f}" for k, v in stale_series.items()
            )
            message = (
                f"FRED data contains stale values: {names} "
                f"(repeated for {repeat_count} consecutive trading days). "
                f"CHG_1D features will be zero — model input is corrupted. "
                f"Live entries are blocked until data updates."
            )
        super().__init__(message)



class MacroFeatureEngine:
    """Merge external macro data into a bar-level OHLCV DataFrame.

    Loads FRED and CFTC CSV files from ``data/raw/macro/`` and computes
    derived features (changes, percentiles, ratios) before joining to
    the target DataFrame by date.

    Parameters
    ----------
    fred_path : Path or None
        Path to ``fred_macro_data.csv``.  Auto-resolved via data_paths if None.
    cot_path : Path or None
        Path to ``cftc_cot_crude_oil.csv``.  Auto-resolved via data_paths if None.
    """

    def __init__(
        self,
        fred_path: Path | str | None = None,
        cot_path: Path | str | None = None,
    ):
        self.fred_path = Path(fred_path) if fred_path else get_data_path("raw/macro/fred_macro_data.csv")
        self.cot_path = Path(cot_path) if cot_path else get_data_path("raw/macro/cftc_cot_crude_oil.csv")

        self._fred_df: pd.DataFrame | None = None
        self._cot_df: pd.DataFrame | None = None

    # ------------------------------------------------------------------
    # Auto-refresh
    # ------------------------------------------------------------------

    def refresh_if_stale(self) -> None:
        """Re-download FRED and/or COT data if the CSV files are stale.

        Staleness thresholds:
            FRED: >24 hours  (daily indicators)
            COT:  >7 days    (weekly CFTC report)

        Safe to call on every startup — only downloads when actually stale.
        Requires ``FRED_API_KEY`` in the environment or ``.env`` for FRED.
        COT downloads from CFTC directly (no API key needed).
        """
        now = datetime.now().timestamp()

        # --- FRED refresh ---
        fred_stale = True
        if self.fred_path.exists():
            age = now - self.fred_path.stat().st_mtime
            fred_stale = age > _FRED_MAX_AGE_SECONDS
            if fred_stale:
                log.info(
                    "FRED data is stale (%.1f hours old), refreshing...",
                    age / 3600,
                )
            else:
                log.info(
                    "FRED data is fresh (%.1f hours old), skipping refresh",
                    age / 3600,
                )
        else:
            log.info("FRED data file not found, downloading...")

        if fred_stale:
            api_key = os.environ.get("FRED_API_KEY", "")
            if not api_key:
                raise ValueError(
                    "FRED_API_KEY not set — cannot refresh FRED data. "
                    "Add FRED_API_KEY to .env or set as environment variable."
                )
            else:
                try:
                    from scripts.download_macro_data import (
                        download_fred_data,
                        save_fred_data,
                    )
                    fred_data = download_fred_data(api_key)
                    save_fred_data(fred_data)
                    # Clear cached data so next merge_all() reloads
                    self._fred_df = None
                    log.info("FRED data refreshed successfully")
                except Exception as exc:
                    log.error("Failed to refresh FRED data: %s", exc)
                    raise exc

        # --- COT refresh ---
        cot_stale = True
        if self.cot_path.exists():
            age = now - self.cot_path.stat().st_mtime
            cot_stale = age > _COT_MAX_AGE_SECONDS
            if cot_stale:
                log.info(
                    "COT data is stale (%.1f days old), refreshing...",
                    age / 86400,
                )
            else:
                log.info(
                    "COT data is fresh (%.1f days old), skipping refresh",
                    age / 86400,
                )
        else:
            log.info("COT data file not found, downloading...")

        if cot_stale:
            try:
                from scripts.download_macro_data import (
                    download_cot_data,
                    save_cot_data,
                )
                cot_data = download_cot_data()
                save_cot_data(cot_data)
                # Clear cached data so next merge_all() reloads
                self._cot_df = None
                log.info("COT data refreshed successfully")
            except Exception as exc:
                log.error("Failed to refresh COT data: %s", exc)
                raise exc

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_fred(self) -> pd.DataFrame:
        """Load and cache the FRED macro CSV."""
        if self._fred_df is not None:
            return self._fred_df

        if not self.fred_path.exists():
            raise FileNotFoundError(
                f"FRED macro data not found at {self.fred_path}.\n"
                "Run: python scripts/download_macro_data.py --fred-only"
            )

        df = pd.read_csv(self.fred_path, parse_dates=["Date"])
        log.debug("Loaded FRED data: %d rows, columns=%s", len(df), list(df.columns))
        self._fred_df = df
        return df

    def _load_cot(self) -> pd.DataFrame:
        """Load and cache the CFTC COT CSV."""
        if self._cot_df is not None:
            return self._cot_df

        if not self.cot_path.exists():
            raise FileNotFoundError(
                f"CFTC COT data not found at {self.cot_path}.\n"
                "Run: python scripts/download_macro_data.py --cot-only"
            )

        df = pd.read_csv(self.cot_path, parse_dates=["Date"])
        log.debug("Loaded COT data: %d rows, columns=%s", len(df), list(df.columns))
        self._cot_df = df
        return df

    # ------------------------------------------------------------------
    # Value-staleness detection
    # ------------------------------------------------------------------

    def _check_value_staleness(self, df: pd.DataFrame) -> None:
        """Raise ``StaleDataException`` if critical FRED series are stuck.

        Examines the tail of each series in ``_STALE_CRITICAL_SERIES``.
        If any series has ``_STALE_REPEAT_THRESHOLD`` or more consecutive
        identical values at the **end** of the dataset, the data is
        considered stale regardless of the CSV file's modification time.

        This catches the scenario where FRED returns a "fresh" file
        whose actual data hasn't updated — producing CHG_1D = 0 for a
        feature the model has never seen at that value during training.

        Parameters
        ----------
        df : pd.DataFrame
            The FRED daily DataFrame (Date-indexed, already ffilled and
            +1 day shifted) as built inside ``_build_fred_features``.

        Raises
        ------
        StaleDataException
            When one or more critical series have repeated tail values.
        """
        stale: dict[str, float] = {}
        max_repeats = 0

        for col, threshold in _STALE_THRESHOLDS.items():
            if col not in df.columns:
                continue

            series = df[col].dropna()
            if len(series) < threshold + 1:
                continue

            # Count how many consecutive identical values at the tail
            tail_val = series.iloc[-1]
            n_repeat = 0
            for val in reversed(series.values):
                if val == tail_val:
                    n_repeat += 1
                else:
                    break

            if n_repeat >= threshold:
                stale[col] = float(tail_val)
                max_repeats = max(max_repeats, n_repeat)
                log.warning(
                    "FRED value-staleness: %s stuck at %.4f for %d "
                    "consecutive trading days (threshold=%d)",
                    col, tail_val, n_repeat, threshold,
                )

        if stale:
            raise StaleDataException(
                stale_series=stale,
                repeat_count=max_repeats,
            )

    def _check_feature_staleness(self, features: pd.DataFrame) -> list[str]:
        """Warn if derived CHG_1D features are stuck at zero.

        Complements ``_check_value_staleness`` by inspecting *computed*
        features rather than raw FRED series.  This catches the scenario
        where raw values pass the staleness check (e.g. only 2-4 repeated
        values at the tail, below ``_STALE_THRESHOLDS``) but the derived
        ``pct_change(1)`` is 0.0 for multiple consecutive trading days.

        This is a **warning-only** advisory.  It does NOT raise
        ``StaleDataException`` or trigger the safety mute.  FRED's DXY
        publication lag (3-5 business days) routinely produces 3-5
        consecutive CHG_1D = 0.0 days that the model tolerates (it is
        one feature among 200+).  Blocking all trades for a single
        lagging feature is disproportionate to the risk.

        The raw ``_check_value_staleness()`` remains the hard gate.

        Parameters
        ----------
        features : pd.DataFrame
            The daily-resolution features DataFrame produced by
            ``_build_fred_features``, indexed by shifted Date.

        Returns
        -------
        list[str]
            Names of features stuck at zero, empty if all OK.
        """
        stale: dict[str, float] = {}
        threshold = _FEATURE_STALE_THRESHOLD

        for feat in _FEATURE_STALE_CRITICAL:
            if feat not in features.columns:
                continue

            series = features[feat].dropna()
            if len(series) < threshold + 1:
                continue

            # Count consecutive zeros at the tail
            tail_val = series.iloc[-1]
            if tail_val != 0.0:
                continue

            n_zero = 0
            for val in reversed(series.values):
                if val == 0.0:
                    n_zero += 1
                else:
                    break

            if n_zero >= threshold:
                stale[feat] = 0.0
                log.warning(
                    "Feature-staleness advisory: %s stuck at 0.0 for %d "
                    "consecutive trading days (threshold=%d) "
                    "-- not blocking, FRED publication lag is likely cause",
                    feat, n_zero, threshold,
                )

        return list(stale.keys())

    # ------------------------------------------------------------------
    # FRED Feature Engineering
    # ------------------------------------------------------------------

    def _build_fred_features(self) -> pd.DataFrame:
        """Build all FRED-derived features on a daily-resolution DataFrame.

        Returns a DataFrame indexed by Date with all macro feature columns.
        """
        df = self._load_fred().copy()
        df = df.set_index("Date").sort_index()

        # Forward-fill gaps (weekends, holidays)
        df = df.ffill()

        # Shift dates forward by 1 day: FRED values are end-of-day.
        # A bar at 10:00 AM should NOT see today's close — only yesterday's.
        # This shift ensures no intra-day lookahead from daily signals.
        df.index = df.index + pd.Timedelta(days=1)
        log.debug("FRED dates shifted +1 day for end-of-day publication lag")

        features = pd.DataFrame(index=df.index)

        # Process each signal
        for col in ["VIX", "OVX", "DXY", "YIELD_CURVE"]:
            if col not in df.columns:
                log.warning("FRED column '%s' not found — skipping", col)
                continue

            series = df[col]

            # Raw level (already ffilled)
            features[f"MACRO_{col}"] = series

            # Change features: pct_change over N trading days
            for w in CHANGE_WINDOWS:
                features[f"MACRO_{col}_CHG_{w}D"] = series.pct_change(w)

            # Percentile features: rank over N trading days
            for w in PCTILE_WINDOWS:
                features[f"MACRO_{col}_PCTILE_{w}D"] = series.rolling(w).apply(
                    lambda x: pd.Series(x).rank(pct=True).iloc[-1],
                    raw=False,
                )

        # Derived features
        if "VIX" in df.columns and "OVX" in df.columns:
            ovx_safe = df["OVX"].replace(0, np.nan)
            features["MACRO_VIX_OVX_RATIO"] = df["VIX"] / ovx_safe

        if "YIELD_CURVE" in df.columns:
            features["MACRO_YIELD_CURVE_SIGN"] = (df["YIELD_CURVE"] > 0).astype(int)

        # FED_FUNDS — monthly, just use raw + change (no percentile needed)
        if "FED_FUNDS" in df.columns:
            features["MACRO_FED_FUNDS"] = df["FED_FUNDS"]

        log.debug("Built %d FRED features", len(features.columns))

        # Value-staleness gate: raise if critical series have repeated
        # values at the tail — file age alone does not guarantee freshness.
        self._check_value_staleness(df)

        # Feature-staleness advisory: warn if derived CHG_1D features
        # are stuck at zero.  This is monitoring-only — does NOT raise.
        # The raw _check_value_staleness() above is the hard mute gate.
        stale_features = self._check_feature_staleness(features)
        if stale_features:
            log.warning(
                "Feature-staleness detected in %s "
                "(advisory only, not blocking trades)",
                stale_features,
            )

        return features

    # ------------------------------------------------------------------
    # COT Feature Engineering
    # ------------------------------------------------------------------

    def _build_cot_features(self) -> pd.DataFrame:
        """Build all COT-derived features on a weekly-resolution DataFrame.

        Returns a DataFrame indexed by Date with all COT feature columns.

        IMPORTANT: CFTC COT data is measured on Tuesday but published on
        Friday.  We shift the date index forward by 3 business days so
        that each bar only sees COT data after its actual publication.
        Without this shift, Wed/Thu bars would have lookahead bias.
        """
        df = self._load_cot().copy()
        df = df.set_index("Date").sort_index()

        # Shift dates forward by 3 business days to match publication date.
        # This prevents lookahead: Tuesday's data becomes available Friday.
        df.index = df.index + pd.offsets.BDay(3)
        log.debug("COT dates shifted +3 business days for publication lag")

        features = pd.DataFrame(index=df.index)

        # Open Interest
        if "OI" in df.columns:
            features["COT_OI"] = df["OI"]
            for w in [1, 3, 5]:  # weeks
                features[f"COT_OI_CHG_{w}W"] = df["OI"].pct_change(w)

        # Money Manager Net Position
        if "MM_Net" in df.columns:
            features["COT_MM_NET"] = df["MM_Net"]
            # Percentile over 52 weeks (1 year)
            features["COT_MM_NET_PCTILE_52W"] = df["MM_Net"].rolling(52).apply(
                lambda x: pd.Series(x).rank(pct=True).iloc[-1],
                raw=False,
            )
            # Percentiles at shorter windows
            for w in [14, 35]:
                features[f"COT_MM_NET_PCTILE_{w}W"] = df["MM_Net"].rolling(w).apply(
                    lambda x: pd.Series(x).rank(pct=True).iloc[-1],
                    raw=False,
                )
            # 4-week momentum
            features["COT_MM_MOMENTUM_4W"] = df["MM_Net"].diff(4)

        # Producer/Merchant Net (commercials — "smart money")
        if "Prod_Net" in df.columns:
            features["COT_PROD_NET"] = df["Prod_Net"]
            features["COT_PROD_NET_PCTILE_52W"] = df["Prod_Net"].rolling(52).apply(
                lambda x: pd.Series(x).rank(pct=True).iloc[-1],
                raw=False,
            )

        # Spec (Swap Dealer) Net
        if "Spec_Net" in df.columns:
            features["COT_SPEC_NET"] = df["Spec_Net"]
            features["COT_SPEC_NET_PCTILE_52W"] = df["Spec_Net"].rolling(52).apply(
                lambda x: pd.Series(x).rank(pct=True).iloc[-1],
                raw=False,
            )

        log.debug("Built %d COT features", len(features.columns))
        return features

    # ------------------------------------------------------------------
    # Merge into bar-level DataFrame
    # ------------------------------------------------------------------

    def merge_all(
        self,
        df: pd.DataFrame,
        include_fred: bool = True,
        include_cot: bool = True,
    ) -> pd.DataFrame:
        """Merge all macro features into a bar-level OHLCV DataFrame.

        The target DataFrame must have a DatetimeIndex. Macro features
        are joined by date (ignoring time) and forward-filled so every
        bar within a trading day gets the most recent daily value.

        Parameters
        ----------
        df : pd.DataFrame
            Bar-level DataFrame with a DatetimeIndex.
        include_fred : bool
            Include FRED-derived features (VIX, DXY, etc.).
        include_cot : bool
            Include CFTC COT-derived features.

        Returns
        -------
        pd.DataFrame
            The input DataFrame with macro feature columns appended.
        """
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError(
                "DataFrame must have a DatetimeIndex for macro merge. "
                f"Got index type: {type(df.index)}"
            )

        # Extract date from the bar-level index for joining
        bar_dates = df.index.normalize()  # Strip time → midnight
        
        # Macro datasets are tz-naive. Ensure bar_dates are tz-naive for reindexing matching.
        if bar_dates.tzinfo is not None:
            bar_dates = bar_dates.tz_localize(None)

        n_before = len(df.columns)

        if include_fred:
            try:
                fred_features = self._build_fred_features()
                # Join by date: each bar gets the daily macro value
                fred_aligned = fred_features.reindex(bar_dates)
                # Forward-fill to handle weekends/holidays in bar data
                fred_aligned = fred_aligned.ffill()
                # Reset index to match the bar-level index
                fred_aligned.index = df.index
                for col in fred_aligned.columns:
                    df[col] = fred_aligned[col].values
                log.debug("Merged %d FRED features", len(fred_aligned.columns))
            except Exception as exc:
                log.error("CRITICAL Error merging FRED features: %s", exc)
                raise

        if include_cot:
            try:
                cot_features = self._build_cot_features()
                # COT is weekly — join by date, ffill to every bar
                cot_aligned = cot_features.reindex(bar_dates)
                cot_aligned = cot_aligned.ffill()
                cot_aligned.index = df.index
                for col in cot_aligned.columns:
                    df[col] = cot_aligned[col].values
                log.debug("Merged %d COT features", len(cot_aligned.columns))
            except Exception as exc:
                log.error("CRITICAL Error merging COT features: %s", exc)
                raise

        n_added = len(df.columns) - n_before
        log.debug("Total macro features added: %d", n_added)
        return df

    def get_feature_names(self) -> list[str]:
        """Return the list of feature names this engine produces.

        Useful for documentation and verifying feature counts.
        """
        # Build features on dummy dates to get column names
        names: list[str] = []

        try:
            fred_features = self._build_fred_features()
            names.extend(fred_features.columns.tolist())
        except Exception:
            pass

        try:
            cot_features = self._build_cot_features()
            names.extend(cot_features.columns.tolist())
        except Exception:
            pass

        return names
