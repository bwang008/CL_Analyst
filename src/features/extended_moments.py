"""Extended-moments feature pack (HourSet_04E+).

Second-degree gradients, cross-moments between series, serial-structure /
time-asymmetry statistics, event-time durations, robust quantile moments,
jump decompositions, volume-distribution stats, and family-completion
mirrors — all derived from data the 02B datasets already load (OHLCV +
existing MACRO/VOL columns). No new data sources.

Families emitted (53 columns):
  TREND_LR_ACCEL_{24,72,168}      slope-of-slope (second derivative of log price)
  TREND_QUAD_CURV_{24,72,168}     quadratic-fit convexity, z-scored vs 840
  XMOM_RV_CORR_{24,168}           corr(log_ret, dlog Volume)
  XMOM_LEV_CORR_{72,336}          corr(log_ret, dVOL_YZ_24) — leverage-effect sign
  XMOM_DXY_BETA_{336,840}         rolling beta of 24-bar return on MACRO_DXY_CHG_1D
  XMOM_OVX_BETA_{336,840}         rolling beta of 24-bar return on MACRO_OVX_CHG_1D
  STRUC_TREV_{24,168}             Ramsey-Rothman time-reversal asymmetry (sigma^3-normalised)
  STRUC_AC1_{24,168}              lag-1 return autocorrelation
  STRUC_VR4_168                   4-lag variance ratio
  DUR_SINCE_HIGH_{72,336,840}     bars since window Close-high, / window
  DUR_SINCE_LOW_{72,336,840}      bars since window Close-low, / window
  DUR_SINCE_SHOCK                 log1p(bars since |log_ret| > 2*sigma_840)
  DUR_RUN_Z                       signed same-direction run length, z vs 840 history
  DIST_QSKEW_{24,120}             Bowley quartile skew
  DIST_QSKEW10_{24,120}           decile (q10/q90) skew
  VOL_JUMP_RATIO_{24,168}         realized variance / bipower variation
  VOL_MAXSUM_{24,168}             max|r| / sum|r| — jump vs grind
  VOL_SEMI_LOGRATIO_{72,336}      log(downside semivariance / upside semivariance)/2
  VOL_RATIO_PKCC_24               Parkinson vol / close-close vol
  VOLU_TOD_SURPRISE               log-volume minus prior same-(dow,hour) 8-slot mean
  VOLU_Z_{24,168}                 log-volume z-score
  VOLU_HHI_{24,168}               volume concentration (Herfindahl, uniform==1)
  EXHAUST_DIST_LOW_{24,72,168,336,840}  (Close - rolling min Close) / ATR_14
  STRUC_CLV_MEAN_{24,168}         rolling mean close-location-value
  LIQ_ROLL_{24,168}               Roll autocovariance spread estimate
  MACRO_VRP_OVX_168               OVX minus annualised realized vol (variance risk premium)

Design rules (enforced by tests):
  * Continuous only — no binary flags (144-model audit: sign/cross flags are dead).
  * Causal — every value uses only bars <= t; anti-lookahead is test-enforced.
  * Loud inputs — missing required input columns raise, never default.
  * Additive — gated by FeatureConfig.include_extended_moments (default False);
    existing DataMaps rebuild byte-identical.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:  # same optional-numba pattern as alpha_factory
    from numba import njit

    def _jit_or_py(func):
        return njit(cache=True)(func)

except ImportError:  # pragma: no cover - numba is present in the trader env

    def _jit_or_py(func):
        return func


@_jit_or_py
def _bars_since_extreme_numba(values, window, use_max):
    """Bars since the rolling-window extreme (argmax/argmin), NaN in warmup."""
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(window - 1, n):
        best = values[i - window + 1]
        best_j = 0
        for j in range(1, window):
            v = values[i - window + 1 + j]
            if use_max:
                if v >= best:  # >=: ties resolve to the MOST RECENT extreme
                    best = v
                    best_j = j
            else:
                if v <= best:
                    best = v
                    best_j = j
        out[i] = (window - 1) - best_j
    return out


# Fixed 8-week lookback for the same-(dow,hour) volume norm.
_TOD_LOOKBACK_SLOTS = 8
_TOD_MIN_SLOTS = 4
_EPS = 1e-12
# Hourly bars -> annualised %: sqrt(24 * 252) * 100 (constant by convention;
# only consistency matters for tree models).
_ANNUALISE_HOURLY_PCT = float(np.sqrt(24.0 * 252.0) * 100.0)


class ExtendedMomentsEngine:
    """Computes the extended-moments pack on a feature frame in-pipeline.

    Runs as Step 4.6 of ``process_from_config`` — after AlphaFactory and the
    external-macro merge, before targets/normalisation/drop_features — so all
    required input columns exist regardless of what a later ``drop_features``
    list removes from the shipped parquet.
    """

    REQUIRED_COLUMNS = (
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "log_ret",
        "VOL_YZ_24",
        "VOL_YZ_168",
        "VOL_PARK_24",
        "MACRO_DXY_CHG_1D",
        "MACRO_OVX",
        "MACRO_OVX_CHG_1D",
    )

    def add_features(self, df: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in self.REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(
                "ExtendedMomentsEngine: required input column(s) missing "
                f"{missing} — the pack must run after AlphaFactory + macro "
                "merge and never guesses inputs."
            )
        if isinstance(df.index, pd.DatetimeIndex):
            dt = df.index
        elif "DateTime" in df.columns:
            dt = pd.DatetimeIndex(df["DateTime"])
        else:
            raise ValueError(
                "ExtendedMomentsEngine: need a DatetimeIndex or DateTime "
                "column for time-of-day features."
            )

        out = df.copy()
        r = out["log_ret"]
        close = out["Close"]
        high = out["High"]
        low = out["Low"]
        log_close = np.log(close)
        vol_log = np.log1p(out["Volume"])

        # ── A. Second-degree gradients ────────────────────────────────
        for w in (24, 72, 168):
            slope = self._rolling_slope(log_close, w)
            out[f"TREND_LR_ACCEL_{w}"] = slope - slope.shift(w)
            curv = self._rolling_curvature(log_close, w)
            out[f"TREND_QUAD_CURV_{w}"] = self._zscore(curv, 840)

        # ── B. Cross-moments ──────────────────────────────────────────
        dvol = vol_log.diff()
        for w in (24, 168):
            out[f"XMOM_RV_CORR_{w}"] = r.rolling(w).corr(dvol)
        dvyz = out["VOL_YZ_24"].diff()
        for w in (72, 336):
            out[f"XMOM_LEV_CORR_{w}"] = r.rolling(w).corr(dvyz)
        ret24 = r.rolling(24).sum()
        for name, drv in (
            ("DXY", out["MACRO_DXY_CHG_1D"]),
            ("OVX", out["MACRO_OVX_CHG_1D"]),
        ):
            for w in (336, 840):
                cov = ret24.rolling(w).cov(drv)
                var = drv.rolling(w).var()
                out[f"XMOM_{name}_BETA_{w}"] = cov / (var + _EPS)

        # ── C. Serial structure / time-asymmetry ──────────────────────
        r1 = r.shift(1)
        trev_num = r * r1**2 - r**2 * r1
        for w in (24, 168):
            den = r.rolling(w).std() ** 3
            out[f"STRUC_TREV_{w}"] = trev_num.rolling(w).mean() / (den + _EPS)
            out[f"STRUC_AC1_{w}"] = r.rolling(w).corr(r1)
        r4 = r.rolling(4).sum()
        out["STRUC_VR4_168"] = r4.rolling(168).var() / (
            4.0 * r.rolling(168).var() + _EPS
        )

        # ── D. Event-time / duration ──────────────────────────────────
        close_np = close.to_numpy(dtype=np.float64)
        for w in (72, 336, 840):
            since_hi = _bars_since_extreme_numba(close_np, w, True)
            since_lo = _bars_since_extreme_numba(close_np, w, False)
            out[f"DUR_SINCE_HIGH_{w}"] = since_hi / w
            out[f"DUR_SINCE_LOW_{w}"] = since_lo / w

        sigma840 = r.rolling(840).std()
        shock = r.abs() > (2.0 * sigma840)
        idx = pd.Series(np.arange(len(out), dtype=float), index=out.index)
        last_shock = idx.where(shock).ffill()
        out["DUR_SINCE_SHOCK"] = np.log1p(idx - last_shock)

        sign = np.sign(r).replace(0.0, np.nan).ffill().fillna(0.0)
        run_id = (sign != sign.shift(1)).cumsum()
        run_len = sign.groupby(run_id).cumcount() + 1
        signed_run = run_len * sign
        out["DUR_RUN_Z"] = self._zscore(signed_run, 840)

        # ── E. Robust & jump moments ──────────────────────────────────
        for w in (24, 120):
            q25 = r.rolling(w).quantile(0.25)
            q50 = r.rolling(w).quantile(0.50)
            q75 = r.rolling(w).quantile(0.75)
            out[f"DIST_QSKEW_{w}"] = (q75 + q25 - 2.0 * q50) / (q75 - q25 + _EPS)
            q10 = r.rolling(w).quantile(0.10)
            q90 = r.rolling(w).quantile(0.90)
            out[f"DIST_QSKEW10_{w}"] = (q90 + q10 - 2.0 * q50) / (q90 - q10 + _EPS)

        abs_r = r.abs()
        for w in (24, 168):
            rv = (r**2).rolling(w).sum()
            bv = (np.pi / 2.0) * (abs_r * abs_r.shift(1)).rolling(w).sum()
            out[f"VOL_JUMP_RATIO_{w}"] = rv / (bv + _EPS)
            out[f"VOL_MAXSUM_{w}"] = abs_r.rolling(w).max() / (
                abs_r.rolling(w).sum() + _EPS
            )
        for w in (72, 336):
            down = (r.clip(upper=0.0) ** 2).rolling(w).mean()
            up = (r.clip(lower=0.0) ** 2).rolling(w).mean()
            out[f"VOL_SEMI_LOGRATIO_{w}"] = 0.5 * np.log(
                (down + _EPS) / (up + _EPS)
            )
        out["VOL_RATIO_PKCC_24"] = out["VOL_PARK_24"] / (
            r.rolling(24).std() + _EPS
        )

        # ── F. Volume distribution ────────────────────────────────────
        slot = pd.MultiIndex.from_arrays([dt.dayofweek, dt.hour])
        expected = (
            vol_log.groupby(slot)
            .transform(
                lambda s: s.rolling(
                    _TOD_LOOKBACK_SLOTS, min_periods=_TOD_MIN_SLOTS
                )
                .mean()
                .shift(1)
            )
        )
        out["VOLU_TOD_SURPRISE"] = vol_log - expected
        for w in (24, 168):
            out[f"VOLU_Z_{w}"] = self._zscore(vol_log, w, full_window=True)
            vol_sum = out["Volume"].rolling(w).sum()
            out[f"VOLU_HHI_{w}"] = (
                (out["Volume"] ** 2).rolling(w).sum() / (vol_sum**2 + _EPS) * w
            )

        # ── G. Family completion ──────────────────────────────────────
        # ATR_14 only materialises at target generation (Step 6), after this
        # engine runs — derive the same TR-mean-14 construction EXEC_ATR_14
        # uses rather than requiring a column that cannot exist yet.
        tr = np.maximum(
            high - low,
            np.maximum(
                (high - close.shift(1)).abs(), (low - close.shift(1)).abs()
            ),
        )
        atr = tr.rolling(14).mean().replace(0, np.nan)
        for w in (24, 72, 168, 336, 840):
            recent_low = close.rolling(w).min()
            out[f"EXHAUST_DIST_LOW_{w}"] = (close - recent_low) / atr
        clv = (2.0 * close - high - low) / (high - low).clip(lower=1e-8)
        for w in (24, 168):
            out[f"STRUC_CLV_MEAN_{w}"] = clv.rolling(w).mean()

        # ── H. Liquidity: Roll spread ─────────────────────────────────
        for w in (24, 168):
            cov = r.rolling(w).cov(r1)
            out[f"LIQ_ROLL_{w}"] = 2.0 * np.sqrt((-cov).clip(lower=0.0))

        # ── I. Variance risk premium ──────────────────────────────────
        out["MACRO_VRP_OVX_168"] = out["MACRO_OVX"] - (
            out["VOL_YZ_168"] * _ANNUALISE_HOURLY_PCT
        )

        return out

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rolling_slope(y: pd.Series, w: int) -> pd.Series:
        """OLS slope over evenly spaced t in O(N) via rolling sums."""
        t_mean = (w - 1) / 2.0
        # denominator: sum over window of (t - t_mean)^2, constant
        denom = w * (w * w - 1) / 12.0
        n = pd.Series(np.arange(len(y), dtype=float), index=y.index)
        sy = y.rolling(w).sum()
        sny = (y * n).rolling(w).sum()
        # window positions are n - (n_end - w + 1); rebase global index
        n_start = n - (w - 1)
        sty = sny - n_start * sy  # sum of (local t)*y
        return (sty - t_mean * sy) / denom

    @staticmethod
    def _rolling_curvature(y: pd.Series, w: int) -> pd.Series:
        """Quadratic coefficient of an OLS parabola fit over the window.

        Uses the orthogonal second-degree weight  p2(t) = (t - t_mean)^2 - v
        with v = (w^2 - 1)/12, so  c = sum(p2 * y) / sum(p2^2).
        """
        t_mean = (w - 1) / 2.0
        v = (w * w - 1) / 12.0
        t = np.arange(w, dtype=float)
        p2 = (t - t_mean) ** 2 - v
        denom = float((p2**2).sum())
        n = pd.Series(np.arange(len(y), dtype=float), index=y.index)
        sy = y.rolling(w).sum()
        sny = (y * n).rolling(w).sum()
        snny = (y * n * n).rolling(w).sum()
        n_start = n - (w - 1)
        # local t = n - n_start; expand (t - t_mean)^2 - v in terms of n
        sty = sny - n_start * sy
        stty = snny - 2.0 * n_start * sny + n_start**2 * sy
        sp2y = stty - 2.0 * t_mean * sty + (t_mean**2 - v) * sy
        return sp2y / denom

    @staticmethod
    def _zscore(s: pd.Series, w: int, full_window: bool = False) -> pd.Series:
        mp = w if full_window else max(2, w // 2)
        mean = s.rolling(w, min_periods=mp).mean()
        std = s.rolling(w, min_periods=mp).std()
        return (s - mean) / (std + _EPS)
