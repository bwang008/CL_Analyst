"""Futures-curve calendar-spread features (``CURVE_*``) — HourSet_03B.

``CurveFeatureEngine`` computes calendar-spread features from two RAW
(unadjusted) Databento calendar-ranked continuous legs (e.g. ``NG.c.0`` front,
``NG.c.1`` second) and merges them into a bar-level training DataFrame.

Design contract (ticket ng-03b-calendar-spread-dataset_07122026_0249, v2):

* **CURVE_ vs TS_ separation.** The ``CURVE_`` namespace is strictly separate
  from the ``TS_`` / ``term_structure`` cross-window indicator features
  produced by ``AlphaFactory.add_term_structure_shapes`` (whose ``TS_`` output
  prefix is hardcoded). Needed shape logic (z-diff, vol-ratio) is mirrored
  here locally with ``CURVE_`` naming; ``alpha_factory.py`` is untouched.
* **Curvature out of scope.** True curve CURVATURE requires a third leg
  (``NG.c.2``) — explicitly out of scope for 03B.
* **Joined-timeline window semantics.** Both legs are INNER-joined at
  concurrent timestamps; every rolling window / shift counts JOINED bars,
  which can deviate from training-bar counts wherever the second leg has
  coverage holes. This is documented, accepted behaviour.
* **Signed-series rule.** ``spread_pct`` crosses zero, so per the house rule
  (alpha_factory TERM_STRUCTURE_FAMILIES: signed indicators get
  Diff/Sign_Agreement/Regime_Cross — never Ratio) all rate-of-change features
  are SIMPLE DIFFERENCES (``diff(n)``), never ``pct_change``; no
  Ratio/Log_Ratio/Invert is computed on ``spread_pct`` itself. The ratio
  angle is covered by ``CURVE_SPREAD_VOLRATIO_24v840`` (spread-vol is a
  positive series — ratio legal, denominator clipped at 1e-8) and
  ``CURVE_SPREAD_Z_DIFF_24v840``.
* **Slope normalization deviation.** ``CURVE_SPREAD_SLOPE_{24,72}`` use
  ``_rolling_slope_r2_numba`` WITHOUT the Close-division used by
  ``TREND_LR_SLOPE`` — spread_pct is already a unitless %/bar quantity.
* **Fail-loud policy / the two-and-only-two 0.0-neutral exceptions.**
  Everything fails loud (missing files, empty joins, unexpected NaN, coverage
  gaps beyond the bounded ffill limit, leading-NaN runs beyond budget).
  Exactly two documented cases emit a neutral ``0.0`` instead of NaN/raise:
    1. rolling z-scores (incl. the seasonal z) where the window std == 0;
    2. ``CURVE_SPREAD_SEASONAL_Z`` cold-start rows with fewer than
       ``seasonal_min_prior_years`` DISTINCT PRIOR YEARS of same-bucket
       history (distinct-year count, NOT observation count).
  Rows are NEVER dropped by this engine (index parity with the 02B control
  depends on it).
* **Warmup reasoning.** Curve windows top out at 2160 joined bars — below the
  existing 4320-bar macro max — so wherever 02B rows survive the pipeline
  warmup the curve windows already have sufficient primary history. The
  leading-NaN region (~2160 joined bars from the 2010-06 leg start) ends well
  before the first surviving 02B row; residual-NaN risk comes ONLY from
  second-leg coverage, governed by ``FFILL_LIMIT_BARS`` + fail-loud.
* **CURVE_ROLL_YIELD deferred to 03C.** No contractual days-to-expiry series
  exists training-side today; "bars to next OBSERVED roll" is a
  lookahead-ILLEGAL substitute and is never computed. The deferral is logged
  loudly on every build (never silently omitted).
* **Seasonal year semantics.** For the (default) ISO week-of-year bucket the
  distinct-prior-year gate counts ISO years (``isocalendar().year``) so that
  year-boundary bars stay paired with their ISO week; the ``month`` bucket
  counts calendar years.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.databento_data_builder import DatabentoDataBuilder
from src.features.alpha_factory import _rolling_slope_r2_numba

log = logging.getLogger(__name__)

# Bounded forward-fill limit (in bars) used when reindexing the joined curve
# timeline onto the training bar index. Covers second-leg coverage holes up to
# one day; anything longer is a hard error (fail-loud, no silent staleness).
# PROVISIONAL default 24 pending the leg-coverage scan (human decision H3).
FFILL_LIMIT_BARS = 24

# House rolling windows (R4): 24/72/168/336/840 (~1M) /2160 (~3M) hourly bars.
Z_WINDOWS = (24, 72, 168, 336, 840, 2160)
SLOPE_WINDOWS = (24, 72)
VOL_WINDOWS_EMITTED = (24, 168)
VOL_WINDOW_ANCHOR = 840          # internal only — VOLRATIO denominator
PCTL_WINDOW = 840
WOW_SHIFT = 168
MOM_SHIFT = 840
ACCEL_WINDOW = 24
ROC_WINDOWS = (1, 3, 6, 12, 24)

# The full, literal emitted column set (29 columns). The 03B A/B parity gate
# pins this exact enumeration — update tests + gate together or not at all.
CURVE_FEATURE_COLUMNS = [
    "CURVE_SPREAD_PCT",
    "CURVE_CONTANGO_SIGN",
    "CURVE_SPREAD_ROC_1",
    "CURVE_SPREAD_ROC_3",
    "CURVE_SPREAD_ROC_6",
    "CURVE_SPREAD_ROC_12",
    "CURVE_SPREAD_ROC_24",
    "CURVE_SPREAD_SLOPE_24",
    "CURVE_SPREAD_SLOPE_72",
    "CURVE_SPREAD_SLOPE_R2_24",
    "CURVE_SPREAD_SLOPE_R2_72",
    "CURVE_SPREAD_ACCEL_24",
    "CURVE_SPREAD_PCT_Z_24",
    "CURVE_SPREAD_PCT_Z_72",
    "CURVE_SPREAD_PCT_Z_168",
    "CURVE_SPREAD_PCT_Z_336",
    "CURVE_SPREAD_PCT_Z_840",
    "CURVE_SPREAD_PCT_Z_2160",
    "CURVE_SPREAD_DIST_MEAN_840",
    "CURVE_SPREAD_PCTL_840",
    "CURVE_SPREAD_Z_DIFF_24v840",
    "CURVE_SPREAD_VOLRATIO_24v840",
    "CURVE_SPREAD_VOL_24",
    "CURVE_SPREAD_VOL_168",
    "CURVE_SPREAD_VOLVOL_168",
    "CURVE_SPREAD_WOW",
    "CURVE_SPREAD_MOM_840",
    "CURVE_BARS_SINCE_ROLL",
    "CURVE_SPREAD_SEASONAL_Z",
]


class CurveFeatureEngine:
    """Compute and merge calendar-spread curve features from two raw legs.

    Parameters
    ----------
    front_leg_csv : str
        Path to the RAW Databento ohlcv CSV of the front calendar leg
        (e.g. ``NG.c.0``). Never a back-adjusted series.
    second_leg_csv : str
        Path to the RAW Databento ohlcv CSV of the second calendar leg
        (e.g. ``NG.c.1``). Never a back-adjusted series.
    seasonal_bucket : str
        Seasonal grouping key for ``CURVE_SPREAD_SEASONAL_Z``:
        ``"week"`` (default, ISO week-of-year) or ``"month"``.
        ``"doy_smoothed"`` is schema-reserved but NOT implemented in 03B —
        requesting it raises loudly.
    seasonal_min_prior_years : int
        Distinct PRIOR years of same-bucket history required before the
        seasonal z emits a real value (else 0.0 neutral). Must be >= 2.
    seasonal_pctl : bool
        Reserved for the optional ``CURVE_SPREAD_SEASONAL_PCTL`` companion —
        opted OUT for 03B (human decision H-seasonal-2). Requesting it raises
        loudly rather than silently ignoring the flag.
    """

    def __init__(
        self,
        front_leg_csv: str,
        second_leg_csv: str,
        seasonal_bucket: str = "week",
        seasonal_min_prior_years: int = 2,
        seasonal_pctl: bool = False,
    ):
        if not front_leg_csv or not str(front_leg_csv).strip():
            raise ValueError("front_leg_csv must be a non-empty path (fail-loud, no silent defaults).")
        if not second_leg_csv or not str(second_leg_csv).strip():
            raise ValueError("second_leg_csv must be a non-empty path (fail-loud, no silent defaults).")
        self.front_leg_csv = Path(front_leg_csv)
        self.second_leg_csv = Path(second_leg_csv)
        if not self.front_leg_csv.exists():
            raise FileNotFoundError(f"Curve front leg CSV not found: {self.front_leg_csv}")
        if not self.second_leg_csv.exists():
            raise FileNotFoundError(f"Curve second leg CSV not found: {self.second_leg_csv}")

        if seasonal_bucket == "doy_smoothed":
            raise NotImplementedError(
                "curve_seasonal_bucket='doy_smoothed' is schema-reserved but not implemented "
                "in 03B (human decision H-seasonal-1 selected 'week'). Implement test-first "
                "before use — do not fall back silently."
            )
        if seasonal_bucket not in ("week", "month"):
            raise ValueError(
                f"Unknown curve_seasonal_bucket {seasonal_bucket!r} — expected 'week' or 'month'."
            )
        if seasonal_min_prior_years < 2:
            raise ValueError(
                f"seasonal_min_prior_years must be >= 2, got {seasonal_min_prior_years} "
                f"(a single prior year cannot anchor a seasonal anomaly)."
            )
        if seasonal_pctl:
            raise NotImplementedError(
                "CURVE_SPREAD_SEASONAL_PCTL is opted OUT for 03B (human decision H-seasonal-2). "
                "Implement test-first (prior-only percentile + no-lookahead mutation coverage) "
                "before enabling — the flag is never silently ignored."
            )

        self.seasonal_bucket = seasonal_bucket
        self.seasonal_min_prior_years = int(seasonal_min_prior_years)
        self.seasonal_pctl = bool(seasonal_pctl)

    # ------------------------------------------------------------------
    # Leg loading + join
    # ------------------------------------------------------------------

    def _load_leg(self, path: Path, label: str) -> pd.DataFrame:
        """Load one raw Databento leg -> tz-naive UTC-indexed close + instrument_id."""
        raw = DatabentoDataBuilder().parse_raw_csv(str(path))
        if "ts_event" not in raw.columns:
            raise ValueError(f"{label} leg {path} has no ts_event column — not a Databento ohlcv CSV.")
        ts = raw["ts_event"]
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_localize(None)  # Databento ts_event is UTC; keep tz-naive UTC convention
        leg = pd.DataFrame(
            {"close": raw["close"].values, "instrument_id": raw["instrument_id"].values},
            index=pd.DatetimeIndex(ts.values, name="DateTime"),
        )
        leg = leg.sort_index()
        if leg["close"].isna().any():
            n_bad = int(leg["close"].isna().sum())
            raise ValueError(f"{label} leg {path} has {n_bad} NaN close values — corrupt input, fail-loud.")
        if leg.index.has_duplicates:
            dups = leg.index[leg.index.duplicated()].unique()
            raise ValueError(
                f"{label} leg {path} has {len(dups)} duplicate timestamps "
                f"(first: {list(dups[:5])}) — one batch job per leg is required."
            )
        return leg

    def _join_legs(self) -> pd.DataFrame:
        """INNER-join the two legs at concurrent timestamps.

        Returns a DataFrame indexed by the joined (tz-naive UTC) timeline with
        columns F1 (front close), F2 (second close), front_iid.
        """
        front = self._load_leg(self.front_leg_csv, "front")
        second = self._load_leg(self.second_leg_csv, "second")

        joined = pd.DataFrame(
            {
                "F1": front["close"],
                "front_iid": front["instrument_id"],
            }
        ).join(second["close"].rename("F2"), how="inner")

        if joined.empty:
            raise ValueError(
                f"Curve legs share NO concurrent timestamps "
                f"({self.front_leg_csv} vs {self.second_leg_csv}) — wrong files?"
            )
        if not joined.index.is_monotonic_increasing:
            raise ValueError("Joined curve timeline is not monotonic increasing — corrupt input.")
        if (joined["F1"] <= 0).any():
            raise ValueError("Front leg contains non-positive close prices — spread_pct undefined, fail-loud.")

        n_front, n_second, n_join = len(front), len(second), len(joined)
        log.info(
            "Curve join: front=%d bars, second=%d bars, joined=%d bars (%.1f%% of front)",
            n_front, n_second, n_join, 100.0 * n_join / max(n_front, 1),
        )
        return joined

    # ------------------------------------------------------------------
    # Feature construction (all on the JOINED timeline)
    # ------------------------------------------------------------------

    @staticmethod
    def _zscore(s: pd.Series, window: int) -> pd.Series:
        """Rolling z-score; std == 0 -> 0.0 (documented neutral exception #1)."""
        mean = s.rolling(window).mean()
        std = s.rolling(window).std()
        z = (s - mean) / std
        return z.mask(std == 0, 0.0)

    def _seasonal_z(self, spread: pd.Series) -> pd.Series:
        """Prior-only seasonal anomaly z-score (CURVE_SPREAD_SEASONAL_Z).

        Within each seasonal bucket, in strict time order:
            mean = group.expanding().mean().shift(1)
            std  = group.expanding().std().shift(1)
        so no row ever sees data at or after its own timestamp.

        A row emits a real z only when its bucket has same-bucket history from
        >= ``seasonal_min_prior_years`` DISTINCT years STRICTLY BEFORE the
        row's own year (ISO year for the week bucket, calendar year for the
        month bucket). Otherwise — and when the prior-only std is 0 — it
        emits the neutral 0.0 (documented exceptions #1/#2). Never NaN,
        never a dropped row.
        """
        idx = spread.index
        if self.seasonal_bucket == "week":
            iso = idx.isocalendar()
            bucket = pd.Series(np.asarray(iso["week"], dtype=np.int64), index=idx)
            years = pd.Series(np.asarray(iso["year"], dtype=np.int64), index=idx)
        else:  # "month" (constructor already rejected everything else)
            bucket = pd.Series(idx.month.astype(np.int64), index=idx)
            years = pd.Series(idx.year.astype(np.int64), index=idx)

        grouped = spread.groupby(bucket.values)
        mean_prior = grouped.transform(lambda g: g.expanding().mean().shift(1))
        std_prior = grouped.transform(lambda g: g.expanding().std().shift(1))

        def _distinct_prior_years(g: pd.Series) -> pd.Series:
            # distinct years among strictly-prior same-bucket rows, EXCLUDING
            # the row's own year (rows are in strict time order within group).
            first_of_year = ~g.duplicated()
            seen_before = first_of_year.cumsum().shift(1).fillna(0.0)
            own_year_seen = (~first_of_year).astype(float)
            return seen_before - own_year_seen

        prior_years = years.groupby(bucket.values).transform(_distinct_prior_years)

        z = (spread - mean_prior) / std_prior
        eligible = (
            (prior_years >= self.seasonal_min_prior_years)
            & std_prior.notna()
            & (std_prior != 0)
        )
        seasonal = pd.Series(
            np.where(eligible.values, z.values, 0.0), index=idx, dtype=np.float64
        )
        if seasonal.isna().any():
            n_bad = int(seasonal.isna().sum())
            raise ValueError(
                f"CURVE_SPREAD_SEASONAL_Z produced {n_bad} NaN values — construction bug "
                f"(the seasonal column must be total: real z, or neutral 0.0)."
            )
        return seasonal

    @staticmethod
    def _bars_since_roll(front_iid: pd.Series) -> pd.Series:
        """Joined bars since the last front-leg roll (instrument_id change).

        Backward-looking only. Computed on the JOINED timeline so a roll is
        detected even when the exact roll bar was dropped by the inner join
        (the instrument_id still changes across consecutive joined rows).
        NaN before the first observed roll (leading region, budgeted at merge).
        """
        iid = front_iid
        is_roll = (iid != iid.shift(1)) & iid.shift(1).notna()
        pos = np.arange(len(iid), dtype=np.float64)
        roll_pos = pd.Series(np.where(is_roll.values, pos, np.nan), index=iid.index).ffill()
        return pd.Series(pos, index=iid.index) - roll_pos

    def build_features(self) -> pd.DataFrame:
        """Compute all 29 CURVE_* columns on the joined timeline."""
        joined = self._join_legs()
        f1 = joined["F1"].astype(np.float64)
        f2 = joined["F2"].astype(np.float64)
        spread = (f2 - f1) / f1
        if spread.isna().any():
            raise ValueError("spread_pct contains NaN on the joined timeline — corrupt join, fail-loud.")

        out = pd.DataFrame(index=joined.index)

        # -- Level / state -------------------------------------------------
        out["CURVE_SPREAD_PCT"] = spread
        out["CURVE_CONTANGO_SIGN"] = np.sign(f2 - f1).astype(np.int64)

        # -- Velocity: simple differences (signed series — NEVER pct_change) --
        for n in ROC_WINDOWS:
            out[f"CURVE_SPREAD_ROC_{n}"] = spread.diff(n)

        # -- Velocity: rolling LR slope / R2 (no Close-division; see module doc) --
        y = spread.to_numpy(dtype=np.float64)
        slope_results = {w: _rolling_slope_r2_numba(y, w) for w in SLOPE_WINDOWS}
        for w in SLOPE_WINDOWS:
            out[f"CURVE_SPREAD_SLOPE_{w}"] = slope_results[w][0]
        for w in SLOPE_WINDOWS:
            out[f"CURVE_SPREAD_SLOPE_R2_{w}"] = slope_results[w][1]

        # -- Acceleration: second difference ------------------------------
        roc_accel = spread.diff(ACCEL_WINDOW)
        out[f"CURVE_SPREAD_ACCEL_{ACCEL_WINDOW}"] = roc_accel - roc_accel.shift(ACCEL_WINDOW)

        # -- Anchor / gap --------------------------------------------------
        z_cols: dict[int, pd.Series] = {}
        for w in Z_WINDOWS:
            z_cols[w] = self._zscore(spread, w)
            out[f"CURVE_SPREAD_PCT_Z_{w}"] = z_cols[w]
        out["CURVE_SPREAD_DIST_MEAN_840"] = spread - spread.rolling(840).mean()
        # Native Rolling.rank — the post-c1c78fc fast path (never apply-rank).
        out[f"CURVE_SPREAD_PCTL_{PCTL_WINDOW}"] = spread.rolling(PCTL_WINDOW).rank(pct=True)

        # -- Cross-timeframe regime (local CURVE_ mirrors of the TS_ shapes) --
        out["CURVE_SPREAD_Z_DIFF_24v840"] = z_cols[24] - z_cols[840]
        vol_24 = spread.rolling(24).std()
        vol_168 = spread.rolling(168).std()
        vol_anchor = spread.rolling(VOL_WINDOW_ANCHOR).std()  # internal, unemitted
        out["CURVE_SPREAD_VOLRATIO_24v840"] = vol_24 / vol_anchor.clip(lower=1e-8)

        # -- Spread vol ------------------------------------------------------
        out["CURVE_SPREAD_VOL_24"] = vol_24
        out["CURVE_SPREAD_VOL_168"] = vol_168
        # House VOL_VOLVOL coefficient-of-variation pattern.
        out["CURVE_SPREAD_VOLVOL_168"] = (
            vol_168.rolling(168).std() / vol_168.rolling(168).mean()
        )

        # -- Long change ----------------------------------------------------
        out["CURVE_SPREAD_WOW"] = spread - spread.shift(WOW_SHIFT)
        out[f"CURVE_SPREAD_MOM_{MOM_SHIFT}"] = spread - spread.shift(MOM_SHIFT)

        # -- Roll context -----------------------------------------------------
        out["CURVE_BARS_SINCE_ROLL"] = self._bars_since_roll(joined["front_iid"])

        # -- CURVE_ROLL_YIELD: DEFERRED to 03C (H-rollyield). LOUD by design. --
        log.warning(
            "CURVE_ROLL_YIELD DEFERRED to 03C: no contractual days-to-expiry series exists "
            "training-side; 'bars to next OBSERVED roll' is a lookahead-ILLEGAL substitute and "
            "is never computed. Emitting %d CURVE_* columns without ROLL_YIELD.",
            len(CURVE_FEATURE_COLUMNS),
        )

        # -- Seasonality -----------------------------------------------------
        out["CURVE_SPREAD_SEASONAL_Z"] = self._seasonal_z(spread)

        # Exact-set guard: the parity gate pins the literal 29-name list.
        if list(out.columns) != CURVE_FEATURE_COLUMNS:
            raise ValueError(
                f"CurveFeatureEngine emitted an unexpected column set: {list(out.columns)} "
                f"!= {CURVE_FEATURE_COLUMNS}"
            )
        return out

    # ------------------------------------------------------------------
    # Merge into the training frame
    # ------------------------------------------------------------------

    def merge_curve(self, df: pd.DataFrame, max_leading_nan_bars: int) -> pd.DataFrame:
        """Merge all CURVE_* features into a bar-level training DataFrame.

        Same index guards as ``MacroFeatureEngine.merge_all``: DatetimeIndex,
        tz-naive comparison, monotonic increasing, unique. The joined-curve
        features are reindexed onto the bar index with a BOUNDED forward fill
        (``limit=FFILL_LIMIT_BARS``); every column is then validated:

        * all-NaN column                              -> ValueError
        * leading-NaN run > ``max_leading_nan_bars``  -> ValueError
        * ANY residual NaN after the first valid bar  -> ValueError listing
          the gap timestamps

        No silent nulls, no row drops.
        """
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError(
                f"DataFrame must have a DatetimeIndex for curve merge. Got index type: {type(df.index)}"
            )
        bar_times = df.index
        if bar_times.tzinfo is not None:
            bar_times = bar_times.tz_localize(None)
        if not bar_times.is_monotonic_increasing:
            raise ValueError("Bar index must be monotonic increasing for ffill reindex.")
        if bar_times.has_duplicates:
            raise ValueError("Bar index has duplicate timestamps — curve merge would be ambiguous.")

        feats = self.build_features()

        collisions = sorted(set(feats.columns) & set(df.columns))
        if collisions:
            raise ValueError(f"Curve merge column collision(s) with the training frame: {collisions}")

        aligned = feats.reindex(bar_times, method="ffill", limit=FFILL_LIMIT_BARS)

        leading_counts: dict[str, int] = {}
        for col in aligned.columns:
            s = aligned[col]
            first = s.first_valid_index()
            if first is None:
                raise ValueError(
                    f"Curve column {col} is all-NaN after merge — legs do not cover the "
                    f"training window ({bar_times[0]} .. {bar_times[-1]})."
                )
            first_pos = int(aligned.index.get_loc(first))
            leading_counts[col] = first_pos
            if first_pos > max_leading_nan_bars:
                raise ValueError(
                    f"Curve column {col} leading-NaN run is {first_pos} bars "
                    f"(> budget {max_leading_nan_bars}) — leg history starts too late "
                    f"for this training window."
                )
            tail = s.iloc[first_pos:]
            if tail.isna().any():
                gap_times = tail.index[tail.isna()]
                raise ValueError(
                    f"Curve column {col} has {len(gap_times)} residual NaN bar(s) after its "
                    f"first valid bar ({first}) — second-leg coverage gap(s) exceed the "
                    f"{FFILL_LIMIT_BARS}-bar ffill limit. First gaps: "
                    f"{[str(t) for t in gap_times[:10]]}"
                )

        max_leading = max(leading_counts.values())
        log.info(
            "Curve merge coverage: %d columns onto %d bars; max leading-NaN run %d bars "
            "(budget %d, column %s); zero residual NaN.",
            len(aligned.columns), len(bar_times), max_leading, max_leading_nan_bars,
            max(leading_counts, key=leading_counts.get),
        )

        for col in aligned.columns:
            df[col] = aligned[col].values
        return df
