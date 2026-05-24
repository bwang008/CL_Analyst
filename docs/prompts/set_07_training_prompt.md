# EXP-030 Generation: Set_07 Model Training & Optimization

## Objective

Train a new generation of LightGBM models (EXP-030+) on the **set_07 dataset** (`data/processed/CL_set_07.parquet`), which contains ~140 extended features (vs 80 in set_06). Use **Optuna** to find optimal LightGBM hyperparameters that maximize PnL and Sharpe ratio. The best models will replace the current EXP-025 (Long) and EXP-026 (Short) models for live trading.

---

## Context

### What Changed in set_07 (vs set_06)

| Change | Details |
|--------|---------|
| New 1-day window (288 bars) | All per-window clusters now computed at 5 windows: 288, 864, 2016, 4032, 10080 |
| Expanded macro windows | 1D/3D/1W/2W/1M/3M (6 windows, was 1M/3M only) |
| Return distribution cluster | `DIST_SKEW_*`, `DIST_KURT_*`, `DIST_ZSCORE_*` per window |
| Stochastic oscillator | `MOM_STOCH_K_*`, `MOM_STOCH_D_*` per window |
| Chaikin Money Flow | `VOLFLOW_CMF_*` per window |
| Cross-timeframe ratios | `CROSS_VOL_RATIO_*`, `CROSS_TREND_DIFF_*`, `CROSS_VWAP_DIFF_*` |
| Day-of-week encoding | `Time_DayOfWeek_Sin`, `Time_DayOfWeek_Cos` |
| Early stopping (B1) | LGBMLearner now holds out last 10% as validation, stops at 50 rounds no improvement |

### Current Best Models (set_06 baselines to beat)

| Model | Target | Dataset | OOS PnL | Win Rate | PF | Trades |
|-------|--------|---------|--------:|:--------:|---:|-------:|
| EXP-025 (Long) | `TARGET_TRIPLE_2x1_24H_LONG` | set_06 | $594K | 44.7% | 3.21 | 3,940 |
| EXP-026 (Short) | `TARGET_TRIPLE_2x1_24H_SHORT` | set_06_shortfix | $696K | 35.0% | 1.97 | 9,082 |

### Current Hyperparameters (both EXP-025 and EXP-026)
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

---

## Tasks

### Task 1: Verify set_07 Dataset

Before training, verify the dataset is ready:
```bash
conda run -n trader --no-capture-output python -c "
import pandas as pd
df = pd.read_parquet('data/processed/CL_set_07.parquet')
print(f'Shape: {df.shape}')
print(f'Columns ({len(df.columns)}): {sorted(df.columns.tolist())}')
print(f'Date range: {df.index.min()} to {df.index.max()}')
print(f'NaN percentage: {df.isna().mean().mean()*100:.2f}%')
# Check targets exist
for t in ['TARGET_TRIPLE_2x1_24H_LONG', 'TARGET_TRIPLE_2x1_24H_SHORT']:
    if t in df.columns:
        print(f'{t}: {df[t].value_counts().to_dict()}')
"
```

Expected: ~140+ feature columns, ~1.2M rows, targets are binary (0/1).

### Task 2: Optuna LightGBM Hyperparameter Search

Create an Optuna study to find the best LightGBM model hyperparameters. The existing script `agent/optuna_lgbm_search.py` can be used as a reference, but you should adapt it for set_07.

**Search space** (tune these — use the current values as center of ranges):
```python
{
    "num_leaves": trial.suggest_int("num_leaves", 15, 63),
    "min_child_samples": trial.suggest_int("min_child_samples", 50, 300),
    "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
    "feature_fraction": trial.suggest_float("feature_fraction", 0.4, 0.9),
    "bagging_fraction": trial.suggest_float("bagging_fraction", 0.4, 0.9),
    "bagging_freq": trial.suggest_int("bagging_freq", 1, 7),
    "reg_alpha": trial.suggest_float("reg_alpha", 0.1, 10.0, log=True),
    "reg_lambda": trial.suggest_float("reg_lambda", 0.1, 10.0, log=True),
    "max_depth": trial.suggest_int("max_depth", 3, 8),
    "min_gain_to_split": trial.suggest_float("min_gain_to_split", 0.0, 2.0),
    "n_estimators": trial.suggest_int("n_estimators", 500, 2000, step=100),
    "objective": "binary",
    "use_focal": True,  # keep focal loss
    "metric": "binary_logloss",
    "validation_fraction": 0.1,  # NEW: enables early stopping
}
```

**Objective function**: For each trial:
1. Run `train_and_evaluate()` with the trial's params on set_07 data
2. Use `--train-cutoff-date 2022-01-01` so all data after 2022 is true OOS
3. Generate OOS predictions on post-cutoff data
4. Run `BacktestEngine` on OOS predictions with baseline strategy params (TP=5.0 ATR, SL=0.75 ATR) to get PnL and Sharpe

**Optimization targets**: Maximize `total_pnl + sharpe_ratio` (weighted composite) or use multi-objective with both.

**Run two separate studies:**
1. **EXP-030 (Long)**: target = `TARGET_TRIPLE_2x1_24H_LONG`, ~100 trials
2. **EXP-031 (Short)**: target = `TARGET_TRIPLE_2x1_24H_SHORT`, ~100 trials

### Task 3: Train Final Models

Using the best hyperparameters from Task 2:

```bash
# EXP-030: Long model
conda run -n trader --no-capture-output python agent/experiment_runner.py \
  --id EXP-030 \
  --strategy "S_Set07_Long" \
  --data data/processed/CL_set_07.parquet \
  --target TARGET_TRIPLE_2x1_24H_LONG \
  --method walk_forward \
  --balance_mode downsample \
  --train-cutoff-date 2022-01-01

# EXP-031: Short model
conda run -n trader --no-capture-output python agent/experiment_runner.py \
  --id EXP-031 \
  --strategy "S_Set07_Short" \
  --data data/processed/CL_set_07.parquet \
  --target TARGET_TRIPLE_2x1_24H_SHORT \
  --method walk_forward \
  --balance_mode downsample \
  --train-cutoff-date 2022-01-01
```

> **Important**: Update `model_params` in `experiment_runner.py` with the Optuna-found best params before running.

### Task 4: Generate OOS Predictions & Backtest

```bash
# Generate predictions for backtesting
conda run -n trader --no-capture-output python agent/generate_model_predictions.py \
  --model-path models/registry/EXP-030_S_Set07_Long/final_model.pkl \
  --data-path data/processed/CL_set_07.parquet \
  --output reports/exp030_long_predictions.csv \
  --prob-col prob_Buy

conda run -n trader --no-capture-output python agent/generate_model_predictions.py \
  --model-path models/registry/EXP-031_S_Set07_Short/final_model.pkl \
  --data-path data/processed/CL_set_07.parquet \
  --output reports/exp031_short_predictions.csv \
  --prob-col prob_Sell

# Backtest with existing strategy configs
conda run -n trader --no-capture-output python agent/backtest_engine.py \
  --predictions reports/exp030_long_predictions.csv \
  --data data/processed/CL_set_07.parquet \
  --threshold 0.60 --tp-mult 5.0 --sl-mult 0.75

conda run -n trader --no-capture-output python agent/backtest_engine.py \
  --predictions reports/exp031_short_predictions.csv \
  --data data/processed/CL_set_07.parquet \
  --threshold 0.60 --tp-mult 5.0 --sl-mult 0.75
```

### Task 5: Strategy Parameter Optimization (Optional but Recommended)

After the models are trained and predictions generated, use the existing `strategy_optimizer.py` to find optimal TP/SL/trailing/threshold for the new models:

```bash
# Create strategy configs for the new models (see configs/strategies/ensemble2_alt.json as template)
# Then run:
conda run -n trader --no-capture-output python agent/strategy_optimizer.py \
  --config configs/strategies/set07_ensemble.json \
  --n-trials 1000
```

### Task 6: Archive & Compare

1. Archive models to `models/registry/EXP-030_*` and `EXP-031_*` using `agent/archive_model.py`
2. Create a comparison table:

| Metric | EXP-025 (set_06 Long) | EXP-030 (set_07 Long) | EXP-026 (set_06 Short) | EXP-031 (set_07 Short) |
|--------|:-----:|:-----:|:-----:|:-----:|
| Total PnL | $594K | ? | $696K | ? |
| Sharpe Ratio | ? | ? | ? | ? |
| Win Rate | 44.7% | ? | 35.0% | ? |
| Profit Factor | 3.21 | ? | 1.97 | ? |
| Trade Count | 3,940 | ? | 9,082 | ? |

3. Document feature importance changes — which new set_07 features are the model using most?

---

## Key Files Reference

| File | Role |
|------|------|
| `main.py` | Core training pipeline — `train_and_evaluate()`, `get_processed_cl_df()` |
| `agent/experiment_runner.py` | Experiment wrapper with CLI, auto-archiving |
| `agent/optuna_lgbm_search.py` | Optuna LightGBM hyperparameter tuner |
| `agent/strategy_optimizer.py` | Optuna strategy parameter tuner (TP/SL/threshold) |
| `agent/generate_model_predictions.py` | Score dataset with saved model → predictions CSV |
| `agent/backtest_engine.py` | FSM backtester → PnL, Sharpe, PF metrics |
| `agent/archive_model.py` | Archive model + config to `models/registry/` |
| `src/LGBMLearner.py` | LightGBM wrapper — now has early stopping (set `validation_fraction: 0.1`) |
| `src/walk_forward.py` | Walk-forward CV — fold results now include `best_iteration`, `converged_early` |
| `src/features/alpha_factory.py` | Feature generation — set_07 uses `include_extended=True` |
| `configs/strategies/ensemble2_alt.json` | Example ensemble config (Long + Short models) |
| `data/DATASETS.json` | Dataset version documentation |

## Environment

- **Conda env:** `trader`
- **Run commands:** `conda run -n trader --no-capture-output python <script>`
- **OS:** Windows (PowerShell) — do NOT use `&&`, use `;` for chaining
- **Data root:** `C:\CL_Analyst_Data\data\` (via `CL_DATA_ROOT` env var)
- **Workflow:** See `.agents/workflows/run-experiment.md` and `.agents/workflows/run-tests.md`

## Success Criteria

1. EXP-030 (Long) should achieve **higher PnL and/or Sharpe** than EXP-025 baseline ($594K PnL)
2. EXP-031 (Short) should achieve **higher PnL and/or Sharpe** than EXP-026 baseline ($696K PnL)
3. Models must be saved as `.pkl` files compatible with `live_trader.py`
4. The live trader auto-detects set_07 models via sentinel features — no manual config needed
5. All results documented in `AGENT_LOG.md`
6. Training diagnostics (`training_diagnostics.json`) saved for each experiment
