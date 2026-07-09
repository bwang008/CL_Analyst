# Run Vectorized Cloud Batch (Workflow C)

> [!WARNING]
> **DEPRECATED — retired manifests/schema.** The `configs/sweep_batch_hourset09_*.json` manifests
> referenced below no longer exist, and their legacy top-level `defaults` manifest schema fails v2
> validation (`BatchSweepConfig.model_validate` requires `infrastructure` + `baseline` —
> `gcp/batch_orchestrator.py:65-68`, invoked by `run_sweep_batch.ps1:355`). Use
> [run-cloud-batch](run-cloud-batch.md) with a v2 manifest instead. Note: the vectorized
> mechanism itself is NOT removed — `-SweepMode "frictionless"` still exists in
> `gcp/run_sweep_batch.ps1` (`:37`, `:1019`) and flows through the v2 pipeline.

// turbo-all

> [!IMPORTANT]
> This workflow uses the **Decoupled Signal Architecture (Workflow C)** for ensemble selection.
> Instead of running full BacktestEngine simulations for each of the 64 ensemble pairs,
> it uses **frictionless vectorized alpha evaluation** (SNR across [6,12,24,48,72] horizons).
> The Top 8 ensembles are selected by **Peak SNR** (parameter-agnostic), then passed to Optuna.
>
> For the legacy subprocess-based sweep, use `/run-cloud-batch` instead.

## Key Difference from /run-cloud-batch

| Step | Legacy (`/run-cloud-batch`) | Vectorized (`/run-vector-cloud-batch`) |
|------|----------------------------|----------------------------------------|
| Ensemble Sweep | Runs `backtest_engine.py` subprocess per pair (~15 min) | Vectorized log-return SNR evaluation (~5 sec) |
| Selection Metric | Profit Factor from baseline config | Peak SNR across 5 horizons (parameter-agnostic) |
| Signal Floor | 30 trades minimum | 360 signals minimum |
| `-SweepMode` flag | `backtest` (default) | `frictionless` |

Everything else (model training, Optuna optimization, report generation) is identical.

> [!IMPORTANT]
> **Sharpe-only (inherited).** This vector chain shares `gcp/gcp_deploy_optimizer.ps1` and
> `gcp/vm_post_optimize.sh` with `/run-cloud-batch`, so it **inherits the sharpe-only default**
> adopted 2026-07-04 (ticket `drop-sortino-objective_07042026_2301`): no `*_sortino.*` artifacts
> are produced. Roll back per run with `-Objective both`.

## Quick Reference

### Three Batch Tiers

| Tier | Manifest Example | Use Case |
|------|-----------------|----------|
| Fast | `configs/sweep_batch_hourset09_fast.json` | Quick pipeline validation (~10-15 min) |
| Canary | `configs/sweep_batch_hourset09_canary.json` | Standard validation (~20-30 min) |
| Production | `configs/sweep_batch_hourset09_production.json` | Deep optimization, final model selection |

### Launch Command

```powershell
# 1. Dry run (validate manifest, no VMs created)
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset09_fast.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -SweepMode "frictionless" `
    -OptMode "ensemble" `
    -DryRun

# 2. Execute
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset09_fast.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -SweepMode "frictionless" `
    -OptMode "ensemble"
```

> [!NOTE]
> The **two** differences from `/run-cloud-batch` are:
> 1. Adding `-SweepMode "frictionless"` — uses vectorized SNR evaluation instead of backtest subprocesses
> 2. Adding `-OptMode "ensemble"` — jointly optimizes Long+Short thresholds (vs individual per-side optimization)
>
> These flags flow through the entire pipeline:
> `run_sweep_batch.ps1` → `gcp_deploy_optimizer.ps1` → `vm_post_optimize.sh`

### Infrastructure

- **Sweep machines**: Same as `/run-cloud-batch` — `c2-standard-16` (16 vCPUs, ~64 GB RAM)
- **Post-optimizer**: Dynamically sized `n2-standard-{8,16,32,48}` based on experiment count
- **Orchestrator**: `gcp/run_sweep_batch.ps1` — fully automated (deploy → monitor → collect → post-optimize → report)

### Key Scripts (Same Pipeline)

| Script | Purpose |
|--------|---------|
| `gcp/run_sweep_batch.ps1` | **Batch orchestrator** — now accepts `-SweepMode` parameter |
| `gcp/gcp_deploy_optimizer.ps1` | Post-optimizer VM deployment — passes `-SweepMode` through |
| `gcp/vm_post_optimize.sh` | VM-side script — passes `--mode` and `--holdout-months` to sweep |
| `agent/sweep_ensembles.py` | Ensemble evaluation — `--mode frictionless` uses alpha evaluator |
| `agent/alpha_evaluator.py` | **NEW** — vectorized SNR/IC evaluation across multiple horizons |
| `agent/forward_returns.py` | **NEW** — log forward returns computation |
| `agent/select_top_ensembles.py` | Top-N selection — auto-detects frictionless vs legacy report format |

### New Pre-Optimization Report Format

The frictionless sweep generates a term-structure table showing SNR decay across horizons:

```
| Ensemble ID | Long Model | Short Model | Signals | Peak Horizon | SNR_6H | SNR_12H | SNR_24H | SNR_48H | SNR_72H | Hit Rate | IC |
```

### Output

```
reports/batch_runs/batch_<timestamp>/
├── batch_progress.json              ← live progress tracker
├── batch_ensemble_pre_opt.md        ← frictionless sweep results (SNR term structure)
├── top_8_ensembles.json             ← selected by Peak SNR
├── batch_summary_optimized_sharpe.md    ← MAIN DELIVERABLE (post-Optuna)
├── wall_clock_summary.md            ← auto-generated timing report
├── optimization_results_*.json      ← raw optimization data
└── manifest.json                    ← frozen config
```

### Monitoring

The orchestrator automatically monitors all phases. Once results appear on GCS, they are downloaded to the local `reports/batch_runs/` directory. Telegram notifications are sent at key milestones.

### Comparing Results (A/B Test)

After running both workflows on the same HourSet, compare:
1. **Pre-optimization selection**: Which Top 8 did each method pick?
   - Legacy: `batch_ensemble_pre_opt.md` (sorted by Profit Factor)
   - Vectorized: `batch_ensemble_pre_opt.md` (sorted by Peak SNR, shows term structure)
2. **Post-optimization performance**: Do the frictionless-selected ensembles produce better Optuna results?
   - Compare `batch_summary_optimized_sharpe.md` from both runs
