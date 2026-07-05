"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/core/dataset_tag.py (NEW leaf),
                            agent/generate_ensemble_artifacts.py (tag call-site,
                                baseline.symbol fail-fast, symbol stamping,
                                post-emission resolver self-check),
                            gcp/vm_e2e_pipeline.py (tag de-dup + ensemble_cfg
                                execution_symbol/models.*.symbol stamping),
                            configs/strategies/ES01B_Sharpe_E03_07042026.json
                                (surgical patch per audit section 2 — Coder owns),
                            src/live_execution/data_manager.py
                                (DataManager._backup_cache_to_repo per-symbol names),
                            tests/smoke_test_pipeline.py (_expected_cache_timestep
                                per-symbol cache names),
                            src/live_execution/live_trader.py (m1 [PNL] display
                                division; m3 dry-run/banner strings),
                            src/live_execution/ibkr_client.py (m3 qualify messages),
                            src/live_execution/cli.py (m3 --quantity help text)
Target Class/Function: derive_dataset_tag, generate_ensemble_artifacts.main,
                       vm_e2e_pipeline.run_pipeline (ensemble_cfg emission),
                       DataManager._backup_cache_to_repo, _expected_cache_timestep,
                       LiveTrader._on_new_bar / LiveTrader._print_account_summary,
                       IBKRConnectionManager.qualify_contract(_async), cli.main
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t6-config-generator-fix_07052026_0043
Phase: RED — written at HEAD 2a8311f (development). Failing by design until the
Coder (a) creates src/core/dataset_tag.py and routes BOTH consumers through it,
(b) stamps execution_symbol + models.<side>.symbol from manifest.baseline.symbol
in the generator with a fail-fast + resolver self-check, (c) surgically patches
the shipped ES01B config per audit section 2 (10-field table), and (d) lands the
audit section 5f cosmetics. CL pins in this file PASS at Red and at Green — they
are the byte-identity fences (blueprint Hard Constraint 1).

Governing docs: .agents/collab/tickets/t6-config-generator-fix_07052026_0043/
blueprint.md (D1/D2 manager rulings), audit.md sections 2/5/6, impact_review.md
(268-case differential method, C1-C4).

NOTE on I/O: generator tests run against a fully synthetic tmp_path mini-repo
(monkeypatch.chdir) with subprocess.run patched — no real backtests, no real
batch dirs. TestES01BPatchedConfig intentionally validates the SHIPPED config
against the real local artifact tree (ticket-mandated: "every referenced
model_path/predictions_path exists on disk" — reviewer-verified present at HEAD).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.core.instrument_master import get_instrument
from src.live_execution.instrument_context import resolve_instrument_context

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "configs" / "strategies"
_BASE_CONFIG_NAME = "hourly_ensemble_010.json"


# ===========================================================================
# Frozen HEAD transcriptions (differential oracles — do NOT import from src)
# ===========================================================================

def _vm_e2e_tag_head_transcription(data_basename: str, symbol: str) -> str:
    """FROZEN transcription of gcp/vm_e2e_pipeline.py:655-661 at HEAD 2a8311f.

    This is the single authoritative tag-derivation logic (duplicated at
    :733-740). src.core.dataset_tag.derive_dataset_tag must be byte-for-byte
    equivalent to THIS (impact_review section 1.1: 268-case differential,
    0 mismatches). bk_ rule fires FIRST; the {symbol}_ prefix match is
    case-insensitive with a case-preserving slice.
    """
    match = re.search(r'bk_(.+)$', data_basename)
    if match:
        dataset_tag = match.group(1)
    elif data_basename.upper().startswith(symbol.upper() + "_"):
        dataset_tag = data_basename[len(symbol) + 1:]
    else:
        dataset_tag = data_basename
    return dataset_tag


def _expected_emitted_config(
    base_config: dict,
    manifest: dict,
    opt_info: dict,
    *,
    symbol: str,
    e2e_prefix: str,
    sweep: str,
    layout: str = "production_output",
    objective: str = "sharpe",
    ensemble_idx: int = 1,
    dataset_tag: str,
    mmddyyyy: str = "01012026",
    long_metric: str = "logloss",
    short_metric: str = "logloss",
) -> dict:
    """FROZEN transcription of agent/generate_ensemble_artifacts.py main()
    emission logic at HEAD 2a8311f (:349-437), parameterized on the ONLY
    T6-sanctioned deltas:
      - ``e2e_prefix`` (D1: the corrected symbol-stripped tag for v2
        ``{sym}_`` inputs; unchanged bk_-derived tag for legacy inputs),
      - ``cfg["execution_symbol"] = symbol`` (in-place overwrite of the base
        config key — value-preserving for CL, the fix for non-CL).
    The D2 keys (models.<side>.symbol) are deliberately NOT added here; tests
    assert them separately and delete them before comparing, so this shape IS
    "today's output" for everything else (blueprint Hard Constraint 1).
    """
    long_desc = f"{long_metric.replace('average_precision', 'AP').replace('logloss', 'LL').upper()}_LONG"
    short_desc = f"{short_metric.replace('average_precision', 'AP').replace('logloss', 'LL').upper()}_SHORT"
    nickname = f"{dataset_tag}_{objective.capitalize()}_E{ensemble_idx:02d}_{mmddyyyy}"

    pred_path_long = f"reports/{sweep}/registry/{layout}/oos_predictions_sweep_long_{long_metric}.csv"
    pred_path_short = f"reports/{sweep}/registry/{layout}/oos_predictions_sweep_short_{short_metric}.csv"

    cfg = json.loads(json.dumps(base_config))  # deep copy — exactly HEAD :354
    cfg["nickname"] = nickname
    cfg["description"] = f"{objective.capitalize()} Ensemble #{ensemble_idx}: {long_desc} + {short_desc}"
    cfg["holdout_months"] = manifest.get("defaults", {}).get("post_optimizer_holdout_months", 6)

    regression_triggered = opt_info.get("regression_guard_triggered", False)
    if regression_triggered:
        cfg["conflict_resolution"] = "hold"
    else:
        cfg["conflict_resolution"] = opt_info.get("params", {}).get("conflict_resolution", "hold")

    if "models" not in cfg:
        cfg["models"] = {"long": {}, "short": {}}

    cfg["models"]["long"]["experiment_id"] = f"{e2e_prefix}_long_{long_metric}"
    cfg["models"]["long"]["model_path"] = f"reports/{sweep}/registry/{layout}/registry/{e2e_prefix}_long_{long_metric}/final_model.pkl"
    cfg["models"]["long"]["predictions_path"] = pred_path_long

    cfg["models"]["short"]["experiment_id"] = f"{e2e_prefix}_short_{short_metric}"
    cfg["models"]["short"]["model_path"] = f"reports/{sweep}/registry/{layout}/registry/{e2e_prefix}_short_{short_metric}/final_model.pkl"
    cfg["models"]["short"]["predictions_path"] = pred_path_short

    if not regression_triggered:
        long_params = opt_info.get("long_params", {})
        if long_params:
            cfg["models"]["long"]["threshold"] = long_params.get("entry_threshold", cfg["models"]["long"].get("threshold", 0.5))
            for k, v in long_params.items():
                if k != "entry_threshold":
                    cfg["long"][k] = v
            cfg["long"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": long_params.get("tp_atr_mult", 0)}]
            cfg["long"]["tiers"] = [{
                "min_prob": long_params.get("entry_threshold", 0),
                "lots": 1,
                "tp_atr_mult": long_params.get("tp_atr_mult", 0),
                "sl_atr_mult": long_params.get("sl_atr_mult", 0),
                "trailing_atr_mult": long_params.get("trailing_atr_mult", 0),
                "max_hold_bars": long_params.get("max_hold_bars", 0)
            }]

        short_params = opt_info.get("short_params", {})
        if short_params:
            cfg["models"]["short"]["threshold"] = short_params.get("entry_threshold", cfg["models"]["short"].get("threshold", 0.5))
            for k, v in short_params.items():
                if k != "entry_threshold":
                    cfg["short"][k] = v
            cfg["short"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": short_params.get("tp_atr_mult", 0)}]
            cfg["short"]["tiers"] = [{
                "min_prob": short_params.get("entry_threshold", 0),
                "lots": 1,
                "tp_atr_mult": short_params.get("tp_atr_mult", 0),
                "sl_atr_mult": short_params.get("sl_atr_mult", 0),
                "trailing_atr_mult": short_params.get("trailing_atr_mult", 0),
                "max_hold_bars": short_params.get("max_hold_bars", 0)
            }]

    # T6 stamp — in-place overwrite of the existing base-config key, so key
    # ORDER (and therefore bytes) are unchanged for CL (impact_review 1.6).
    cfg["execution_symbol"] = symbol
    return cfg


# ===========================================================================
# TestDeriveDatasetTag — src/core/dataset_tag.derive_dataset_tag (ghost import)
# ===========================================================================

# Representative fixture list mirroring the reviewer's 268-case method
# (impact_review section 1.1): real data/processed shapes x 8 registry symbols
# + targeted edges. Filenames (some with paths/extensions) — the test applies
# os.path.splitext(os.path.basename(...)) exactly like both consumers do.
_TAG_FIXTURE_FILENAMES = [
    # modern v2 {sym}_ shapes
    "CL_HourSet_14B.parquet", "ES_HourSet_01B.parquet", "ZC_HourSet_01A.parquet",
    "GC_HourSet_02.parquet", "NG_HourSet_05.csv", "SI_HourSet_01B.csv",
    "ZS_HourSet_03.parquet", "NQ_HourSet_01.parquet",
    # legacy bk_ shapes
    "cl-1h_bk_HourSet_06.parquet", "cl-5m_bk_HourSet_13A.parquet",
    "cl-1h_bk_HourSet_09.parquet", "cl-5m_bk.csv",
    # no-symbol-prefix shapes
    "HourSet_09.parquet", "dataset.csv",
    # raw/exec shapes
    "CL_raw.parquet", "ES_raw_1h.parquet", "ZC_raw.parquet", "CL_raw_1h.parquet",
    # edges: non-prefix micro, double prefix, case variants, path-qualified
    "MES_HourSet_01.parquet", "CL_CL_HourSet_14B.parquet",
    "es_hourset_01b.parquet", "Cl_HourSet_14B.parquet",
    "data/processed/ES_HourSet_01B.parquet",
    # degenerate: empty remainder, symbol-only, empty
    "ES_.parquet", "ES.parquet", "",
]

_TAG_SYMBOLS = ["CL", "ES", "ZC", "GC", "NG", "SI", "ZS", "NQ"]


class TestDeriveDatasetTag:
    """src/core/dataset_tag.py must exist and be byte-equivalent to the
    vm_e2e_pipeline HEAD logic (blueprint Target Files; audit section 5a)."""

    @staticmethod
    def _helper():
        # Ghost import (TDD protocol) — the Coder creates this module.
        from src.core.dataset_tag import derive_dataset_tag
        return derive_dataset_tag

    @pytest.mark.parametrize(
        "basename,symbol,expected",
        [
            # audit section 6 test-1 table
            ("cl-5m_bk_HourSet_13A", "CL", "HourSet_13A"),
            ("cl-1h_bk_HourSet_06", "CL", "HourSet_06"),
            ("CL_HourSet_14B", "CL", "HourSet_14B"),
            ("ES_HourSet_01B", "ES", "HourSet_01B"),
            ("ZC_HourSet_01A", "ZC", "HourSet_01A"),
            ("HourSet_09", "CL", "HourSet_09"),
            # case-insensitive prefix match, case-preserving remainder
            ("es_hourset_01b", "ES", "hourset_01b"),
            ("Cl_HourSet_14B", "CL", "HourSet_14B"),
            # symbol not a prefix -> passthrough
            ("ES_HourSet_01B", "CL", "ES_HourSet_01B"),
            ("MES_HourSet_01", "ES", "MES_HourSet_01"),
            # bk_ rule has PRECEDENCE over the symbol prefix
            ("CL_bk_HourSet_06", "CL", "HourSet_06"),
            # double prefix strips exactly ONE
            ("CL_CL_HourSet_14B", "CL", "CL_HourSet_14B"),
            # degenerate edges
            ("", "CL", ""),
            ("ES", "ES", "ES"),
            ("ES_", "ES", ""),
        ],
    )
    def test_absolute_tag_table(self, basename, symbol, expected):
        derive_dataset_tag = self._helper()
        assert derive_dataset_tag(basename, symbol) == expected

    def test_differential_against_frozen_vm_e2e_transcription(self):
        """Zero mismatches over the fixture list x all 8 symbols — the
        reviewer's differential method (impact_review section 1.1) frozen
        into the suite. Any drift between the shared helper and the HEAD
        vm_e2e logic is a hard failure."""
        derive_dataset_tag = self._helper()
        mismatches = []
        for fname in _TAG_FIXTURE_FILENAMES:
            basename = os.path.splitext(os.path.basename(fname))[0]
            for symbol in _TAG_SYMBOLS:
                expected = _vm_e2e_tag_head_transcription(basename, symbol)
                actual = derive_dataset_tag(basename, symbol)
                if actual != expected:
                    mismatches.append((fname, symbol, expected, actual))
        assert not mismatches, (
            f"{len(mismatches)} divergence(s) from the frozen vm_e2e HEAD "
            f"tag logic: {mismatches[:10]}"
        )


# ===========================================================================
# TestTagSingleSource — identity pin: ONE function object, no inline copies
# ===========================================================================

class TestTagSingleSource:
    """Divergence-by-copy must become structurally impossible (audit
    section 6 test 2): both consumers import THE SAME function object from
    src.core.dataset_tag, and the inline duplicated derivation blocks are
    gone from both files."""

    def test_generator_resolves_tag_from_shared_module(self):
        import src.core.dataset_tag as tag_mod  # ghost import
        import agent.generate_ensemble_artifacts as gen_mod
        assert getattr(gen_mod, "derive_dataset_tag", None) is tag_mod.derive_dataset_tag, (
            "agent/generate_ensemble_artifacts.py must import derive_dataset_tag "
            "from src.core.dataset_tag at module level (audit section 5b)"
        )

    def test_vm_e2e_resolves_tag_from_shared_module(self):
        import src.core.dataset_tag as tag_mod  # ghost import
        import gcp.vm_e2e_pipeline as vm_mod
        assert getattr(vm_mod, "derive_dataset_tag", None) is tag_mod.derive_dataset_tag, (
            "gcp/vm_e2e_pipeline.py must import derive_dataset_tag from "
            "src.core.dataset_tag at module level (audit section 5c)"
        )

    def test_inline_tag_derivation_blocks_removed(self):
        """The literal inline regex ``bk_(.+)$`` must survive ONLY inside
        src/core/dataset_tag.py. Today it appears once in the generator
        (:325) and twice in vm_e2e_pipeline (:655, :734)."""
        gen_src = (_PROJECT_ROOT / "agent" / "generate_ensemble_artifacts.py").read_text(encoding="utf-8")
        vm_src = (_PROJECT_ROOT / "gcp" / "vm_e2e_pipeline.py").read_text(encoding="utf-8")
        assert gen_src.count("bk_(.+)$") == 0, (
            "generator still carries an inline copy of the tag derivation"
        )
        assert vm_src.count("bk_(.+)$") == 0, (
            "vm_e2e_pipeline still carries inline copies of the tag derivation"
        )


# ===========================================================================
# TestGeneratorSymbolPropagation — fixture manifests + tmp_path artifact trees
# ===========================================================================

_SWEEP_BY_TAG = {
    "HourSet_13A": "sweep_hs13a_3x1_6h_canary_20260101-0000",
    "HourSet_14B": "sweep_hs14b_2x1_6h_scout_20260101-0000",
    "HourSet_01B": "sweep_es01b_2x1_6h_scout_20260101-0000",
}


def _opt_info_fixture() -> dict:
    return {
        "regression_guard_triggered": False,
        "params": {"conflict_resolution": "reverse_position"},
        "long_params": {
            "entry_threshold": 0.53, "tp_atr_mult": 4.5, "sl_atr_mult": 0.5,
            "trailing_atr_mult": 3.15, "max_hold_bars": 12, "cooldown_bars": 5,
        },
        "short_params": {
            "entry_threshold": 0.56, "tp_atr_mult": 3.5, "sl_atr_mult": 2.0,
            "trailing_atr_mult": 2.1, "max_hold_bars": 12, "cooldown_bars": 11,
        },
    }


def _build_batch_fixture(
    tmp_path: Path,
    *,
    symbol: str | None,
    data_file: str,
    label: str,
    bundle_prefix: str,
    sweep: str,
    base_config_mutator=None,
):
    """Build a synthetic mini-repo under tmp_path:
      configs/strategies/hourly_ensemble_010.json  (copied from the real base)
      batch_20260101_0000/{manifest,opt-results,top_pairs}.json
      reports/<sweep>/registry/production_output/
          registry/<bundle_prefix>_{long,short}_logloss/final_model.pkl
          oos_predictions_sweep_{long,short}_logloss.csv
    symbol=None omits baseline.symbol entirely (fail-fast fixture).
    """
    strategies_dir = tmp_path / "configs" / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    base = json.loads((_CONFIG_DIR / _BASE_CONFIG_NAME).read_text(encoding="utf-8"))
    if base_config_mutator is not None:
        base_config_mutator(base)
    (strategies_dir / _BASE_CONFIG_NAME).write_text(json.dumps(base, indent=4), encoding="utf-8")

    batch_dir = tmp_path / "batch_20260101_0000"
    batch_dir.mkdir()

    long_key = f"{sweep}_E2E_{label}_long_logloss"
    short_key = f"{sweep}_E2E_{label}_short_logloss"
    pair_key = f"{long_key}|{short_key}"

    baseline: dict = {}
    if symbol is not None:
        baseline["symbol"] = symbol
    manifest = {
        "baseline": baseline,
        "defaults": {
            "local_data_path": f"data/processed/{data_file}",
            "strategy_config": _BASE_CONFIG_NAME,
            "post_optimizer_holdout_months": 6,
        },
        "experiments": [{"label": f"{label} fixture"}],
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    opt_results = {pair_key: {"optuna_info": _opt_info_fixture(), "metrics": {}}}
    (batch_dir / "optimization_results_ensembles_sharpe.json").write_text(
        json.dumps(opt_results, indent=2), encoding="utf-8"
    )
    (batch_dir / "top_pairs.json").write_text(
        json.dumps([{"target_long": long_key, "target_short": short_key}], indent=2),
        encoding="utf-8",
    )

    layout_dir = tmp_path / "reports" / sweep / "registry" / "production_output"
    for side in ("long", "short"):
        bundle = layout_dir / "registry" / f"{bundle_prefix}_{side}_logloss"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "final_model.pkl").write_bytes(b"fixture-model")
        (layout_dir / f"oos_predictions_sweep_{side}_logloss.csv").write_text(
            "DateTime,prob\n2026-01-01,0.5\n", encoding="utf-8"
        )

    (tmp_path / "data" / "processed").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "processed" / data_file).write_bytes(b"")

    return batch_dir, manifest, opt_results[pair_key]["optuna_info"], base


def _run_generator(monkeypatch, tmp_path: Path, batch_dir: Path, data_file: str) -> None:
    """Drive main() — the narrowest emission seam at HEAD (everything is
    inline in main). Backtest subprocesses are patched out; CWD is the
    synthetic mini-repo so all relative paths resolve inside tmp_path."""
    import agent.generate_ensemble_artifacts as gen_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "generate_ensemble_artifacts.py",
        "--batch-dir", str(batch_dir),
        "--data", f"data/processed/{data_file}",
        "--objectives", "sharpe",
    ])
    fake_proc = SimpleNamespace(stdout="", stderr="")
    with patch.object(gen_mod.subprocess, "run", return_value=fake_proc):
        gen_mod.main()


def _read_emitted(batch_dir: Path, label: str) -> tuple[str, dict]:
    config_path = batch_dir / "configs" / f"{label}_Sharpe_E01_01012026.json"
    assert config_path.is_file(), f"generator did not emit {config_path}"
    text = config_path.read_text(encoding="utf-8")
    return text, json.loads(text)


class TestGeneratorSymbolPropagation:
    def test_cl_legacy_bk_input_byte_identical_except_models_symbol(self, tmp_path, monkeypatch):
        """Blueprint Hard Constraint 1 + D2: legacy ``bk_`` CL input emits
        TODAY'S bytes exactly, plus exactly one new key per side:
        models.{long,short}.symbol == "CL"."""
        sweep = _SWEEP_BY_TAG["HourSet_13A"]
        batch_dir, manifest, opt_info, base = _build_batch_fixture(
            tmp_path, symbol="CL", data_file="cl-5m_bk_HourSet_13A.parquet",
            label="HS13A", bundle_prefix="E2E_HourSet_13A", sweep=sweep,
        )
        _run_generator(monkeypatch, tmp_path, batch_dir, "cl-5m_bk_HourSet_13A.parquet")
        text, emitted = _read_emitted(batch_dir, "HS13A")

        # Serialization pin: file is the canonical json.dump(..., indent=4)
        assert text == json.dumps(emitted, indent=4)

        # D2: the new handshake keys (the ONLY additions for CL)
        assert emitted["models"]["long"].get("symbol") == "CL"
        assert emitted["models"]["short"].get("symbol") == "CL"

        # Byte-identity modulo D2: delete the two new keys -> exact HEAD shape
        del emitted["models"]["long"]["symbol"]
        del emitted["models"]["short"]["symbol"]
        expected = _expected_emitted_config(
            base, manifest, opt_info, symbol="CL",
            e2e_prefix="E2E_HourSet_13A", sweep=sweep, dataset_tag="HS13A",
        )
        assert json.dumps(emitted, indent=4) == json.dumps(expected, indent=4)

    def test_cl_v2_input_emits_symbol_stripped_prefixes(self, tmp_path, monkeypatch):
        """D1 (manager-accepted): v2 ``CL_HourSet_14B`` input must emit
        ``E2E_HourSet_14B_*`` (vm_e2e's stripped registry names, which exist)
        — NOT today's dead ``E2E_CL_HourSet_14B_*`` (audit section 1d)."""
        sweep = _SWEEP_BY_TAG["HourSet_14B"]
        batch_dir, manifest, opt_info, base = _build_batch_fixture(
            tmp_path, symbol="CL", data_file="CL_HourSet_14B.parquet",
            label="HS14B", bundle_prefix="E2E_HourSet_14B", sweep=sweep,
        )
        _run_generator(monkeypatch, tmp_path, batch_dir, "CL_HourSet_14B.parquet")
        text, emitted = _read_emitted(batch_dir, "HS14B")

        for side in ("long", "short"):
            entry = emitted["models"][side]
            assert entry["experiment_id"] == f"E2E_HourSet_14B_{side}_logloss"
            assert entry["model_path"] == (
                f"reports/{sweep}/registry/production_output/registry/"
                f"E2E_HourSet_14B_{side}_logloss/final_model.pkl"
            )
            # D1 pin: the corrected path EXISTS in the fixture tree
            assert (tmp_path / entry["model_path"]).is_file()
            assert entry.get("symbol") == "CL"
        assert emitted["execution_symbol"] == "CL"

        # everything else golden-identical (audit section 6 test 4)
        del emitted["models"]["long"]["symbol"]
        del emitted["models"]["short"]["symbol"]
        expected = _expected_emitted_config(
            base, manifest, opt_info, symbol="CL",
            e2e_prefix="E2E_HourSet_14B", sweep=sweep, dataset_tag="HS14B",
        )
        assert json.dumps(emitted, indent=4) == json.dumps(expected, indent=4)

        ctx = resolve_instrument_context(emitted)
        assert ctx.execution_symbol == "CL"

    def test_es_manifest_propagates_es_and_resolves(self, tmp_path, monkeypatch):
        """The exact shipped-ES01B failure mode, fixed at the source: an ES
        manifest must stamp execution_symbol/models.*.symbol "ES", strip the
        prefix, and the emitted config must pass the resolver."""
        sweep = _SWEEP_BY_TAG["HourSet_01B"]
        batch_dir, manifest, opt_info, base = _build_batch_fixture(
            tmp_path, symbol="ES", data_file="ES_HourSet_01B.parquet",
            label="ES01B", bundle_prefix="E2E_HourSet_01B", sweep=sweep,
        )
        _run_generator(monkeypatch, tmp_path, batch_dir, "ES_HourSet_01B.parquet")
        text, emitted = _read_emitted(batch_dir, "ES01B")

        assert emitted["execution_symbol"] == "ES"
        for side in ("long", "short"):
            entry = emitted["models"][side]
            assert entry.get("symbol") == "ES"
            assert entry["experiment_id"] == f"E2E_HourSet_01B_{side}_logloss"
            assert (tmp_path / entry["model_path"]).is_file()

        # Full-shape pin (same construction as the CL tests, symbol=ES)
        del emitted["models"]["long"]["symbol"]
        del emitted["models"]["short"]["symbol"]
        expected = _expected_emitted_config(
            base, manifest, opt_info, symbol="ES",
            e2e_prefix="E2E_HourSet_01B", sweep=sweep, dataset_tag="ES01B",
        )
        assert json.dumps(emitted, indent=4) == json.dumps(expected, indent=4)

        # Round-trip: the emitted config must resolve as ES (call it here too,
        # independent of the generator's internal self-check)
        emitted_full = json.loads(text)
        ctx = resolve_instrument_context(emitted_full)
        assert ctx.execution_symbol == "ES"
        assert ctx.brain_symbol == "ES"
        assert ctx.execution_instrument.exchange == "CME"

    def test_missing_baseline_symbol_raises_fatal(self, tmp_path, monkeypatch):
        """No-silent-defaults house rule (blueprint Hard Constraint 2):
        a manifest without baseline.symbol must ValueError with FATAL
        wording — never fall back to CL."""
        sweep = _SWEEP_BY_TAG["HourSet_13A"]
        batch_dir, *_ = _build_batch_fixture(
            tmp_path, symbol=None, data_file="cl-5m_bk_HourSet_13A.parquet",
            label="HS13A", bundle_prefix="E2E_HourSet_13A", sweep=sweep,
        )
        with pytest.raises(ValueError, match="FATAL"):
            _run_generator(monkeypatch, tmp_path, batch_dir, "cl-5m_bk_HourSet_13A.parquet")

    def test_unknown_baseline_symbol_raises_via_get_instrument(self, tmp_path, monkeypatch):
        """baseline.symbol must be validated against the instrument registry
        (audit section 5b step 1: get_instrument fail-fast)."""
        sweep = _SWEEP_BY_TAG["HourSet_13A"]
        batch_dir, *_ = _build_batch_fixture(
            tmp_path, symbol="XX", data_file="cl-5m_bk_HourSet_13A.parquet",
            label="HS13A", bundle_prefix="E2E_HourSet_13A", sweep=sweep,
        )
        with pytest.raises(ValueError, match=r"Unknown instrument symbol|FATAL"):
            _run_generator(monkeypatch, tmp_path, batch_dir, "cl-5m_bk_HourSet_13A.parquet")

    def test_emitted_config_failing_resolver_raises(self, tmp_path, monkeypatch):
        """Post-emission self-check (audit section 5b step 5): an emitted
        config that cannot start live must be a BATCH FAILURE, not a warning.

        Tamper vector: the base config is doctored with an incompatible
        ``brain_symbol: ES`` — a mismatched symbol that survives the T6
        execution_symbol/models.*.symbol stamping (which would overwrite a
        tampered models.<side>.symbol), so ONLY the resolver self-check can
        catch it. resolve_instrument_context raises ValueError for it."""
        def _tamper(base_cfg: dict) -> None:
            base_cfg["brain_symbol"] = "ES"  # incompatible with execution CL

        sweep = _SWEEP_BY_TAG["HourSet_13A"]
        batch_dir, *_ = _build_batch_fixture(
            tmp_path, symbol="CL", data_file="cl-5m_bk_HourSet_13A.parquet",
            label="HS13A", bundle_prefix="E2E_HourSet_13A", sweep=sweep,
            base_config_mutator=_tamper,
        )
        with pytest.raises(ValueError):
            _run_generator(monkeypatch, tmp_path, batch_dir, "cl-5m_bk_HourSet_13A.parquet")


# ===========================================================================
# TestES01BPatchedConfig — the shipped config, post surgical patch (audit s2)
# NOTE: FAILS at Red — the file still carries execution_symbol "CL" and dead
# E2E_ES_* paths until the Coder patches it (Coder owns the config edit).
# ===========================================================================

_ES01B_NAME = "ES01B_Sharpe_E03_07042026.json"
_ES01B_SWEEP = "reports/sweep_es01b_2x1_6h_scout_20260704-0701/registry/production_output/registry"
_ES01B_PRED = "reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/predictions/ES01B_Sharpe_E03_predictions.csv"


def _load_es01b() -> dict:
    return json.loads((_CONFIG_DIR / _ES01B_NAME).read_text(encoding="utf-8"))


class TestES01BPatchedConfig:
    def test_patched_fields_match_audit_table(self):
        """audit section 2 — the complete 10-field delta, pinned exactly.
        EVOLVED (ticket seedless-5m-live-stream_07052026_0546, sanctioned
        336d29f repair): 336d29f flipped execution_symbol ES->MES without
        evolving this pin (broken at HEAD f165b9d) — pins the CURRENT
        shipped value; brain handshake (models.*.symbol == "ES")
        unchanged."""
        cfg = _load_es01b()
        assert cfg["execution_symbol"] == "MES"
        assert "brain_symbol" not in cfg, (
            "audit section 2: do NOT add brain_symbol — ES is its own brain "
            "(structural derivation covers it)"
        )
        for side in ("long", "short"):
            entry = cfg["models"][side]
            assert entry["symbol"] == "ES"
            assert entry["experiment_id"] == f"E2E_HourSet_01B_{side}_logloss"
            assert entry["model_path"] == (
                f"{_ES01B_SWEEP}/E2E_HourSet_01B_{side}_logloss/final_model.pkl"
            )
            assert entry["predictions_path"] == _ES01B_PRED
        assert cfg["models"]["long"]["symbol"] == cfg["models"]["short"]["symbol"] == "ES"

    def test_resolves_as_es_es_cme(self):
        """EVOLVED (ticket seedless-5m-live-stream_07052026_0546,
        sanctioned 336d29f repair): 336d29f flipped ES01B to execution MES
        without evolving this pin (broken at HEAD f165b9d). Repaired to
        the CURRENT shipped resolution: execution MES / brain ES /
        exchange CME / tick 0.25 — the execution instrument is now the
        MICRO, so the multiplier pin follows the MES registry value (5)."""
        cfg = _load_es01b()
        ctx = resolve_instrument_context(cfg)
        assert ctx.execution_symbol == "MES"
        assert ctx.brain_symbol == "ES"
        assert ctx.execution_instrument.exchange == "CME"
        assert ctx.execution_instrument.multiplier == 5
        assert ctx.execution_instrument.tick_size == 0.25

    def test_referenced_artifacts_exist_on_disk(self):
        """Ticket-mandated on-disk validation (impact_review 1.3 verified all
        four files present locally at HEAD once the paths are corrected)."""
        cfg = _load_es01b()
        for side in ("long", "short"):
            entry = cfg["models"][side]
            assert (_PROJECT_ROOT / entry["model_path"]).is_file(), (
                f"models.{side}.model_path does not exist on disk: "
                f"{entry['model_path']}"
            )
            assert (_PROJECT_ROOT / entry["predictions_path"]).is_file(), (
                f"models.{side}.predictions_path does not exist on disk: "
                f"{entry['predictions_path']}"
            )

    def test_untouched_fields_pinned(self):
        """audit section 2: 'everything else unchanged' — sentinel pins.
        Guards the Coder's patch surface.
        EVOLVED (ticket seedless-5m-live-stream_07052026_0546, sanctioned
        336d29f repair): client_id 1010->2000 pins the CURRENT shipped
        value (336d29f flipped the config without evolving this pin —
        broken at HEAD f165b9d). All other sentinels unchanged."""
        cfg = _load_es01b()
        assert cfg["nickname"] == "ES01B_Sharpe_E03_07042026"
        assert cfg["live_config"]["client_id"] == 2000
        assert cfg["models"]["long"]["threshold"] == 0.53
        assert cfg["models"]["short"]["threshold"] == 0.56
        assert cfg["holdout_months"] == 6
        assert cfg["conflict_resolution"] == "reverse_position"
        assert cfg["bar_size"] == "1h"


# ===========================================================================
# TestVmE2eEnsembleCfg — the ensemble_cfg emission one-liner (blueprint 5c)
# ===========================================================================

class TestVmE2eEnsembleCfg:
    """The ensemble_cfg emission (vm_e2e_pipeline.run_pipeline, emission sites
    #2/#3 of audit section 1f) must stamp execution_symbol and models.*.symbol
    from the run ``symbol`` parameter.

    The emission is INLINE in run_pipeline (only reachable through heavy
    pipeline code: model training, data loads), and the blueprint mandates a
    one-liner — no extraction refactor. The narrowest structural seam that
    fails today and passes post-implementation is therefore a source pin on
    run_pipeline itself. Value-preservation for CL is by construction
    (symbol == "CL" and the base strategy_cfg is already CL)."""

    @staticmethod
    def _run_pipeline_source() -> str:
        import gcp.vm_e2e_pipeline as vm_mod
        return inspect.getsource(vm_mod.run_pipeline)

    def test_ensemble_cfg_stamps_execution_symbol_from_run_symbol(self):
        src = self._run_pipeline_source()
        assert re.search(
            r"ensemble_cfg\[\s*['\"]execution_symbol['\"]\s*\]\s*=\s*symbol\b", src
        ), (
            'run_pipeline must stamp ensemble_cfg["execution_symbol"] = symbol '
            "(blueprint 5c one-liner) — kills the CL leak into "
            "sweep_{metric}.json and TopKTracker candidates"
        )

    def test_ensemble_cfg_stamps_models_symbol_from_run_symbol(self):
        src = self._run_pipeline_source()
        dict_literal = re.findall(r"['\"]symbol['\"]\s*:\s*symbol\b", src)
        assignments = re.findall(r"\[\s*['\"]symbol['\"]\s*\]\s*=\s*symbol\b", src)
        assert len(dict_literal) + len(assignments) >= 2, (
            "run_pipeline must stamp models.long.symbol and models.short.symbol "
            "from the run symbol in the ensemble_cfg models block (either as "
            '"symbol": symbol dict entries or [...]["symbol"] = symbol '
            "assignments)"
        )


# ===========================================================================
# TestCosmetics — ONLY the audit section 5f enumerated CL-identical items
# ===========================================================================

def _make_dm_stub(tmp_path, monkeypatch, *, symbol: str, cache_name: str, meta_name: str):
    import src.live_execution.data_manager as dm_mod

    backup_dir = tmp_path / "cache_backups"
    monkeypatch.setattr(dm_mod, "_CACHE_BACKUP_DIR", backup_dir)

    dm = object.__new__(dm_mod.DataManager)
    dm.symbol = symbol
    dm.execution_symbol = symbol
    dm._df = pd.DataFrame({
        "DateTime": pd.date_range("2026-01-01", periods=3, freq="5min"),
        "Open": [1.0, 1.0, 1.0], "High": [1.0, 1.0, 1.0],
        "Low": [1.0, 1.0, 1.0], "Close": [1.0, 1.0, 1.0],
        "Volume": [1.0, 1.0, 1.0],
    })
    processed = tmp_path / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    dm.cache_path = processed / cache_name
    meta = processed / meta_name
    meta.write_text('{"last_front_month": "STUB"}', encoding="utf-8")
    dm.roll_metadata_path = meta
    return dm, backup_dir


def _make_bar_stub(symbol: str, *, position: int, acct: dict, dry_run: bool, signal):
    """object.__new__ LiveTrader stub for _on_new_bar — mirrors the proven
    seam in tests/test_exit_bar_semantics.py (F3). Sets ONLY _execution_symbol
    for instrument identity; _execution_instrument is provided opportunistically
    (skipped if the Coder implements it as a read-only property with the
    established _tick_size/_brain_instrument registry-fallback pattern)."""
    from src.live_execution.live_trader import LiveTrader

    trader = LiveTrader.__new__(LiveTrader)
    trader.exec_client = MagicMock()
    trader.exec_client.get_account_summary.return_value = acct
    trader.exec_client.get_position.return_value = position
    trader.strategy = MagicMock()
    trader.strategy.name = "StubStrategy"
    trader.strategy.direction = "LONG"
    trader.strategy.evaluate.return_value = signal
    trader.telemetry = MagicMock()
    trader.data_manager_1h = MagicMock()
    trader.dry_run = dry_run
    trader._open_orders = {}
    trader._tp_order_ids = []
    trader._sl_order_id = None
    trader._max_position_size = 1
    trader._position_bars_held = 3
    trader._data_mute = False
    trader._virtual_ledger = {"5m": 0, "1h": 0}
    trader._last_virtual_ledger_log = ""
    trader._position_side = 0
    trader._execution_symbol = symbol
    trader._emergency_halt = False
    trader._check_trailing_stop = MagicMock()
    trader._front_month_last_close = 70.0
    trader._atr_period_long = 14
    trader._atr_period_short = 14
    trader._atr_period = 14
    trader._rollover_in_progress = False
    trader._check_time_barrier = MagicMock(return_value=False)
    trader._pending_entry_order_id = None
    trader._needs_macro = False
    trader.feature_names = ["Open", "High", "Low", "Close", "Volume"]
    trader._lean_features = False
    trader._front_month_str = "202608"
    try:
        trader._execution_instrument = get_instrument(symbol)
    except AttributeError:
        pass  # future read-only property — registry fallback covers the stub
    return trader


def _hold_signal():
    return SimpleNamespace(
        action="HOLD", buy_prob=0.62, sell_prob=0.01, probability=0.62,
        confidence_pct=62.0, signal_label="Hold", skip_reason=None,
        tp_price=None, sl_price=None, lots=1,
    )


def _buy_signal():
    return SimpleNamespace(
        action="BUY", buy_prob=0.71, sell_prob=0.02, probability=0.71,
        confidence_pct=71.0, signal_label="Buy", skip_reason=None,
        tp_price=72.0, sl_price=69.0, lots=1,
    )


def _drive_on_new_bar(trader, caplog):
    features = pd.DataFrame(
        [{"Open": 70.0, "High": 70.0, "Low": 70.0, "Close": 70.0, "Volume": 100}]
    )
    with patch("src.live_execution.live_trader.build_live_features", return_value=features):
        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            trader._on_new_bar(
                bar_time=pd.Timestamp("2026-07-01 13:00:00", tz="UTC"),
                rolling_df=features,
                stream="1h",
            )


def _acct(symbol_position: int, avg_cost: float) -> dict:
    # m2 rename is DEFERRED (audit section 5f): the account-summary dict KEYS
    # stay cl_* for every symbol — this fixture deliberately provides ONLY
    # cl_* keys so a premature key rename fails loudly here.
    return {
        "account": "DU1899929",
        "net_liquidation": 1000000.0,
        "available_funds": 950000.0,
        "cl_position": symbol_position,
        "cl_unrealized_pnl": 500.0,
        "cl_realized_pnl": 200.0,
        "cl_market_value": 65500.0,
        "cl_avg_cost": avg_cost,
    }


class TestCosmetics:
    # ---- audit 5f: per-symbol backup filenames (_backup_cache_to_repo) ----

    def test_backup_filenames_cl_byte_identical(self, tmp_path, monkeypatch):
        """CL pin: legacy backup names unchanged (stems warm_start_cache /
        .roll_metadata -> today's exact literals). Passes at Red and Green."""
        dm, backup_dir = _make_dm_stub(
            tmp_path, monkeypatch, symbol="CL",
            cache_name="warm_start_cache.parquet", meta_name=".roll_metadata.json",
        )
        dm._backup_cache_to_repo(reason="rollover")
        parquets = [p.name for p in backup_dir.glob("*.parquet")]
        jsons = [p.name for p in backup_dir.glob("*.json")]
        assert len(parquets) == 1 and re.fullmatch(
            r"warm_start_cache_\d{8}_\d{6}_rollover\.parquet", parquets[0]
        ), parquets
        assert len(jsons) == 1 and re.fullmatch(
            r"roll_metadata_\d{8}_\d{6}_rollover\.json", jsons[0]
        ), jsons

    def test_backup_filenames_es_per_symbol(self, tmp_path, monkeypatch):
        """ES DataManager backups must be namespaced (no cross-symbol backup
        collision in a fleet): warm_start_cache_ES_* / roll_metadata_ES_*."""
        dm, backup_dir = _make_dm_stub(
            tmp_path, monkeypatch, symbol="ES",
            cache_name="warm_start_cache_ES.parquet", meta_name=".roll_metadata_ES.json",
        )
        dm._backup_cache_to_repo(reason="rollover")
        parquets = [p.name for p in backup_dir.glob("*.parquet")]
        jsons = [p.name for p in backup_dir.glob("*.json")]
        assert len(parquets) == 1 and re.fullmatch(
            r"warm_start_cache_ES_\d{8}_\d{6}_rollover\.parquet", parquets[0]
        ), parquets
        assert len(jsons) == 1 and re.fullmatch(
            r"roll_metadata_ES_\d{8}_\d{6}_rollover\.json", jsons[0]
        ), jsons

    # ---- audit 5f: smoke_test_pipeline cadence regex ----

    @pytest.mark.parametrize("name,minutes", [
        ("warm_start_cache.parquet", 5),        # CL legacy — unchanged
        ("warm_start_cache_1h.parquet", 60),    # CL legacy — unchanged
        ("warm_start_cache_5m.parquet", 5),     # legacy numeric — unchanged
    ])
    def test_smoke_cadence_regex_cl_names_unchanged(self, name, minutes):
        from tests.smoke_test_pipeline import _expected_cache_timestep
        assert _expected_cache_timestep(name) == pd.Timedelta(minutes=minutes)

    @pytest.mark.parametrize("name,minutes", [
        ("warm_start_cache_ZC_1h.parquet", 60),  # ticket-named case
        ("warm_start_cache_ES.parquet", 5),
        ("warm_start_cache_ES_1h.parquet", 60),
        ("warm_start_cache_ZC_5m.parquet", 5),
    ])
    def test_smoke_cadence_regex_accepts_per_symbol_names(self, name, minutes):
        from tests.smoke_test_pipeline import _expected_cache_timestep
        assert _expected_cache_timestep(name) == pd.Timedelta(minutes=minutes)

    def test_smoke_cadence_regex_rejects_non_cache_names(self):
        from tests.smoke_test_pipeline import _expected_cache_timestep
        assert _expected_cache_timestep("some_other_file.parquet") is None

    # ---- audit 5f m1: [PNL] display division by instrument multiplier ----

    def test_pnl_display_division_cl_pinned(self, caplog):
        """CL pin: averageCost 65,000 -> entryPrice=65.00 (multiplier 1000 —
        byte-identical to HEAD's /1000.0). Passes at Red and Green."""
        trader = _make_bar_stub(
            "CL", position=1, acct=_acct(1, 65000.0), dry_run=False,
            signal=_hold_signal(),
        )
        _drive_on_new_bar(trader, caplog)
        pnl = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[PNL]")]
        assert len(pnl) == 1, f"expected exactly one [PNL] line, got {pnl}"
        assert "entryPrice=65.00" in pnl[0]
        assert "unrealizedPnL=$500.00" in pnl[0]
        assert "Portfolio lookup failed" not in caplog.text

    def test_pnl_display_division_es_uses_instrument_multiplier(self, caplog):
        """ES: averageCost 300,600 / multiplier 50 -> entryPrice=6012.00.
        Red: HEAD divides by the hardcoded 1000.0 -> 300.60."""
        trader = _make_bar_stub(
            "ES", position=1, acct=_acct(1, 300600.0), dry_run=False,
            signal=_hold_signal(),
        )
        _drive_on_new_bar(trader, caplog)
        pnl = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[PNL]")]
        assert len(pnl) == 1, f"expected exactly one [PNL] line, got {pnl}"
        assert "entryPrice=6012.00" in pnl[0]
        assert "Portfolio lookup failed" not in caplog.text

    # ---- audit 5f m3: dry-run log line ----

    def test_dry_run_log_cl_byte_identical(self, caplog):
        """CL pin: the dry-run line is byte-identical to HEAD."""
        trader = _make_bar_stub(
            "CL", position=0, acct=_acct(0, 0.0), dry_run=True,
            signal=_buy_signal(),
        )
        _drive_on_new_bar(trader, caplog)
        dry = [r.getMessage() for r in caplog.records if r.getMessage().startswith("DRY RUN")]
        assert len(dry) == 1, f"expected exactly one DRY RUN line, got {dry}"
        assert dry[0] == "DRY RUN — would place bracket order BUY 1 CL (prob=0.71)"

    def test_dry_run_log_es_names_instance_symbol(self, caplog):
        """ES: the dry-run line must carry the instance symbol, not 'CL'."""
        trader = _make_bar_stub(
            "ES", position=0, acct=_acct(0, 0.0), dry_run=True,
            signal=_buy_signal(),
        )
        _drive_on_new_bar(trader, caplog)
        dry = [r.getMessage() for r in caplog.records if r.getMessage().startswith("DRY RUN")]
        assert len(dry) == 1, f"expected exactly one DRY RUN line, got {dry}"
        assert "ES" in dry[0]
        assert "CL" not in dry[0]

    # ---- audit 5f m3: startup account banner ----

    @staticmethod
    def _make_banner_stub(symbol: str, acct: dict):
        from src.live_execution import live_trader as lt_module
        trader = object.__new__(lt_module.LiveTrader)
        trader._execution_symbol = symbol
        trader.exec_client = MagicMock()
        trader.exec_client.get_account_summary.return_value = acct
        trader.telemetry = MagicMock()
        trader.telemetry.trade_summary.return_value = {
            "total_signals": 42, "executed_trades": 5,
            "first_signal": "2026-06-20T10:00:00",
            "last_signal": "2026-06-25T15:00:00", "total_bars": 1000,
        }
        return trader

    def test_account_banner_cl_byte_identical(self, caplog):
        trader = self._make_banner_stub("CL", _acct(1, 65000.0))
        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            trader._print_account_summary()
        assert "ACCOUNT SUMMARY (CL Only)" in caplog.text
        assert "CL Avg Cost:" in caplog.text
        assert "CL Unrealized PnL:" in caplog.text

    def test_account_banner_es_symbol_derived(self, caplog):
        """ES: banner text symbol-derived (dict KEYS stay cl_* pending m2 —
        the fixture provides only cl_* keys on purpose)."""
        trader = self._make_banner_stub("ES", _acct(1, 300600.0))
        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            trader._print_account_summary()
        messages = [r.getMessage() for r in caplog.records]
        headers = [m for m in messages if "ACCOUNT SUMMARY" in m]
        assert headers, f"no ACCOUNT SUMMARY header logged: {messages}"
        assert "(CL Only)" not in caplog.text
        assert any("ES" in m for m in headers)
        assert "CL Avg Cost" not in caplog.text

    # ---- audit 5f m3: qualify_contract error messages ----

    @staticmethod
    def _make_ibkr_stub(sync_result=None, async_result=None):
        from src.live_execution.ibkr_client import IBKRConnectionManager
        mgr = object.__new__(IBKRConnectionManager)
        mgr.ib = MagicMock()
        mgr.ib.isConnected.return_value = True
        mgr.ib.qualifyContracts.return_value = sync_result if sync_result is not None else []
        mgr.ib.qualifyContractsAsync = AsyncMock(
            return_value=async_result if async_result is not None else []
        )
        return mgr

    def test_qualify_contract_error_cl_pinned(self):
        mgr = self._make_ibkr_stub()
        contract = SimpleNamespace(symbol="CL", localSymbol="CLQ6")
        with pytest.raises(ValueError) as ei:
            mgr.qualify_contract(contract)
        msg = str(ei.value)
        assert "Failed to qualify" in msg
        assert "CL" in msg

    def test_qualify_contract_error_names_actual_symbol(self):
        """Red: HEAD hardcodes 'Failed to qualify CL contract with IBKR.'"""
        mgr = self._make_ibkr_stub()
        contract = SimpleNamespace(symbol="ES", localSymbol="ESU6")
        with pytest.raises(ValueError) as ei:
            mgr.qualify_contract(contract)
        msg = str(ei.value)
        assert "Failed to qualify" in msg
        assert "ES" in msg, f"error must name the actual contract symbol: {msg!r}"

    def test_qualify_contract_async_error_names_actual_symbol(self):
        mgr = self._make_ibkr_stub()
        contract = SimpleNamespace(symbol="ES", localSymbol="ESU6")
        with pytest.raises(ValueError) as ei:
            asyncio.run(mgr.qualify_contract_async(contract))
        msg = str(ei.value)
        assert "Failed to qualify" in msg
        assert "ES" in msg, f"error must name the actual contract symbol: {msg!r}"

    # ---- audit 5f m3: cli --quantity help text ----

    def test_cli_quantity_help_symbol_neutral(self, monkeypatch, capsys):
        """cli.py:158 'Number of CL contracts' -> symbol-neutral 'contracts';
        the 'CL Analyst' program title is a product name and STAYS (audit 5f
        LEAVE list)."""
        from src.live_execution import cli
        monkeypatch.setattr(sys, "argv", ["cli", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        out = " ".join(capsys.readouterr().out.split())
        assert "Number of CL contracts" not in out
        assert "contracts per trade" in out
        assert "CL Analyst" in out  # product title pin (LEAVE)
