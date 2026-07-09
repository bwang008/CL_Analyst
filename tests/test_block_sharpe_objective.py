"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/strategy_optimizer.py (primary),
    agent/batch_post_optimizer.py, agent/unified_pair_optimizer.py,
    scripts/preflight_holdout_check.py
Target Class/Function: _block_sharpe_score, _block_sharpe_details,
    BLOCK_OBJECTIVE_METRICS, _compute_objective_score, run_optimization,
    main (CLI --objective), parse_objectives, build_arg_parser,
    unified_pair_optimizer.main (--objectives), check_block_layout
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: block-sharpe-objective-ab_07092026_1031

RED-PHASE tests pinning the block-wise Sharpe objective contracts BEFORE
implementation (blueprint sections 1, 2, 3, 7 + Tests).

Contract summary (the implementation MUST satisfy exactly this):

agent/strategy_optimizer.py
  * BLOCK_OBJECTIVE_METRICS == {"block_min", "block_median", "block_mean_std"}
  * _block_sharpe_score(monthly_pnls, window_start, window_end, n_blocks,
                        metric, lambda_dispersion) -> float
      - monthly_pnls: pd.Series with DatetimeIndex of month-end bins (as
        produced by ``trades.resample("M").sum()``).
      - Reindex to the FULL calendar month range of [window_start, window_end]
        with fill 0.  The month CONTAINING window_end is included even when
        window_end is mid-month (real predictions end mid-month; their
        month-end resample bin must not be dropped).
      - Partition into n_blocks contiguous near-equal blocks in TIME order;
        remainder months go to the EARLIEST blocks (42 -> 14/14/14,
        41 -> 14/14/13, 43 -> 15/14/14).
      - Per block: annualized monthly Sharpe = mean/std * sqrt(12) with
        POPULATION std (np.std ddof=0, matching the existing sharpe path).
        Block std < 1e-9 OR all-zero (no trades) -> block Sharpe 0.0
        (never a division error, never -9999 at block level, never the cap).
      - Each block Sharpe is clipped to +/- OBJECTIVE_SCORE_CAP (5.0)
        BEFORE aggregation.
      - Aggregation: block_min = min; block_median = median;
        block_mean_std = mean - lambda_dispersion * std(per-block Sharpes,
        POPULATION ddof=0).  Signed values everywhere — no harmonic or
        geometric means.
  * _block_sharpe_details(monthly_pnls, window_start, window_end, n_blocks)
      -> Mapping with keys:
        "block_sharpes": list[float] of length n_blocks (post-clip values)
        "block_bounds":  list of n_blocks (start, end) date-like pairs, each
                         coercible via pd.Timestamp, giving the first/last
                         calendar month of each block.
      This is the per-block diagnostics source for optuna_info.block_sharpes.
  * _compute_objective_score:
      - EXISTING 3-positional-arg call form for sharpe/sortino keeps working
        and stays NUMERICALLY IDENTICAL to today (hardcoded regression
        values below were computed from the pre-change implementation).
      - Block metrics: _compute_objective_score(result, metric, trade_floor,
        window_start=..., window_end=..., n_blocks=..., lambda_dispersion=...)
        routes through the block helper, then min(agg, OBJECTIVE_SCORE_CAP),
        then _apply_trade_floor_penalty — exactly like the existing branches.
      - Zero trades -> -9999.0 for every metric (block included).
  * run_optimization accepts n_blocks / lambda_dispersion / min_block_months
    kwargs and HARD-RAISES ValueError (no silent fallback — house rule) when
    objective_metric is a block metric and the POST-HOLDOUT in-sample window
    has fewer months than n_blocks * min_block_months.
  * CLI --objective choices include the three block metrics.

agent/batch_post_optimizer.py
  * parse_objectives(value: str) -> list[str]: comma-separated list, every
    element in {sharpe, sortino, block_min, block_median, block_mean_std},
    "both" -> ["sharpe", "sortino"], surrounding whitespace tolerated,
    unknown metric / empty input rejected loudly (ValueError or
    argparse.ArgumentTypeError).
  * build_arg_parser() -> argparse.ArgumentParser with --n-blocks (default 3),
    --lambda-dispersion (default 1.0), --min-block-months (default 10), and
    an --objective that accepts a comma-separated list.

agent/unified_pair_optimizer.py
  * New --objectives argument, default "sharpe".  A single arm reads ONLY
    batch_summary_optimized_<arm>.md and writes top_pairs.json when the arm
    is sharpe, else top_pairs_<arm>.json.  NO cross-arm dedup ever.

scripts/preflight_holdout_check.py
  * check_block_layout(window_start, window_end, holdout_months, objectives,
    n_blocks, min_block_months) -> (ok: bool, message: str), a pure function
    the CLI wraps.  Passes when no block metric is present; fails when a
    block metric is present and (window_months - holdout_months) <
    n_blocks * min_block_months; the message reports the computed calendar
    block layout (months per block).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------

SQRT12 = float(np.sqrt(12))

# Exact per-block Sharpes for alternating monthly-PnL patterns over an EVEN
# number of months (mean and population std are exact in binary floats):
#   pattern (+2, -1): mean 0.5,  std 1.5 -> +0.3333*sqrt(12)
#   pattern (+1, -2): mean -0.5, std 1.5 -> -0.3333*sqrt(12)
#   pattern (+4, -1): mean 1.5,  std 2.5 -> +0.6*sqrt(12)
S_A = (0.5 / 1.5) * SQRT12    # ~ +1.1547
S_B = (-0.5 / 1.5) * SQRT12   # ~ -1.1547
S_C = (1.5 / 2.5) * SQRT12    # ~ +2.0785

WS = pd.Timestamp("2022-01-01")   # canonical 42-month test window
WE = pd.Timestamp("2025-06-30")   # 2022-01 .. 2025-06 inclusive = 42 months

ABS_TOL = 1e-12


def _so():
    import agent.strategy_optimizer as so
    return so


def _require(mod, name):
    """Fetch a to-be-implemented attribute; clean RED failure when missing."""
    obj = getattr(mod, name, None)
    if obj is None:
        pytest.fail(
            f"RED PHASE: `{name}` is not implemented in {mod.__name__} yet "
            f"(ticket block-sharpe-objective-ab_07092026_1031)"
        )
    return obj


def _alt_series(start_month_end: str, n_months: int, hi: float, lo: float) -> pd.Series:
    """Monthly PnL series alternating hi, lo, hi, lo... at month-end stamps
    (the shape ``trades.resample('M').sum()`` produces)."""
    idx = pd.date_range(start_month_end, periods=n_months, freq="M")
    vals = [hi if i % 2 == 0 else lo for i in range(n_months)]
    return pd.Series(vals, index=idx, dtype=float)


def _three_block_series() -> pd.Series:
    """42 months (2022-01..2025-06): block1 pattern A, block2 B, block3 C."""
    a = _alt_series("2022-01-31", 14, 2.0, -1.0)   # months  0..13 -> S_A
    b = _alt_series("2023-03-31", 14, 1.0, -2.0)   # months 14..27 -> S_B
    c = _alt_series("2024-05-31", 14, 4.0, -1.0)   # months 28..41 -> S_C
    return pd.concat([a, b, c])


def _block_score(series, metric, lam=1.0, ws=WS, we=WE, n_blocks=3):
    fn = _require(_so(), "_block_sharpe_score")
    return fn(
        monthly_pnls=series,
        window_start=ws,
        window_end=we,
        n_blocks=n_blocks,
        metric=metric,
        lambda_dispersion=lam,
    )


def _block_details(series, ws=WS, we=WE, n_blocks=3):
    fn = _require(_so(), "_block_sharpe_details")
    return fn(monthly_pnls=series, window_start=ws, window_end=we, n_blocks=n_blocks)


def _months_per_block(block_bounds):
    """Calendar months per block from (start, end) bounds — representation
    agnostic (works whether bounds are month-start or month-end stamps)."""
    out = []
    for start, end in block_bounds:
        sp = pd.Timestamp(start).to_period("M")
        ep = pd.Timestamp(end).to_period("M")
        out.append((ep - sp).n + 1)
    return out


def _mk_result(trades):
    """BacktestResult-like duck-type: list of (exit date str, pnl dollars)."""
    tr = [SimpleNamespace(exit_dt=pd.Timestamp(d), net_pnl_dollars=float(p))
          for d, p in trades]
    return SimpleNamespace(trade_count=len(tr), trades=tr)


def _monthly_trades(start_month: str, monthly_values) -> list:
    """One trade per month (day 15) so resample('M').sum() == monthly_values."""
    months = pd.date_range(start_month, periods=len(monthly_values), freq="M")
    return [(m.strftime("%Y-%m-15"), v) for m, v in zip(months, monthly_values)]


# ===========================================================================
# A. strategy_optimizer — module constants
# ===========================================================================


class TestBlockObjectiveConstants:
    """Pin the new BLOCK_OBJECTIVE_METRICS constant and the untouched cap."""

    def test_block_objective_metrics_constant(self):
        metrics = _require(_so(), "BLOCK_OBJECTIVE_METRICS")
        assert metrics == {"block_min", "block_median", "block_mean_std"}

    def test_baseline_metrics_not_in_block_set(self):
        metrics = _require(_so(), "BLOCK_OBJECTIVE_METRICS")
        assert "sharpe" not in metrics
        assert "sortino" not in metrics

    def test_objective_score_cap_unchanged(self):
        """Guard rail: OBJECTIVE_SCORE_CAP stays 5.0 (blueprint non-change)."""
        assert _so().OBJECTIVE_SCORE_CAP == 5.0


# ===========================================================================
# A. strategy_optimizer — _block_sharpe_details partition contract
# ===========================================================================


class TestBlockPartition:
    """Contiguous near-equal calendar partition, remainder to EARLIEST blocks."""

    def test_42_months_even_partition(self):
        """42 months / 3 blocks -> 14/14/14."""
        series = _alt_series("2022-01-31", 2, 1.0, -1.0)
        details = _block_details(series, ws=WS, we=WE, n_blocks=3)
        assert _months_per_block(details["block_bounds"]) == [14, 14, 14]

    def test_41_months_remainder_to_earliest(self):
        """41 months / 3 blocks -> 14/14/13 (remainder month to block 1+2)."""
        series = _alt_series("2022-01-31", 2, 1.0, -1.0)
        details = _block_details(
            series, ws=WS, we=pd.Timestamp("2025-05-31"), n_blocks=3
        )
        assert _months_per_block(details["block_bounds"]) == [14, 14, 13]

    def test_43_months_remainder_to_earliest(self):
        """43 months / 3 blocks -> 15/14/14 (remainder month to block 1)."""
        series = _alt_series("2022-01-31", 2, 1.0, -1.0)
        details = _block_details(
            series, ws=WS, we=pd.Timestamp("2025-07-31"), n_blocks=3
        )
        assert _months_per_block(details["block_bounds"]) == [15, 14, 14]

    def test_partition_contiguous_and_spans_window(self):
        """Blocks tile the calendar window: first block starts at window_start's
        month, last block ends at window_end's month, no gaps/overlaps."""
        details = _block_details(_three_block_series(), ws=WS, we=WE, n_blocks=3)
        bounds = details["block_bounds"]
        assert len(bounds) == 3
        assert pd.Timestamp(bounds[0][0]).to_period("M") == WS.to_period("M")
        assert pd.Timestamp(bounds[-1][1]).to_period("M") == WE.to_period("M")
        for i in range(len(bounds) - 1):
            prev_end = pd.Timestamp(bounds[i][1]).to_period("M")
            next_start = pd.Timestamp(bounds[i + 1][0]).to_period("M")
            assert next_start == prev_end + 1, (
                f"blocks {i} and {i + 1} are not contiguous: "
                f"{prev_end} -> {next_start}"
            )

    def test_mid_month_window_end_includes_terminal_month(self):
        """window_end 2025-06-12 (mid-month): June 2025 is IN the window.
        Its month-end resample bin (2025-06-30) must be retained, and the
        partition is still 42 months -> 14/14/14."""
        series = _three_block_series()  # last bin stamped 2025-06-30
        details = _block_details(
            series, ws=WS, we=pd.Timestamp("2025-06-12"), n_blocks=3
        )
        counts = _months_per_block(details["block_bounds"])
        assert counts == [14, 14, 14]
        assert (
            pd.Timestamp(details["block_bounds"][-1][1]).to_period("M")
            == pd.Period("2025-06", freq="M")
        )
        # June's data was not dropped by the reindex -> block 3 keeps S_C
        assert details["block_sharpes"][2] == pytest.approx(S_C, abs=ABS_TOL)


# ===========================================================================
# A. strategy_optimizer — calendar 0-fill + degenerate blocks + clipping
# ===========================================================================


class TestBlockCalendarReindexAndDegenerates:

    def test_partition_covers_calendar_window_not_trade_span(self):
        """Trades only in the LAST third of the window: the partition still
        spans the whole 42-month calendar window (a config that only trades
        2024-2025 must NOT get its blocks squeezed into its active period),
        and the inactive early blocks score exactly 0.0."""
        sparse = _alt_series("2024-05-31", 14, 2.0, -1.0)  # months 28..41 only
        details = _block_details(sparse, ws=WS, we=WE, n_blocks=3)

        assert _months_per_block(details["block_bounds"]) == [14, 14, 14]
        assert pd.Timestamp(details["block_bounds"][0][0]).to_period("M") \
            == pd.Period("2022-01", freq="M")

        sharpes = details["block_sharpes"]
        assert len(sharpes) == 3
        assert sharpes[0] == 0.0
        assert sharpes[1] == 0.0
        assert sharpes[2] == pytest.approx(S_A, abs=ABS_TOL)

    def test_block_min_zero_for_inactive_blocks(self):
        """block_min over the sparse series is 0.0 — inactive months count."""
        sparse = _alt_series("2024-05-31", 14, 2.0, -1.0)
        assert _block_score(sparse, "block_min") == 0.0

    def test_all_zero_pnl_block_scores_zero(self):
        """A block whose months are PRESENT but all 0.0 -> block Sharpe 0.0."""
        zeros = _alt_series("2022-01-31", 14, 0.0, 0.0)      # block 1: zeros
        rest = _alt_series("2023-03-31", 28, 2.0, -1.0)      # blocks 2+3: A
        details = _block_details(pd.concat([zeros, rest]))
        assert details["block_sharpes"][0] == 0.0

    def test_constant_nonzero_block_scores_zero_not_cap(self):
        """std < 1e-9 with positive mean -> 0.0, NOT the sortino-style 5.0
        'holy grail' and NOT -9999 (blueprint design decision 2)."""
        flat = _alt_series("2022-01-31", 14, 100.0, 100.0)   # constant +100
        rest = _alt_series("2023-03-31", 28, 2.0, -1.0)
        details = _block_details(pd.concat([flat, rest]))
        assert details["block_sharpes"][0] == 0.0

    def test_per_block_clip_at_plus_minus_cap(self):
        """Per-block Sharpes are clipped to +/-5.0 BEFORE aggregation."""
        hot = _alt_series("2022-01-31", 14, 3.0, 1.0)    # mean 2, std 1 -> +6.93
        cold = _alt_series("2023-03-31", 14, -1.0, -3.0)  # mean -2, std 1 -> -6.93
        mild = _alt_series("2024-05-31", 14, 2.0, -1.0)   # S_A
        details = _block_details(pd.concat([hot, cold, mild]))
        sharpes = details["block_sharpes"]
        assert sharpes[0] == 5.0
        assert sharpes[1] == -5.0
        assert sharpes[2] == pytest.approx(S_A, abs=ABS_TOL)

    def test_aggregation_uses_clipped_block_values(self):
        """block_mean_std must aggregate the CLIPPED per-block Sharpes: a
        single lucky block cannot drag the mean beyond the cap's reach."""
        hot = _alt_series("2022-01-31", 14, 3.0, 1.0)
        cold = _alt_series("2023-03-31", 14, -1.0, -3.0)
        mild = _alt_series("2024-05-31", 14, 2.0, -1.0)
        series = pd.concat([hot, cold, mild])

        clipped = np.array([5.0, -5.0, S_A])
        expected = float(clipped.mean() - 1.0 * clipped.std())  # ddof=0
        assert _block_score(series, "block_mean_std", lam=1.0) \
            == pytest.approx(expected, abs=ABS_TOL)


# ===========================================================================
# A. strategy_optimizer — aggregation math
# ===========================================================================


class TestBlockAggregation:
    """min / median / mean-std aggregation over per-block Sharpes
    [S_A, S_B, S_C] ~ [+1.1547, -1.1547, +2.0785]."""

    def test_block_min(self):
        assert _block_score(_three_block_series(), "block_min") \
            == pytest.approx(S_B, abs=ABS_TOL)

    def test_block_median(self):
        assert _block_score(_three_block_series(), "block_median") \
            == pytest.approx(S_A, abs=ABS_TOL)

    def test_block_mean_std_lambda_zero_is_mean(self):
        blocks = np.array([S_A, S_B, S_C])
        assert _block_score(_three_block_series(), "block_mean_std", lam=0.0) \
            == pytest.approx(float(blocks.mean()), abs=ABS_TOL)

    @pytest.mark.parametrize("lam", [1.0, 2.5])
    def test_block_mean_std_lambda_scales_dispersion(self, lam):
        """block_mean_std = mean - lambda * population std of block Sharpes."""
        blocks = np.array([S_A, S_B, S_C])
        expected = float(blocks.mean() - lam * blocks.std())  # ddof=0
        assert _block_score(_three_block_series(), "block_mean_std", lam=lam) \
            == pytest.approx(expected, abs=ABS_TOL)

    def test_all_negative_blocks_well_defined(self):
        """Negative block Sharpes must flow through every aggregation without
        NaN/error (no harmonic/geometric means anywhere)."""
        b1 = _alt_series("2022-01-31", 14, 1.0, -2.0)   # -1.1547
        b2 = _alt_series("2023-03-31", 14, 1.0, -3.0)   # mean -1, std 2 -> -1.7321
        b3 = _alt_series("2024-05-31", 14, 0.0, -2.0)   # mean -1, std 1 -> -3.4641
        series = pd.concat([b1, b2, b3])

        exp_blocks = np.array([
            (-0.5 / 1.5) * SQRT12,
            (-1.0 / 2.0) * SQRT12,
            (-1.0 / 1.0) * SQRT12,
        ])
        score_min = _block_score(series, "block_min")
        assert score_min == pytest.approx(float(exp_blocks.min()), abs=ABS_TOL)
        assert np.isfinite(score_min)

        score_ms = _block_score(series, "block_mean_std", lam=1.0)
        expected_ms = float(exp_blocks.mean() - exp_blocks.std())
        assert score_ms == pytest.approx(expected_ms, abs=ABS_TOL)
        assert np.isfinite(score_ms)


# ===========================================================================
# A. strategy_optimizer — _compute_objective_score sharpe/sortino REGRESSION
# ===========================================================================

# Hardcoded expected values computed by running the CURRENT (pre-change)
# _compute_objective_score in the trader env on 2026-07-09.  A refactor that
# drifts the sharpe/sortino math will fail these exact comparisons.
#
# Trade set 1 -> monthly PnLs [1000, -500, 2000, 1500, -1000, 3000]
#   raw sharpe = mean/std*sqrt(12) = 2.502172968684897 (uncapped, floor met)
_TRADES_MIXED = [
    ("2024-01-10", 600), ("2024-01-20", 400),
    ("2024-02-10", -300), ("2024-02-20", -200),
    ("2024-03-10", 1500), ("2024-03-20", 500),
    ("2024-04-10", 1000), ("2024-04-20", 500),
    ("2024-05-10", -600), ("2024-05-20", -400),
    ("2024-06-10", 2000), ("2024-06-20", 1000),
]
_EXPECTED_SHARPE_FLOOR_MET = 2.502172968684897
_EXPECTED_SHARPE_FLOOR_PENALIZED = 0.24374378060078877   # floor=100, 12 trades
_EXPECTED_SORTINO_CAPPED = 5.0                            # same trades, capped

# Trade set 2 -> near-constant positive months -> sharpe >> 5 -> capped
_TRADES_CAP = [
    ("2024-01-15", 1000), ("2024-02-15", 1010), ("2024-03-15", 990),
    ("2024-04-15", 1000), ("2024-05-15", 1005), ("2024-06-15", 995),
]

# Trade set 3 -> negative-mean months -> negative sortino, floor bypassed
_TRADES_NEG = [
    ("2024-01-15", 1000), ("2024-02-15", -2000), ("2024-03-15", 1500),
    ("2024-04-15", -1800), ("2024-05-15", 500), ("2024-06-15", -2500),
]
_EXPECTED_SORTINO_NEGATIVE = -1.2706412872832564


class TestComputeObjectiveScoreSharpeRegression:
    """The refactored dispatch must keep the sharpe/sortino paths numerically
    IDENTICAL to today — including the legacy 3-positional-arg call form."""

    def test_sharpe_exact_value_floor_met(self):
        result = _mk_result(_TRADES_MIXED)
        score = _so()._compute_objective_score(result, "sharpe", 10.0)
        assert score == _EXPECTED_SHARPE_FLOOR_MET

    def test_sharpe_exact_value_floor_penalized(self):
        result = _mk_result(_TRADES_MIXED)
        score = _so()._compute_objective_score(result, "sharpe", 100.0)
        assert score == _EXPECTED_SHARPE_FLOOR_PENALIZED

    def test_sharpe_capped_at_five(self):
        result = _mk_result(_TRADES_CAP)
        score = _so()._compute_objective_score(result, "sharpe", 5.0)
        assert score == 5.0

    def test_sortino_capped_at_five(self):
        result = _mk_result(_TRADES_MIXED)
        score = _so()._compute_objective_score(result, "sortino", 10.0)
        assert score == _EXPECTED_SORTINO_CAPPED

    def test_sortino_negative_exact_value(self):
        result = _mk_result(_TRADES_NEG)
        score = _so()._compute_objective_score(result, "sortino", 5.0)
        assert score == _EXPECTED_SORTINO_NEGATIVE

    def test_zero_trades_returns_minus_9999(self):
        result = SimpleNamespace(trade_count=0, trades=[])
        assert _so()._compute_objective_score(result, "sharpe", 10.0) == -9999.0


# ===========================================================================
# A. strategy_optimizer — _compute_objective_score block-metric routing
# ===========================================================================


class TestComputeObjectiveScoreBlockRouting:
    """Block metrics route through the SAME block math (window-bounds + block
    params passed as keywords), then cap, then _apply_trade_floor_penalty."""

    @staticmethod
    def _three_block_trades():
        """One trade per month over 2022-01..2025-06 whose monthly sums equal
        _three_block_series() -> per-block Sharpes [S_A, S_B, S_C]."""
        vals = list(_three_block_series().values)
        return _monthly_trades("2022-01-31", vals)

    def _score(self, result, metric, floor, lam=1.0):
        return _so()._compute_objective_score(
            result, metric, floor,
            window_start=WS, window_end=WE,
            n_blocks=3, lambda_dispersion=lam,
        )

    def test_block_min_negative_returned_as_is(self):
        """block_min = S_B < 0: negative scores bypass the floor penalty
        (identical to the existing branches)."""
        result = _mk_result(self._three_block_trades())
        assert self._score(result, "block_min", 100.0) \
            == pytest.approx(S_B, abs=ABS_TOL)

    def test_block_mean_std_matches_block_helper_math(self):
        blocks = np.array([S_A, S_B, S_C])
        expected = float(blocks.mean() - 1.0 * blocks.std())  # negative
        result = _mk_result(self._three_block_trades())
        assert self._score(result, "block_mean_std", 100.0, lam=1.0) \
            == pytest.approx(expected, abs=ABS_TOL)

    def test_block_min_positive_gets_trade_floor_penalty(self):
        """Positive block score x the EXISTING sigmoid floor penalty."""
        vals = list(_alt_series("2022-01-31", 42, 2.0, -1.0).values)  # all A
        result = _mk_result(_monthly_trades("2022-01-31", vals))      # 42 trades
        expected = _so()._apply_trade_floor_penalty(S_A, 42, 100.0)
        assert self._score(result, "block_min", 100.0) \
            == pytest.approx(expected, abs=ABS_TOL)

    def test_block_metric_zero_trades_minus_9999(self):
        result = SimpleNamespace(trade_count=0, trades=[])
        assert self._score(result, "block_min", 10.0) == -9999.0


# ===========================================================================
# A. strategy_optimizer — run_optimization block-window guard (mocked I/O)
# ===========================================================================

_MINIMAL_CONFIG = {
    "nickname": "test_model",
    "execution_class": "TieredEnsembleStrategy",
    "exit_mode": "TIERED",
    "tp_atr_mult": 1.5,
    "sl_atr_mult": 1.0,
    "trailing_atr_mult": 100.0,
    "max_hold_bars": 24,
    "cooldown_bars": 4,
    "entry_threshold": 0.55,
    "allow_concurrent": False,
    "max_concurrent": 1,
    "models": {"long": {"threshold": 0.55}, "short": {"threshold": 0.55}},
    "long": {
        "tp_atr_mult": 1.5, "sl_atr_mult": 1.0,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 1.5}],
        "tiers": [{"min_prob": 0.55, "lots": 1}],
    },
    "short": {
        "tp_atr_mult": 1.5, "sl_atr_mult": 1.0,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 1.5}],
        "tiers": [{"min_prob": 0.55, "lots": 1}],
    },
}


def _make_preds(start: str, end: str) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq="h")
    return pd.DataFrame({"pred_long": 0.6, "pred_short": 0.4}, index=idx)


def _make_stub_ohlcv() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=200, freq="h")
    return pd.DataFrame(
        {"open": 70.0, "high": 71.0, "low": 69.0, "close": 70.5, "volume": 1000},
        index=idx,
    )


def _make_mock_baseline_result():
    result = MagicMock()
    result.trades = []
    result.total_pnl = 0.0
    result.profit_factor = 0.0
    result.win_rate = 0.0
    result.trade_count = 0
    result.max_drawdown = 0.0
    result.equity_curve = pd.Series([0.0])
    result.start_dt = pd.Timestamp("2024-01-01")
    result.end_dt = pd.Timestamp("2024-06-01")
    result.exit_distribution = {}
    return result


class _EarlyExit(Exception):
    """Controlled short-circuit at study.optimize() — 'the run proceeded'."""


def _run_optimization_mocked(objective_metric, stub_preds, **run_kwargs):
    """Call run_optimization with all heavy I/O mocked (same pattern as
    tests/test_objective_seed_offset.py).  Returns True when the run reached
    study.optimize() (i.e. the block-window guard passed).  Exceptions other
    than the controlled _EarlyExit propagate to the caller."""
    import agent.strategy_optimizer as mod

    mock_study = MagicMock()
    mock_study.best_trial = MagicMock()
    mock_study.best_trial.number = 0
    mock_study.best_trial.params = {"tp_atr_mult": 1.5}
    mock_study.best_trial.value = 1.0
    mock_study.trials = [mock_study.best_trial]
    mock_study.optimize.side_effect = _EarlyExit("short-circuit")

    class FakeTPESampler:
        def __init__(self, *, seed=None, **kwargs):
            pass

    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_mock_baseline_result()
    mock_engine_cls = MagicMock()
    mock_engine_cls.from_config.return_value = mock_engine

    stub_ohlcv = _make_stub_ohlcv()

    patches = [
        patch("optuna.samplers.TPESampler", FakeTPESampler),
        patch("optuna.create_study", return_value=mock_study),
        patch(
            "src.live_execution.config_loader.load_strategy_config",
            return_value=copy.deepcopy(_MINIMAL_CONFIG),
        ),
        patch.object(mod, "_load_ensemble_predictions", return_value=stub_preds),
        patch.object(mod, "load_ohlcv_dual", return_value=(stub_ohlcv, stub_ohlcv.copy())),
        patch.object(mod, "attach_atr_cache", side_effect=lambda df: df),
        patch.object(mod, "BacktestEngine", mock_engine_cls),
        patch.object(mod, "extract_metrics", return_value={
            "total_pnl": 0, "profit_factor": 0, "win_rate": 0,
            "trade_count": 0, "max_drawdown": 0,
        }),
        patch.object(mod, "make_objective", return_value=MagicMock()),
        patch.object(mod, "BestResultTracker", return_value=MagicMock()),
        patch.object(mod, "TopKTracker", return_value=MagicMock()),
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.unlink"),
        patch.object(mod, "_extract_warm_start_params", return_value=None),
        patch.object(mod, "send_telegram", return_value=None),
    ]

    reached_optimize = False
    for p in patches:
        p.start()
    try:
        try:
            mod.run_optimization(
                config_path="fake_config.json",
                n_trials=3,
                objective_metric=objective_metric,
                random_seed=42,
                quiet=True,
                **run_kwargs,
            )
        except _EarlyExit:
            reached_optimize = True
    finally:
        for p in patches:
            p.stop()

    return reached_optimize


class TestRunOptimizationBlockWindowGuard:
    """Hard ValueError when the POST-HOLDOUT in-sample window is smaller than
    n_blocks * min_block_months for a block metric.  No silent fallback to
    single sharpe — house rule."""

    def test_short_insample_window_hard_raises(self):
        """36-month predictions minus 12-month holdout = 24 in-sample months
        < 3 * 10 -> ValueError (loud, mentioning the block layout)."""
        preds = _make_preds("2022-01-01", "2024-12-31")
        with pytest.raises(ValueError) as excinfo:
            _run_optimization_mocked(
                "block_min", preds,
                holdout_months=12,
                n_blocks=3, lambda_dispersion=1.0, min_block_months=10,
            )
        assert "block" in str(excinfo.value).lower()

    def test_sufficient_insample_window_proceeds(self):
        """48-month predictions minus 12-month holdout = 36 in-sample months
        >= 3 * 10 -> guard passes and the study runs."""
        preds = _make_preds("2022-01-01", "2025-12-31")
        reached = _run_optimization_mocked(
            "block_min", preds,
            holdout_months=12,
            n_blocks=3, lambda_dispersion=1.0, min_block_months=10,
        )
        assert reached, "run_optimization never reached study.optimize()"

    def test_sharpe_ignores_block_guard(self):
        """Non-block metrics are never window-gated: the same short window
        must proceed for sharpe (and accept the block kwargs inertly)."""
        preds = _make_preds("2024-01-01", "2024-12-31")  # ~12 months
        reached = _run_optimization_mocked(
            "sharpe", preds,
            n_blocks=3, lambda_dispersion=1.0, min_block_months=10,
        )
        assert reached, "sharpe run must not be blocked by the block guard"


# ===========================================================================
# A. strategy_optimizer — CLI --objective choices
# ===========================================================================


class TestStrategyOptimizerCLIObjectiveChoices:

    def _run_main(self, monkeypatch, objective):
        import agent.strategy_optimizer as mod
        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return {}, MagicMock()

        monkeypatch.setattr(mod, "run_optimization", fake_run)
        monkeypatch.setattr(sys, "argv", [
            "strategy_optimizer.py",
            "--config", "fake_config.json",
            "--objective", objective,
        ])
        mod.main()
        return captured

    @pytest.mark.parametrize(
        "metric", ["block_min", "block_median", "block_mean_std"]
    )
    def test_cli_accepts_block_metric(self, monkeypatch, metric):
        captured = self._run_main(monkeypatch, metric)
        assert captured.get("objective_metric") == metric

    @pytest.mark.parametrize("metric", ["sharpe", "sortino"])
    def test_cli_still_accepts_legacy_metrics(self, monkeypatch, metric):
        captured = self._run_main(monkeypatch, metric)
        assert captured.get("objective_metric") == metric


# ===========================================================================
# B. batch_post_optimizer — comma-list --objective parsing + block params
# ===========================================================================


class TestBatchParseObjectives:
    """parse_objectives(value) — the importable comma-list parsing seam."""

    def _parse(self):
        import agent.batch_post_optimizer as bpo
        return _require(bpo, "parse_objectives")

    def test_single_sharpe(self):
        assert self._parse()("sharpe") == ["sharpe"]

    def test_single_sortino(self):
        assert self._parse()("sortino") == ["sortino"]

    def test_both_alias_maps_to_sharpe_sortino(self):
        assert self._parse()("both") == ["sharpe", "sortino"]

    def test_comma_list_all_four_arms_order_preserved(self):
        assert self._parse()("sharpe,block_min,block_median,block_mean_std") == [
            "sharpe", "block_min", "block_median", "block_mean_std",
        ]

    def test_whitespace_tolerated(self):
        assert self._parse()(" sharpe , block_min ") == ["sharpe", "block_min"]

    def test_unknown_metric_rejected_loudly(self):
        with pytest.raises((ValueError, argparse.ArgumentTypeError)):
            self._parse()("sharpe,bogus_metric")

    def test_empty_string_rejected_loudly(self):
        with pytest.raises((ValueError, argparse.ArgumentTypeError)):
            self._parse()("")


class TestBatchArgParserBlockParams:
    """build_arg_parser() — importable parser seam for the new block args."""

    _MIN_ARGV = ["--batch-dir", "some_dir", "--random-seed", "7"]

    def _parser(self):
        import agent.batch_post_optimizer as bpo
        return _require(bpo, "build_arg_parser")()

    def test_n_blocks_default_3(self):
        args = self._parser().parse_args(self._MIN_ARGV)
        assert args.n_blocks == 3

    def test_lambda_dispersion_default_1(self):
        args = self._parser().parse_args(self._MIN_ARGV)
        assert args.lambda_dispersion == 1.0

    def test_min_block_months_default_10(self):
        args = self._parser().parse_args(self._MIN_ARGV)
        assert args.min_block_months == 10

    def test_objective_accepts_comma_separated_four_arms(self):
        """The old choices=[sharpe,sortino,both] must be gone: a comma list
        parses cleanly and normalizes to the 4-arm list."""
        import agent.batch_post_optimizer as bpo
        args = self._parser().parse_args(
            self._MIN_ARGV
            + ["--objective", "sharpe,block_min,block_median,block_mean_std"]
        )
        raw = args.objective
        if isinstance(raw, str):
            raw = _require(bpo, "parse_objectives")(raw)
        assert raw == ["sharpe", "block_min", "block_median", "block_mean_std"]


# ===========================================================================
# C. unified_pair_optimizer — per-arm selection, no cross-arm dedup
# ===========================================================================


def _upo_summary_md(rows):
    """batch_summary_optimized_<arm>.md fixture in the exact table shape
    parse_markdown_table() consumes (cols[2]=Trades opt, cols[6]=PnL opt,
    cols[7]=PnL holdout).  rows: list of (label, trades, pnl_opt, pnl_ho)."""
    def table():
        lines = [
            "| Experiment | Trades (pre) | Trades (opt) | PF (pre) | PF (opt) "
            "| PnL (pre) | PnL (opt) | PnL (holdout) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for label, trades, pnl_opt, pnl_ho in rows:
            lines.append(
                f"| {label} | 100 | {trades} | 1.10 | 1.30 "
                f"| $1,000 | ${pnl_opt:,} | ${pnl_ho:,} |"
            )
        return "\n".join(lines)

    return (
        "# Batch Summary (Optimized)\n"
        "\n"
        "### Long Model (Logloss)\n"
        "\n" + table() + "\n"
        "\n"
        "### Short Model (Logloss)\n"
        "\n" + table() + "\n"
    )


def _upo_batch_dir(tmp_path, md_files):
    """Create a batch dir with batch_progress.json + the given summary mds.
    md_files: {filename: rows}."""
    batch = tmp_path / "batch_test"
    batch.mkdir()
    progress = {
        "manifest": "manifest.json",
        "experiments": [
            {"label": "ExpA", "gcs_prefix": "expa_prefix", "status": "COMPLETED"},
            {"label": "ExpB", "gcs_prefix": "expb_prefix", "status": "COMPLETED"},
        ],
    }
    (batch / "batch_progress.json").write_text(
        json.dumps(progress), encoding="utf-8"
    )
    for fname, rows in md_files.items():
        (batch / fname).write_text(_upo_summary_md(rows), encoding="utf-8")
    return batch


def _run_upo(batch_dir, extra_args):
    import agent.unified_pair_optimizer as upo
    argv = [
        "unified_pair_optimizer.py",
        "--batch-dir", str(batch_dir),
        "--top-n", "1",
    ] + extra_args
    with patch.object(sys, "argv", argv):
        upo.main()


# Rows: ExpA is the arm-under-test's model; ExpB is a decoy in OTHER arms'
# summaries with absurdly high robustness — if any cross-arm read/dedup
# survives, ExpB wins selection and the assertions below catch it.
_ROWS_EXPA = [("ExpA", 150, 5000, 2000)]
_ROWS_EXPB_DECOY = [("ExpB", 500, 9000000, 9000000)]


class TestUnifiedPairOptimizerPerArm:

    def test_block_arm_reads_only_its_md_and_writes_suffixed_json(self, tmp_path):
        """--objectives block_min: selection comes ONLY from
        batch_summary_optimized_block_min.md; output is
        top_pairs_block_min.json; top_pairs.json is NOT written."""
        batch = _upo_batch_dir(tmp_path, {
            "batch_summary_optimized_block_min.md": _ROWS_EXPA,
            "batch_summary_optimized_sharpe.md": _ROWS_EXPB_DECOY,
            "batch_summary_optimized_sortino.md": _ROWS_EXPB_DECOY,
        })
        _run_upo(batch, ["--objectives", "block_min"])

        out = batch / "top_pairs_block_min.json"
        assert out.exists(), "block_min arm must write top_pairs_block_min.json"
        assert not (batch / "top_pairs.json").exists(), (
            "block_min arm must NOT write the sharpe-named top_pairs.json"
        )

        pairs = json.loads(out.read_text(encoding="utf-8"))
        assert len(pairs) == 1
        assert pairs[0]["target_long"] == "oos_predictions_expa_prefix_long_logloss"
        assert pairs[0]["target_short"] == "oos_predictions_expa_prefix_short_logloss"
        assert "expb_prefix" not in json.dumps(pairs), (
            "cross-arm contamination: decoy rows from another arm's summary "
            "leaked into this arm's pair selection"
        )

    def test_sharpe_arm_writes_top_pairs_json_no_cross_arm_dedup(self, tmp_path):
        """--objectives sharpe: reads ONLY the sharpe md (a huge-robustness
        sortino decoy must NOT be pooled in) and writes parity-compatible
        top_pairs.json."""
        batch = _upo_batch_dir(tmp_path, {
            "batch_summary_optimized_sharpe.md": _ROWS_EXPA,
            "batch_summary_optimized_sortino.md": _ROWS_EXPB_DECOY,
        })
        _run_upo(batch, ["--objectives", "sharpe"])

        out = batch / "top_pairs.json"
        assert out.exists(), "sharpe arm must write top_pairs.json"
        pairs = json.loads(out.read_text(encoding="utf-8"))
        assert len(pairs) == 1
        assert pairs[0]["target_long"] == "oos_predictions_expa_prefix_long_logloss"
        assert "expb_prefix" not in json.dumps(pairs), (
            "sortino rows were pooled into the sharpe arm — cross-arm dedup "
            "must be gone"
        )

    def test_default_objectives_is_sharpe(self, tmp_path):
        """No --objectives flag -> default 'sharpe': same isolation contract
        (today's code pools sortino — that behavior must die)."""
        batch = _upo_batch_dir(tmp_path, {
            "batch_summary_optimized_sharpe.md": _ROWS_EXPA,
            "batch_summary_optimized_sortino.md": _ROWS_EXPB_DECOY,
        })
        _run_upo(batch, [])

        out = batch / "top_pairs.json"
        assert out.exists()
        pairs = json.loads(out.read_text(encoding="utf-8"))
        assert "expb_prefix" not in json.dumps(pairs), (
            "default run pooled the sortino summary — default must be "
            "sharpe-only, no cross-arm dedup"
        )
        assert pairs[0]["target_long"] == "oos_predictions_expa_prefix_long_logloss"


# ===========================================================================
# D. preflight_holdout_check — importable block-layout gate
# ===========================================================================


class TestPreflightBlockLayoutGate:
    """check_block_layout(...) -> (ok, message): pure function the CLI wraps
    (the CLI keeps its exit-nonzero-on-failure convention around it)."""

    def _check(self):
        try:
            from scripts.preflight_holdout_check import check_block_layout
        except ImportError:
            pytest.fail(
                "RED PHASE: check_block_layout is not implemented in "
                "scripts/preflight_holdout_check.py yet "
                "(ticket block-sharpe-objective-ab_07092026_1031)"
            )
        return check_block_layout

    def test_no_block_metric_passes_regardless_of_window(self):
        """Block gate is inert for sharpe/sortino-only runs — even on a window
        far too small for blocks."""
        ok, message = self._check()(
            window_start=pd.Timestamp("2024-01-01"),
            window_end=pd.Timestamp("2024-06-30"),
            holdout_months=12,
            objectives=["sharpe", "sortino"],
            n_blocks=3,
            min_block_months=10,
        )
        assert ok
        assert isinstance(message, str)

    def test_block_metric_insufficient_window_fails(self):
        """~29-month window minus 12-month holdout < 3*10=30 required months
        -> FAIL, message names the block requirement (validation gate 4)."""
        ok, message = self._check()(
            window_start=pd.Timestamp("2024-01-01"),
            window_end=pd.Timestamp("2026-06-12"),
            holdout_months=12,
            objectives=["sharpe", "block_min"],
            n_blocks=3,
            min_block_months=10,
        )
        assert not ok
        assert "block" in message.lower()
        assert "30" in message, (
            "failure message must state the required months "
            "(n_blocks * min_block_months = 30)"
        )

    def test_block_metric_sufficient_window_passes_and_reports_layout(self):
        """The real 15B window (2022-01 -> 2026-06, holdout 12): in-sample
        2022-01..2025-06 = 42 months -> PASS with the 14/14/14 calendar
        layout reported (blueprint sizing table)."""
        ok, message = self._check()(
            window_start=pd.Timestamp("2022-01-01"),
            window_end=pd.Timestamp("2026-06-12"),
            holdout_months=12,
            objectives=["block_min", "block_median", "block_mean_std"],
            n_blocks=3,
            min_block_months=10,
        )
        assert ok
        assert "14" in message, (
            "pass message must report the computed months-per-block layout "
            "(14/14/14 for this window)"
        )
