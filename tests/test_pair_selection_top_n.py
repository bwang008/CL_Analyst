"""Tests for the manifest-driven pair-selection width knob (pair_selection_top_n).

Post-optimizer pass-1 optimizes exec params per side; then
agent/unified_pair_optimizer.py selects the top-N long and top-N short models
by robustness score and emits the N x N combinatorial pairs to top_pairs.json.
Historically N was hardcoded at the vm_post_optimize.sh call site (argparse
default 2 -> 4 pairs). This suite pins the new manifest-driven width:

A. OptunaConfig.pair_selection_top_n — default 2 (byte-compatible with every
   existing manifest), explicit values accepted, loud ValueError outside [1, 8].
B. unified_pair_optimizer.select_pairs_for_objective — top_n=4 emits exactly
   4x4 = 16 pairs in robustness order; top_n=2 emits 4 (regression); fewer
   qualifying models per side degrade gracefully (slice of what exists).
C. Threading text-contracts (same style as tests/test_resume_batch.py):
   manifest optuna.pair_selection_top_n -> run_sweep_batch.ps1 -PairTopN ->
   gcp_deploy_optimizer.ps1 --pair-top-n= -> vm_post_optimize.sh
   --top-n "$PAIR_TOP_N"; resume_batch.ps1 forwards the same way. The legacy
   ensemble-sweep branch (select_top_ensembles.py --top-n 8) stays untouched.
D. Report non-capping — the grafted-baseline renderer and the canonical pair
   ordering render ALL pairs from top_pairs.json (16-pair fixture), never a
   hardcoded 4.
"""

import copy
import itertools
import json
import os
import re
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.unified_pair_optimizer import select_pairs_for_objective  # noqa: E402
from src.config.schemas import OptunaConfig  # noqa: E402

RUN_SWEEP_PS1 = os.path.join(PROJECT_ROOT, "gcp", "run_sweep_batch.ps1")
DEPLOY_PS1 = os.path.join(PROJECT_ROOT, "gcp", "gcp_deploy_optimizer.ps1")
VM_SH = os.path.join(PROJECT_ROOT, "gcp", "vm_post_optimize.sh")
RESUME_PS1 = os.path.join(PROJECT_ROOT, "scripts", "resume_batch.ps1")


def _text(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


# ===========================================================================
# A. Schema: pair_selection_top_n
# ===========================================================================


class TestPairSelectionTopNSchema:

    def test_default_is_2(self):
        cfg = OptunaConfig(post_optimizer_holdout_months=12)
        assert cfg.pair_selection_top_n == 2

    def test_explicit_4_accepted(self):
        cfg = OptunaConfig(post_optimizer_holdout_months=12,
                           pair_selection_top_n=4)
        assert cfg.pair_selection_top_n == 4

    @pytest.mark.parametrize("bad", [0, 9])
    def test_out_of_range_raises(self, bad):
        with pytest.raises(ValueError, match="pair_selection_top_n"):
            OptunaConfig(post_optimizer_holdout_months=12,
                         pair_selection_top_n=bad)

    def test_bounds_are_inclusive(self):
        assert OptunaConfig(post_optimizer_holdout_months=12,
                            pair_selection_top_n=1).pair_selection_top_n == 1
        assert OptunaConfig(post_optimizer_holdout_months=12,
                            pair_selection_top_n=8).pair_selection_top_n == 8


# ===========================================================================
# B. select_pairs_for_objective width behavior
# ===========================================================================

_HEADER = (
    "| Experiment | Trades (pre) | Trades (opt) | PF (pre) | PF (opt) "
    "| PnL (pre) | PnL (opt) | PnL (holdout) |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def _row(label, pnl_opt):
    return (f"| {label} | 100 | 150 | 1.10 | 1.30 | $1,000 "
            f"| ${pnl_opt:,} | $2,000 |\n")


def _summary_md(n_longs, n_shorts):
    """Fabricate a pass-1 optimized summary. All rows qualify (PnL opt > 0,
    PnL holdout > 0, trades >= 100). PnL (opt) descends with the index so the
    robustness order is L1 > L2 > ... and S1 > S2 > ..."""
    md = "# Batch Summary (Optimized)\n\n### Long Model (Logloss)\n\n" + _HEADER
    for i in range(1, n_longs + 1):
        md += _row(f"L{i}", 10000 - i * 1000)
    md += "\n### Short Model (Logloss)\n\n" + _HEADER
    for i in range(1, n_shorts + 1):
        md += _row(f"S{i}", 10000 - i * 1000)
    return md


def _progress(n_longs, n_shorts):
    exps = [{"label": f"L{i}", "gcs_prefix": f"l{i}"}
            for i in range(1, n_longs + 1)]
    exps += [{"label": f"S{i}", "gcs_prefix": f"s{i}"}
             for i in range(1, n_shorts + 1)]
    return {"experiments": exps}


def _expected_pairs(top_longs, top_shorts):
    # The fixture rows all qualify, so both passed_filter flags are True —
    # the flags are ADDITIVE keys on every emitted pair (report generators
    # mark penalized slots from them).
    return [
        {"target_long": f"oos_predictions_l{li}_long_logloss",
         "target_short": f"oos_predictions_s{si}_short_logloss",
         "long_passed_filter": True,
         "short_passed_filter": True}
        for li, si in itertools.product(top_longs, top_shorts)
    ]


def _run_selection(tmp_path, n_longs, n_shorts, top_n):
    (tmp_path / "batch_summary_optimized_sharpe.md").write_text(
        _summary_md(n_longs, n_shorts), encoding="utf-8")
    select_pairs_for_objective(
        str(tmp_path), "sharpe", _progress(n_longs, n_shorts), top_n)
    out = tmp_path / "top_pairs.json"
    assert out.is_file(), "top_pairs.json not written"
    return json.loads(out.read_text(encoding="utf-8"))


class TestSelectPairsWidth:

    def test_top_n_4_emits_16_pairs_in_robustness_order(self, tmp_path):
        pairs = _run_selection(tmp_path, n_longs=5, n_shorts=5, top_n=4)
        assert len(pairs) == 16
        assert pairs == _expected_pairs([1, 2, 3, 4], [1, 2, 3, 4])

    def test_top_n_2_regression_emits_4_pairs(self, tmp_path):
        pairs = _run_selection(tmp_path, n_longs=5, n_shorts=5, top_n=2)
        assert len(pairs) == 4
        assert pairs == _expected_pairs([1, 2], [1, 2])

    def test_fewer_qualifying_shorts_degrades_gracefully(self, tmp_path):
        # Only 3 shorts exist at all -> top_n=4 slices what exists: 4x3 = 12.
        pairs = _run_selection(tmp_path, n_longs=5, n_shorts=3, top_n=4)
        assert len(pairs) == 12
        assert pairs == _expected_pairs([1, 2, 3, 4], [1, 2, 3])

    def test_penalized_leg_carries_false_flag(self, tmp_path):
        # A short with negative holdout PnL fails the qualify filter; it
        # still FILLS the slot (penalize-not-drop) but must be flagged.
        md = ("# Batch Summary (Optimized)\n\n### Long Model (Logloss)\n\n"
              + _HEADER + _row("L1", 9000) + _row("L2", 8000)
              + "\n### Short Model (Logloss)\n\n" + _HEADER
              + _row("S1", 9000)
              + "| S2 | 100 | 150 | 1.10 | 1.30 | $1,000 | $5,000 "
              + "| $-2,000 |\n")
        (tmp_path / "batch_summary_optimized_sharpe.md").write_text(
            md, encoding="utf-8")
        select_pairs_for_objective(
            str(tmp_path), "sharpe", _progress(2, 2), 2)
        pairs = json.loads(
            (tmp_path / "top_pairs.json").read_text(encoding="utf-8"))
        assert len(pairs) == 4
        flags = {(p["target_short"], p["short_passed_filter"])
                 for p in pairs}
        assert ("oos_predictions_s1_short_logloss", True) in flags
        assert ("oos_predictions_s2_short_logloss", False) in flags
        assert all(p["long_passed_filter"] for p in pairs)


# ===========================================================================
# C. Threading text-contracts (manifest -> PS1 -> deploy -> sh)
# ===========================================================================


class TestRunSweepBatchThreading:

    def test_reads_manifest_pair_selection_top_n(self):
        text = _text(RUN_SWEEP_PS1)
        assert re.search(r"\$optuna\.pair_selection_top_n", text), (
            "run_sweep_batch.ps1 must read pair_selection_top_n from the "
            "manifest optuna block")
        assert re.search(
            r"if\s*\(\s*\$null\s+-eq\s+\$pairTopN\s*\)\s*\{\s*\$pairTopN\s*=\s*2",
            text), "absent pair_selection_top_n must default to 2 explicitly"

    def test_forwards_pairtopn_to_deploy(self):
        text = _text(RUN_SWEEP_PS1)
        assert re.search(r'"-PairTopN",\s*\$pairTopN', text), (
            "run_sweep_batch.ps1 must pass -PairTopN to "
            "gcp_deploy_optimizer.ps1")


class TestDeployOptimizerThreading:

    def test_declares_pairtopn_param_default_2(self):
        text = _text(DEPLOY_PS1)
        assert re.search(r"\[int\]\s*\$PairTopN\s*=\s*2", text), (
            "gcp_deploy_optimizer.ps1 must declare [int]$PairTopN = 2")

    def test_appends_pair_top_n_flag_to_launch_cmd(self):
        text = _text(DEPLOY_PS1)
        assert "--pair-top-n=$PairTopN" in text, (
            "gcp_deploy_optimizer.ps1 must append --pair-top-n=$PairTopN to "
            "the VM launch command")


class TestVmPostOptimizeThreading:

    def test_parses_pair_top_n_arg(self):
        text = _text(VM_SH)
        assert re.search(
            r'--pair-top-n=\*\)\s*PAIR_TOP_N="\$\{arg#\*=\}"', text), (
            "vm_post_optimize.sh must parse --pair-top-n=* into PAIR_TOP_N")

    def test_defaults_to_2_when_empty(self):
        text = _text(VM_SH)
        assert re.search(
            r'if\s*\[\s*-z\s*"\$PAIR_TOP_N"\s*\][\s\S]{0,40}?PAIR_TOP_N=2',
            text), "empty PAIR_TOP_N must resolve to the explicit default 2"

    def test_individual_mode_call_passes_top_n(self):
        text = _text(VM_SH)
        assert re.search(
            r'unified_pair_optimizer\.py\s+--batch-dir\s+"\$BATCH_DIR"\s+'
            r'--objectives\s+"\$ARM"\s+--top-n\s+"\$PAIR_TOP_N"', text), (
            "the individual-mode unified_pair_optimizer.py call must pass "
            '--top-n "$PAIR_TOP_N"')

    def test_legacy_ensemble_branch_untouched(self):
        text = _text(VM_SH)
        assert re.search(
            r"select_top_ensembles\.py[\s\S]{0,200}?--top-n 8", text), (
            "the legacy ensemble-sweep branch (select_top_ensembles.py "
            "--top-n 8) must stay untouched")


class TestResumeBatchThreading:

    def test_reads_manifest_pair_selection_top_n(self):
        text = _text(RESUME_PS1)
        assert re.search(
            r"\$manifest\.baseline\.training_workflow\.optuna\."
            r"pair_selection_top_n", text), (
            "resume_batch.ps1 must read pair_selection_top_n from the raw "
            "manifest")
        assert re.search(
            r"if\s*\(\s*\$null\s+-eq\s+\$pairTopN\s*\)\s*\{\s*\$pairTopN\s*=\s*2",
            text), "absent pair_selection_top_n must default to 2 explicitly"

    def test_forwards_pairtopn_to_deploy(self):
        text = _text(RESUME_PS1)
        assert re.search(r'"-PairTopN",\s*\$pairTopN', text), (
            "resume_batch.ps1 must pass -PairTopN to gcp_deploy_optimizer.ps1")


# ===========================================================================
# D. Report non-capping: 16 pairs render end-to-end (fixture level)
# ===========================================================================

# Reuse the proven harness pieces from the baseline-artifacts suite (tests is
# a package; the engine subprocess and graft seam are mocked the same way).
from tests.test_baseline_ensemble_artifacts import (  # noqa: E402
    BASE_CONFIG,
    CANNED_ENGINE_OUTPUT,
    _side_block,
)

import scripts.generate_baseline_ensemble_artifacts as bl  # noqa: E402
from agent.generate_ensemble_artifacts import _canonical_pair_order  # noqa: E402

BATCH_NAME_16 = "batch_20260714_000001_TOPN"
LONG_SWEEPS = [f"sweep_ng_topn_l{i}_scout_20260714-000001" for i in range(1, 5)]
SHORT_SWEEPS = [f"sweep_ng_topn_s{i}_scout_20260714-000001" for i in range(1, 5)]
PAIR_KEYS_16 = [
    f"oos_predictions_{ls}_long_logloss|oos_predictions_{ss}_short_logloss"
    for ls, ss in itertools.product(LONG_SWEEPS, SHORT_SWEEPS)
]
GRAFT_16 = {"long": _side_block(0.61, 4.0), "short": _side_block(0.57, 3.0)}


def _write_json_file(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _build_16_pair_batch(ws):
    _write_json_file(ws / "configs" / "strategies" / "fixture_base.json",
                     BASE_CONFIG)
    data_file = ws / "data" / "processed" / "NG_HourSet_FX.parquet"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_bytes(b"PARQUET-FIXTURE-BYTES")

    for sweep, side in ([(s, "long") for s in LONG_SWEEPS]
                        + [(s, "short") for s in SHORT_SWEEPS]):
        layout = ws / "reports" / sweep / "registry" / "production_output"
        layout.mkdir(parents=True, exist_ok=True)
        (layout / f"oos_predictions_sweep_{side}_logloss.csv").write_text(
            f"DateTime,prob\n2026-01-01,0.5\n# {sweep}\n", encoding="utf-8")

    batch = ws / "reports" / "batch_runs" / BATCH_NAME_16
    batch.mkdir(parents=True)
    _write_json_file(batch / "top_pairs.json", [
        {"target_long": k.split("|")[0], "target_short": k.split("|")[1]}
        for k in PAIR_KEYS_16
    ])
    _write_json_file(batch / "manifest.json", {
        "baseline": {
            "symbol": "NG",
            "data_workflow": {"dataset_version": "HourSet_FX"},
            "training_workflow": {
                "optuna": {"post_optimizer_holdout_months": 12,
                           "pair_selection_top_n": 4},
            },
            "execution_workflow": {
                "slippage_per_side": 0.001,
                "strategy_config_path": "configs/strategies/fixture_base.json",
                "execution_data_path": "gs://fixture/NG_raw_DOES_NOT_EXIST.parquet",
            },
        },
        "experiments": [{"label": "NG TOPN L1"}],
    })
    return batch


class TestSixteenPairRendering:

    @pytest.fixture()
    def batch16(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.chdir(ws)
        batch = _build_16_pair_batch(ws)
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, *a, **kw: subprocess.CompletedProcess(
                cmd, 0, stdout=CANNED_ENGINE_OUTPUT, stderr=""))
        monkeypatch.setattr(
            bl, "_guard_shipped_pair_config",
            lambda *a, **kw: copy.deepcopy(GRAFT_16))
        monkeypatch.setattr(bl, "resolve_instrument_context", lambda cfg: None)
        return batch

    def test_baseline_reports_render_all_16_pairs(self, batch16):
        bl.main(["--batch-dir", str(batch16)])

        bt = (batch16 / "sharpe_baseline_backtests.md").read_text(
            encoding="utf-8")
        headings = re.findall(r"^## Ensemble (\d+):", bt, re.MULTILINE)
        assert [int(h) for h in headings] == list(range(1, 17))

        sm = (batch16 / "batch_summary_baseline_sharpe.md").read_text(
            encoding="utf-8")
        assert "(Top 16)" in sm, (
            "summary header must carry the dynamic pair count, not a "
            "hardcoded Top 4")
        # 1 result row + 2 per-side param rows per pair; every slot present.
        labels = re.findall(r"^\| (E\d{2}) \|", sm, re.MULTILINE)
        assert len(labels) == 48
        assert set(labels) == {f"E{i:02d}" for i in range(1, 17)}

        cfgs = sorted(
            p.name for p in (batch16 / "configs" / "baseline").glob("*.json"))
        assert len(cfgs) == 16
        assert cfgs[0].startswith("TOPN_Sharpe_E01_baseline_")
        assert cfgs[-1].startswith("TOPN_Sharpe_E16_baseline_")

    def test_canonical_pair_order_returns_all_16(self, batch16):
        opt_data = {k: {"n": i} for i, k in enumerate(PAIR_KEYS_16)}
        ordered = _canonical_pair_order(opt_data, str(batch16), "sharpe")
        assert [k for k, _ in ordered] == PAIR_KEYS_16
