# HANDOFF

## Current Branch
- `main`

## Last Completed Task
- **Model Investigation & Bug Fixes (2026-03-13)**: Fixed archive path bug, SingleModelStrategy threshold bug, case-insensitive prediction column matching. Created `ensemble3_3.json` (best config: $2.66M PnL, 4.01 PF, 46.1% WR over 50 months). Added `scripts/extract_feature_importance.py` and auto-extraction to archive process.

## Current Known Bugs / Issues
- **`trailing_atr_mult = 0.0` bug**: Setting this to 0 does not disable the trailing stop as expected — it triggers immediately. Workaround: set to a large value (e.g. 99.0) to effectively disable.
- **Evaluator naming**: `reports/vault_metrics.json` uses class names `{1: "Buy", 2: "Sell"}` even for binary short targets. For `TARGET_TRIPLE_2x1_24H_SHORT`, the "Buy" slot corresponds to the positive short label.
- **Binary probabilities**: With focal loss custom objective, LightGBM `predict()` may emit logits (not 0-1). The live trader applies sigmoid transform; use `agent/threshold_sweep_binary.py` (sigmoid-aware) for binary sweeps.
- **Data coverage**: processed datasets start at `2009-01-15…` in `set_06`; true 2008-era OOS requires earlier data coverage in processed parquet.
- **Optuna `--n-jobs 3` on Windows**: SQLite locking causes crashes around 20-50 trials. Use `--n-jobs 2` on Windows. Error log goes to `models/optuna_studies/{study_name}_errors.log`.
- **Live-backtest parity gap**: Live predictions may differ significantly from OOS predictions. Old `ensemble2_alt` was spamming sells every bar (live prob much higher than OOS). Monitor with `scripts/plot_prediction_distributions.py`.

## Execution Strategies (Critical Rules)
All strategies are in `src/live_execution/strategies/execution_models.py`.

| Strategy | `execution_class` | Threshold Source | Behavior |
|---|---|---|---|
| **SingleModelStrategy** | _(default when unset)_ | `models.{direction}.threshold` → `entry_threshold` → **1.0 (no trades + warning)** | Single direction only |
| **ConservativeEnsembleStrategy** | `"ConservativeEnsembleStrategy"` | `models.long.threshold` / `models.short.threshold` | Dual-model, no position flipping |
| **AggressiveEnsembleStrategy** | `"AggressiveEnsembleStrategy"` | Same as Conservative | Dual-model WITH position flipping |
| **TieredEnsembleStrategy** | `"TieredEnsembleStrategy"` | Per-tier `min_prob` | Per-tier TP/SL/trailing overrides |

> **IMPORTANT**: Strategies ONLY decide when to enter and in which direction. All trade management (TP/SL/trailing/cooldown/time barrier) is handled by `BacktestEngine` from the config. Strategies are purely signal filters.

> **IMPORTANT**: Prediction column matching is case-insensitive (via `_resolve_prob_column` in `backtest_engine.py`). In dual-model merge, if a long CSV has no 'buy' column or short CSV has no 'sell' column, a `ValueError` is raised — no silent fallback.

## Strategy Config Structure
Configs are in `configs/strategies/`. Reference: `configs/strategies/config_readme.md`.

**Single-model** (manatee.json, koala.json):
```json
{
    "nickname": "Manatee", "direction": "LONG",
    "entry_threshold": 0.60, "tp_atr_mult": 3.0, "sl_atr_mult": 1.5,
    "sizing_tiers": {"0.80": 3, "0.70": 2, "0.60": 1},
    "live_config": {"experiment_id": "EXP-017_S_Ultimate", "client_id": 10}
}
```

**Ensemble** (ensemble3_3.json — current best):
```json
{
    "nickname": "Ensemble3_3",
    "execution_class": "ConservativeEnsembleStrategy",
    "models": {
        "long": {"experiment_id": "EXP-033_optuna_v2_set08_154feat_logloss", "predictions_path": "models/registry/.../oos_predictions.csv", "threshold": 0.60},
        "short": {"experiment_id": "EXP-032_optuna_v2_set08_short_logloss", "predictions_path": "models/registry/.../oos_predictions.csv", "threshold": 0.60}
    },
    "sizing_tiers": {"0.80": 3, "0.70": 2, "0.60": 1},
    "live_config": {"client_id": 18}
}
```

> **Note:** `predictions_path` is used by `backtest_engine.py` (auto-resolve predictions from config). `experiment_id` is used by the live trader to load models from the registry.

## Client ID Assignments
| Config | client_id |
|--------|-----------|
| manatee.json | 10 |
| koala.json | 11 |
| manatee_single.json | 12 |
| ensemble_conservative.json | 13 |
| ensemble2_alt.json | 15 |
| manatee3.json | 16 |
| koala3.json | 17 |
| ensemble3.json / ensemble3_3.json | 18 |
| ensemble3_3_conservative.json | 20 |

## Feature Importance
- **Auto-extracted during archive**: `archive_model.py` now auto-extracts `feature_importance.csv` from the PKL Booster object when no CSV is explicitly provided.
- **Manual extraction**: `python scripts/extract_feature_importance.py EXP-ID --top 20 --filter EXHAUST --save`
- **PKL format**: Models are stored as `{"model": <Booster>, "feature_names": [...], "n_features_in_": int, "params": {...}}`. Use `model.feature_importance(importance_type='gain')` for Booster, `model.feature_importances_` for LGBMClassifier.

## Dataset Feature Counts
| Dataset | Features | EXHAUST | Notes |
|---------|----------|---------|-------|
| set_06 | 82 | 0 | Original |
| set_07 | 141 | 0 | Added extended features |
| set_08 | 156 | 15 | Added exhaustion features |

## Data File Requirements
- **Shared raw data**: The CL seed CSV (`cl-5m_bk.csv`, ~72 MB) is **not stored in git** (too large). Set `CL_DATA_ROOT` env var to the shared data folder. Code falls back to `data/raw/cl-5m_bk.csv` if unset.
- **Model registry**: `models/registry/` **tracked by git** and appears in worktrees/clones.
- **Processed data** (`data/processed/*.parquet`): Generated by `DataProcessor`. Not in git — regenerate as needed.

## Immediate Next Steps
- Increase Optuna search range/trials and run on GCP cloud VM
- Run Optuna config/strategy optimization on best models (EXP-032/033)
- Run backtesting with live system data to verify parity
- Verify trade execution, config parameter handling (SL, concurrent rules, sizing tiers)
- Address `trailing_atr_mult = 0.0` bug
