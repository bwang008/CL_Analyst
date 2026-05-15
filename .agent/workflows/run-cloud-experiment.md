---
description: How to run an Optuna hyperparameter search on GCP using the canary deploy + monitor wrapper
---

# Run Cloud Experiment Workflow

// turbo-all

## Prerequisites
- Google Cloud SDK installed and authenticated (`gcloud auth login`)
- Project set: `gcloud config set project cltrainer`
- Data uploaded to GCS: `gs://cltrainer-optuna-results/data/<dataset>.parquet`
- Working directory: `c:\Users\bwang\Documents\GitHub\CL_Analyst_Development`

---

## Batch Sweep Run (Recommended — Fully Automated)

The batch orchestrator manages N experiments end-to-end: deploy → monitor → collect → post-optimize.

### 1. Verify no VMs are running (avoid quota conflicts):
```powershell
gcloud compute instances list
```

### 2. Dry run to validate manifest:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_canary.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun
```

### 3. Launch the batch:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_canary.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
```

### 4. The orchestrator automatically:
   - Deploys VMs across fallback zones (quota-aware, up to 2 concurrent)
   - Monitors via background PS jobs polling every 90s
   - Sends Telegram notifications at key milestones
   - Runs artifact verification gate before VM deletion
   - Captures crash diagnostics on failure
   - Generates consolidated batch summary
   - Deploys a post-optimizer VM, waits for completion, downloads results
   - Produces final `batch_summary_optimized.md`

### 5. Review results:
```
reports/batch_runs/batch_<timestamp>/
├── batch_progress.json              ← live progress tracker
├── batch_summary.md                 ← unoptimized results
├── batch_summary_optimized.md       ← MAIN DELIVERABLE
├── optimization_results.json        ← raw optimization data
└── manifest.json                    ← frozen config
```

### Manifest Format
```json
{
  "defaults": {
    "machine_type": "n2-highcpu-48",
    "provisioning_model": "STANDARD",
    "gcs_data_path": "gs://cltrainer-optuna-results/data/<dataset>.parquet",
    "strategy_config": "hourly_ensemble_008.json",
    "metrics": "logloss,average_precision",
    "timeout_minutes": 240,
    "max_concurrent_vcpus": 96,
    "vcpus_per_vm": 48,
    "post_optimizer_trials": 200,
    "post_optimizer_holdout_months": 6
  },
  "experiments": [
    {
      "label": "HS08 3x1 12H",
      "target_long": "TARGET_TRIPLE_3x1_12H_LONG",
      "target_short": "TARGET_TRIPLE_3x1_12H_SHORT",
      "gcs_prefix": "sweep_hs08_3x1_12h"
    }
  ]
}
```

---

## Single Experiment Run (Manual — Legacy)

### 1. Delete any existing canary VM:
```powershell
gcloud compute instances delete optuna-runner-canary --zone=us-central1-a --quiet
```

### 2. Deploy:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_sweep.ps1 `
    -TargetLong TARGET_TRIPLE_3x1_12H_LONG `
    -TargetShort TARGET_TRIPLE_3x1_12H_SHORT `
    -ProvisioningModel STANDARD
```

### 3. Monitor:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_monitor.ps1 `
    -VmName optuna-runner-canary `
    -GcsPrefix canary `
    -ExperimentLabel "Single Sweep" `
    -PollIntervalSeconds 120
```

### 4. Clean up:
```powershell
gcloud compute instances delete optuna-runner-canary --zone=us-central1-a --quiet
```

---

## Quick Status Check
```powershell
.\gcp\gcp_check_status.ps1 -VmName <vm-name>
```

## View Live Output (SSH)
```powershell
gcloud compute ssh <vm-name> --zone=<zone> --command="tmux attach -t sweep"
# Ctrl+B then D to detach without stopping
```

---

## Code Upload Reference

Both deploy scripts (`gcp_deploy_sweep.ps1` and `gcp_deploy_optimizer.ps1`) upload these `src/` files to the VM:

| File | Required By |
|------|-------------|
| `src/__init__.py` | Package init |
| `src/util.py` | `backtest_engine.py` (get_X_y) |
| `src/LGBMLearner.py` | `backtest_engine.py` (model loading) |
| `src/data_paths.py` | `backtest_engine.py` (lazy import for fallback data loading) |
| `src/data_processor.py` | `experiment_runner.py` (data processing) |
| `src/live_execution/strategies/execution_models.py` | `backtest_engine.py` (strategy factory) |
| `src/live_execution/strategies/configurable_strategy.py` | Strategy execution |
| `src/features/feature_buckets.py` | `optuna_lgbm_search_v2.py` (bucket toggling) |

> **IMPORTANT**: When adding new `from src.*` imports to any `agent/` file that runs on a VM, you MUST also add the file to the `$codeFiles` array in both `gcp_deploy_sweep.ps1` and `gcp_deploy_optimizer.ps1`. Failure to do so will cause `ModuleNotFoundError` on the VM.

## Important Notes
- Machine: `n2-highcpu-48` (48 vCPUs, ~48 GB RAM)
- Default config: `N_WORKERS=4`, `THREADS_PER_WORKER=12` (4 × 12 = 48 cores)
- CPU validation will FATAL error if cores don't match the config
- SPOT VMs can be preempted — use STANDARD for runs that must complete
- Preferred region: **us-west1** (us-central1 can be saturated)
- Zone fallback: pass comma-separated zones to `-Zone` parameter
