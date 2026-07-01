"""Tests for the 'Best Trial' report column vs the regression guard (Fix 1).

When the regression guard reverts to baseline, the study's best trial was
discarded and the Optimized columns are intentionally blank (pre==opt). Printing
the study's best trial number (e.g. ``#2/3``) in that case is misleading — it
reads as though a non-baseline trial was selected. ``format_best_trial`` must
report the revert explicitly instead.
"""

from agent.batch_post_optimizer import format_best_trial


def test_guard_reverted_shows_baseline_not_trial_number():
    """When the guard triggered, show 'baseline (guard)', not the trial number."""
    opt_info = {
        "trial_number": 2,
        "n_trials": 3,
        "regression_guard_triggered": True,
    }
    out = format_best_trial(opt_info)
    assert out == "baseline (guard)"
    assert "#2" not in out  # the discarded trial number must not appear


def test_genuine_improvement_shows_trial_number():
    """When the guard did NOT trigger, show the selected trial as #n/N."""
    opt_info = {
        "trial_number": 47,
        "n_trials": 200,
        "regression_guard_triggered": False,
    }
    assert format_best_trial(opt_info) == "#47/200"


def test_missing_guard_flag_defaults_to_trial_number():
    """Legacy artifacts without the guard flag fall back to the trial number."""
    opt_info = {"trial_number": 5, "n_trials": 100}
    assert format_best_trial(opt_info) == "#5/100"


def test_missing_trial_number_shows_dash():
    """No trial number and no guard -> '-'."""
    opt_info = {"n_trials": 100}
    assert format_best_trial(opt_info) == "-"


def test_guard_takes_precedence_over_present_trial_number():
    """Even with a valid trial number, a triggered guard wins the display."""
    opt_info = {
        "trial_number": 0,
        "n_trials": 3,
        "regression_guard_triggered": True,
    }
    assert format_best_trial(opt_info) == "baseline (guard)"
