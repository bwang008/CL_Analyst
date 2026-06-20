# Run Cloud Batch Experiment Workflow

// turbo-all

> [!IMPORTANT]
> **This workflow is superseded by the unified cloud experiment workflow.**
> Use the `/run-cloud-experiment` workflow instead, which covers all three tiers (canary, scout, production) with the current C2-16 infrastructure.
>
> See: `.agent/workflows/run-cloud-experiment.md`

## Quick Reference

### Three Batch Tiers

| Tier | Manifest | Targets | LGBM Trials | Post-Opt Trials | Use Case |
|------|---------|---------|-------------|-----------------|----------|
| Canary | `configs/sweep_batch_hourset08_canary.json` | 2 | 50 | 20 | Pipeline validation (~20-30 min) |
| Scout | `configs/sweep_batch_hourset08_scout.json` | 8 | 200 | 500 | Moderate exploration, ballpark performance |
| Production | `configs/sweep_batch_hourset08_production.json` | 8 | 500 | 1500 | Deep optimization, final model selection |

### Launch Command
```powershell
# 1. Dry run
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun

# 2. Execute
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
```

### Infrastructure

- **Sweep machine**: `c2-standard-16` (16 vCPUs, ~64 GB RAM)
- **Max concurrency**: 288 vCPUs (18 C2-16 VMs) + `max_concurrent_vms: 8` (IP gate)
- **IP quota**: 8 per region (pending increase to 30). Post-optimizer runs AFTER sweep VMs are deleted.
- **Post-optimizer**: Dynamically sized `n2-standard-{8,16,32,48}` based on experiment count
- **Orchestrator**: `gcp/run_sweep_batch.ps1` — fully automated (deploy → monitor → collect → post-optimize → report)

### Key Scripts

| Script | Purpose |
|--------|---------|
| `gcp/run_sweep_batch.ps1` | **Batch orchestrator** — quota-aware, multi-zone, auto post-optimize |
| `gcp/gcp_deploy_sweep.ps1` | Single VM deployment |
| `gcp/gcp_deploy_optimizer.ps1` | Post-optimizer VM deployment |
| `gcp/vm_sweep_run.sh` | VM-side sweep script |
| `gcp/collect_batch_results.ps1` | Manual result aggregation (auto in orchestrator) |

### Output
```
reports/batch_runs/batch_<timestamp>/
├── batch_progress.json              ← live progress tracker
├── batch_summary.md                 ← unoptimized results
├── batch_summary_optimized.md       ← MAIN DELIVERABLE
├── sharpe_ensemble_backtests.md     ← full backtest dumps for sharpe ensembles
├── sortino_ensemble_backtests.md    ← full backtest dumps for sortino ensembles
├── wall_clock_summary.md            ← auto-generated timing report
├── optimization_results.json        ← raw optimization data
├── configs/                         ← backtest-ready config JSONs per ensemble
├── predictions/                     ← merged prediction CSVs per ensemble
└── manifest.json                    ← frozen config
```
