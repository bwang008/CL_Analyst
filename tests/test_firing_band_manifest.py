"""Tests for the manifest-tunable signal-firing band.

Ticket: firing-band-manifest_07072026_1202

Ticket 1a (dynamic-entry-threshold) made the post-optimizer derive per-model
entry-threshold bounds from a signal-firing band, defaulting to the module
constants ``FIRING_FRAC_MIN=0.05`` / ``FIRING_FRAC_MAX=0.45``. This ticket makes
that band *manifest-tunable* — OPTIONAL fields on ``ExecutionWorkflowConfig`` with
explicit non-None defaults (0.05 / 0.45), read by ``batch_post_optimizer`` and
threaded down to ``run_optimization``.

Covered here:
  1. Schema: valid band parses; inverted / negative / >1.0 bands raise; omitting
     both fields yields the 0.05 / 0.45 defaults; a real on-disk manifest still
     validates unchanged.
  2. batch_post_optimizer: reads a custom band from the manifest dict, and falls
     back to the strategy_optimizer module constants when the band is absent.
  3. Threading: run_single_optimization forwards firing_frac_min/max into
     run_optimization (asserted via monkeypatch capturing kwargs — no full run).
"""

import json
import os

import pytest
from pydantic import ValidationError

from src.config.schemas import ExecutionWorkflowConfig, BatchSweepConfig

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 1. Schema: ExecutionWorkflowConfig firing-band fields
# ---------------------------------------------------------------------------

def _base_exec_kwargs(**overrides):
    """Minimal valid ExecutionWorkflowConfig kwargs (band omitted by default)."""
    kwargs = dict(
        slippage_per_side=0.01,
        execution_data_path="gs://bucket/data/CL_raw.parquet",
        strategy_config_path="configs/strategies/hourly_ensemble_010.json",
        opt_mode="individual",
    )
    kwargs.update(overrides)
    return kwargs


def test_firing_band_defaults_when_omitted():
    """Omitting both fields yields the explicit 0.05 / 0.45 defaults."""
    cfg = ExecutionWorkflowConfig(**_base_exec_kwargs())
    assert cfg.firing_frac_min == 0.05
    assert cfg.firing_frac_max == 0.45


def test_firing_band_custom_valid_parses():
    """A valid custom band is accepted and stored verbatim."""
    cfg = ExecutionWorkflowConfig(**_base_exec_kwargs(
        firing_frac_min=0.10, firing_frac_max=0.40
    ))
    assert cfg.firing_frac_min == 0.10
    assert cfg.firing_frac_max == 0.40


def test_firing_band_inverted_raises():
    """min >= max is an inverted band and must be rejected loudly."""
    with pytest.raises(ValidationError):
        ExecutionWorkflowConfig(**_base_exec_kwargs(
            firing_frac_min=0.45, firing_frac_max=0.05
        ))


def test_firing_band_equal_raises():
    """min == max is a zero-width band and must be rejected."""
    with pytest.raises(ValidationError):
        ExecutionWorkflowConfig(**_base_exec_kwargs(
            firing_frac_min=0.20, firing_frac_max=0.20
        ))


def test_firing_band_negative_raises():
    """A negative / zero min violates 0.0 < min and must be rejected."""
    with pytest.raises(ValidationError):
        ExecutionWorkflowConfig(**_base_exec_kwargs(
            firing_frac_min=-0.01, firing_frac_max=0.45
        ))
    with pytest.raises(ValidationError):
        ExecutionWorkflowConfig(**_base_exec_kwargs(
            firing_frac_min=0.0, firing_frac_max=0.45
        ))


def test_firing_band_above_one_raises():
    """max > 1.0 is out of range and must be rejected."""
    with pytest.raises(ValidationError):
        ExecutionWorkflowConfig(**_base_exec_kwargs(
            firing_frac_min=0.05, firing_frac_max=1.5
        ))


def test_firing_band_max_exactly_one_ok():
    """max == 1.0 is the inclusive upper bound and is allowed."""
    cfg = ExecutionWorkflowConfig(**_base_exec_kwargs(
        firing_frac_min=0.05, firing_frac_max=1.0
    ))
    assert cfg.firing_frac_max == 1.0


def test_existing_manifest_without_band_still_validates():
    """A real on-disk manifest that omits the band still validates and defaults."""
    # HourSet_15A is one of the 34 manifests we deliberately did NOT edit.
    candidates = [
        os.path.join(PROJECT_ROOT, "configs", "batch_manifest_v2_hourset15a_scout.json"),
        os.path.join(PROJECT_ROOT, "configs", "batch_manifest_v2_hourset14a_canary.json"),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        pytest.skip("No unedited v2 manifest available to test default inheritance.")
    with open(path, encoding="utf-8-sig") as f:
        raw = json.load(f)
    cfg = BatchSweepConfig(**raw)
    ew = cfg.baseline.execution_workflow
    assert ew is not None
    assert ew.firing_frac_min == 0.05
    assert ew.firing_frac_max == 0.45


def test_example_manifest_with_band_validates():
    """The two edited example manifests carry the band explicitly and validate."""
    for name in ("batch_manifest_v2_hourset14a_scout.json",
                 "batch_manifest_v2_hourset14b_scout.json"):
        path = os.path.join(PROJECT_ROOT, "configs", name)
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
        # The example fields must be present in the raw JSON template.
        ew_raw = raw["baseline"]["execution_workflow"]
        assert ew_raw["firing_frac_min"] == 0.05, f"{name} missing example firing_frac_min"
        assert ew_raw["firing_frac_max"] == 0.45, f"{name} missing example firing_frac_max"
        cfg = BatchSweepConfig(**raw)
        assert cfg.baseline.execution_workflow.firing_frac_min == 0.05
        assert cfg.baseline.execution_workflow.firing_frac_max == 0.45


# ---------------------------------------------------------------------------
# 2. batch_post_optimizer: read the band from a manifest dict
# ---------------------------------------------------------------------------

def _resolve_firing_band(manifest):
    """Call the batch_post_optimizer helper that reads the band from a manifest.

    Imported lazily so the RED phase fails on a clear AttributeError until the
    coder adds the helper.
    """
    import agent.batch_post_optimizer as bpo
    return bpo.resolve_firing_band(manifest)


def test_band_read_from_manifest_custom():
    """A manifest WITH a custom band yields exactly the manifest's values."""
    manifest = {
        "baseline": {
            "execution_workflow": {
                "firing_frac_min": 0.12,
                "firing_frac_max": 0.38,
            }
        }
    }
    f_min, f_max = _resolve_firing_band(manifest)
    assert f_min == 0.12
    assert f_max == 0.38


def test_band_falls_back_to_module_constants():
    """A manifest WITHOUT the band yields the strategy_optimizer module constants."""
    import agent.strategy_optimizer as so
    manifest = {"baseline": {"execution_workflow": {
        "slippage_per_side": 0.01,
        "opt_mode": "individual",
    }}}
    f_min, f_max = _resolve_firing_band(manifest)
    assert f_min == so.FIRING_FRAC_MIN
    assert f_max == so.FIRING_FRAC_MAX


def test_band_falls_back_when_execution_workflow_absent():
    """Even a manifest with no execution_workflow at all falls back to constants."""
    import agent.strategy_optimizer as so
    f_min, f_max = _resolve_firing_band({"baseline": {}})
    assert f_min == so.FIRING_FRAC_MIN
    assert f_max == so.FIRING_FRAC_MAX


# ---------------------------------------------------------------------------
# 3. Threading: run_single_optimization forwards the band to run_optimization
# ---------------------------------------------------------------------------

def test_run_single_optimization_threads_band(monkeypatch):
    """run_single_optimization must forward firing_frac_min/max into run_optimization."""
    import agent.batch_post_optimizer as bpo

    captured = {}

    def fake_run_optimization(*args, **kwargs):
        captured.update(kwargs)
        # run_single_optimization unpacks (best_cfg, best_result)
        return ({"optuna_info": {}}, object())

    def fake_extract_metrics(_result):
        return {}

    monkeypatch.setattr(bpo, "run_optimization", fake_run_optimization)
    monkeypatch.setattr(bpo, "extract_metrics", fake_extract_metrics)

    result = bpo.run_single_optimization(
        config_path="cfg.json",
        predictions_path="preds.csv",
        ohlcv_path="ohlcv.parquet",
        n_trials=1,
        min_trades=0,
        label="unit-test",
        firing_frac_min=0.13,
        firing_frac_max=0.37,
    )

    assert result["status"] == "OK"
    assert captured.get("firing_frac_min") == 0.13
    assert captured.get("firing_frac_max") == 0.37


def test_run_single_optimization_band_defaults_to_module_constants(monkeypatch):
    """When the band args are omitted, the module constants are forwarded."""
    import agent.batch_post_optimizer as bpo
    import agent.strategy_optimizer as so

    captured = {}

    def fake_run_optimization(*args, **kwargs):
        captured.update(kwargs)
        return ({"optuna_info": {}}, object())

    monkeypatch.setattr(bpo, "run_optimization", fake_run_optimization)
    monkeypatch.setattr(bpo, "extract_metrics", lambda _r: {})

    bpo.run_single_optimization(
        config_path="cfg.json",
        predictions_path="preds.csv",
        ohlcv_path="ohlcv.parquet",
        n_trials=1,
        min_trades=0,
        label="unit-test",
    )

    assert captured.get("firing_frac_min") == so.FIRING_FRAC_MIN
    assert captured.get("firing_frac_max") == so.FIRING_FRAC_MAX
