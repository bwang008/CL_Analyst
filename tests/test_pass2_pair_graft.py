"""Tests for the 2026-07-12 pass-2 stabilization changes:

A. unified_pair_optimizer selection score — holdout weight 6x -> 2x
   (recalibrated for the 12-month post-opt holdout; 6x was set in the
   6-month era and made selection effectively holdout-only).
B. batch_post_optimizer pass-1 grafting — each pair's pass-2 base config
   carries the legs' pass-1 winning params, so the ensemble study
   warm-starts from (and the regression guard protects) the PROVEN
   combination instead of the generic baseline.
C. Aggressive-tier search space sentinel — TP ATR cap lowered 8.0 -> 6.0.
D. OptunaConfig.post_optimizer_ensemble_trials — optional split budget,
   None means inherit (resolved in the PS1 chain, not silently downstream).
"""

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.unified_pair_optimizer import parse_markdown_table
from agent.batch_post_optimizer import _parse_pred_prefix, _build_pair_base_config
from agent.strategy_optimizer import _PARAM_RANGES
from src.config.schemas import OptunaConfig


# ---------------------------------------------------------------------------
# A. Selection score: robustness = pnl_opt + 2 * pnl_holdout
# ---------------------------------------------------------------------------

_SUMMARY_MD = """# Batch Summary (Optimized)

### Long Model (Logloss)

| Experiment | Trades (pre) | Trades (opt) | PF (pre) | PF (opt) | PnL (pre) | PnL (opt) | PnL (holdout) |
|---|---|---|---|---|---|---|---|
| ExpA | 100 | 150 | 1.10 | 1.30 | $1,000 | $5,000 | $2,000 |
| ExpB | 100 | 150 | 1.10 | 1.30 | $1,000 | $5,000 | $-2,000 |
"""

_PROGRESS = {"experiments": [
    {"label": "ExpA", "gcs_prefix": "expa_prefix"},
    {"label": "ExpB", "gcs_prefix": "expb_prefix"},
]}


class TestSelectionScoreWeight:

    def _models(self, tmp_path):
        md = tmp_path / "batch_summary_optimized_sharpe.md"
        md.write_text(_SUMMARY_MD, encoding="utf-8")
        return parse_markdown_table(str(md), "long", "logloss", "sharpe", _PROGRESS)

    def test_robustness_is_opt_plus_2x_holdout(self, tmp_path):
        models = self._models(tmp_path)
        expa = next(m for m in models if m["experiment"] == "ExpA")
        assert expa["robustness"] == pytest.approx(5000 + 2 * 2000)

    def test_negative_holdout_still_filtered_and_penalized(self, tmp_path):
        models = self._models(tmp_path)
        expb = next(m for m in models if m["experiment"] == "ExpB")
        assert not expb["passed_filter"]
        assert expb["robustness"] == pytest.approx(5000 + 2 * -2000 - 1000000.0)


# ---------------------------------------------------------------------------
# B. Pass-1 grafting
# ---------------------------------------------------------------------------

# Minimal TieredEnsembleStrategy config in the hourly_ensemble_010 shape.
_BASE_CFG = {
    "execution_class": "TieredEnsembleStrategy",
    "models": {"long": {"threshold": 0.5}, "short": {"threshold": 0.53}},
    "long": {
        "tiers": [{"min_prob": 0.5, "tp_atr_mult": 1.5, "sl_atr_mult": 3.5}],
        "tp_atr_mult": 1.5, "sl_atr_mult": 3.5,
        "cooldown_bars": 7, "atr_period": 36, "max_hold_bars": 48,
    },
    "short": {
        "tiers": [{"min_prob": 0.53, "tp_atr_mult": 10.0, "sl_atr_mult": 3.0}],
        "tp_atr_mult": 10.0, "sl_atr_mult": 3.0,
        "cooldown_bars": 7, "atr_period": 20, "max_hold_bars": 240,
    },
}

_LONG_P1_PARAMS = {
    "entry_threshold": 0.56, "tp_atr_mult": 6.0, "sl_atr_mult": 1.0,
    "cooldown_bars": 1, "atr_period": 28, "max_hold_bars": 30,
    "consecutive_signal_threshold": 2,
    "trailing_atr_mult": 2.4, "trailing_sl_atr_offset": 1.2,
}

_GCS_TO_LABEL = {"expa_prefix": "ExpA", "expb_prefix": "ExpB"}

_LONG_PREFIX = "oos_predictions_expa_prefix_long_average_precision"
_SHORT_PREFIX = "oos_predictions_expb_prefix_short_logloss"


def _pass1_results(short_guard=True):
    results = {
        "ExpA|long|average_precision": {
            "status": "OK",
            "optuna_info": {"params": dict(_LONG_P1_PARAMS)},
        },
    }
    if short_guard:
        # Regression-guard leg: empty params BY CONTRACT — its winner IS the
        # baseline config, so the generic short block must be kept verbatim.
        results["ExpB|short|logloss"] = {
            "status": "OK",
            "optuna_info": {"params": {}, "regression_guard_triggered": True},
        }
    return results


class TestParsePredPrefix:

    def test_average_precision_long(self):
        assert _parse_pred_prefix(_LONG_PREFIX) == (
            "expa_prefix", "long", "average_precision")

    def test_logloss_short(self):
        assert _parse_pred_prefix(_SHORT_PREFIX) == (
            "expb_prefix", "short", "logloss")

    def test_timestamped_gcs_prefix_survives(self):
        got = _parse_pred_prefix(
            "oos_predictions_sweep_ng_hs02b_3x1_6h_scout_20260711-061128_long_average_precision")
        assert got == ("sweep_ng_hs02b_3x1_6h_scout_20260711-061128",
                       "long", "average_precision")

    def test_legacy_registry_prefix_rejected(self):
        assert _parse_pred_prefix("registry_something_long_logloss") is None


class TestBuildPairBaseConfig:

    def _base_path(self, tmp_path):
        p = tmp_path / "base.json"
        p.write_text(json.dumps(_BASE_CFG), encoding="utf-8")
        return str(p)

    def test_long_grafted_short_guard_kept_generic(self, tmp_path):
        out = str(tmp_path / "pair_ens1.json")
        got = _build_pair_base_config(
            self._base_path(tmp_path), _pass1_results(), _GCS_TO_LABEL,
            _LONG_PREFIX, _SHORT_PREFIX, out,
        )
        assert got == out and os.path.exists(out)
        cfg = json.loads(open(out, encoding="utf-8").read())

        # Long side carries the pass-1 winner (tiers are authoritative).
        assert cfg["long"]["tp_atr_mult"] == 6.0
        assert cfg["long"]["sl_atr_mult"] == 1.0
        assert cfg["long"]["cooldown_bars"] == 1
        assert cfg["long"]["atr_period"] == 28
        assert cfg["long"]["tiers"][0]["min_prob"] == 0.56
        assert cfg["long"]["tiers"][0]["tp_atr_mult"] == 6.0

        # Guard-kept short side is the generic block, byte-for-byte.
        assert cfg["short"] == _BASE_CFG["short"]

    def test_both_legs_unresolvable_falls_back_to_generic(self, tmp_path):
        out = str(tmp_path / "pair_ens1.json")
        base = self._base_path(tmp_path)
        got = _build_pair_base_config(
            base, {}, {},  # no pass-1 entries, no labels
            _LONG_PREFIX, _SHORT_PREFIX, out,
        )
        assert got == base
        assert not os.path.exists(out)

    def test_output_json_is_bom_less_utf8(self, tmp_path):
        out = str(tmp_path / "pair_ens1.json")
        _build_pair_base_config(
            self._base_path(tmp_path), _pass1_results(), _GCS_TO_LABEL,
            _LONG_PREFIX, _SHORT_PREFIX, out,
        )
        raw = open(out, "rb").read()
        assert not raw.startswith(b"\xef\xbb\xbf")


# ---------------------------------------------------------------------------
# B2. Guard-triggered ensembles still render the per-pair grafted baseline
# ---------------------------------------------------------------------------

class TestGuardEnsembleReportBaseline:
    """A pass-2 regression guard empties long/short_params BY CONTRACT; the
    report must still render baseline_side_params (the grafted per-pair
    baseline), not fall back to the global base config (canary
    batch_20260712_020614 finding)."""

    def test_guard_ensemble_renders_grafted_baseline(self, tmp_path):
        from agent.batch_post_optimizer import generate_optimized_report

        all_results = {
            "oos_predictions_x_long_ap|oos_predictions_y_short_ll": {
                "status": "OK",
                "metrics": {"total_pnl": 1.0, "trade_count": 10},
                "optuna_info": {
                    "regression_guard_triggered": True,
                    "long_params": {},
                    "short_params": {},
                    "baseline_side_params": {
                        "long": {"entry_threshold": 0.502254013,
                                 "tp_atr_mult": 4.0, "sl_atr_mult": 3.0,
                                 "cooldown_bars": 9, "atr_period": 36},
                        "short": {"entry_threshold": 0.53,
                                  "tp_atr_mult": 10.0, "sl_atr_mult": 3.0,
                                  "cooldown_bars": 7, "atr_period": 20},
                    },
                    "baseline_metrics": {"total_pnl": 1.0, "trade_count": 10},
                    "holdout_metrics": {},
                },
            },
        }
        # Global base config with DIFFERENT values than the grafted baseline —
        # if the report falls back to it, the assertion below catches it.
        base_cfg = {
            "models": {"long": {"threshold": 0.5}, "short": {"threshold": 0.53}},
            "long": {"tp_atr_mult": 1.5}, "short": {"tp_atr_mult": 10.0},
        }
        md = generate_optimized_report(
            str(tmp_path), {"manifest": "m.json"}, all_results, "ohlcv.parquet",
            base_cfg=base_cfg,
        )
        assert "0.502254013 / 0.53" in md, (
            "guard-triggered ensemble must render the grafted per-pair "
            "baseline threshold, not the global base config")
        assert "| TP ATR Mult | 4.0 / 10.0 |" in md.replace("  ", " ") or \
               "4.0 / 10.0" in md


# ---------------------------------------------------------------------------
# C. Search-space sentinel
# ---------------------------------------------------------------------------

class TestSearchSpaceTier:

    def test_tp_atr_mult_range(self):
        # 8.0 cap restored per user decision 2026-07-12 (was briefly 6.0).
        assert _PARAM_RANGES["tp_atr_mult"] == (4.0, 8.0, 1.0, "float")


# ---------------------------------------------------------------------------
# D. Split trial budgets schema field
# ---------------------------------------------------------------------------

class TestEnsembleTrialsSchema:

    def test_absent_means_inherit(self):
        cfg = OptunaConfig(post_optimizer_holdout_months=12)
        assert cfg.post_optimizer_ensemble_trials is None

    def test_explicit_value_kept(self):
        cfg = OptunaConfig(post_optimizer_holdout_months=12,
                           post_optimizer_ensemble_trials=150)
        assert cfg.post_optimizer_ensemble_trials == 150
