"""
TDD-TESTER AUTHORIZATION
Target Implementation File: scripts/build_screen_config.py (NEW standalone generator)
Target Class/Function: build_screen_config.build_config, build_screen_config.main (CLI)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: screen-config-generator_07072026_1541
Phase: RED — written before scripts/build_screen_config.py exists. These tests fail
by design (ModuleNotFoundError / non-existent CLI) until the Coder implements the
generator per the blueprint.

Scope (blueprint):
- A CLI that emits a MasterConfig-shaped screen config (training_workflow.mode="screen").
- Exactly one source: --from-manifest (v2 BatchSweepConfig) XOR --from-dataset (parquet).
- --from-manifest: read baseline.symbol / dataset_version / train_cutoff_date and the
  deduped, order-preserved union of every experiments[].overrides.training_workflow.target_columns.
- --from-dataset: resolve parquet via get_data_root()/processed/<symbol>_<ver>.parquet
  (mirror vm_e2e_pipeline.main), collect all TARGET_TRIPLE_*_LONG/_SHORT (skip _MULTI).
- VALIDATE via MasterConfig(**cfg) before writing; fail loud (non-zero, write nothing)
  on invalid/empty-targets.
- Errors: neither/both sources, --from-dataset without --symbol, missing parquet, zero targets.

NOTE on I/O: tests run against synthetic tmp parquet/manifest files and write output
to tmp_path. The generator is invoked as a subprocess (real CLI, real exit codes) and
also imported directly for build_config() unit checks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _PROJECT_ROOT / "scripts" / "build_screen_config.py"

# Ensure src is importable for the direct-import checks
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(args, cwd=None):
    """Invoke the generator CLI as a subprocess; return CompletedProcess."""
    cmd = [sys.executable, str(_SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )


def _write_dataset(dir_path: Path, symbol: str, dataset_version: str, columns):
    """Write a tiny parquet with the given columns at the resolved dataset path.

    Mirrors vm_e2e_pipeline.main resolution: <symbol>_<ver>.parquet unless the
    version already starts with the symbol.
    """
    processed = dir_path / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    if dataset_version.upper().startswith(symbol.upper()):
        name = f"{dataset_version}.parquet"
    else:
        name = f"{symbol}_{dataset_version}.parquet"
    df = pd.DataFrame({c: [0.0, 1.0, 0.0] for c in columns})
    path = processed / name
    df.to_parquet(path)
    return path


def _si_manifest_dict(target_sets):
    """Build a minimal v2 BatchSweepConfig dict with the given per-experiment
    target_columns lists."""
    experiments = []
    for i, tc in enumerate(target_sets):
        experiments.append({
            "label": f"exp {i}",
            "gcs_prefix": f"sweep_exp_{i}",
            "overrides": {
                "symbol": "SI",
                "training_workflow": {"target_columns": tc},
            },
        })
    return {
        "_comment": "synthetic",
        "infrastructure": {
            "machine_type": "c2-standard-16",
            "provisioning_model": "STANDARD",
            "timeout_minutes": 360,
            "max_concurrent_vcpus": 288,
            "vcpus_per_vm": 16,
            "max_concurrent_vms": 12,
        },
        "baseline": {
            "symbol": "SI",
            "data_workflow": {
                "dataset_version": "HourSet_01B",
                "resolution": "1h",
                "targets": {
                    "raw_horizon": 120,
                    "atr_period": 14,
                    "definitions": [
                        {"type": "triple_barrier", "tp_multiplier": 2.0,
                         "sl_multiplier": 1.0, "horizon": 6},
                    ],
                },
            },
            "training_workflow": {
                "train_cutoff_date": "2022-01-01",
                "holdout_cutoff_date": None,
                "target_columns": [],
                "gcs_base_dir": "gs://cltrainer-optuna-results/scout",
                "random_seed": 42,
                "optuna": {"post_optimizer_holdout_months": 6},
            },
            "execution_workflow": {
                "slippage_per_side": 0.005,
                "opt_mode": "individual",
                "execution_data_path": "gs://cltrainer-optuna-results/data/SI_raw.parquet",
                "strategy_config_path": "configs/strategies/hourly_ensemble_010.json",
            },
        },
        "experiments": experiments,
    }


# ===========================================================================
# --from-dataset
# ===========================================================================

class TestFromDataset:
    def test_collects_long_short_triples_excludes_multi(self, tmp_path, monkeypatch):
        """Build a parquet with LONG/SHORT triple targets + a _MULTI + a non-target
        column; assert the generated config lists EXACTLY the LONG/SHORT triples,
        mode == 'screen', and MasterConfig(**cfg) validates."""
        cols = [
            "TARGET_TRIPLE_2x1_6H_LONG",
            "TARGET_TRIPLE_2x1_6H_SHORT",
            "TARGET_TRIPLE_3x1_24H_LONG",
            "TARGET_TRIPLE_3x1_24H_SHORT",
            "TARGET_TRIPLE_2x1_6H_MULTI",  # must be excluded
            "RSI_14",                       # non-target, excluded
            "Close",                        # non-target, excluded
        ]
        data_root = tmp_path / "data"
        _write_dataset(data_root, "SI", "HourSet_01B", cols)
        # Point get_data_root() at our tmp tree
        monkeypatch.setenv("CL_DATA_ROOT", str(tmp_path))

        out_path = tmp_path / "screen_si.json"
        res = _run_cli([
            "--from-dataset", "HourSet_01B",
            "--symbol", "SI",
            "--train-cutoff-date", "2025-06-01",
            "--out", str(out_path),
        ])
        assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
        assert out_path.exists()

        cfg = json.loads(out_path.read_text())
        assert cfg["symbol"] == "SI"
        assert cfg["training_workflow"]["mode"] == "screen"
        assert cfg["training_workflow"]["train_cutoff_date"] == "2025-06-01"
        assert set(cfg["training_workflow"]["target_columns"]) == {
            "TARGET_TRIPLE_2x1_6H_LONG",
            "TARGET_TRIPLE_2x1_6H_SHORT",
            "TARGET_TRIPLE_3x1_24H_LONG",
            "TARGET_TRIPLE_3x1_24H_SHORT",
        }
        assert not any(c.endswith("_MULTI") for c in cfg["training_workflow"]["target_columns"])

        # Generated config re-validates through the real schema
        from src.config.schemas import MasterConfig
        MasterConfig(**cfg)

    def test_missing_parquet_fails_nonzero(self, tmp_path, monkeypatch):
        """Dataset parquet missing -> non-zero exit, no output written."""
        monkeypatch.setenv("CL_DATA_ROOT", str(tmp_path))
        (tmp_path / "data" / "processed").mkdir(parents=True, exist_ok=True)
        out_path = tmp_path / "screen_si.json"
        res = _run_cli([
            "--from-dataset", "DoesNotExist",
            "--symbol", "SI",
            "--train-cutoff-date", "2025-06-01",
            "--out", str(out_path),
        ])
        assert res.returncode != 0
        assert not out_path.exists()

    def test_dataset_without_symbol_errors(self, tmp_path, monkeypatch):
        """--from-dataset requires --symbol."""
        monkeypatch.setenv("CL_DATA_ROOT", str(tmp_path))
        out_path = tmp_path / "screen.json"
        res = _run_cli([
            "--from-dataset", "HourSet_01B",
            "--train-cutoff-date", "2025-06-01",
            "--out", str(out_path),
        ])
        assert res.returncode != 0
        assert not out_path.exists()

    def test_zero_targets_writes_nothing(self, tmp_path, monkeypatch):
        """A parquet with no TARGET_TRIPLE_*_LONG/_SHORT columns -> error, no write."""
        cols = ["Close", "RSI_14", "TARGET_TRIPLE_2x1_6H_MULTI"]
        data_root = tmp_path / "data"
        _write_dataset(data_root, "SI", "HourSet_01B", cols)
        monkeypatch.setenv("CL_DATA_ROOT", str(tmp_path))
        out_path = tmp_path / "screen_si.json"
        res = _run_cli([
            "--from-dataset", "HourSet_01B",
            "--symbol", "SI",
            "--train-cutoff-date", "2025-06-01",
            "--out", str(out_path),
        ])
        assert res.returncode != 0
        assert not out_path.exists()

    def test_symbol_prefixed_version_resolves(self, tmp_path, monkeypatch):
        """When dataset_version already starts with the symbol, the parquet name is
        <version>.parquet (mirrors vm_e2e_pipeline)."""
        cols = ["TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT"]
        data_root = tmp_path / "data"
        # version "SI_HourSet_01B" starts with symbol "SI"
        _write_dataset(data_root, "SI", "SI_HourSet_01B", cols)
        monkeypatch.setenv("CL_DATA_ROOT", str(tmp_path))
        out_path = tmp_path / "screen_si.json"
        res = _run_cli([
            "--from-dataset", "SI_HourSet_01B",
            "--symbol", "SI",
            "--train-cutoff-date", "2025-06-01",
            "--out", str(out_path),
        ])
        assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
        assert out_path.exists()


# ===========================================================================
# --from-manifest
# ===========================================================================

class TestFromManifest:
    def test_union_deduped_order_preserved(self, tmp_path):
        """Two experiments with overlapping target_columns -> deduped union,
        first-seen order preserved; symbol/dataset/cutoff read from baseline."""
        manifest = _si_manifest_dict([
            ["TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT",
             "TARGET_TRIPLE_3x1_6H_LONG"],
            ["TARGET_TRIPLE_3x1_6H_LONG",  # duplicate of set 1
             "TARGET_TRIPLE_4x1_36H_LONG", "TARGET_TRIPLE_4x1_36H_SHORT"],
        ])
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        out_path = tmp_path / "screen_si.json"

        res = _run_cli([
            "--from-manifest", str(mpath),
            "--out", str(out_path),
        ])
        assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
        assert out_path.exists()

        cfg = json.loads(out_path.read_text())
        assert cfg["symbol"] == "SI"
        assert cfg["data_workflow"]["dataset_version"] == "HourSet_01B"
        assert cfg["training_workflow"]["train_cutoff_date"] == "2022-01-01"
        assert cfg["training_workflow"]["mode"] == "screen"
        # Deduped + first-seen order preserved
        assert cfg["training_workflow"]["target_columns"] == [
            "TARGET_TRIPLE_2x1_6H_LONG",
            "TARGET_TRIPLE_2x1_6H_SHORT",
            "TARGET_TRIPLE_3x1_6H_LONG",
            "TARGET_TRIPLE_4x1_36H_LONG",
            "TARGET_TRIPLE_4x1_36H_SHORT",
        ]

        from src.config.schemas import MasterConfig
        MasterConfig(**cfg)

    def test_train_cutoff_override(self, tmp_path):
        """--train-cutoff-date overrides the manifest baseline value."""
        manifest = _si_manifest_dict([
            ["TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT"],
        ])
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        out_path = tmp_path / "screen_si.json"
        res = _run_cli([
            "--from-manifest", str(mpath),
            "--train-cutoff-date", "2024-01-01",
            "--out", str(out_path),
        ])
        assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
        cfg = json.loads(out_path.read_text())
        assert cfg["training_workflow"]["train_cutoff_date"] == "2024-01-01"

    def test_symbol_conflict_errors(self, tmp_path):
        """--symbol conflicting with the manifest baseline symbol -> error, no write."""
        manifest = _si_manifest_dict([
            ["TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT"],
        ])
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        out_path = tmp_path / "screen_si.json"
        res = _run_cli([
            "--from-manifest", str(mpath),
            "--symbol", "CL",  # conflicts with baseline SI
            "--out", str(out_path),
        ])
        assert res.returncode != 0
        assert not out_path.exists()

    def test_empty_targets_writes_nothing(self, tmp_path):
        """A manifest whose experiments carry no target_columns -> zero-target error,
        writes nothing (validation gate)."""
        manifest = _si_manifest_dict([[], []])
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        out_path = tmp_path / "screen_si.json"
        res = _run_cli([
            "--from-manifest", str(mpath),
            "--out", str(out_path),
        ])
        assert res.returncode != 0
        assert not out_path.exists()

    def test_real_si_scout_manifest(self, tmp_path):
        """Smoke against the real SI HS01B scout manifest committed in the repo."""
        real = _PROJECT_ROOT / "configs" / "batch_manifest_v2_si_hourset01b_scout.json"
        if not real.exists():
            pytest.skip("real manifest not present")
        out_path = tmp_path / "screen_si_real.json"
        res = _run_cli([
            "--from-manifest", str(real),
            "--out", str(out_path),
        ])
        assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
        cfg = json.loads(out_path.read_text())
        assert cfg["symbol"] == "SI"
        assert cfg["training_workflow"]["mode"] == "screen"
        # 4 experiments x 2 sides = 8 unique targets
        assert len(cfg["training_workflow"]["target_columns"]) == 8
        from src.config.schemas import MasterConfig
        MasterConfig(**cfg)


# ===========================================================================
# Mutually-exclusive / missing source args
# ===========================================================================

class TestSourceArgs:
    def test_neither_source_errors(self, tmp_path):
        res = _run_cli(["--out", str(tmp_path / "x.json")])
        assert res.returncode != 0
        assert not (tmp_path / "x.json").exists()

    def test_both_sources_errors(self, tmp_path):
        manifest = _si_manifest_dict([["TARGET_TRIPLE_2x1_6H_LONG"]])
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        res = _run_cli([
            "--from-manifest", str(mpath),
            "--from-dataset", "HourSet_01B",
            "--symbol", "SI",
            "--out", str(tmp_path / "x.json"),
        ])
        assert res.returncode != 0
        assert not (tmp_path / "x.json").exists()


# ===========================================================================
# build_config() direct-import unit checks (validation gate)
# ===========================================================================

class TestBuildConfigUnit:
    def test_build_config_returns_valid_masterconfig_shape(self):
        """build_config() constructs a dict that MasterConfig accepts and carries
        mode=screen + the provided targets."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_screen_config", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        cfg = mod.build_config(
            symbol="SI",
            dataset_version="HourSet_01B",
            train_cutoff_date="2025-06-01",
            holdout_cutoff_date=None,
            random_seed=42,
            target_columns=["TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT"],
        )
        assert cfg["training_workflow"]["mode"] == "screen"
        assert cfg["training_workflow"]["target_columns"] == [
            "TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT",
        ]
        assert "execution_workflow" not in cfg
        from src.config.schemas import MasterConfig
        MasterConfig(**cfg)

    def test_build_config_empty_targets_raises(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_screen_config", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with pytest.raises(Exception):
            mod.build_config(
                symbol="SI",
                dataset_version="HourSet_01B",
                train_cutoff_date="2025-06-01",
                holdout_cutoff_date=None,
                random_seed=42,
                target_columns=[],
            )
