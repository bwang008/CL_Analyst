# Agent Briefing: OOS Training Pipeline Refactor & Verification

## Objective

Refactor the CL Analyst model training and prediction pipeline to properly support **out-of-sample (OOS) predictions** with a hard date cutoff. Remove redundancy between scripts, integrate the feature into the existing pipeline, and verify everything works end-to-end.

## Background & Problem

This is a quantitative trading system for CL (Crude Oil) futures using LightGBM models. The system trains binary classifiers (Buy/Sell signals) and runs backtests to evaluate them before live deployment.

**Critical Issue Found:** The existing prediction generation script (`agent/generate_model_predictions.py`) was scoring the **entire training dataset** with the saved model, producing in-sample predictions. The backtest engine then ran on these and reported inflated metrics (70% WR that drops to 35% on true OOS data). A previous agent added a `--train-cutoff` flag to `generate_model_predictions.py` as a quick fix, but this **duplicates training logic** that already exists in `main.py` and `experiment_runner.py`.

### The Redundancy

- `main.py train_and_evaluate()` — Full training pipeline with walk-forward, fold evaluation, vault metrics, visualizations, model saving
- `agent/experiment_runner.py` — Wraps `main.py` with experiment tracking, auto-archiving to `models/registry/`
- `agent/generate_model_predictions.py` — Was originally just a scoring utility. Now has a bolted-on `--train-cutoff` mode that retrains a model inline (duplicating `main.py`'s training logic without walk-forward, archiving, or proper reporting)

**Goal:** The `--train-cutoff` training logic should live in the proper training pipeline (`experiment_runner.py` / `main.py`), NOT in the prediction generation script. The prediction script should go back to being a pure scoring utility.

---

## Project Structure (Key Files)

### Training Pipeline
| File | Role | Lines |
|------|------|-------|
| [main.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/main.py) | Core training pipeline — `train_and_evaluate()` function. CLI handles `train` command. Uses `WalkForwardSplitter` for gym/vault split. | ~835 |
| [agent/experiment_runner.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/agent/experiment_runner.py) | Experiment wrapper — calls `main.py train_and_evaluate()`, adds experiment ID tracking, auto-archives to `models/registry/` | ~200 |
| [src/walk_forward.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/walk_forward.py) | `WalkForwardSplitter` class — expanding window CV with holdout (vault). `holdout_pct=0.15` takes last 15% as vault. | ~417 |
| [src/LGBMLearner.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/LGBMLearner.py) | LightGBM model wrapper — `add_evidence()`, `query()`, `save()`, `load()`. Supports focal loss. | ~290 |
| [src/util.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/util.py) | `get_feature_columns()`, `get_X_y()`, `downsample_majority()` | — |

### Prediction & Backtesting
| File | Role | Lines |
|------|------|-------|
| [agent/generate_model_predictions.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/agent/generate_model_predictions.py) | **NEEDS REFACTOR** — Score a dataset with a saved model. Has redundant `--train-cutoff` mode that should be removed after proper pipeline support is added. | ~200 |
| [agent/backtest_engine.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/agent/backtest_engine.py) | `BacktestEngine` — FSM backtester. CLI: `--predictions`, `--data`, `--config`. Reads `prob_Buy`/`prob_Sell` columns. | ~1317 |

### Model Registry & Configs
| Path | Contents |
|------|----------|
| `models/registry/EXP-017_S_Ultimate/` | Buy model — `final_model.pkl`, `config.json`, `metrics.json` |
| `models/registry/EXP-020_S_Ultimate_Short/` | Sell model — same structure |
| `configs/strategies/manatee.json` | Buy strategy config (LONG, EXP-017) |
| `configs/strategies/koala.json` | Sell strategy config (SHORT, EXP-020) |
| `configs/strategies/ensemble_conservative.json` | Ensemble combining both |

### Data
| Path | Description |
|------|-------------|
| `data/processed/CL_set_06.parquet` | Training data for buy model (1,127,977 rows, 2009-2024) |
| `data/processed/CL_set_06_shortfix.parquet` | Training data for sell model (1,207,895 rows, 2009-2026) |

### Tests
| Path | Relevance |
|------|-----------|
| `tests/test_cooldown.py` | Tests for cooldown logic + timezone fix (10 tests, all passing) |
| `tests/test_reconnection.py` | Tests for reconnection logic (8 tests) |
| `tests/test_configurable_strategy.py` | Tests for strategy evaluation (17 tests) |
| `tests/test_bracket_order.py` | Tests for order placement (3 pre-existing failures on marketable_limit price calc — unrelated) |

---

## Tasks

### Task 1: Add `--train-cutoff-date` to `experiment_runner.py` / `main.py`

Add support for a hard date cutoff in the training pipeline:

```bash
python agent/experiment_runner.py \
  --id EXP-025 \
  --strategy "S_Ultimate_Short_OOS" \
  --data data/processed/CL_set_06_shortfix.parquet \
  --target TARGET_TRIPLE_2x1_24H_SHORT \
  --method walk_forward \
  --balance_mode downsample \
  --train-cutoff-date 2022-01-01
```

When `--train-cutoff-date` is provided:
1. Filter the input DataFrame to only rows before that date BEFORE passing to `train_and_evaluate()`
2. The walk-forward + vault split then operates on the pre-cutoff data only
3. After training, also generate OOS predictions on post-cutoff data and save them as a separate CSV
4. Archive everything to `models/registry/` as usual

**Implementation approach:** The cleanest place is likely in `main.py train_and_evaluate()` — add a `train_cutoff_date: str | None = None` parameter. If provided, filter `df` early (after loading, before splitting). Then the rest of the pipeline works unchanged. Pass the cutoff through from `experiment_runner.py` CLI args.

Review [main.py lines 107-533](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/main.py#L107-L533) for the `train_and_evaluate()` function, and [agent/experiment_runner.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/agent/experiment_runner.py) for the CLI wrapper.

### Task 2: Clean up `generate_model_predictions.py`

After Task 1 is complete:
1. Remove the `--train-cutoff` mode from `generate_model_predictions.py` — it should go back to being a pure scoring utility
2. Keep the `--oos-start-date` filtering mode (useful for scoring a saved model on a subset of data)
3. The script should only: load model → load data → (optionally filter by date) → predict → save CSV

Review [agent/generate_model_predictions.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/agent/generate_model_predictions.py) for the current state.

### Task 3: Verify End-to-End

1. **Run a training experiment** with the new `--train-cutoff-date` for both models:
   - EXP-017 buy model: `--target TARGET_TRIPLE_2x1_24H_LONG --data data/processed/CL_set_06.parquet --train-cutoff-date 2022-01-01`
   - EXP-020 sell model: `--target TARGET_TRIPLE_2x1_24H_SHORT --data data/processed/CL_set_06_shortfix.parquet --train-cutoff-date 2022-01-01`

2. **Run backtests** on the OOS predictions with the strategy configs:
   ```bash
   python agent/backtest_engine.py --predictions <oos_predictions.csv> --data <parquet> --config configs/strategies/koala.json
   python agent/backtest_engine.py --predictions <oos_predictions.csv> --data <parquet> --config configs/strategies/manatee.json
   ```

3. **Compare results** against the reference OOS numbers from the quick-fix run:
   - Sell model OOS baseline: 35% WR, PF=1.97, $696K PnL, 9,082 trades
   - Buy model OOS baseline: 44.7% WR, PF=3.21, $594K PnL, 3,940 trades

4. **Run tests**: `conda run -n trader --no-capture-output python -m pytest tests/ -v --tb=short`

### Task 4: Review Documentation

Review these files to understand the full system and update documentation if needed:
- [README.md](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/README.md) — project overview
- [HANDOFF.md](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/HANDOFF.md) — current state, known bugs
- [configs/strategies/config_readme.md](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/configs/strategies/config_readme.md) — strategy config documentation

---

## Key Model Parameters (Both Models)

Both EXP-017 and EXP-020 use identical hyperparameters:
```json
{
  "num_leaves": 31,
  "min_child_samples": 166,
  "learning_rate": 0.0524,
  "feature_fraction": 0.694,
  "bagging_fraction": 0.648,
  "bagging_freq": 1,
  "reg_alpha": 2.737,
  "reg_lambda": 7.379,
  "max_depth": 4,
  "min_gain_to_split": 0.990,
  "n_estimators": 1000,
  "objective": "binary",
  "use_focal": true,
  "metric": "binary_logloss"
}
```
Balance mode: `downsample` (majority class downsampled to match minority).

## Environment

- **Conda env:** `trader` — activate with `conda activate trader`
- **Run commands:** `conda run -n trader --no-capture-output python <script>`
- **OS:** Windows (PowerShell)
- **Python:** 3.x with LightGBM, pandas, numpy, joblib

## Workflow Reference

To run tests: see [.agents/workflows/run-tests.md](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/.agents/workflows/run-tests.md)
To run experiments: see [.agents/workflows/run-experiment.md](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/.agents/workflows/run-experiment.md)
