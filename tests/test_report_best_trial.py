"""Tests for the 'Best Trial' report column vs the regression guard (Fix 1).

When the regression guard reverts to baseline, the study's best trial was
discarded and the Optimized columns are intentionally blank (pre==opt). Printing
the study's best trial number (e.g. ``#2/3``) in that case is misleading — it
reads as though a non-baseline trial was selected. ``format_best_trial`` must
report the revert explicitly instead.
"""

from agent.batch_post_optimizer import format_best_trial, generate_optimized_report


# ---------------------------------------------------------------------------
# Regression: guard-triggered rows must NOT source Opt columns from
# all_trial_params (ticket optimizer-objective-report-parity_07032026_0830).
# ---------------------------------------------------------------------------

# The 8 optimized-parameter columns in the individual summary table, in the
# order they appear after the "PnL (holdout)" column.
_OPT_COL_NAMES = [
    "Opt Thr",
    "Opt TP",
    "Opt SL",
    "Opt TrgF",
    "Opt DstF",
    "Opt Cool",
    "Opt Hold",
    "Opt Consec",
]


def _make_guard_result(objective_metric, all_trial_params):
    """A single guard-triggered short/average_precision target.

    ``params`` is empty (the optimizer clears it on a guard revert) but a
    populated ``all_trial_params`` from the discarded trial is present — the
    exact shape that used to leak rejected params into the Opt columns.
    """
    label = "NG01A 3x1 6H"
    opt_info = {
        "trial_number": 0 if objective_metric == "sharpe" else 1,
        "n_trials": 3,
        "regression_guard_triggered": True,
        "params": {},
        "all_trial_params": all_trial_params,
        "baseline_metrics": {
            "trade_count": 10,
            "buy_trades": 0,
            "sell_trades": 10,
            "profit_factor": 1.1,
            "total_pnl": 500.0,
        },
        "holdout_metrics": {},
        "wall_time_seconds": 12,
    }
    opt_key = f"{label}|short|average_precision"
    all_results = {
        opt_key: {
            "status": "OK",
            "metrics": {
                "trade_count": 10,
                "buy_trades": 0,
                "sell_trades": 10,
                "profit_factor": 1.1,
                "total_pnl": 500.0,
            },
            "optuna_info": opt_info,
        }
    }
    progress = {
        "manifest": "test",
        "experiments": [
            {"label": label, "status": "COMPLETED", "local_dir": "/nonexistent"}
        ],
    }
    return label, all_results, progress


def _norm(cell):
    """Collapse a padded markdown cell to its logical content.

    ``align_markdown_table`` pads blank cells with dashes (e.g. ``-------``),
    so a logically-empty '-' cell renders as a run of dashes. Normalize any
    all-dash cell back to '-' so assertions test content, not padding.
    """
    cell = cell.strip()
    if cell and set(cell) == {"-"}:
        return "-"
    return cell


def _summary_opt_cells(report, label):
    """Return the 8 normalized Opt-* cells from the guarded summary row for
    ``label`` (the row whose Best Trial reads 'baseline (guard)')."""
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if not stripped.startswith(f"| {label} "):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        # Only the guard-triggered data row (not the empty placeholder rows).
        if _norm(cells[-1]) != "baseline (guard)":
            continue
        # columns: label, trades(pre), trades(opt), PF(pre), PF(opt), PnL(pre),
        # PnL(opt), PnL(holdout), then the 8 Opt cols, then Best Trial.
        return [_norm(c) for c in cells[8:16]]
    raise AssertionError(f"No guarded summary row found for label {label!r}")


def test_guarded_row_opt_columns_not_from_all_trial_params():
    """Guard-triggered rows must render '-' for every Opt column, ignoring
    the discarded trial's all_trial_params."""
    all_trial_params = {
        "entry_threshold_short": 0.77,
        "tp_atr_mult_short": 3.5,
        "sl_atr_mult_short": 1.5,
        "trigger_frac_short": 0.4,
        "distance_frac_short": 0.6,
        "cooldown_bars_short": 5,
        "max_hold_bars_short": 20,
        "consecutive_signal_threshold_short": 2,
    }
    label, all_results, progress = _make_guard_result("sharpe", all_trial_params)
    report = generate_optimized_report(
        batch_dir="/tmp/batch",
        progress=progress,
        all_results=all_results,
        ohlcv_path="/tmp/ohlcv.parquet",
        objective_metric="sharpe",
    )

    # Summary table: every Opt column must be '-', none leaking trial params.
    opt_cells = _summary_opt_cells(report, label)
    assert opt_cells == ["-"] * 8, f"Opt columns leaked trial params: {opt_cells}"
    for v in ("0.77", "3.5", "1.5", "0.4", "0.6"):
        assert v not in " ".join(opt_cells)

    # Detail section: the per-parameter Optimized cells must also be '-'.
    detail_start = report.index("## Optimized Parameters Detail")
    detail = report[detail_start:]
    for _display, val in [
        ("Threshold", "0.77"),
        ("TP ATR Mult", "3.5"),
        ("SL ATR Mult", "1.5"),
    ]:
        assert val not in detail, f"Detail section leaked discarded trial param {val}"


def test_guarded_row_is_objective_invariant():
    """Two guard-triggered reports differing only in objective / all_trial_params
    must produce identical Opt columns (all blank)."""
    sharpe_params = {
        "entry_threshold_short": 0.77,
        "tp_atr_mult_short": 3.5,
        "sl_atr_mult_short": 1.5,
    }
    sortino_params = {
        "entry_threshold_short": 0.42,
        "tp_atr_mult_short": 2.1,
        "sl_atr_mult_short": 0.9,
    }
    label_a, results_a, progress_a = _make_guard_result("sharpe", sharpe_params)
    label_b, results_b, progress_b = _make_guard_result("sortino", sortino_params)

    report_a = generate_optimized_report(
        batch_dir="/tmp/batch",
        progress=progress_a,
        all_results=results_a,
        ohlcv_path="/tmp/ohlcv.parquet",
        objective_metric="sharpe",
    )
    report_b = generate_optimized_report(
        batch_dir="/tmp/batch",
        progress=progress_b,
        all_results=results_b,
        ohlcv_path="/tmp/ohlcv.parquet",
        objective_metric="sortino",
    )

    cells_a = _summary_opt_cells(report_a, label_a)
    cells_b = _summary_opt_cells(report_b, label_b)
    assert cells_a == cells_b == ["-"] * 8


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
