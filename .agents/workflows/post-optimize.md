---
description: Run strategy optimization on all models in a completed batch directory
---

# Post-Optimize Workflow

// turbo-all

## Overview

Runs `vm_post_optimize.sh` on a completed batch directory, which executes a 3-step pipeline natively on the cloud:
1. **Sweep Ensembles**: Cross-pairs all Long and Short base models using a baseline config (`sweep_ensembles.py`)
2. **Top-8 Selection**: Automatically filters and ranks the 256 ensembles based on trade count and holdout PnL (`select_top_ensembles.py`)
3. **Targeted Optimization**: Runs `batch_post_optimizer.py` *only* on the selected top 8 ensemble pairs, optimizing strategy parameters (threshold, TP/SL, trailing, cooldown, hold bars) for the combined ensemble using Optuna.

> [!NOTE]
> Post-optimization runs **automatically** at the end of `run_sweep_batch.ps1`. You only need this workflow for manual re-runs or standalone optimization.

## Prerequisites
- A completed batch directory under `reports/batch_runs/` (e.g., `reports/batch_runs/batch_20260513_1941`)
- The batch must contain `batch_progress.json` and experiment artifact directories

---

## Option A — Cloud (Recommended — Automatic in Batch Pipeline)

When running via `run_sweep_batch.ps1`, post-optimization is **fully automated**: the orchestrator deploys a dynamically-sized VM after all sweep experiments complete.

- **VM sizing**: `n2-standard-{8,16,32,48}` based on experiment count (2 tasks per experiment)
- **Workers**: Automatically matched to VM vCPUs (e.g., n2-standard-16 → 16 workers)
- **IP addresses**: Post-optimizer runs AFTER all sweep VMs are deleted — no IP contention

For **manual** post-optimization (e.g., re-running on a completed batch):
```powershell
& .\gcp\gcp_deploy_optimizer.ps1 `
    -BatchId batch_XXXXXXXX_XXXX `
    -NTrials 500 `
    -HoldoutMonths 6 `
    -MachineType n2-standard-16 `
    -Workers 16
```
(Invoke the script directly with `&` — never prefix `powershell -ExecutionPolicy Bypass`, a safety
classifier blocks it.)

The VM will:
1. Download all experiment artifacts from GCS
2. Run `sweep_ensembles.py` to cross-pair all models and generate `batch_ensemble_pre_opt.md`
3. Run `select_top_ensembles.py` to filter and rank the Top 8 models
4. Run `batch_post_optimizer.py` on the top 8 pairs
5. Upload results (`batch_ensemble_pre_opt.md`, `batch_summary_optimized.md`, `top_8_ensembles.json`) back to GCS
4. Upload results + configs to `gs://cltrainer-optuna-results/batch_optimizer/<batch_id>/`
5. Send Telegram notifications per-task with convergence info (best trial #)
6. Self-shutdown on completion

**Download results after VM terminates:**
```powershell
$batchId = "batch_XXXXXXXX_XXXX"
# Download reports + results
gcloud storage cp "gs://cltrainer-optuna-results/batch_optimizer/$batchId/batch_ensemble_pre_opt.md" "reports\batch_runs\$batchId\"
gcloud storage cp "gs://cltrainer-optuna-results/batch_optimizer/$batchId/top_8_ensembles.json" "reports\batch_runs\$batchId\"
gcloud storage cp "gs://cltrainer-optuna-results/batch_optimizer/$batchId/batch_summary_optimized_sharpe.md" "reports\batch_runs\$batchId\"
gcloud storage cp "gs://cltrainer-optuna-results/batch_optimizer/$batchId/optimization_results_sharpe.json" "reports\batch_runs\$batchId\"
# Download correctly-formatted strategy configs
New-Item -ItemType Directory -Force -Path "reports\batch_runs\$batchId\configs"
gcloud storage cp -r "gs://cltrainer-optuna-results/batch_optimizer/$batchId/batch_configs/*" "reports\batch_runs\$batchId\configs\"
```

**Then validate the downloaded configs (blocking):** run the CONFIG VALIDATION GATE from
[build-symbol-pipeline](build-symbol-pipeline.md) Phase 6 against the batch dir (from the repo root):
```powershell
conda run -n trader python <scratchpad>\validate_batch_configs.py reports\batch_runs\$batchId
```
Exit 0 required (zero configs found = FAIL): each config must resolve via
`resolve_instrument_context`, match the manifest's `baseline.symbol`, carry `models.*.symbol`, and
point at on-disk `model_path`/`predictions_path`.

> [!IMPORTANT]
> Use `n2-standard-*` machines (not `n2-highcpu-*`). Each parallel worker loads the OHLCV parquet (~2GB), so you need sufficient RAM. `n2-highcpu-48` (48GB) will OOM with 24+ workers.

---

## Option B — Local (Slow, ~3-6 hours for 12 targets)

> [!WARNING]
> **C1 residual — this is the target-pairs path.** Local `batch_post_optimizer.py` in target-pairs
> mode (`:1045-1134`) deep-copies the raw CL base config and `agent/strategy_optimizer.py` writes
> the results as `*_opt_*.json` (`:1443-1447`) / `*_hybrid_*.json` (`:1868-1872`) into
> `configs/strategies/` with **no symbol stamping**. **Never ship these `_opt_`/`_hybrid_`
> emissions for a non-CL symbol** — quarantine/delete them and regenerate via
> `agent/generate_ensemble_artifacts.py` (which stamps `execution_symbol` + `models.*.symbol` from
> `baseline.symbol`). See [build-symbol-pipeline](build-symbol-pipeline.md) Phase 5 (C1).

```powershell
python agent/batch_post_optimizer.py `
    --batch-dir reports/batch_runs/batch_XXXXXXXX_XXXX `
    --n-trials 1000 `
    --holdout-months 4 `
    --workers 4

# Generate correctly-formatted strategy configs
python agent/generate_batch_configs.py `
    --batch-dir reports/batch_runs/batch_XXXXXXXX_XXXX
```

> [!NOTE]
> Local runs are limited by CPU cores. With 4 workers on a typical machine, expect ~6 hours for 12 targets × 2 metrics = 24 optimization tasks.

---

## Output Files

Once complete, review the optimized results:
- `reports/batch_runs/<batch_id>/batch_summary_optimized.md` — comparison table with baseline vs optimized metrics, holdout PnL, and best trial convergence info
- `reports/batch_runs/<batch_id>/optimization_results.json` — raw results with full parameter details
- **`reports/batch_runs/<batch_id>/configs/`** — correctly-formatted strategy configs (with all top-level keys) ready for `backtest_engine.py` and `live_trader.py`
- Per-experiment `*_opt.json` configs in each experiment's `registry/canary_output/` directory (legacy, may be missing top-level keys)

## Interpreting Results

- **Best Trial column**: Shows `#N/total` — if N is very high (>80% of total), the optimizer may not have converged and more trials could help
- **PnL (opt h/o)**: Holdout PnL on unseen data — negative values suggest overfitting
- **Trades (opt)**: Very low trade counts (<5) with high PF often indicate overfitting to a few lucky trades
