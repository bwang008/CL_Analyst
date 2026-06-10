"""Tests for agent.alpha_evaluator — frictionless ensemble evaluation."""

import numpy as np
import pandas as pd
import pytest

from agent.alpha_evaluator import evaluate_ensemble


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_forward_returns(index, horizons=(6, 12, 24, 48, 72), values=None):
    """Build a synthetic forward-returns DataFrame.

    If *values* is a 1-D array, it is broadcast to all horizons.
    If *values* is None, small random returns are generated.
    """
    if values is None:
        rng = np.random.default_rng(42)
        values = rng.normal(0, 0.01, size=len(index))
    data = {}
    for h in horizons:
        data[f"fwd_ret_{h}"] = values
    return pd.DataFrame(data, index=index)


@pytest.fixture
def dates():
    """800 hourly timestamps (roughly 33 days)."""
    return pd.date_range("2024-01-01", periods=800, freq="h")


@pytest.fixture
def perfect_signal_data(dates):
    """Returns (long_probs, short_probs, forward_returns) where the binary
    signal is always correct (buy when returns are positive, sell when
    negative)."""
    rng = np.random.default_rng(123)
    returns = rng.normal(0.001, 0.01, size=len(dates))  # slight positive drift

    # Construct probs so that binary signal matches sign of returns
    long_probs = pd.Series(
        np.where(returns > 0, 0.8, 0.2), index=dates, name="prob_Buy"
    )
    short_probs = pd.Series(
        np.where(returns < 0, 0.8, 0.2), index=dates, name="prob_Sell"
    )
    fwd = _make_forward_returns(dates, values=returns)
    return long_probs, short_probs, fwd


@pytest.fixture
def random_signal_data(dates):
    """Random probs, random returns — SNR should be ≈ 0."""
    rng = np.random.default_rng(999)
    long_probs = pd.Series(rng.uniform(0.3, 0.7, len(dates)), index=dates)
    short_probs = pd.Series(rng.uniform(0.3, 0.7, len(dates)), index=dates)
    returns = rng.normal(0, 0.01, len(dates))
    fwd = _make_forward_returns(dates, values=returns)
    return long_probs, short_probs, fwd


@pytest.fixture
def inverted_signal_data(dates):
    """Inverted signal — buy when returns are negative, sell when positive."""
    rng = np.random.default_rng(456)
    returns = rng.normal(0.001, 0.01, size=len(dates))

    long_probs = pd.Series(
        np.where(returns < 0, 0.8, 0.2), index=dates, name="prob_Buy"
    )
    short_probs = pd.Series(
        np.where(returns > 0, 0.8, 0.2), index=dates, name="prob_Sell"
    )
    fwd = _make_forward_returns(dates, values=returns)
    return long_probs, short_probs, fwd


# ---------------------------------------------------------------------------
# Tests — Signal quality
# ---------------------------------------------------------------------------

class TestEvaluateEnsembleSignalQuality:
    """Test that SNR direction reflects signal quality."""

    def test_perfect_signal_positive_snr(self, perfect_signal_data):
        """A signal always aligned with returns should yield positive SNR."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        assert metrics["peak_snr"] > 0, f"Expected positive SNR, got {metrics['peak_snr']}"

    def test_random_signal_near_zero_snr(self, random_signal_data):
        """A random signal should yield SNR close to 0."""
        lp, sp, fwd = random_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        # Allowing generous tolerance (|snr| < 0.15) for random noise
        assert abs(metrics["peak_snr"]) < 0.15, (
            f"Random signal SNR should be near 0, got {metrics['peak_snr']}"
        )

    def test_inverted_signal_negative_snr(self, inverted_signal_data):
        """An inverted signal should yield negative SNR."""
        lp, sp, fwd = inverted_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        assert metrics["peak_snr"] < 0, f"Expected negative SNR, got {metrics['peak_snr']}"


# ---------------------------------------------------------------------------
# Tests — SNR dimensionless (no annualization)
# ---------------------------------------------------------------------------

class TestSNRDimensionless:
    """Verify that SNR = mean/std with NO annualization factor."""

    def test_snr_equals_mean_over_std(self, perfect_signal_data):
        """Manually compute mean/std and compare to reported SNR."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)

        # Reproduce calculation for the first horizon (6)
        binary = np.where(lp.values > 0.5, 1, np.where(sp.values > 0.5, -1, 0))
        fpnl = binary * fwd["fwd_ret_6"].values
        valid = np.isfinite(fpnl)
        expected_snr = np.mean(fpnl[valid]) / np.std(fpnl[valid])

        assert metrics["snr_6"] == pytest.approx(expected_snr, rel=1e-9)

    def test_no_sqrt_252(self, perfect_signal_data):
        """SNR must NOT be multiplied by sqrt(252) or any scaling factor."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)

        binary = np.where(lp.values > 0.5, 1, np.where(sp.values > 0.5, -1, 0))
        fpnl = binary * fwd["fwd_ret_6"].values
        valid = np.isfinite(fpnl)
        raw_snr = np.mean(fpnl[valid]) / np.std(fpnl[valid])
        annualized = raw_snr * np.sqrt(252)

        # The reported SNR must match the raw, NOT the annualized
        assert metrics["snr_6"] != pytest.approx(annualized, rel=0.01)
        assert metrics["snr_6"] == pytest.approx(raw_snr, rel=1e-9)


# ---------------------------------------------------------------------------
# Tests — IC uses Spearman rank correlation
# ---------------------------------------------------------------------------

class TestICSpearman:
    """Verify that IC uses Spearman (rank) correlation, not Pearson."""

    def test_ic_nonzero_for_perfect_signal(self, perfect_signal_data):
        """Perfect signal should have non-trivial IC."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        # At least one horizon should have |IC| > 0.05
        ics = [metrics[f"ic_{h}"] for h in [6, 12, 24, 48, 72]]
        assert any(abs(ic) > 0.05 for ic in ics), f"All ICs near zero: {ics}"

    def test_ic_uses_continuous_signal(self, dates):
        """IC is computed on continuous signal = prob_Buy - prob_Sell, not
        binary. Verify by constructing a signal with varying magnitudes."""
        rng = np.random.default_rng(77)
        # Continuous signal with clear monotonic relationship to returns
        cont = rng.uniform(-1, 1, len(dates))
        returns = cont * 0.01 + rng.normal(0, 0.001, len(dates))  # noisy linear

        long_probs = pd.Series(np.clip(0.5 + cont / 2, 0, 1), index=dates)
        short_probs = pd.Series(np.clip(0.5 - cont / 2, 0, 1), index=dates)
        fwd = _make_forward_returns(dates, values=returns)

        metrics = evaluate_ensemble(long_probs, short_probs, fwd, threshold=0.5)
        # With a near-linear relationship, IC should be meaningfully positive
        assert metrics["ic_6"] > 0.3, f"Expected high IC, got {metrics['ic_6']}"


# ---------------------------------------------------------------------------
# Tests — Signal count and hit rate
# ---------------------------------------------------------------------------

class TestSignalCountAndHitRate:
    """Test signal_count and hit_rate aggregate metrics."""

    def test_signal_count(self, dates):
        """Signal count = number of bars where |binary_signal| > 0."""
        # All long probs > 0.5 → all signals are +1
        lp = pd.Series(0.8, index=dates)
        sp = pd.Series(0.2, index=dates)
        fwd = _make_forward_returns(dates)
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        assert metrics["signal_count"] == len(dates)

    def test_zero_signal_count(self, dates):
        """No probs above threshold → signal_count = 0."""
        lp = pd.Series(0.3, index=dates)
        sp = pd.Series(0.3, index=dates)
        fwd = _make_forward_returns(dates)
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        assert metrics["signal_count"] == 0

    def test_hit_rate_perfect(self, perfect_signal_data):
        """Perfect signal should have hit_rate close to 1."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        assert metrics["hit_rate"] > 0.9, f"Expected high hit rate, got {metrics['hit_rate']}"

    def test_hit_rate_inverted(self, inverted_signal_data):
        """Inverted signal should have hit_rate close to 0."""
        lp, sp, fwd = inverted_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        assert metrics["hit_rate"] < 0.1, f"Expected low hit rate, got {metrics['hit_rate']}"


# ---------------------------------------------------------------------------
# Tests — Holdout split
# ---------------------------------------------------------------------------

class TestHoldoutSplit:
    """Test that holdout_start produces separate eval/holdout metrics."""

    def test_holdout_produces_prefixed_metrics(self, perfect_signal_data):
        """With holdout_start set, both eval and holdout metrics exist."""
        lp, sp, fwd = perfect_signal_data
        holdout_start = lp.index[600]  # split at bar 600
        metrics = evaluate_ensemble(
            lp, sp, fwd, threshold=0.5, holdout_start=holdout_start
        )
        # Eval metrics (no prefix)
        assert "peak_snr" in metrics
        assert "signal_count" in metrics
        # Holdout metrics (prefixed)
        assert "holdout_peak_snr" in metrics
        assert "holdout_signal_count" in metrics
        assert "holdout_snr_6" in metrics
        assert "holdout_hit_rate" in metrics

    def test_holdout_signal_counts_add_up(self, perfect_signal_data):
        """Eval + holdout signal_count should equal full signal_count."""
        lp, sp, fwd = perfect_signal_data
        holdout_start = lp.index[600]

        full_metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        split_metrics = evaluate_ensemble(
            lp, sp, fwd, threshold=0.5, holdout_start=holdout_start
        )
        total = split_metrics["signal_count"] + split_metrics["holdout_signal_count"]
        assert total == full_metrics["signal_count"]


# ---------------------------------------------------------------------------
# Tests — Peak horizon
# ---------------------------------------------------------------------------

class TestPeakHorizon:
    """Test peak_snr and peak_horizon selection."""

    def test_peak_horizon_in_horizons(self, perfect_signal_data):
        """peak_horizon should be one of the evaluated horizons."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        assert metrics["peak_horizon"] in [6, 12, 24, 48, 72]

    def test_peak_snr_is_max(self, perfect_signal_data):
        """peak_snr must equal the maximum of all per-horizon SNRs."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        all_snrs = [metrics[f"snr_{h}"] for h in [6, 12, 24, 48, 72]]
        assert metrics["peak_snr"] == pytest.approx(max(all_snrs))


# ---------------------------------------------------------------------------
# Tests — Monthly breakdown
# ---------------------------------------------------------------------------

class TestMonthlyBreakdown:
    """Test monthly breakdown output."""

    def test_monthly_breakdown_keys(self, perfect_signal_data):
        """monthly_breakdown should be a dict with YYYY-MM keys."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        breakdown = metrics.get("monthly_breakdown", {})
        assert isinstance(breakdown, dict)
        assert len(breakdown) > 0
        for key in breakdown:
            assert len(key) == 7  # "YYYY-MM"
            assert key[4] == "-"

    def test_monthly_breakdown_values_are_float(self, perfect_signal_data):
        """All monthly breakdown values should be numeric."""
        lp, sp, fwd = perfect_signal_data
        metrics = evaluate_ensemble(lp, sp, fwd, threshold=0.5)
        for v in metrics["monthly_breakdown"].values():
            assert isinstance(v, float)
