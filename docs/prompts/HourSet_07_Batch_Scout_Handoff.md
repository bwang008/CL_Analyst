# HourSet_07 Batch Scout — Target Inventory & Agent Handoff

## 1. Available Training Targets in `CL_HourSet_07.parquet`

There are **32 TARGET columns** total. Excluding `_MULTI` (3-class) and `_RET_*` (regression), the actionable binary Long/Short pairs are:

| # | Barrier Config | Hold Period | Long Target | Short Target |
|---|---|---|---|---|
| 1 | 1.5x TP / 1.0x SL | 24H | `TARGET_TRIPLE_1p5x1_24H_LONG` | `TARGET_TRIPLE_1p5x1_24H_SHORT` |
| 2 | 1.0x TP / 0.5x SL | 3H | `TARGET_TRIPLE_1x0.5_3H_LONG` | `TARGET_TRIPLE_1x0.5_3H_SHORT` |
| 3 | 1.0x TP / 0.5x SL | 6H | `TARGET_TRIPLE_1x0.5_6H_LONG` | `TARGET_TRIPLE_1x0.5_6H_SHORT` |
| 4 | 1.0x TP / 0.5x SL | 12H | `TARGET_TRIPLE_1x0.5_12H_LONG` | `TARGET_TRIPLE_1x0.5_12H_SHORT` |
| 5 | 1.0x TP / 2.0x SL | 3H | `TARGET_TRIPLE_1x2_3H_LONG` | `TARGET_TRIPLE_1x2_3H_SHORT` |
| 6 | 1.0x TP / 2.0x SL | 6H | `TARGET_TRIPLE_1x2_6H_LONG` | `TARGET_TRIPLE_1x2_6H_SHORT` |
| 7 | 1.0x TP / 2.0x SL | 12H | `TARGET_TRIPLE_1x2_12H_LONG` | `TARGET_TRIPLE_1x2_12H_SHORT` |
| 8 | 2.0x TP / 1.0x SL | 3H | `TARGET_TRIPLE_2x1_3H_LONG` | `TARGET_TRIPLE_2x1_3H_SHORT` |
| 9 | 2.0x TP / 1.0x SL | 6H | `TARGET_TRIPLE_2x1_6H_LONG` | `TARGET_TRIPLE_2x1_6H_SHORT` |
| 10 | 2.0x TP / 1.0x SL | 12H | `TARGET_TRIPLE_2x1_12H_LONG` | `TARGET_TRIPLE_2x1_12H_SHORT` |

## 2. Manifest Created

A batch manifest has been created at:
`configs/canary_batch_hourset07_scout.json`

It sweeps all 10 target pairs with **both `logloss` and `average_precision`** metrics on `n2-highcpu-48` SPOT VMs (max 2 concurrent).

## 3. GCS Data Confirmation

`cl-1h_bk_HourSet_07.parquet` is already uploaded at:
```
gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_07.parquet
```

## 4. Strategy Optimization Step

After the E2E pipeline trains the model and runs the initial backtest with default strategy parameters, there is an additional **execution parameter optimization** step that should be run. This uses Optuna to search over trading strategy parameters (thresholds, cooldown, max hold bars, etc.) to find the best configuration for each trained model.

### Scripts Involved

| Script | Purpose | Search Space |
|---|---|---|
| `gcp/vm_e2e_pipeline.py` → `optimize_ensemble_params()` | **Restricted** optimization built into the E2E pipeline. Runs automatically when `--opt-trials > 0` and `--holdout-cutoff-date` is passed. | `entry_threshold`, `cooldown_bars`, `max_hold_bars`, `consecutive_signal_threshold` (TP/SL frozen) |
| `agent/strategy_optimizer.py` | **Full** standalone optimization (can be run locally after artifacts are downloaded). | All of the above PLUS `tp_atr_mult`, `sl_atr_mult`, `trailing_atr_mult` |

### How It Works in the Pipeline

The E2E pipeline (`vm_e2e_pipeline.py`) has this built-in at line 299. When activated:
1. It uses the **validation split** predictions (requires 3-way split via `--holdout-cutoff-date`)
2. Runs an Optuna search over restricted execution params (threshold, cooldown, hold bars)
3. Saves the optimized config to `canary_output/lab/optimized_ensemble_cfg.json`
4. Then runs the final backtest on the **holdout** split with the optimized config

This produces a **before** (base strategy config) and **after** (optimized config) comparison automatically.

### Enabling It for This Batch

To activate this in the batch run, the deploy script needs `--opt-trials` and `--holdout-cutoff-date` passed through. These can be set either:
- **In the manifest defaults** (not currently supported — manifest doesn't pass these through)
- **By modifying `gcp_deploy_canary.ps1`** to pass them, OR
- **By running `agent/strategy_optimizer.py` locally** after downloading batch results

**Recommended approach for this scout batch**: Run the batch as-is (default 2-way split), then after downloading results, run the standalone strategy optimizer locally on the winners:

```powershell
python agent/strategy_optimizer.py `
  --config configs/strategies/hourly_ensemble_007.json `
  --n-trials 500 `
  --predictions reports/<gcs_prefix>/canary_output/oos_predictions_long_logloss.csv `
  --data data/processed/CL_HourSet_07.parquet
```

## 5. Where to Find Key Data Points for the Report

| Data Point | Source File | How to Extract |
|---|---|---|
| Wall clock time (total batch) | `reports/batch_runs/<BATCH_ID>/batch_progress.json` | `.started_at` and `.completed_at` fields |
| Wall clock time (per experiment) | `reports/batch_runs/<BATCH_ID>/batch_progress.json` | `.experiments[].wall_time_min` field |
| Wall clock time (pipeline) | `canary_output/pipeline_summary.json` | `.wall_time_seconds` field |
| Backtest metrics (PnL, PF, WR, trades, DD) | `canary_output/pipeline_summary.json` | `.backtest_results.{long,short,ensemble}_*` |
| Feature count | `canary_output/registry/*/feature_importance.csv` | Row count of the CSV |
| Feature importance (top/bottom 10) | `canary_output/registry/*/feature_importance.csv` | Sort by `importance` column, head/tail 10 |
| Model params (num_leaves, max_depth, etc.) | `canary_output/registry/*/experiment_config.json` | `.model_params` object |
| Early stopping / convergence | `canary_output/registry/*/final_model.pkl` | Load with `joblib`, check `model.best_iteration` vs `model.num_trees()` |
| Probability distribution (min/max) | `canary_output/oos_predictions_{direction}_{metric}.csv` | `prob_Buy` or `prob_Sell` column min/max/mean |
| Optimized strategy params | `canary_output/lab/optimized_ensemble_cfg.json` | Full optimized config |

---

## 6. Agent Handoff Prompt — Part 1 (Batch Execution)

Copy this prompt and hand to Agent 1:

---

### PROMPT 1 START

**Objective**: Run the HourSet_07 scout batch sweep across all 10 target pairs.

**Context**: The `CL_HourSet_07.parquet` dataset has been corrected for data leakage and broken features. We are sweeping all available targets with both `logloss` and `average_precision` metrics to identify which barrier configs have genuine out-of-sample edge.

**Follow the `/run-cloud-experiment` workflow.**

**Step 1 — Launch the Batch**

```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_canary_batch.ps1 -ManifestPath configs\canary_batch_hourset07_scout.json -EnableTelegram
```

This launches 10 experiments (2 concurrent VMs at a time, ~90 min timeout each). The batch orchestrator handles:
- Fresh VM per experiment (clean state)
- Quota-aware scheduling (100 vCPU cap, 48 per VM)
- Artifact verification gate before VM deletion
- Telegram notifications at milestones

**Step 2 — Monitor and Wait**

The batch orchestrator will print progress as experiments complete. Wait for the batch to finish. Note the `<BATCH_ID>` printed at the start (format: `batch_YYYYMMDD_HHMM`).

**Step 3 — Collect Results**

After the batch completes:

```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\collect_batch_results.ps1 -BatchId <BATCH_ID>
```

Results will be downloaded to `reports/batch_runs/<BATCH_ID>/`.

**Step 4 — Verify Artifacts**

Confirm that each experiment directory under `reports/` has:
- `pipeline_summary.json`
- `canary_output/registry/` with model `.pkl` files
- `canary_output/oos_predictions_*.csv` files
- `canary_output/registry/*/feature_importance.csv` files

Report any experiments that failed or have missing artifacts.

**Done.** Hand off to Part 2 once all artifacts are confirmed downloaded.

### PROMPT 1 END

---

## 7. Agent Handoff Prompt — Part 2 (Analysis & Report)

Copy this prompt and hand to Agent 2 after Part 1 completes:

---

### PROMPT 2 START

**Objective**: Analyze the HourSet_07 batch scout results, run strategy optimization on winners, and generate a comprehensive report.

**Context**: A batch sweep of 10 target pairs × 2 metrics (logloss + average_precision) has completed. Results are downloaded to `reports/batch_runs/<BATCH_ID>/` and individual experiment output directories under `reports/scout_hs07_*/`. The manifest used is `configs/canary_batch_hourset07_scout.json`.

**Step 1 — Run Strategy Optimization on Winners**

For every experiment where the ensemble Profit Factor > 1.0 in `pipeline_summary.json`, run the standalone strategy optimizer:

```powershell
python agent/strategy_optimizer.py `
  --config <path_to_ensemble_config_from_canary_output> `
  --n-trials 500 `
  --predictions <path_to_merged_oos_predictions> `
  --data data/processed/CL_HourSet_07.parquet
```

The optimizer script is at `agent/strategy_optimizer.py`. It automatically:
1. Runs a **baseline** backtest with the unmodified config
2. Searches 500 trials over execution parameters (multi-objective: maximize PF, minimize drawdown)
3. Saves the **optimized** config as `*_opt.json` alongside the original
4. Prints a side-by-side **BASELINE vs OPTIMIZED** comparison
5. Saves all trial results to `reports/strategy_optimization_*.csv`

The optimized result should always be ≥ the baseline.

**Step 2 — Generate the Report**

Create the report at: `reports/HourSet_07_Batch_Scout_Report.md`

The report MUST follow this exact structure:

```markdown
# HourSet_07 Batch Scout Report

> Generated: <timestamp>
> Manifest: configs/canary_batch_hourset07_scout.json
> Dataset: cl-1h_bk_HourSet_07.parquet (242 columns)
> Train Cutoff: 2023-01-01
> Total Wall Clock Time: <HH:MM from batch_progress.json started_at to completed_at>
> Experiments Run: 10 target pairs × 2 metrics = 20 model pairs

## Executive Summary & Rankings

Rank all experiments by **ensemble Profit Factor** (descending). Include both logloss and precision variants. Each row is a clickable anchor to the detailed section below.

| Rank | Model Name | Target Pair | Metric | Trades | WR | PF | PnL | Max DD | Sharpe | Threshold | Opt PF | Opt PnL | Wall Time | Verdict |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 1 | [HS07_2x1_6H_logloss](#2x1-6h) | 2x1 6H | logloss | ... | ... | ... | ... | ... | ... | ... | ... | ... | Xm | ✅ Promote |

Model Name links should jump to the detailed section for that target pair.

Verdict criteria:
- ✅ Promote: PF > 1.3 AND trades >= 10
- ⚠️ Thin: PF > 1.0 but trades < 10 (needs more data)
- ❌ No Edge: PF <= 1.0

---

## Detailed Results per Target

For EACH of the 10 target pairs, create TWO sub-sections (one per direction). Use the target pair name as an anchor.

### <a id="2x1-6h"></a>2x1 6H

**Wall Clock Time**: Xm Ys (from pipeline_summary.json wall_time_seconds)

#### LONG — TARGET_TRIPLE_2x1_6H_LONG

**Logloss Model**:

| Metric | Value |
|---|---|
| Trades | ... |
| Win Rate | ...% |
| Profit Factor | ... |
| Net PnL | $... |
| Max Drawdown | $... |
| Features Trained | ... |
| n_estimators (config) | ... |
| best_iteration (actual) | ... |
| Early Stopped? | Yes/No |
| num_leaves | ... |
| max_depth | ... |
| learning_rate | ... |
| feature_fraction | ... |
| Probability Min | ... |
| Probability Max | ... |
| Probability Mean | ... |
| Probability Median | ... |
| Signals Above Threshold | ... |

**Top 10 Features** (by gain importance):
| Rank | Feature | Importance |
|---|---|---:|
| 1 | ... | ... |
| ... | ... | ... |

**Bottom 10 Features**:
| Rank | Feature | Importance |
|---|---|---:|
| ... | ... | ... |

**Average Precision Model**:
(Same format as Logloss above)

---

#### SHORT — TARGET_TRIPLE_2x1_6H_SHORT

(Same format as LONG above, for both logloss and average_precision)

---

#### Ensemble Results

| Metric | Logloss Ensemble | Avg Precision Ensemble |
|---|---:|---:|
| Trades | ... | ... |
| Win Rate | ... | ... |
| Profit Factor | ... | ... |
| Net PnL | ... | ... |
| Max Drawdown | ... | ... |

#### Strategy Optimization (if baseline PF > 1.0)

| | Baseline | Optimized | Delta |
|---|---:|---:|---:|
| Profit Factor | ... | ... | +... |
| Net PnL | ... | ... | +$... |
| Max Drawdown | ... | ... | ... |
| Threshold | ... | ... | ... |
| TP ATR Mult | ... | ... | ... |
| SL ATR Mult | ... | ... | ... |
| Trailing ATR | ... | ... | ... |
| Max Hold Bars | ... | ... | ... |

---

(Repeat for all 10 target pairs)

---

## Observations & Recommendations

Summarize:
1. Which barrier configs showed genuine edge
2. Whether logloss or average_precision produced better models
3. Which hold periods (3H/6H/12H/24H) performed best
4. Whether long or short models dominated
5. How much strategy optimization improved results vs baseline
6. Probability distribution health — which models had compressed vs spread distributions
7. Early stopping patterns — which models converged vs maxed out
8. Top recurring features across winning models
9. Final recommendation: which targets to promote to full production runs
```

**Where to find each data point:**
- Wall clock time (total): `reports/batch_runs/<BATCH_ID>/batch_progress.json` → `.started_at` / `.completed_at`
- Wall clock time (per exp): same file → `.experiments[].wall_time_min`, also `pipeline_summary.json` → `.wall_time_seconds`
- Backtest metrics: `pipeline_summary.json` → `.backtest_results`
- Feature count: row count of `registry/*/feature_importance.csv`
- Feature importance top/bottom 10: sort `feature_importance.csv` by `importance` column
- Model params: `registry/*/experiment_config.json` → `.model_params`
- Early stopping: load `.pkl` with `joblib`, check `model.best_iteration` vs `model.num_trees()` (see README.md "Model Diagnostics" section)
- Probability distribution: read `oos_predictions_*.csv`, compute min/max/mean/median of `prob_Buy` or `prob_Sell` column
- Sharpe: if not in pipeline_summary, compute from equity curve or note as N/A
- Strategy optimization: `agent/strategy_optimizer.py` output → `*_opt.json` and `reports/strategy_optimization_*.csv`
- Use the `/report-template` workflow for additional column reference if needed.

### PROMPT 2 END
