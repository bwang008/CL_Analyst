"""Tests for the --render-optimized mode of generate_baseline_ensemble_artifacts.

Renders a READABLE companion of the optimized summaries from the structured
optimization_results JSONs, without ever touching the machine-contract
markdowns (batch_summary_optimized_<obj>[.|_ensembles_]md — parsed
positionally by agent/unified_pair_optimizer, scripts/compare_objective_arms.py
and the report tests). Self-contained tmp fixtures; the real batch dirs are
never touched.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.generate_baseline_ensemble_artifacts import (  # noqa: E402
    assert_baseline_output_path,
    main,
)

OBJECTIVE = "sharpe"
BATCH_NAME = "batch_20260712_000002_FIXT"
READABLE_NAME = f"batch_summary_optimized_{OBJECTIVE}_readable.md"
OPT_FILE = f"optimization_results_ensembles_{OBJECTIVE}.json"

SWEEP_AA = "sweep_ng_fixt_aa_scout_20260712-000002"
SWEEP_BB = "sweep_ng_fixt_bb_scout_20260712-000002"
SWEEP_CC = "sweep_ng_fixt_cc_scout_20260712-000002"
SWEEP_DD = "sweep_ng_fixt_dd_scout_20260712-000002"

PAIR_A_LONG = f"oos_predictions_{SWEEP_AA}_long_average_precision"
PAIR_A_SHORT = f"oos_predictions_{SWEEP_BB}_short_logloss"
PAIR_A_KEY = f"{PAIR_A_LONG}|{PAIR_A_SHORT}"
PAIR_B_LONG = f"oos_predictions_{SWEEP_CC}_long_logloss"
PAIR_B_SHORT = f"oos_predictions_{SWEEP_DD}_short_average_precision"
PAIR_B_KEY = f"{PAIR_B_LONG}|{PAIR_B_SHORT}"

BASELINE_A_LONG_THR = 0.61

SHIPPED_LONG_PARAMS = {
    "entry_threshold": 0.515, "tp_atr_mult": 6.5, "sl_atr_mult": 2.5,
    "trailing_atr_mult": 3.9, "trailing_sl_atr_offset": 1.56,
    "cooldown_bars": 9, "max_hold_bars": 24,
    "consecutive_signal_threshold": 4, "atr_period": 36,
}
SHIPPED_SHORT_PARAMS = {
    "entry_threshold": 0.4467, "tp_atr_mult": 3.5, "sl_atr_mult": 3.0,
    "trailing_atr_mult": 0.7, "trailing_sl_atr_offset": 0.42,
    "cooldown_bars": 5, "max_hold_bars": 36,
    "consecutive_signal_threshold": 4, "atr_period": 36,
}
BASELINE_SIDE_PARAMS = {
    "long": {**SHIPPED_LONG_PARAMS, "entry_threshold": BASELINE_A_LONG_THR},
    "short": {**SHIPPED_SHORT_PARAMS, "entry_threshold": 0.57},
}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    return ws


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _pair_entry(long_label, short_label, guard=False):
    return {
        "status": "OK",
        "metrics": {"total_pnl": 101553.0, "trade_count": 1135,
                    "profit_factor": 1.26},
        "experiment_labels": {"long_label": long_label,
                              "short_label": short_label},
        "optuna_info": {
            "trial_number": 137, "n_trials": 150,
            "regression_guard_triggered": guard,
            "baseline_side_params": copy.deepcopy(BASELINE_SIDE_PARAMS),
            "long_params": copy.deepcopy(SHIPPED_LONG_PARAMS),
            "short_params": copy.deepcopy(SHIPPED_SHORT_PARAMS),
            "params": {},
            "baseline_metrics": {"total_pnl": 56274.0, "trade_count": 831,
                                 "profit_factor": 1.16},
            "holdout_metrics": {"total_pnl": -23766.0, "trade_count": 325,
                                "profit_factor": 0.78},
            "block_sharpes": [2.8, 1.8, 0.1],
        },
    }


def _build_batch_fixture(ws: Path, guard_pair_a=False) -> Path:
    batch = ws / BATCH_NAME
    batch.mkdir(parents=True)
    _write_json(batch / "manifest.json", {"baseline": {"symbol": "NG"}})
    _write_json(batch / "top_pairs.json", [
        {"target_long": PAIR_A_LONG, "target_short": PAIR_A_SHORT},
        {"target_long": PAIR_B_LONG, "target_short": PAIR_B_SHORT},
    ])
    # Insertion order deliberately SHUFFLED (B before A) — ordering must come
    # from top_pairs.json via the real _canonical_pair_order.
    _write_json(batch / OPT_FILE, {
        PAIR_B_KEY: _pair_entry("NG FIXT CC", "NG FIXT DD"),
        PAIR_A_KEY: _pair_entry("NG FIXT AA", "NG FIXT BB",
                                guard=guard_pair_a),
    })

    params = {
        "entry_threshold": 0.6, "tp_atr_mult": 8.0, "sl_atr_mult": 3.0,
        "trailing_atr_mult": 1.6, "trailing_sl_atr_offset": 0.96,
        "cooldown_bars": 9, "max_hold_bars": 30,
        "consecutive_signal_threshold": 4, "atr_period": 12,
    }

    def entry(guard):
        return {
            "status": "OK",
            "metrics": {"total_pnl": 109655.0, "trade_count": 549,
                        "profit_factor": 1.59},
            "optuna_info": {
                "params": copy.deepcopy(params),
                "baseline_metrics": {"total_pnl": -17150.0,
                                     "trade_count": 720,
                                     "profit_factor": 0.95},
                "holdout_metrics": {"total_pnl": -2228.0, "trade_count": 131,
                                    "profit_factor": 0.9},
                "block_sharpes": [2.1, 0.3, 0.8],
                "trial_number": 114, "n_trials": 150,
                "regression_guard_triggered": guard,
            },
        }

    _write_json(batch / f"optimization_results_{OBJECTIVE}.json", {
        "NG FIXT AA|long|logloss": entry(False),
        "NG FIXT AA|short|logloss": entry(True),
    })
    return batch


def _snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }


def _table_cells(text, first_cell_value, second_cell_value):
    """Return the stripped cells of the first table row whose first two cells
    match — layout-independent (padding-tolerant)."""
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) > 2 and cells[0] == first_cell_value \
                and cells[1] == second_cell_value:
            return cells
    raise AssertionError(
        f"no table row with cells [{first_cell_value!r}, {second_cell_value!r}]")


def _rendered(workspace, **kw):
    batch = _build_batch_fixture(workspace, **kw)
    main(["--batch-dir", str(batch), "--render-optimized"])
    return batch, (batch / READABLE_NAME).read_text(encoding="utf-8")


class TestRenderOptimized:
    def test_additive_and_only_the_readable_file_is_new(self, workspace):
        batch = _build_batch_fixture(workspace)
        before = _snapshot(workspace)
        main(["--batch-dir", str(batch), "--render-optimized"])
        after = _snapshot(workspace)

        assert set(after) - set(before) == {f"{BATCH_NAME}/{READABLE_NAME}"}
        for rel, blob in before.items():
            assert after[rel] == blob, f"pre-existing file modified: {rel}"

    def test_results_params_order_and_provenance(self, workspace):
        _, text = _rendered(workspace)
        # E-slot order from top_pairs.json (insertion order is shuffled B, A)
        e01 = _table_cells(text, "E01", "NG FIXT AA / NG FIXT BB")
        assert e01[3] == "$56,274" and e01[4] == "$101,553"
        assert e01[5] == "$-23,766"
        assert e01[7] == "2.8/1.8/0.1" and e01[8] == "#137/150"
        # per-side shipped params, tagged optimized
        long_row = _table_cells(text, "E01", "Long")
        assert long_row[3] == "0.515" and long_row[-1] == "optimized"
        # individual section rows + params
        ind = _table_cells(text, "NG FIXT AA", "Long")
        assert ind[2] == "LL" and ind[3] == "$-17,150" \
            and ind[4] == "$109,655" and ind[5] == "$-2,228"
        # machine-contract statement
        assert "parsing authority" in text

    def test_guard_pair_renders_shipped_baseline_params_tagged(self, workspace):
        _, text = _rendered(workspace, guard_pair_a=True)
        e01 = _table_cells(text, "E01", "NG FIXT AA / NG FIXT BB")
        assert e01[8] == "baseline (guard)"
        long_row = _table_cells(text, "E01", "Long")
        # baseline long thr, not the discarded pass-2 0.515
        assert long_row[3] == str(BASELINE_A_LONG_THR)
        assert long_row[-1] == "baseline (guard)"

    def test_guarded_individual_row_renders_dash_params(self, workspace):
        _, text = _rendered(workspace)
        rows = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("| NG FIXT AA"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                rows.append(cells)
        guard_rows = [r for r in rows if r[-1] == "baseline (guard)"]
        assert guard_rows, "guarded individual params row missing"
        assert all(c == "-" for c in guard_rows[0][3:12]), (
            "guarded row must not render the discarded trial's params")

    def test_missing_individual_json_raises(self, workspace):
        batch = _build_batch_fixture(workspace)
        (batch / f"optimization_results_{OBJECTIVE}.json").unlink()
        with pytest.raises(ValueError, match="optimization_results"):
            main(["--batch-dir", str(batch), "--render-optimized"])

    def test_missing_ensembles_json_renders_note(self, workspace):
        batch = _build_batch_fixture(workspace)
        (batch / OPT_FILE).unlink()
        main(["--batch-dir", str(batch), "--render-optimized"])
        text = (batch / READABLE_NAME).read_text(encoding="utf-8")
        assert "ensembles section skipped" in text
        assert "NG FIXT AA" in text  # individual section still rendered

    def test_namespace_guard_allows_readable_rejects_machine_names(self):
        assert_baseline_output_path(f"some/batch/{READABLE_NAME}")
        with pytest.raises(ValueError):
            assert_baseline_output_path(
                "some/batch/batch_summary_optimized_sharpe.md")
        with pytest.raises(ValueError):
            assert_baseline_output_path(
                "some/batch/batch_summary_optimized_ensembles_sharpe.md")
