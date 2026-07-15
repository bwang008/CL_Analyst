"""Tests for scripts/generate_baseline_ensemble_artifacts.py.

Lineage: ticket pre-ensemble-artifacts_07122026_1712 built this module as the
"pre" counterfactual renderer sourcing optuna_info.baseline_side_params from
the pass-2 ensembles JSON. It was promoted to the DEFAULT pipeline product
(operator decision, 2026-07-12) when pass-2 became opt-in
(-EnsembleOptimization): renamed pre -> baseline, and re-sourced from the
pass-1 graft (agent.generate_ensemble_artifacts._guard_shipped_pair_config ->
batch_post_optimizer._build_pair_base_config) so it works when pass-2 never
ran. This suite replaces the original pre-contract suite accordingly.

All fixtures are self-contained tmp dirs (chdir'd), the engine subprocess is
mocked, and the graft reconstruction is monkeypatched at the module seam (the
real graft path is covered by tests/test_pass2_pair_graft.py and
tests/test_config_generator_symbols.py).
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.generate_baseline_ensemble_artifacts as bl  # noqa: E402
from scripts.generate_baseline_ensemble_artifacts import (  # noqa: E402
    assert_baseline_output_path,
    assert_order_matches_existing,
    build_baseline_config,
    load_top_pairs,
    main,
    parse_engine_output,
    side_params_for_summary,
)

OBJECTIVE = "sharpe"
BATCH_NAME = "batch_20260712_000001_FIXT"
SWEEP_AA = "sweep_ng_fixt_aa_scout_20260712-000001"
SWEEP_BB = "sweep_ng_fixt_bb_scout_20260712-000001"
SWEEP_CC = "sweep_ng_fixt_cc_scout_20260712-000001"
SWEEP_DD = "sweep_ng_fixt_dd_scout_20260712-000001"

PAIR_A_LONG = f"oos_predictions_{SWEEP_AA}_long_average_precision"
PAIR_A_SHORT = f"oos_predictions_{SWEEP_BB}_short_logloss"
PAIR_A_KEY = f"{PAIR_A_LONG}|{PAIR_A_SHORT}"
PAIR_B_LONG = f"oos_predictions_{SWEEP_CC}_long_logloss"
PAIR_B_SHORT = f"oos_predictions_{SWEEP_DD}_short_average_precision"
PAIR_B_KEY = f"{PAIR_B_LONG}|{PAIR_B_SHORT}"

DATE_TOKEN = "07122026"  # from batch name 20260712
TAG = "FIXT"             # batch name parts[3]


def _side_block(thr, tp):
    return {
        "tp_atr_mult": tp, "sl_atr_mult": 1.5, "trailing_atr_mult": 2.0,
        "trailing_sl_atr_offset": 0.8, "cooldown_bars": 3,
        "max_hold_bars": 12, "consecutive_signal_threshold": 2,
        "atr_period": 14,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": tp}],
        "tiers": [{"min_prob": thr, "lots": 1, "tp_atr_mult": tp,
                   "sl_atr_mult": 1.5, "trailing_atr_mult": 2.0,
                   "max_hold_bars": 12}],
    }


GRAFT_A = {"long": _side_block(0.61, 4.0), "short": _side_block(0.57, 3.0)}
GRAFT_B = {"long": _side_block(0.52, 5.0), "short": _side_block(0.48, 6.0)}
GRAFTS = {PAIR_A_KEY: GRAFT_A, PAIR_B_KEY: GRAFT_B}

BASE_CONFIG = {
    "nickname": "fixture_base",
    "execution_class": "TieredEnsembleStrategy",
    "exit_mode": "TIERED",
    "execution_symbol": "CL",
    "bar_size": "1h",
    "conflict_resolution": "flatten",
    "models": {"long": {"threshold": 0.5}, "short": {"threshold": 0.5}},
    "long": _side_block(0.5, 2.0),
    "short": _side_block(0.5, 2.0),
}

CANNED_ENGINE_OUTPUT = """Loaded strategy config 'FIXT_Sharpe_E01_baseline_07122026'
Running backtest on historical data...
==========================================================================================
                               BACKTEST REPORT: Historical
==========================================================================================
  AGGREGATE SUMMARY
------------------------------------------------------------------------------------------
  Total Trades:     1071
  Total Net PnL:    $     85,342.29
  Max Drawdown:     $    -31,000.55

  MONTHLY BREAKDOWN
------------------------------------------------------------------------------------------
  2022-06    |     16 |     3 |    13 | 75.0%  | 33.3%  | 84.6%   | $ 22,906.67 |  2.88
------------------------------------------------------------------------------------------
  2022 Total |    218 |    40 |   178 | 60.6%  | 50.0%  | 62.9%   | $ 38,278.24 |  1.22
==========================================================================================

==========================================================================================
HOLDOUT REPORT  (12 months: 2025-07-02 -> 2026-07-02, 5,902 bars)
==========================================================================================
  AGGREGATE SUMMARY
------------------------------------------------------------------------------------------
  Total Trades:     243
  Total Net PnL:    $     25,528.41
  Max Drawdown:     $     -8,750.25

  MONTHLY BREAKDOWN
------------------------------------------------------------------------------------------
  2025-12    |     19 |     3 |    16 | 52.6%  | 66.7%  | 50.0%   | $  7,883.49 |  1.85
------------------------------------------------------------------------------------------
  2025 Total |    124 |    29 |    95 | 55.6%  | 48.3%  | 57.9%   | $    684.87 |  1.04
==========================================================================================
  2026-01    |     17 |     3 |    14 | 47.1%  | 66.7%  | 42.9%   | $ 10,700.63 |  1.81
==========================================================================================
  EXIT DISTRIBUTION BY SIDE
------------------------------------------------------------------------------------------

======================================================================
                            A/B COMPARISON
======================================================================
  Metric                     Optimizer Window   Holdout (Unseen)
----------------------------------------------------------------------
  Trade Count                             828                243
  Total Net PnL                    $59,813.88         $25,528.41
  Max Drawdown                    $-12,345.67         $-8,750.25
======================================================================
"""

GARBAGE_ENGINE_OUTPUT = "FIXTURE-GARBAGE-7Q nothing parseable here\n"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    return ws


def _snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _build_batch_fixture(ws: Path) -> Path:
    (ws / "configs" / "strategies").mkdir(parents=True, exist_ok=True)
    _write_json(ws / "configs" / "strategies" / "fixture_base.json", BASE_CONFIG)

    data_file = ws / "data" / "processed" / "NG_HourSet_FX.parquet"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_bytes(b"PARQUET-FIXTURE-BYTES")

    for sweep, side, metric in (
            (SWEEP_AA, "long", "average_precision"),
            (SWEEP_BB, "short", "logloss"),
            (SWEEP_CC, "long", "logloss"),
            (SWEEP_DD, "short", "average_precision")):
        layout = ws / "reports" / sweep / "registry" / "production_output"
        layout.mkdir(parents=True, exist_ok=True)
        (layout / f"oos_predictions_sweep_{side}_{metric}.csv").write_text(
            f"DateTime,prob\n2026-01-01,0.5\n# {sweep}\n", encoding="utf-8")

    batch = ws / "reports" / "batch_runs" / BATCH_NAME
    batch.mkdir(parents=True)
    _write_json(batch / "top_pairs.json", [
        {"target_long": PAIR_A_LONG, "target_short": PAIR_A_SHORT},
        {"target_long": PAIR_B_LONG, "target_short": PAIR_B_SHORT},
    ])
    _write_json(batch / "manifest.json", {
        "baseline": {
            "symbol": "NG",
            "data_workflow": {"dataset_version": "HourSet_FX"},
            "training_workflow": {
                "optuna": {"post_optimizer_holdout_months": 12},
            },
            "execution_workflow": {
                "slippage_per_side": 0.001,
                "strategy_config_path": "configs/strategies/fixture_base.json",
                "execution_data_path": "gs://fixture/NG_raw_DOES_NOT_EXIST.parquet",
            },
        },
        "experiments": [{"label": "NG FIXT AA"}],
    })
    return batch


def _install_fakes(monkeypatch, stdout_text):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout_text, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        bl, "_guard_shipped_pair_config",
        lambda batch_dir, objective, pair_key, base_cfg_path, out_path:
            copy.deepcopy(GRAFTS[pair_key]))
    monkeypatch.setattr(bl, "resolve_instrument_context", lambda cfg: None)
    return calls


BATCH_REL = f"reports/batch_runs/{BATCH_NAME}"
EXPECTED_NEW_REL = {
    f"{BATCH_REL}/configs/baseline/{TAG}_Sharpe_E01_baseline_{DATE_TOKEN}.json",
    f"{BATCH_REL}/configs/baseline/{TAG}_Sharpe_E02_baseline_{DATE_TOKEN}.json",
    f"{BATCH_REL}/predictions/baseline/{TAG}_Sharpe_E01_baseline_long_predictions.csv",
    f"{BATCH_REL}/predictions/baseline/{TAG}_Sharpe_E01_baseline_short_predictions.csv",
    f"{BATCH_REL}/predictions/baseline/{TAG}_Sharpe_E02_baseline_long_predictions.csv",
    f"{BATCH_REL}/predictions/baseline/{TAG}_Sharpe_E02_baseline_short_predictions.csv",
    f"{BATCH_REL}/batch_summary_baseline_{OBJECTIVE}.md",
    f"{BATCH_REL}/{OBJECTIVE}_baseline_backtests.md",
}


# ===========================================================================
# Namespace write-guard
# ===========================================================================


class TestNamespaceGuard:
    def test_accepts_sanctioned_outputs(self):
        assert_baseline_output_path("b/batch_summary_baseline_sharpe.md")
        assert_baseline_output_path("b/sharpe_baseline_backtests.md")
        assert_baseline_output_path(
            "b/configs/baseline/NG_Sharpe_E01_baseline_07122026.json")
        assert_baseline_output_path(
            "b/predictions/baseline/NG_Sharpe_E01_baseline_long_predictions.csv")
        assert_baseline_output_path(
            "b/batch_summary_optimized_sharpe_readable.md")

    @pytest.mark.parametrize("path", [
        "b/batch_summary_optimized_sharpe.md",
        "b/batch_summary_optimized_ensembles_sharpe.md",
        "b/sharpe_ensemble_backtests.md",
        "b/configs/NG_Sharpe_E01_baseline_07122026.json",       # not under baseline/
        "b/configs/optimized/NG_Sharpe_E01_07122026.json",      # no marker
        "b/predictions/NG_Sharpe_E01_baseline_long_predictions.csv",
        "b/top_pairs.json",
    ])
    def test_rejects_everything_else(self, path):
        with pytest.raises(ValueError):
            assert_baseline_output_path(path)


# ===========================================================================
# build_baseline_config (pure)
# ===========================================================================


def _skeleton():
    sk = copy.deepcopy(BASE_CONFIG)
    sk["execution_symbol"] = "NG"
    sk["holdout_months"] = 12
    for side in ("long", "short"):
        sk["models"][side].update({
            "experiment_id": f"E2E_FX_{side}_x", "model_path": "m.pkl",
            "predictions_path": "p.csv", "symbol": "NG"})
    return sk


class TestBuildBaselineConfig:
    def test_grafted_blocks_shipped_verbatim_and_threshold_mirrored(self):
        out = build_baseline_config(_skeleton(), GRAFT_A, "NICK")
        for side in ("long", "short"):
            assert out[side] == GRAFT_A[side]
            assert out["models"][side]["threshold"] == pytest.approx(
                GRAFT_A[side]["tiers"][0]["min_prob"])
        assert out["conflict_resolution"] == "hold"
        assert out["nickname"] == "NICK"
        assert out["holdout_months"] == 12
        assert out["execution_symbol"] == "NG"

    def test_inputs_not_mutated(self):
        sk = _skeleton()
        sk_before = copy.deepcopy(sk)
        graft_before = copy.deepcopy(GRAFT_A)
        build_baseline_config(sk, GRAFT_A, "NICK")
        assert sk == sk_before
        assert GRAFT_A == graft_before

    def test_missing_side_block_raises(self):
        with pytest.raises(ValueError, match="side block"):
            build_baseline_config(_skeleton(), {"long": GRAFT_A["long"]}, "N")

    def test_missing_tier_min_prob_raises(self):
        graft = copy.deepcopy(GRAFT_A)
        del graft["short"]["tiers"]
        with pytest.raises(ValueError, match="min_prob"):
            build_baseline_config(_skeleton(), graft, "N")

    def test_missing_models_side_raises(self):
        sk = _skeleton()
        del sk["models"]["short"]
        with pytest.raises(ValueError, match="models.short"):
            build_baseline_config(sk, GRAFT_A, "N")

    def test_side_params_for_summary(self):
        sp = side_params_for_summary(GRAFT_A["long"])
        assert sp["entry_threshold"] == pytest.approx(0.61)
        assert sp["tp_atr_mult"] == pytest.approx(4.0)
        incomplete = {"tiers": [{"min_prob": 0.6}]}
        sp2 = side_params_for_summary(incomplete)
        assert sp2["atr_period"] == "-"


# ===========================================================================
# Pair selection + heading parity
# ===========================================================================


class TestPairSelection:
    def test_load_top_pairs_order_and_source(self, workspace):
        batch = _build_batch_fixture(workspace)
        keys, name = load_top_pairs(str(batch), OBJECTIVE)
        assert keys == [PAIR_A_KEY, PAIR_B_KEY]
        assert name == "top_pairs.json"

    def test_arm_variant_preferred(self, workspace):
        batch = _build_batch_fixture(workspace)
        _write_json(batch / "top_pairs_block_min.json", [
            {"target_long": PAIR_B_LONG, "target_short": PAIR_B_SHORT},
        ])
        keys, name = load_top_pairs(str(batch), "block_min")
        assert keys == [PAIR_B_KEY]
        assert name == "top_pairs_block_min.json"

    def test_missing_pairs_file_raises(self, workspace):
        batch = _build_batch_fixture(workspace)
        (batch / "top_pairs.json").unlink()
        with pytest.raises(ValueError, match="pair selection must run"):
            load_top_pairs(str(batch), OBJECTIVE)

    def test_malformed_pairs_raise(self, workspace):
        batch = _build_batch_fixture(workspace)
        _write_json(batch / "top_pairs.json", [{"nope": 1}])
        with pytest.raises(ValueError, match="malformed"):
            load_top_pairs(str(batch), OBJECTIVE)

    def test_heading_parity_pass_and_fail(self, workspace):
        batch = _build_batch_fixture(workspace)
        good = (f"## Ensemble 1: {SWEEP_AA} / {SWEEP_BB}\n"
                f"## Ensemble 2: {SWEEP_CC} / {SWEEP_DD}\n")
        (batch / f"{OBJECTIVE}_ensemble_backtests.md").write_text(
            good, encoding="utf-8")
        assert_order_matches_existing(
            [PAIR_A_KEY, PAIR_B_KEY], str(batch), OBJECTIVE)
        swapped = (f"## Ensemble 1: {SWEEP_CC} / {SWEEP_DD}\n"
                   f"## Ensemble 2: {SWEEP_AA} / {SWEEP_BB}\n")
        (batch / f"{OBJECTIVE}_ensemble_backtests.md").write_text(
            swapped, encoding="utf-8")
        with pytest.raises(ValueError, match="order mismatch"):
            assert_order_matches_existing(
                [PAIR_A_KEY, PAIR_B_KEY], str(batch), OBJECTIVE)


# ===========================================================================
# End-to-end main() — additive, correct configs, reports
# ===========================================================================


class TestMainBaseline:
    def test_additive_and_expected_file_set(self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        before = _snapshot(workspace)
        main(["--batch-dir", str(batch)])
        after = _snapshot(workspace)

        assert set(after) - set(before) == EXPECTED_NEW_REL
        for rel, blob in before.items():
            assert after[rel] == blob, f"pre-existing file modified: {rel}"

    def test_configs_carry_graft_and_baseline_predictions(
            self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        main(["--batch-dir", str(batch)])

        e01 = json.loads((batch / "configs" / "baseline" /
                          f"{TAG}_Sharpe_E01_baseline_{DATE_TOKEN}.json")
                         .read_text(encoding="utf-8"))
        assert e01["long"] == GRAFT_A["long"]
        assert e01["short"] == GRAFT_A["short"]
        assert e01["models"]["long"]["threshold"] == pytest.approx(0.61)
        assert e01["models"]["short"]["threshold"] == pytest.approx(0.57)
        assert e01["conflict_resolution"] == "hold"
        assert e01["execution_symbol"] == "NG"
        assert e01["holdout_months"] == 12
        for side in ("long", "short"):
            assert "/predictions/baseline/" in e01["models"][side]["predictions_path"]
            assert e01["models"][side]["symbol"] == "NG"
        # per-side copies carry the ORIGIN sweep's content
        long_copy = (batch / "predictions" / "baseline" /
                     f"{TAG}_Sharpe_E01_baseline_long_predictions.csv")
        assert SWEEP_AA in long_copy.read_text(encoding="utf-8")

    def test_e_order_follows_pairs_file_and_engine_called_per_pair(
            self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        calls = _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        main(["--batch-dir", str(batch)])

        bt = (batch / f"{OBJECTIVE}_baseline_backtests.md").read_text(
            encoding="utf-8")
        assert f"## Ensemble 1: {SWEEP_AA} / {SWEEP_BB}" in bt
        assert f"## Ensemble 2: {SWEEP_CC} / {SWEEP_DD}" in bt
        assert bt.index("## Ensemble 1:") < bt.index("## Ensemble 2:")
        assert len(calls) == 2
        assert calls[0][0] == sys.executable
        assert "--config" in calls[0]
        cfg_arg = calls[0][calls[0].index("--config") + 1]
        assert "_baseline_" in cfg_arg and "configs" in cfg_arg

    def test_summary_contents_and_provenance(self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        main(["--batch-dir", str(batch)])

        sm = (batch / f"batch_summary_baseline_{OBJECTIVE}.md").read_text(
            encoding="utf-8")
        assert "_build_pair_base_config" in sm          # provenance
        assert "top_pairs.json" in sm                   # order contract
        assert "pass-2 report absent" in sm             # no optimized report
        assert "$25,528.41" in sm                       # holdout PnL
        assert "10,700.63" in sm                        # holdout monthly row
        assert "22,906.67" not in sm                    # full-window-only row
        assert "0.61" in sm                             # E01 long threshold
        assert f"{OBJECTIVE}_baseline_backtests.md" in sm
        # Drawdown columns: raw holdout MaxDD + recovery factor
        # (holdout PnL / |holdout MaxDD| = 25,528.41 / 8,750.25 = 2.92)
        assert "MaxDD (ho)" in sm
        assert "PnL/DD (ho)" in sm
        assert "$-8,750.25" in sm
        assert "2.92" in sm

    def test_summary_drawdown_unparsed_never_zero(self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        _install_fakes(monkeypatch, GARBAGE_ENGINE_OUTPUT)
        main(["--batch-dir", str(batch)])
        sm = (batch / f"batch_summary_baseline_{OBJECTIVE}.md").read_text(
            encoding="utf-8")
        # ratio must carry the UNPARSED marker, never a fabricated number
        assert "MaxDD (ho)" in sm
        assert "PnL/DD (ho)" in sm
        assert "UNPARSED" in sm

    def test_order_contract_verified_when_optimized_report_exists(
            self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        good = (f"## Ensemble 1: {SWEEP_AA} / {SWEEP_BB}\n"
                f"## Ensemble 2: {SWEEP_CC} / {SWEEP_DD}\n")
        (batch / f"{OBJECTIVE}_ensemble_backtests.md").write_text(
            good, encoding="utf-8")
        _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        main(["--batch-dir", str(batch)])
        sm = (batch / f"batch_summary_baseline_{OBJECTIVE}.md").read_text(
            encoding="utf-8")
        assert "verified 1:1" in sm

    def test_mismatched_optimized_report_fails_loudly(
            self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        swapped = (f"## Ensemble 1: {SWEEP_CC} / {SWEEP_DD}\n"
                   f"## Ensemble 2: {SWEEP_AA} / {SWEEP_BB}\n")
        (batch / f"{OBJECTIVE}_ensemble_backtests.md").write_text(
            swapped, encoding="utf-8")
        _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        with pytest.raises(ValueError, match="order mismatch"):
            main(["--batch-dir", str(batch)])

    def test_unparseable_stdout_not_fatal_and_loud(self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        _install_fakes(monkeypatch, GARBAGE_ENGINE_OUTPUT)
        main(["--batch-dir", str(batch)])

        sm = (batch / f"batch_summary_baseline_{OBJECTIVE}.md").read_text(
            encoding="utf-8")
        bt = (batch / f"{OBJECTIVE}_baseline_backtests.md").read_text(
            encoding="utf-8")
        assert "UNPARSED" in sm
        assert "$0.00" not in sm
        assert "FIXTURE-GARBAGE-7Q" in bt
        assert (batch / "configs" / "baseline" /
                f"{TAG}_Sharpe_E01_baseline_{DATE_TOKEN}.json").is_file()

    def test_graft_unavailable_is_fatal(self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        monkeypatch.setattr(
            bl, "_guard_shipped_pair_config",
            lambda *a, **k: None)
        with pytest.raises(ValueError, match="refusing any fallback"):
            main(["--batch-dir", str(batch)])

    def test_missing_manifest_fields_fail_loudly(self, workspace, monkeypatch):
        batch = _build_batch_fixture(workspace)
        _install_fakes(monkeypatch, CANNED_ENGINE_OUTPUT)
        m = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        del m["baseline"]["execution_workflow"]["slippage_per_side"]
        _write_json(batch / "manifest.json", m)
        with pytest.raises(ValueError, match="slippage"):
            main(["--batch-dir", str(batch)])

    def test_main_requires_batch_dir(self):
        with pytest.raises(SystemExit):
            main([])


# ===========================================================================
# parse_engine_output
# ===========================================================================


class TestParseEngineOutput:
    def test_canned_output_parses(self):
        p = parse_engine_output(CANNED_ENGINE_OUTPUT)
        assert p["full_pnl"] == pytest.approx(85342.29)
        assert p["full_trades"] == 1071
        assert p["opt_pnl"] == pytest.approx(59813.88)
        assert p["opt_trades"] == 828
        assert p["holdout_pnl"] == pytest.approx(25528.41)
        assert p["holdout_trades"] == 243
        assert p["full_maxdd"] == pytest.approx(-31000.55)
        assert p["opt_maxdd"] == pytest.approx(-12345.67)
        assert p["holdout_maxdd"] == pytest.approx(-8750.25)
        assert "10,700.63" in p["holdout_monthly_breakdown"]
        assert "22,906.67" not in p["holdout_monthly_breakdown"]

    def test_garbage_is_unparsed_per_field(self):
        p = parse_engine_output(GARBAGE_ENGINE_OUTPUT)
        assert all(v == "UNPARSED" for v in p.values())
