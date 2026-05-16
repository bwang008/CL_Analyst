---
description: Run strategy optimization on all models in a completed batch directory
---

# Post-Optimize Workflow

// turbo-all

## Overview

Runs `batch_post_optimizer.py` on all completed experiments in a batch directory, optimizing strategy parameters (threshold, TP/SL, trailing, cooldown, hold bars, consecutive signals) for each target × metric × direction combination using Optuna (1000 trials per task).

> [!NOTE]
> Post-optimization runs **automatically** at the end of both `run_canary_batch.ps1` and `run_sweep_batch.ps1`. You only need this workflow for manual re-runs or standalone optimization.

## Prerequisites
- A completed batch directory under `reports/batch_runs/` (e.g., `reports/batch_runs/batch_20260513_1941`)
- The batch must contain `batch_progress.json` and experiment artifact directories

---

## Option A — Cloud (Recommended, ~90 min for 12 targets)

Deploys an `n2-standard-32` VM (32 vCPUs, 128GB RAM) with 24 parallel workers.

```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_optimizer.ps1 `
    -BatchId batch_XXXXXXXX_XXXX `
    -NTrials 1000 `
    -HoldoutMonths 4 `
    -Workers 24
```

The VM will:
1. Download all experiment artifacts from GCS
2. Run 24 optimizations in parallel (12 targets × 2 metrics)
3. **Generate correctly-formatted strategy configs** from optimization results
4. Upload results + configs to `gs://cltrainer-optuna-results/batch_optimizer/<batch_id>/`
5. Send Telegram notifications per-task with convergence info (best trial #)
6. Self-shutdown on completion

**Download results after VM terminates:**
```powershell
$batchId = "batch_XXXXXXXX_XXXX"
# Download optimization report + results
gcloud storage cp "gs://cltrainer-optuna-results/batch_optimizer/$batchId/batch_summary_optimized.md" "reports\batch_runs\$batchId\"
gcloud storage cp "gs://cltrainer-optuna-results/batch_optimizer/$batchId/optimization_results.json" "reports\batch_runs\$batchId\"
# Download correctly-formatted strategy configs
New-Item -ItemType Directory -Force -Path "reports\batch_runs\$batchId\configs"
gcloud storage cp -r "gs://cltrainer-optuna-results/batch_optimizer/$batchId/batch_configs/*" "reports\batch_runs\$batchId\configs\"
```

> [!IMPORTANT]
> Use `n2-standard-32` (128GB RAM), NOT `n2-highcpu`. 24 parallel workers each load the OHLCV parquet (~2GB each), requiring ~50GB+ total RAM. The `n2-highcpu-48` (48GB) will OOM.

---

## Option B — Local (Slow, ~3-6 hours for 12 targets)

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

- **Best Trial column**: Shows `#N/1000` — if N is very high (>900), the optimizer may not have converged and more trials could help
- **PnL (opt h/o)**: Holdout PnL on unseen data — negative values suggest overfitting
- **Trades (opt)**: Very low trade counts (<5) with high PF often indicate overfitting to a few lucky trades
