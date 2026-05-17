---
description: Run strategy optimization on all models in a completed batch directory
---

# Post-Optimize Workflow

// turbo-all

## Overview

Runs `batch_post_optimizer.py` on all completed experiments in a batch directory, optimizing strategy parameters (threshold, TP/SL, trailing, cooldown, hold bars, consecutive signals) for each target × metric × direction combination using Optuna. Trial count is set per-tier in the manifest (canary: 20, scout: 500, production: 1500).

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
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_optimizer.ps1 `
    -BatchId batch_XXXXXXXX_XXXX `
    -NTrials 500 `
    -HoldoutMonths 6 `
    -MachineType n2-standard-16 `
    -Workers 16
```

The VM will:
1. Download all experiment artifacts from GCS
2. Run optimizations in parallel (targets × 2 metrics)
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
> Use `n2-standard-*` machines (not `n2-highcpu-*`). Each parallel worker loads the OHLCV parquet (~2GB), so you need sufficient RAM. `n2-highcpu-48` (48GB) will OOM with 24+ workers.

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

- **Best Trial column**: Shows `#N/total` — if N is very high (>80% of total), the optimizer may not have converged and more trials could help
- **PnL (opt h/o)**: Holdout PnL on unseen data — negative values suggest overfitting
- **Trades (opt)**: Very low trade counts (<5) with high PF often indicate overfitting to a few lucky trades
