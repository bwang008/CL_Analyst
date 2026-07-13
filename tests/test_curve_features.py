"""Tests for src/features/curve_features.py — CURVE_* calendar-spread engine.

Mandatory suite per ticket ng-03b-calendar-spread-dataset_07122026_0249 (v2):
math correctness on synthetic two-leg fixtures, the all-columns no-lookahead
mutation test (incl. SEASONAL_Z), the seasonal causality fixture, cold-start
semantics (distinct-PRIOR-YEAR counting, 0.0 neutral, no row drops), merge
policy (bounded ffill, gap raise, leading-NaN budget, tz/monotonic guards),
the loud CURVE_ROLL_YIELD deferral, and the Time_Month_Sin/Cos encoding.
"""

import logging

import numpy as np
import pandas as pd
import pytest

from src.features.curve_features import (
    CURVE_FEATURE_COLUMNS,
    FFILL_LIMIT_BARS,
    CurveFeatureEngine,
)

UTC_NS = "datetime64[ns]"


# ---------------------------------------------------------------------------
# Fixture helpers — synthetic Databento-format leg CSVs
# ---------------------------------------------------------------------------

def write_leg_csv(path, timestamps, closes, instrument_ids):
    """Write a synthetic raw Databento ohlcv-1h CSV (fixed-precision ints)."""
    closes = np.asarray(closes, dtype=np.float64)
    scaled = np.round(closes * 1e9).astype(np.int64)
    df = pd.DataFrame(
        {
            "ts_event": pd.DatetimeIndex(timestamps).asi8,
            "rtype": 34,
            "publisher_id": 1,
            "instrument_id": np.asarray(instrument_ids, dtype=np.int64),
            "open": scaled,
            "high": scaled,
            "low": scaled,
            "close": scaled,
            "volume": 100,
        }
    )
    df.to_csv(path, index=False)
    return str(path)


def parsed(closes):
    """The exact float64 values the engine sees after the 1e9 round-trip."""
    closes = np.asarray(closes, dtype=np.float64)
    return np.round(closes * 1e9).astype(np.int64) / 1e9


def make_engine(tmpdir, timestamps, front_closes, second_closes,
                front_iids=None, second_timestamps=None, second_iids=None,
                **kwargs):
    n = len(timestamps)
    if front_iids is None:
        front_iids = 100 + np.arange(n) // 720          # roll every 720 bars
    ts2 = timestamps if second_timestamps is None else second_timestamps
    if second_iids is None:
        second_iids = 200 + np.arange(len(ts2)) // 720
    f_csv = write_leg_csv(tmpdir / "front.csv", timestamps, front_closes, front_iids)
    s_csv = write_leg_csv(tmpdir / "second.csv", ts2, second_closes, second_iids)
    return CurveFeatureEngine(f_csv, s_csv, **kwargs)


# ---------------------------------------------------------------------------
# Base math fixture: 3,000 hourly bars, full overlap, rolls every 720 bars
# ---------------------------------------------------------------------------

N_BASE = 3000


def _base_series():
    rng = np.random.RandomState(42)
    ts = pd.date_range("2020-01-01", periods=N_BASE, freq="H")
    front = 5.0 + np.cumsum(rng.normal(0.0, 0.005, N_BASE))
    # dollar spread crosses zero (sign / ROC-vs-pct_change fixtures need it)
    spread_dollar = 0.05 * np.sin(np.arange(N_BASE) * 2 * np.pi / 500.0) \
        + rng.normal(0.0, 0.004, N_BASE)
    second = front + spread_dollar
    # exact-equality bar for the CONTANGO_SIGN == 0 case
    second[500] = front[500]
    # constant stretch (>= 24+24 bars) for the zero-std z case
    front[1000:1060] = 5.0
    second[1000:1060] = 5.2
    assert (front > 0).all() and (second > 0).all()
    return ts, front, second


@pytest.fixture(scope="module")
def base_build(tmp_path_factory):
    ts, front, second = _base_series()
    tmpdir = tmp_path_factory.mktemp("curve_base")
    engine = make_engine(tmpdir, ts, front, second)
    out = engine.build_features()
    f1 = pd.Series(parsed(front), index=ts)
    f2 = pd.Series(parsed(second), index=ts)
    sp = (f2 - f1) / f1
    return out, sp, f1, f2


# ---------------------------------------------------------------------------
# Column-set + docstring contract
# ---------------------------------------------------------------------------

def test_exact_column_set(base_build):
    out, _, _, _ = base_build
    assert list(out.columns) == CURVE_FEATURE_COLUMNS
    assert len(out.columns) == 29


def test_module_docstring_contract():
    import src.features.curve_features as cf
    doc = cf.__doc__
    assert "TS_" in doc                      # CURVE_/TS_ separation stated
    assert "NG.c.2" in doc                   # curvature out of scope stated
    assert "JOINED" in doc or "joined" in doc  # joined-timeline window semantics
    assert "0.0" in doc                      # the two neutral exceptions stated


# ---------------------------------------------------------------------------
# Math correctness per column group
# ---------------------------------------------------------------------------

def test_spread_pct(base_build):
    out, sp, _, _ = base_build
    assert np.allclose(out["CURVE_SPREAD_PCT"].values, sp.values, rtol=0, atol=1e-15)
    assert not out["CURVE_SPREAD_PCT"].isna().any()


def test_contango_sign(base_build):
    out, _, f1, f2 = base_build
    sign = out["CURVE_CONTANGO_SIGN"]
    assert set(np.unique(sign.values)).issubset({-1, 0, 1})
    expected = np.sign(f2.values - f1.values).astype(np.int64)
    assert (sign.values == expected).all()
    assert sign.iloc[500] == 0  # exact-equality bar


def test_roc_is_simple_diff_not_pct_change(base_build):
    out, sp, _, _ = base_build
    for n in (1, 3, 6, 12, 24):
        expected = sp.diff(n)
        got = out[f"CURVE_SPREAD_ROC_{n}"]
        pd.testing.assert_series_equal(got, expected, check_names=False, check_freq=False)
    # zero-crossing rows exist and diff != pct_change semantics there
    pct = sp.pct_change(1)
    crossing = (sp.shift(1) < 0) & (sp > 0)
    crossing &= pct.notna() & out["CURVE_SPREAD_ROC_1"].notna()
    assert crossing.any(), "fixture must contain zero-crossings"
    diffs = out.loc[crossing, "CURVE_SPREAD_ROC_1"].values
    pcts = pct[crossing].values
    assert not np.allclose(diffs, pcts), "ROC must not be pct_change on a signed series"


def test_slope_r2_on_linear_fixture(tmp_path):
    n = 200
    ts = pd.date_range("2021-01-01", periods=n, freq="H")
    b = 1e-4
    front = np.full(n, 5.0)
    second = 5.0 * (1.0 + 0.01 + b * np.arange(n))  # spread_pct = 0.01 + b*t
    engine = make_engine(tmp_path, ts, front, second,
                         front_iids=np.full(n, 100), second_iids=np.full(n, 200))
    out = engine.build_features()
    for w in (24, 72):
        slope = out[f"CURVE_SPREAD_SLOPE_{w}"]
        r2 = out[f"CURVE_SPREAD_SLOPE_R2_{w}"]
        assert slope.iloc[: w - 1].isna().all()
        assert np.allclose(slope.iloc[w - 1:].values, b, rtol=0, atol=1e-7)
        assert np.allclose(r2.iloc[w - 1:].values, 1.0, rtol=0, atol=1e-6)


def test_accel_second_difference(base_build):
    out, sp, _, _ = base_build
    d24 = sp.diff(24)
    expected = d24 - d24.shift(24)
    pd.testing.assert_series_equal(out["CURVE_SPREAD_ACCEL_24"], expected, check_names=False, check_freq=False)


def test_zscores_and_zero_std_neutral(base_build):
    out, sp, _, _ = base_build
    for w in (24, 72, 168, 336, 840, 2160):
        mean = sp.rolling(w).mean()
        std = sp.rolling(w).std()
        col = out[f"CURVE_SPREAD_PCT_Z_{w}"]
        ok = std.notna() & (std != 0)
        assert np.allclose(col[ok].values, ((sp - mean) / std)[ok].values, equal_nan=False)
        # documented neutral exception #1: std == 0 -> exactly 0.0 (never NaN/inf)
        zero_std = std == 0
        if zero_std.any():
            assert (col[zero_std] == 0.0).all()
    # the constant stretch guarantees the zero-std branch fires for Z_24
    std24 = sp.rolling(24).std()
    assert (std24 == 0).any(), "fixture must exercise the zero-std branch"


def test_dist_mean_840(base_build):
    out, sp, _, _ = base_build
    expected = sp - sp.rolling(840).mean()
    pd.testing.assert_series_equal(out["CURVE_SPREAD_DIST_MEAN_840"], expected, check_names=False, check_freq=False)


def test_pctl_840_matches_naive_rank(base_build):
    out, sp, _, _ = base_build
    got = out["CURVE_SPREAD_PCTL_840"]
    assert got.iloc[:839].isna().all()
    vals = sp.values
    for i in range(839, 900):  # spot-check a contiguous block
        window = vals[i - 839: i + 1]
        expected = pd.Series(window).rank(pct=True).iloc[-1]
        assert got.iloc[i] == pytest.approx(expected, abs=1e-12)


def test_z_diff_24v840(base_build):
    out, _, _, _ = base_build
    expected = out["CURVE_SPREAD_PCT_Z_24"] - out["CURVE_SPREAD_PCT_Z_840"]
    pd.testing.assert_series_equal(
        out["CURVE_SPREAD_Z_DIFF_24v840"], expected, check_names=False, check_freq=False
    )


def test_volratio_and_vols(base_build):
    out, sp, _, _ = base_build
    vol24 = sp.rolling(24).std()
    vol168 = sp.rolling(168).std()
    vol840 = sp.rolling(840).std()
    pd.testing.assert_series_equal(out["CURVE_SPREAD_VOL_24"], vol24, check_names=False, check_freq=False)
    pd.testing.assert_series_equal(out["CURVE_SPREAD_VOL_168"], vol168, check_names=False, check_freq=False)
    expected_ratio = vol24 / vol840.clip(lower=1e-8)
    pd.testing.assert_series_equal(
        out["CURVE_SPREAD_VOLRATIO_24v840"], expected_ratio, check_names=False, check_freq=False
    )


def test_volratio_denominator_clip(tmp_path):
    """A fully-constant spread makes VOL_840 exactly 0 — the 1e-8 clip must
    keep the ratio finite (0.0), never NaN/inf from 0/0."""
    n = 1000
    ts = pd.date_range("2021-01-01", periods=n, freq="H")
    engine = make_engine(tmp_path, ts, np.full(n, 5.0), np.full(n, 5.2),
                         front_iids=np.full(n, 100), second_iids=np.full(n, 200))
    out = engine.build_features()
    ratio = out["CURVE_SPREAD_VOLRATIO_24v840"].iloc[839:]
    assert np.isfinite(ratio.values).all()
    assert (ratio.values == 0.0).all()


def test_volvol_168(base_build):
    out, sp, _, _ = base_build
    vol168 = sp.rolling(168).std()
    expected = vol168.rolling(168).std() / vol168.rolling(168).mean()
    pd.testing.assert_series_equal(out["CURVE_SPREAD_VOLVOL_168"], expected, check_names=False, check_freq=False)


def test_wow_and_mom(base_build):
    out, sp, _, _ = base_build
    pd.testing.assert_series_equal(
        out["CURVE_SPREAD_WOW"], sp - sp.shift(168), check_names=False, check_freq=False
    )
    pd.testing.assert_series_equal(
        out["CURVE_SPREAD_MOM_840"], sp - sp.shift(840), check_names=False, check_freq=False
    )


def test_bars_since_roll(base_build):
    out, _, _, _ = base_build
    col = out["CURVE_BARS_SINCE_ROLL"]
    # rolls at joined positions 720, 1440, 2160, 2880 (front iid changes)
    assert col.iloc[:720].isna().all()          # before first observed roll
    assert col.iloc[720] == 0.0
    assert col.iloc[721] == 1.0
    assert col.iloc[1439] == 719.0
    assert col.iloc[1440] == 0.0
    assert col.iloc[2160] == 0.0
    assert col.iloc[2879] == 719.0
    assert col.iloc[2880] == 0.0
    assert col.iloc[2999] == 119.0


def test_bars_since_roll_survives_join_dropped_roll_bar(tmp_path):
    """If the exact roll bar is dropped by the inner join (second leg missing
    that timestamp), the roll must still be detected on the joined timeline."""
    n = 300
    ts = pd.date_range("2021-01-01", periods=n, freq="H")
    front = np.full(n, 5.0) + 0.001 * np.arange(n)
    second = front + 0.1
    iids = np.where(np.arange(n) < 150, 100, 101)  # roll at bar 150
    keep = np.ones(n, dtype=bool)
    keep[150] = False                               # second leg misses the roll bar
    engine = make_engine(
        tmp_path, ts, front, second[keep], front_iids=iids,
        second_timestamps=ts[keep], second_iids=np.full(keep.sum(), 200),
    )
    out = engine.build_features()
    col = out["CURVE_BARS_SINCE_ROLL"]
    # joined timeline: bars 0..149 (iid 100) then 151.. (iid 101)
    assert len(col) == n - 1
    assert col.iloc[:150].isna().all()
    assert col.iloc[150] == 0.0     # first joined bar with the new iid
    assert col.iloc[151] == 1.0


# ---------------------------------------------------------------------------
# No-lookahead mutation test — EVERY column
# ---------------------------------------------------------------------------

def test_no_lookahead_mutation_all_columns(tmp_path):
    ts, front, second = _base_series()
    t_pos = 1500
    t_cut = ts[t_pos]

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    out_a = make_engine(dir_a, ts, front, second).build_features()

    # Mutate BOTH legs strictly after T: values changed AND join shape changed
    front_m = front.copy()
    second_m = second.copy()
    front_m[t_pos + 1:] = front_m[t_pos + 1:] * 1.5 + 0.37
    second_m[t_pos + 1:] = second_m[t_pos + 1:] * 0.6 + 1.23
    keep = np.ones(len(ts), dtype=bool)
    keep[t_pos + 100: t_pos + 140] = False          # drop future joined bars
    out_b = make_engine(
        dir_b, ts, front_m, second_m[keep],
        second_timestamps=ts[keep], second_iids=np.full(keep.sum(), 200),
    ).build_features()

    a = out_a.loc[:t_cut]
    b = out_b.loc[:t_cut]
    assert list(a.columns) == CURVE_FEATURE_COLUMNS
    pd.testing.assert_frame_equal(a, b)  # byte-identical at/before T, all 29 columns


# ---------------------------------------------------------------------------
# Seasonal fixture: 3.5 years hourly — causality, cold start, year counting
# ---------------------------------------------------------------------------

def _seasonal_series():
    ts = pd.date_range("2010-01-04", "2013-07-01", freq="H")  # ~30.6k bars
    n = len(ts)
    rng = np.random.RandomState(7)
    front = 5.0 + 0.2 * np.sin(np.arange(n) * 2 * np.pi / 2000.0)
    week = ts.isocalendar().week.astype(float).values
    seasonal_pattern = 0.02 * np.sin(2 * np.pi * week / 52.0)
    second = front * (1.0 + 0.01 + seasonal_pattern) + rng.normal(0, 0.002, n)
    assert (front > 0).all() and (second > 0).all()
    return ts, front, second


@pytest.fixture(scope="module")
def seasonal_build(tmp_path_factory):
    ts, front, second = _seasonal_series()
    tmpdir = tmp_path_factory.mktemp("curve_seasonal")
    engine = make_engine(tmpdir, ts, front, second,
                         front_iids=100 + np.arange(len(ts)) // 720)
    out = engine.build_features()
    f1 = pd.Series(parsed(front), index=ts)
    f2 = pd.Series(parsed(second), index=ts)
    sp = (f2 - f1) / f1
    return ts, front, second, out, sp


def test_seasonal_cold_start_neutral_zero(seasonal_build):
    ts, _, _, out, _ = seasonal_build
    col = out["CURVE_SPREAD_SEASONAL_Z"]
    assert not col.isna().any()                     # never NaN
    assert len(col) == len(ts)                      # never drops rows
    iso_year = ts.isocalendar().year.astype(int).values
    first_two_years = np.isin(iso_year, [2010, 2011])
    # <2 distinct PRIOR ISO years -> exactly 0.0
    assert (col.values[first_two_years] == 0.0).all()
    # from the 3rd distinct year onward real values appear
    assert (col.values[iso_year >= 2012] != 0.0).any()


def test_seasonal_distinct_year_not_observation_count(seasonal_build):
    ts, _, _, out, _ = seasonal_build
    col = out["CURVE_SPREAD_SEASONAL_Z"]
    # A mid-2011 row has hundreds of same-bucket PRIOR OBSERVATIONS (all of
    # 2010 + earlier 2011 hours) but only ONE distinct prior year -> still 0.0
    probe = (ts >= pd.Timestamp("2011-06-01")) & (ts < pd.Timestamp("2011-06-08"))
    assert probe.sum() > 100
    assert (col.values[probe] == 0.0).all()


def test_seasonal_z_prior_only_construction(seasonal_build):
    """Manual recomputation: each row's z uses ONLY strictly-prior same-bucket
    values (expanding + shift(1) proof at a specific 2012 timestamp)."""
    ts, _, _, out, sp = seasonal_build
    t0 = pd.Timestamp("2012-05-16 10:00:00")        # week 20, ISO year 2012
    assert t0 in sp.index
    iso = ts.isocalendar()
    week = np.asarray(iso["week"], dtype=np.int64)
    t0_week = int(pd.Timestamp(t0).isocalendar()[1])
    same_bucket = sp[(week == t0_week)]
    prior = same_bucket[same_bucket.index < t0]
    expected = (sp.loc[t0] - prior.mean()) / prior.std()
    got = out.loc[t0, "CURVE_SPREAD_SEASONAL_Z"]
    assert got == pytest.approx(expected, rel=1e-10)
    assert got != 0.0


def test_seasonal_causality_future_year_mutation(tmp_path):
    """Mutating future-year same-week rows must NOT change earlier seasonal
    values — and the fixture proves it WOULD have changed them under a
    non-causal (full-group) construction."""
    ts, front, second = _seasonal_series()
    t0 = pd.Timestamp("2012-05-16 10:00:00")
    t_pos = ts.get_loc(t0)

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    out_a = make_engine(dir_a, ts, front, second).build_features()

    second_m = second.copy()
    second_m[t_pos + 1:] = second_m[t_pos + 1:] * 3.0 + 0.5   # wild future mutation
    out_b = make_engine(dir_b, ts, front, second_m).build_features()

    # every column, not just seasonal: byte-identical at/before t0
    pd.testing.assert_frame_equal(out_a.loc[:t0], out_b.loc[:t0])

    # discriminating power: a full-group (cheating) mean WOULD differ
    f1 = pd.Series(parsed(front), index=ts)
    f2 = pd.Series(parsed(second), index=ts)
    sp = (f2 - f1) / f1
    week = np.asarray(ts.isocalendar()["week"], dtype=np.int64)
    t0_week = int(t0.isocalendar()[1])
    grp = sp[week == t0_week]
    cheat = (sp.loc[t0] - grp.mean()) / grp.std()   # includes 2013 rows
    assert out_a.loc[t0, "CURVE_SPREAD_SEASONAL_Z"] != pytest.approx(cheat, rel=1e-6)


def test_seasonal_zero_std_neutral(tmp_path):
    """Identical same-bucket history across years -> prior std == 0 -> 0.0
    (documented neutral exception, never inf/NaN)."""
    ts = pd.date_range("2010-01-04", "2013-07-01", freq="H")
    n = len(ts)
    engine = make_engine(tmp_path, ts, np.full(n, 5.0), np.full(n, 5.1),
                         front_iids=100 + np.arange(n) // 720)
    out = engine.build_features()
    col = out["CURVE_SPREAD_SEASONAL_Z"]
    assert not col.isna().any()
    assert (col.values == 0.0).all()


# ---------------------------------------------------------------------------
# Merge policy
# ---------------------------------------------------------------------------

def _merge_fixture_series(n=2400):
    rng = np.random.RandomState(11)
    ts = pd.date_range("2020-01-01", periods=n, freq="H")
    front = 5.0 + np.cumsum(rng.normal(0, 0.004, n))
    second = front + 0.05 + 0.02 * np.sin(np.arange(n) / 100.0) + rng.normal(0, 0.003, n)
    iids = np.where(np.arange(n) < 100, 100, 100 + 1 + np.arange(n) // 800)  # early roll
    assert (front > 0).all() and (second > 0).all()
    return ts, front, second, iids


def test_merge_happy_path(tmp_path):
    ts, front, second, iids = _merge_fixture_series()
    engine = make_engine(tmp_path, ts, front, second, front_iids=iids)
    df = pd.DataFrame({"Close": np.arange(len(ts), dtype=float)}, index=ts)
    merged = engine.merge_curve(df, max_leading_nan_bars=2200)
    for col in CURVE_FEATURE_COLUMNS:
        assert col in merged.columns
        s = merged[col]
        first = s.first_valid_index()
        assert first is not None
        assert not s.loc[first:].isna().any()      # zero residual NaN
    assert len(merged) == len(ts)                   # no row drops


def test_merge_gap_within_limit_ffills(tmp_path):
    ts, front, second, iids = _merge_fixture_series()
    keep = np.ones(len(ts), dtype=bool)
    keep[2300:2310] = False                         # 10-bar second-leg hole (< 24)
    engine = make_engine(tmp_path, ts, front, second[keep], front_iids=iids,
                         second_timestamps=ts[keep],
                         second_iids=np.full(keep.sum(), 200))
    df = pd.DataFrame({"Close": np.arange(len(ts), dtype=float)}, index=ts)
    merged = engine.merge_curve(df, max_leading_nan_bars=2200)
    # the hole is forward-filled from the last covered bar
    assert merged["CURVE_SPREAD_PCT"].iloc[2300] == merged["CURVE_SPREAD_PCT"].iloc[2299]
    assert not merged["CURVE_SPREAD_PCT"].iloc[2300:2310].isna().any()


def test_merge_gap_beyond_limit_raises_with_timestamps(tmp_path):
    ts, front, second, iids = _merge_fixture_series()
    keep = np.ones(len(ts), dtype=bool)
    keep[2250:2250 + FFILL_LIMIT_BARS + 10] = False  # 34-bar hole (> 24)
    engine = make_engine(tmp_path, ts, front, second[keep], front_iids=iids,
                         second_timestamps=ts[keep],
                         second_iids=np.full(keep.sum(), 200))
    df = pd.DataFrame({"Close": np.arange(len(ts), dtype=float)}, index=ts)
    with pytest.raises(ValueError, match="residual NaN"):
        engine.merge_curve(df, max_leading_nan_bars=2200)
    # the error must list gap timestamps
    with pytest.raises(ValueError, match=str(ts[2250 + FFILL_LIMIT_BARS]).split(" ")[0]):
        engine.merge_curve(df, max_leading_nan_bars=2200)


def test_merge_leading_nan_budget(tmp_path):
    ts, front, second, iids = _merge_fixture_series()
    # legs start 300 bars AFTER the training index starts
    engine = make_engine(tmp_path, ts[300:], front[300:], second[300:],
                         front_iids=iids[300:],
                         second_iids=np.full(len(ts) - 300, 200))
    df = pd.DataFrame({"Close": np.arange(len(ts), dtype=float)}, index=ts)
    with pytest.raises(ValueError, match="leading-NaN"):
        engine.merge_curve(df, max_leading_nan_bars=100)


def test_merge_all_nan_column_raises(tmp_path):
    ts, front, second, iids = _merge_fixture_series(n=500)
    engine = make_engine(tmp_path, ts, front, second, front_iids=iids)
    early = pd.date_range("2015-01-01", periods=500, freq="H")   # before leg history
    df = pd.DataFrame({"Close": np.arange(500, dtype=float)}, index=early)
    with pytest.raises(ValueError, match="all-NaN"):
        engine.merge_curve(df, max_leading_nan_bars=2200)


def test_merge_index_guards(tmp_path):
    ts, front, second, iids = _merge_fixture_series(n=500)
    engine = make_engine(tmp_path, ts, front, second, front_iids=iids)

    df_int = pd.DataFrame({"Close": np.arange(500, dtype=float)})
    with pytest.raises(ValueError, match="DatetimeIndex"):
        engine.merge_curve(df_int, max_leading_nan_bars=2200)

    shuffled = ts[:500].tolist()
    shuffled[10], shuffled[20] = shuffled[20], shuffled[10]
    df_bad = pd.DataFrame({"Close": np.arange(500, dtype=float)},
                          index=pd.DatetimeIndex(shuffled))
    with pytest.raises(ValueError, match="monotonic"):
        engine.merge_curve(df_bad, max_leading_nan_bars=2200)

    dup_idx = pd.DatetimeIndex(list(ts[:499]) + [ts[498]])
    df_dup = pd.DataFrame({"Close": np.arange(500, dtype=float)}, index=dup_idx)
    with pytest.raises(ValueError, match="duplicate"):
        engine.merge_curve(df_dup, max_leading_nan_bars=2200)


def test_merge_tz_aware_bar_index(tmp_path):
    ts, front, second, iids = _merge_fixture_series()
    engine = make_engine(tmp_path, ts, front, second, front_iids=iids)
    df_naive = pd.DataFrame({"Close": np.arange(len(ts), dtype=float)}, index=ts)
    df_aware = pd.DataFrame({"Close": np.arange(len(ts), dtype=float)},
                            index=ts.tz_localize("UTC"))
    merged_naive = engine.merge_curve(df_naive.copy(), max_leading_nan_bars=2200)
    merged_aware = engine.merge_curve(df_aware.copy(), max_leading_nan_bars=2200)
    for col in CURVE_FEATURE_COLUMNS:
        np.testing.assert_array_equal(merged_naive[col].values, merged_aware[col].values)


def test_merge_column_collision_raises(tmp_path):
    ts, front, second, iids = _merge_fixture_series(n=500)
    engine = make_engine(tmp_path, ts, front, second, front_iids=iids)
    df = pd.DataFrame({"CURVE_SPREAD_PCT": np.arange(500, dtype=float)}, index=ts)
    with pytest.raises(ValueError, match="collision"):
        engine.merge_curve(df, max_leading_nan_bars=2200)


# ---------------------------------------------------------------------------
# Constructor hygiene + loud deferral
# ---------------------------------------------------------------------------

def test_constructor_missing_files_raise(tmp_path):
    ts = pd.date_range("2021-01-01", periods=10, freq="H")
    real = write_leg_csv(tmp_path / "real.csv", ts, np.full(10, 5.0), np.full(10, 1))
    with pytest.raises(FileNotFoundError):
        CurveFeatureEngine(str(tmp_path / "missing.csv"), real)
    with pytest.raises(FileNotFoundError):
        CurveFeatureEngine(real, str(tmp_path / "missing.csv"))
    with pytest.raises(ValueError, match="non-empty"):
        CurveFeatureEngine("", real)


def test_constructor_rejects_unimplemented_options(tmp_path):
    ts = pd.date_range("2021-01-01", periods=10, freq="H")
    a = write_leg_csv(tmp_path / "a.csv", ts, np.full(10, 5.0), np.full(10, 1))
    b = write_leg_csv(tmp_path / "b.csv", ts, np.full(10, 5.1), np.full(10, 2))
    with pytest.raises(NotImplementedError, match="doy_smoothed"):
        CurveFeatureEngine(a, b, seasonal_bucket="doy_smoothed")
    with pytest.raises(ValueError, match="curve_seasonal_bucket"):
        CurveFeatureEngine(a, b, seasonal_bucket="fortnight")
    with pytest.raises(ValueError, match=">= 2"):
        CurveFeatureEngine(a, b, seasonal_min_prior_years=1)
    with pytest.raises(NotImplementedError, match="SEASONAL_PCTL"):
        CurveFeatureEngine(a, b, seasonal_pctl=True)


def test_disjoint_legs_raise(tmp_path):
    ts_a = pd.date_range("2021-01-01", periods=50, freq="H")
    ts_b = pd.date_range("2022-01-01", periods=50, freq="H")
    a = write_leg_csv(tmp_path / "a.csv", ts_a, np.full(50, 5.0), np.full(50, 1))
    b = write_leg_csv(tmp_path / "b.csv", ts_b, np.full(50, 5.1), np.full(50, 2))
    with pytest.raises(ValueError, match="concurrent"):
        CurveFeatureEngine(a, b).build_features()


def test_duplicate_timestamps_in_leg_raise(tmp_path):
    ts = pd.date_range("2021-01-01", periods=50, freq="H")
    dup_ts = pd.DatetimeIndex(list(ts) + [ts[10]])
    a = write_leg_csv(tmp_path / "a.csv", dup_ts, np.full(51, 5.0), np.full(51, 1))
    b = write_leg_csv(tmp_path / "b.csv", ts, np.full(50, 5.1), np.full(50, 2))
    with pytest.raises(ValueError, match="duplicate timestamps"):
        CurveFeatureEngine(a, b).build_features()


def test_roll_yield_deferral_logged_loudly(tmp_path, caplog):
    ts, front, second, iids = _merge_fixture_series(n=500)
    engine = make_engine(tmp_path, ts, front, second, front_iids=iids)
    with caplog.at_level(logging.WARNING, logger="src.features.curve_features"):
        engine.build_features()
    assert "CURVE_ROLL_YIELD DEFERRED to 03C" in caplog.text
    assert "lookahead" in caplog.text


# ---------------------------------------------------------------------------
# Time_Month_Sin/Cos month encoding (data_processor.add_time_features)
# ---------------------------------------------------------------------------

class TestMonthEncoding:
    @staticmethod
    def _frame():
        idx = pd.DatetimeIndex(
            ["2022-01-15 10:00", "2022-04-01 05:00", "2022-07-20 22:00", "2022-10-31 13:00"]
        )
        return pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx)

    def _processor(self):
        from src.data_processor import DataProcessor
        return DataProcessor(input_path="unused.csv", output_path="unused.parquet")

    def test_flag_off_columns_absent(self):
        df = self._processor().add_time_features(self._frame(), include_day_of_week=True)
        assert "Time_Month_Sin" not in df.columns
        assert "Time_Month_Cos" not in df.columns

    def test_default_is_off(self):
        df = self._processor().add_time_features(self._frame())
        assert "Time_Month_Sin" not in df.columns

    def test_values_at_known_dates(self):
        df = self._processor().add_time_features(
            self._frame(), include_day_of_week=True, include_month=True
        )
        # Jan (month 1): phase 0 -> sin 0, cos 1
        assert df["Time_Month_Sin"].iloc[0] == pytest.approx(0.0, abs=1e-12)
        assert df["Time_Month_Cos"].iloc[0] == pytest.approx(1.0, abs=1e-12)
        # Apr (month 4): phase pi/2 -> sin 1, cos 0
        assert df["Time_Month_Sin"].iloc[1] == pytest.approx(1.0, abs=1e-12)
        assert df["Time_Month_Cos"].iloc[1] == pytest.approx(0.0, abs=1e-12)
        # Jul (month 7): phase pi -> sin 0, cos -1
        assert df["Time_Month_Sin"].iloc[2] == pytest.approx(0.0, abs=1e-12)
        assert df["Time_Month_Cos"].iloc[2] == pytest.approx(-1.0, abs=1e-12)
        # Oct (month 10): phase 3pi/2 -> sin -1, cos 0
        assert df["Time_Month_Sin"].iloc[3] == pytest.approx(-1.0, abs=1e-12)
        assert df["Time_Month_Cos"].iloc[3] == pytest.approx(0.0, abs=1e-12)

    def test_existing_time_columns_unchanged(self):
        base = self._processor().add_time_features(self._frame(), include_day_of_week=True)
        gated = self._processor().add_time_features(
            self._frame(), include_day_of_week=True, include_month=True
        )
        for col in ["Time_Sin", "Time_Cos", "Time_DayOfWeek_Sin", "Time_DayOfWeek_Cos"]:
            np.testing.assert_array_equal(base[col].values, gated[col].values)
