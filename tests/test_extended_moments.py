"""Tests for the extended-moments feature pack (HourSet_04E+).

Contracts enforced:
  * exact 53-column emission set
  * causality (anti-lookahead) across ALL columns
  * loud failure on missing inputs
  * closed-form correctness of the slope/curvature helpers
  * known-value behaviour per family
  * additive gating: FeatureConfig flag defaults False; existing DataMaps parse
  * bucket classification for the new prefixes
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.features.extended_moments import ExtendedMomentsEngine
from src.config.schemas import FeatureConfig
from src.features.feature_buckets import classify_feature

N = 2600
SEED = 7


EXPECTED_COLUMNS = sorted(
    [f"TREND_LR_ACCEL_{w}" for w in (24, 72, 168)]
    + [f"TREND_QUAD_CURV_{w}" for w in (24, 72, 168)]
    + [f"XMOM_RV_CORR_{w}" for w in (24, 168)]
    + [f"XMOM_LEV_CORR_{w}" for w in (72, 336)]
    + [f"XMOM_DXY_BETA_{w}" for w in (336, 840)]
    + [f"XMOM_OVX_BETA_{w}" for w in (336, 840)]
    + [f"STRUC_TREV_{w}" for w in (24, 168)]
    + [f"STRUC_AC1_{w}" for w in (24, 168)]
    + ["STRUC_VR4_168"]
    + [f"DUR_SINCE_HIGH_{w}" for w in (72, 336, 840)]
    + [f"DUR_SINCE_LOW_{w}" for w in (72, 336, 840)]
    + ["DUR_SINCE_SHOCK", "DUR_RUN_Z"]
    + [f"DIST_QSKEW_{w}" for w in (24, 120)]
    + [f"DIST_QSKEW10_{w}" for w in (24, 120)]
    + [f"VOL_JUMP_RATIO_{w}" for w in (24, 168)]
    + [f"VOL_MAXSUM_{w}" for w in (24, 168)]
    + [f"VOL_SEMI_LOGRATIO_{w}" for w in (72, 336)]
    + ["VOL_RATIO_PKCC_24"]
    + ["VOLU_TOD_SURPRISE"]
    + [f"VOLU_Z_{w}" for w in (24, 168)]
    + [f"VOLU_HHI_{w}" for w in (24, 168)]
    + [f"EXHAUST_DIST_LOW_{w}" for w in (24, 72, 168, 336, 840)]
    + [f"STRUC_CLV_MEAN_{w}" for w in (24, 168)]
    + [f"LIQ_ROLL_{w}" for w in (24, 168)]
    + ["MACRO_VRP_OVX_168"]
)


def _make_frame(n=N, seed=SEED):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-03", periods=n, freq="H")
    r = rng.normal(0, 0.004, n)
    r[0] = np.nan
    close = 4.0 * np.exp(np.nancumsum(r))
    spread = np.abs(rng.normal(0, 0.002, n)) + 1e-4
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = rng.integers(500, 50_000, n).astype(float)
    df = pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
            "log_ret": r,
        },
        index=idx,
    )
    # deliberately NO ATR_14: it only exists post-target-generation in the
    # real pipeline; the engine must derive its own TR-based ATR
    log_hl = np.log(df["High"] / df["Low"])
    df["VOL_PARK_24"] = np.sqrt((log_hl**2).rolling(24).mean() / (4 * np.log(2)))
    df["VOL_YZ_24"] = pd.Series(r, index=idx).rolling(24).std()
    df["VOL_YZ_168"] = pd.Series(r, index=idx).rolling(168).std()
    daily = np.repeat(rng.normal(0, 0.3, n // 24 + 1), 24)[:n]
    df["MACRO_DXY_CHG_1D"] = daily
    df["MACRO_OVX"] = 35.0 + np.abs(rng.normal(0, 5, n))
    df["MACRO_OVX_CHG_1D"] = np.repeat(rng.normal(0, 1.0, n // 24 + 1), 24)[:n]
    return df


@pytest.fixture(scope="module")
def built():
    df = _make_frame()
    out = ExtendedMomentsEngine().add_features(df)
    new = sorted(set(out.columns) - set(df.columns))
    return df, out, new


# ── contract ──────────────────────────────────────────────────────────


def test_exact_column_set(built):
    _, _, new = built
    assert new == EXPECTED_COLUMNS
    assert len(new) == 53


def test_missing_input_raises():
    df = _make_frame(400)
    for col in ("VOL_YZ_24", "MACRO_OVX", "VOL_PARK_24", "log_ret"):
        with pytest.raises(ValueError, match=col):
            ExtendedMomentsEngine().add_features(df.drop(columns=[col]))


def test_no_datetime_raises():
    df = _make_frame(400).reset_index(drop=True)
    with pytest.raises(ValueError, match="DatetimeIndex or DateTime"):
        ExtendedMomentsEngine().add_features(df)


def test_input_frame_not_mutated():
    df = _make_frame(600)
    snapshot = df.copy(deep=True)
    ExtendedMomentsEngine().add_features(df)
    pd.testing.assert_frame_equal(df, snapshot)


def test_deterministic(built):
    df, out, new = built
    again = ExtendedMomentsEngine().add_features(df)
    pd.testing.assert_frame_equal(out[new], again[new])


def test_no_lookahead_any_column(built):
    """Truncating the input must not change any previously computed value."""
    df, out, new = built
    cut = len(df) - 300
    out_cut = ExtendedMomentsEngine().add_features(df.iloc[:cut])
    pd.testing.assert_frame_equal(
        out[new].iloc[: cut - 1], out_cut[new].iloc[: cut - 1]
    )


def test_columns_alive_at_series_end(built):
    """Every feature must be non-NaN in the final rows (live-usable)."""
    _, out, new = built
    tail = out[new].iloc[-50:]
    for c in new:
        if c == "DUR_SINCE_SHOCK":
            continue  # NaN only if no 2-sigma shock ever occurred
        assert tail[c].notna().all(), f"{c} has NaN in final 50 rows"


def test_continuous_not_binary(built):
    _, out, new = built
    for c in new:
        assert out[c].nunique() > 20, f"{c} looks like a flag ({out[c].nunique()})"


# ── helper math ───────────────────────────────────────────────────────


def test_rolling_slope_exact_on_linear():
    y = pd.Series(3.0 * np.arange(500.0) + 2.0)
    s = ExtendedMomentsEngine._rolling_slope(y, 24)
    assert np.allclose(s.iloc[30:], 3.0)


def test_rolling_curvature_exact():
    t = np.arange(500.0)
    quad = ExtendedMomentsEngine._rolling_curvature(pd.Series(t**2), 24)
    assert np.allclose(quad.iloc[30:], 1.0)
    lin = ExtendedMomentsEngine._rolling_curvature(pd.Series(5.0 * t), 24)
    assert np.allclose(lin.iloc[30:], 0.0, atol=1e-8)


def test_accel_on_quadratic_is_2w():
    df = _make_frame(1200)
    df["Close"] = np.exp(1e-6 * np.arange(len(df), dtype=float) ** 2)
    out = ExtendedMomentsEngine().add_features(df)
    for w in (24, 72):
        got = out[f"TREND_LR_ACCEL_{w}"].iloc[300]
        assert got == pytest.approx(2.0 * w * 1e-6, rel=1e-6)


# ── family behaviour ──────────────────────────────────────────────────


def _alternating_frame(n=900, a=0.01):
    df = _make_frame(n)
    r = np.where(np.arange(n) % 2 == 0, a, -a)
    r[0] = np.nan
    df["log_ret"] = r
    return df


def test_ac1_alternating_is_minus_one():
    out = ExtendedMomentsEngine().add_features(_alternating_frame())
    assert out["STRUC_AC1_24"].iloc[100] == pytest.approx(-1.0, abs=1e-6)


def test_vr4_alternating_near_zero():
    out = ExtendedMomentsEngine().add_features(_alternating_frame())
    assert abs(out["STRUC_VR4_168"].iloc[400]) < 0.05


def test_trev_symmetric_near_zero():
    out = ExtendedMomentsEngine().add_features(_alternating_frame())
    assert abs(out["STRUC_TREV_24"].iloc[100]) < 0.2


def test_jump_ratio_constant_magnitude_is_2_over_pi():
    out = ExtendedMomentsEngine().add_features(_alternating_frame())
    assert out["VOL_JUMP_RATIO_24"].iloc[100] == pytest.approx(2 / np.pi, rel=1e-3)


def test_jump_ratio_spikes_on_jump():
    df = _make_frame(900)
    r = np.full(900, 0.001)
    r[0] = np.nan
    r[500] = 0.08
    df["log_ret"] = r
    out = ExtendedMomentsEngine().add_features(df)
    assert out["VOL_JUMP_RATIO_24"].iloc[505] > 5.0


def test_maxsum_constant_is_one_over_w():
    out = ExtendedMomentsEngine().add_features(_alternating_frame())
    assert out["VOL_MAXSUM_24"].iloc[100] == pytest.approx(1 / 24, rel=1e-6)


def test_semi_logratio_sign():
    df = _make_frame(900)
    r = np.full(900, -0.002)
    r[0] = np.nan
    df["log_ret"] = r
    out = ExtendedMomentsEngine().add_features(df)
    assert out["VOL_SEMI_LOGRATIO_72"].iloc[200] > 5.0


def test_qskew_bounded(built):
    _, out, _ = built
    for c in ("DIST_QSKEW_24", "DIST_QSKEW_120", "DIST_QSKEW10_24",
              "DIST_QSKEW10_120"):
        vals = out[c].dropna()
        assert vals.between(-1.0 - 1e-9, 1.0 + 1e-9).all(), c


def test_dur_since_high_low_monotonic_close():
    df = _make_frame(1200)
    df["Close"] = np.linspace(1.0, 2.0, len(df))  # strictly rising
    out = ExtendedMomentsEngine().add_features(df)
    assert (out["DUR_SINCE_HIGH_72"].iloc[100:] == 0).all()
    assert np.allclose(out["DUR_SINCE_LOW_72"].iloc[100:], 71 / 72)


def test_dur_since_shock_counts_up():
    df = _make_frame(1300)
    # alternating +-a: sigma == a, so |r| = a < 2*sigma never shocks; only
    # the injected spike does
    r = np.where(np.arange(1300) % 2 == 0, 0.001, -0.001)
    r[0] = np.nan
    r[1000] = 0.10
    df["log_ret"] = r
    out = ExtendedMomentsEngine().add_features(df)
    assert out["DUR_SINCE_SHOCK"].iloc[999] != out["DUR_SINCE_SHOCK"].iloc[999]  # NaN
    assert out["DUR_SINCE_SHOCK"].iloc[1000] == pytest.approx(np.log1p(0))
    assert out["DUR_SINCE_SHOCK"].iloc[1005] == pytest.approx(np.log1p(5))


def test_clv_mean_close_at_high_is_one():
    df = _make_frame(600)
    df["Close"] = df["High"]
    out = ExtendedMomentsEngine().add_features(df)
    assert out["STRUC_CLV_MEAN_24"].iloc[100] == pytest.approx(1.0, abs=1e-6)


def test_liq_roll_zero_when_positively_autocorrelated():
    df = _make_frame(900)
    r = np.full(900, 0.002)  # perfectly persistent -> cov(r, r-1) >= 0
    r[0] = np.nan
    df["log_ret"] = r
    out = ExtendedMomentsEngine().add_features(df)
    assert out["LIQ_ROLL_24"].iloc[200] == 0.0


def test_xmom_rv_corr_perfect_when_volume_tracks_returns():
    df = _make_frame(900)
    cum = pd.Series(df["log_ret"].fillna(0).cumsum(), index=df.index)
    df["Volume"] = np.expm1(10.0 + cum)  # log1p(Volume) = 10 + cumsum(r)
    out = ExtendedMomentsEngine().add_features(df)
    assert out["XMOM_RV_CORR_24"].iloc[200] == pytest.approx(1.0, abs=1e-6)


def test_xmom_beta_one_when_driver_equals_ret24():
    df = _make_frame(1600)
    ret24 = df["log_ret"].rolling(24).sum()
    df["MACRO_DXY_CHG_1D"] = ret24.fillna(0.0)
    out = ExtendedMomentsEngine().add_features(df)
    assert out["XMOM_DXY_BETA_336"].iloc[800] == pytest.approx(1.0, rel=1e-3)


def test_vrp_recompute(built):
    df, out, _ = built
    expect = df["MACRO_OVX"] - df["VOL_YZ_168"] * np.sqrt(24 * 252) * 100
    got = out["MACRO_VRP_OVX_168"]
    pd.testing.assert_series_equal(
        got.dropna(), expect.dropna(), check_names=False
    )


def test_volu_hhi_uniform_volume_is_one():
    df = _make_frame(600)
    df["Volume"] = 1000.0
    out = ExtendedMomentsEngine().add_features(df)
    assert out["VOLU_HHI_24"].iloc[100] == pytest.approx(1.0, rel=1e-6)


def test_tod_surprise_prior_only():
    """Same-slot history only: first 4 same-slot observations are NaN."""
    df = _make_frame(24 * 7 * 6)  # 6 weeks
    out = ExtendedMomentsEngine().add_features(df)
    first_slot = out["VOLU_TOD_SURPRISE"].iloc[::24 * 7]  # same (dow, hour)
    assert first_slot.iloc[:4].isna().all()
    assert first_slot.iloc[5:].notna().all()


# ── gating / schema / buckets ─────────────────────────────────────────


def test_feature_config_flag_defaults_false():
    assert FeatureConfig().include_extended_moments is False
    assert FeatureConfig(include_extended_moments=True).include_extended_moments


def test_existing_datamaps_still_parse():
    root = Path(__file__).resolve().parents[1] / "configs" / "master"
    for p in sorted(root.glob("DataMap_*.json")):
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
        fc = FeatureConfig(**raw["data_workflow"]["features"])
        if "04E" not in p.name:
            assert fc.include_extended_moments is False, p.name


def test_bucket_classification():
    assert classify_feature("XMOM_RV_CORR_24") == "extended_moments"
    assert classify_feature("DUR_SINCE_SHOCK") == "extended_moments"
    assert classify_feature("VOLU_HHI_168") == "extended_moments"
    # names reusing existing prefixes stay in their families
    assert classify_feature("TREND_QUAD_CURV_24") == "trend"
    assert classify_feature("STRUC_AC1_24") == "structure"
    assert classify_feature("VOL_JUMP_RATIO_24") == "volatility"
    assert classify_feature("MACRO_VRP_OVX_168") == "macro_tech"
