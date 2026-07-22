"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/sweep_ensembles.py (_resolve_base_threshold),
                            src/live_execution/strategies/configurable_strategy.py
                            (ensemble no-tiers threshold strictness)
Secondary Targets: configs/strategies/*.json (5 fleet configs: models.*.threshold
                   removed; tiers[].min_prob is canonical)
Status: FINALIZED
Ticket: threshold-min-prob-consolidation_07222026_1230

For TIERED configs, models.*.threshold was a synced informational duplicate of
tiers[].min_prob (TieredEnsembleStrategy documents it as "cosmetic/
informational" and warns on divergence; generate_ensemble_artifacts writes it
FROM tiers[0].min_prob). Runtime (ConfigurableStrategy tiered branch),
optimizer warm-start (strategy_optimizer, tiers-first), and parity tooling
(prediction_parity_compare, tiers override) all already prefer tiers. The
redundancy is removed from the 5 fleet configs; the remaining readers with
SILENT defaults are hardened:
  * agent/sweep_ensembles.py defaulted to 0.55 and then WROTE that value into
    every tiers[*].min_prob of the patched config — a silent 0.55 could
    rewrite a strategy's entry threshold. Now: tiers-first resolution with a
    loud failure when nothing resolves.
  * ConfigurableStrategy's no-tiers ensemble branch defaulted to 0.50
    silently. Now: models.<side>.threshold is REQUIRED for a present side
    (absent side = fail-closed 1.0 sentinel, mirroring the tiered branch).
Non-tiered configs keep models.*.threshold as their explicit source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.sweep_ensembles import _resolve_base_threshold
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy


REPO_ROOT = Path(__file__).resolve().parents[1]

FLEET_CONFIGS = [
    "HS14B_Sharpe_E01_06262026.json",
    "ES02B_Sharpe_E01_07112026.json",
    "NG01B_Sharpe_E03_07052026.json",
    "GC02B_Sharpe_E04_07102026.json",
    "SI01B_Sharpe_E02_07062026.json",
]


# ---------------------------------------------------------------------------
# 1. sweep_ensembles._resolve_base_threshold
# ---------------------------------------------------------------------------


class TestResolveBaseThreshold:
    def test_tiers_are_canonical(self):
        cfg = {
            "long": {"tiers": [{"min_prob": 0.62, "lots": 1},
                               {"min_prob": 0.56, "lots": 1}]},
            "models": {"long": {"threshold": 0.99}},  # divergent -> ignored
        }
        assert _resolve_base_threshold(cfg, "long") == pytest.approx(0.56)

    def test_models_threshold_fallback_without_tiers(self):
        cfg = {"models": {"short": {"threshold": 0.54}}}
        assert _resolve_base_threshold(cfg, "short") == pytest.approx(0.54)

    def test_neither_source_raises_no_silent_default(self):
        """The old 0.55 default got written back into every tiers[*].min_prob
        of the patched config — silence here rewrote entry thresholds."""
        cfg = {"models": {"long": {}}, "long": {}}
        with pytest.raises(ValueError, match="no entry threshold"):
            _resolve_base_threshold(cfg, "long")

    def test_fleet_configs_resolve_from_tiers_alone(self):
        """All 5 fleet configs must resolve BOTH sides from tiers with
        models.*.threshold gone."""
        for name in FLEET_CONFIGS:
            cfg = json.loads(
                (REPO_ROOT / "configs" / "strategies" / name).read_text(
                    encoding="utf-8"
                )
            )
            for side in ("long", "short"):
                thr = _resolve_base_threshold(cfg, side)
                assert 0.0 < thr < 1.0, (
                    f"{name} {side}: implausible threshold {thr}"
                )
                assert thr == pytest.approx(
                    min(float(t["min_prob"]) for t in cfg[side]["tiers"])
                )


# ---------------------------------------------------------------------------
# 2. ConfigurableStrategy no-tiers ensemble strictness
# ---------------------------------------------------------------------------


class TestEnsembleThresholdStrictness:
    def _write_cfg(self, tmp_path, cfg: dict) -> str:
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def test_missing_threshold_raises_not_silent_050(self, tmp_path):
        """A no-tiers ensemble config without models.long.threshold used to
        get a silent 0.50; it must now crash loudly (before any model I/O)."""
        cfg_path = self._write_cfg(tmp_path, {
            "nickname": "strict_test",
            "override_global_filters": True,
            "models": {"long": {"model_path": "x"}, "short": {"model_path": "y"}},
        })
        with pytest.raises(ValueError, match=r"models\.long\.threshold"):
            ConfigurableStrategy(config_path=cfg_path)

    def test_tiered_config_without_models_threshold_is_fine(self):
        """Fleet-shaped configs (tiers present, models.*.threshold absent)
        must pass the threshold-resolution stage: thresholds derive from
        tiers[].min_prob, never from the removed key. (Source pin: the
        tiered branch reads tiers only.)"""
        for name in FLEET_CONFIGS:
            cfg = json.loads(
                (REPO_ROOT / "configs" / "strategies" / name).read_text(
                    encoding="utf-8"
                )
            )
            for side in ("long", "short"):
                assert "threshold" not in cfg["models"][side], (
                    f"{name}: models.{side}.threshold reappeared — "
                    f"tiers[].min_prob is canonical for tiered configs"
                )
                assert cfg[side]["tiers"], f"{name}: {side} tiers missing"


# ---------------------------------------------------------------------------
# 3. Fleet-config hygiene (guards both consolidation tickets)
# ---------------------------------------------------------------------------


class TestFleetConfigHygiene:
    @pytest.mark.parametrize("name", FLEET_CONFIGS)
    def test_no_dead_vocabulary_keys(self, name):
        """Fleet configs must not carry the removed redundant/dead keys:
        models.*.threshold (this ticket) and sl/tp_cooldown_bars
        (cooldown-single-authority-wiring_07222026_1051)."""
        raw = (REPO_ROOT / "configs" / "strategies" / name).read_text(
            encoding="utf-8"
        )
        cfg = json.loads(raw)
        assert "sl_cooldown_bars" not in raw and "tp_cooldown_bars" not in raw
        for side in ("long", "short"):
            assert "threshold" not in cfg.get("models", {}).get(side, {})
            assert "cooldown_bars" in cfg[side], (
                f"{name}: per-side cooldown_bars (the single cooldown "
                f"authority) must stay present"
            )
