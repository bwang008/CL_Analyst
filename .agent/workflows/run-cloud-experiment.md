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
Three tiers are available, each with its own manifest:

| Tier | Manifest | Experiments | LGBM Trials | Post-Opt Trials | Use Case |
| ---------- | --------------------------------------------- | ----------- | ----------- | --------------- | ------------------------------------------- |
| **Canary** | `sweep_batch_hourset08_canary.json` | 2 | 50 | 20 | Pipeline validation (~20-30 min) |
| **Scout** | `sweep_batch_hourset08_scout.json` | 8 | 200 | 500 | Moderate exploration, ballpark performance |
| **Production** | `sweep_batch_hourset08_production.json` | 8 | 500 | 1500 | Deep optimization, final model selection |

### 1. Verify no VMs are running (avoid quota conflicts):
```powershell
gcloud compute instances list
```

### 2. Dry run to validate manifest (replace manifest path for your tier):
```powershell
# Canary (fast pipeline test):
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_canary.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun

# Scout (moderate exploration):
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun

# Production (deep optimization):
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_production.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun
```

### 3. Launch the batch (replace manifest for your tier):
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\sweep_batch_hourset08_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
```

### 4. The orchestrator automatically:
   - Deploys VMs across fallback zones (quota-aware concurrency gating by vCPUs and VM count)
   - Monitors via background PS jobs polling every 90s
   - Sends Telegram notifications at key milestones
   - Runs artifact verification gate before VM deletion
   - Captures crash diagnostics on failure
   - Generates consolidated batch summary
   - Deploys a post-optimizer VM, waits for completion, downloads results
   - Sweeps 256 ensemble pairs and automatically selects Top 8
   - Produces final `batch_ensemble_pre_opt.md`, `batch_summary_optimized.md`, and `wall_clock_summary.md`

### 5. Review results:
```
reports/batch_runs/batch_<timestamp>/
├── batch_progress.json              ← live progress tracker
├── batch_summary.md                 ← unoptimized results
├── batch_ensemble_pre_opt.md        ← baseline sweep of 256 ensembles
├── top_8_ensembles.json             ← dynamically selected top ensembles
├── batch_summary_optimized.md       ← MAIN DELIVERABLE
├── wall_clock_summary.md            ← auto-generated timing report
├── optimization_results.json        ← raw optimization data
└── manifest.json                    ← frozen config
```

### Manifest Format
```json
{
  "defaults": {
    "machine_type": "c2-standard-16",
    "provisioning_model": "STANDARD",
    "gcs_data_path": "gs://cltrainer-optuna-results/data/<dataset>.parquet",
    "strategy_config": "hourly_ensemble_008.json",
    "metrics": "logloss,average_precision",
    "timeout_minutes": 240,
    "max_concurrent_vcpus": 288,
    "vcpus_per_vm": 16,
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
- **Sweep machine**: `c2-standard-16` (16 vCPUs, ~64 GB RAM) — migrated from `n2-highcpu-48` as of 2026-05-16
- **Threading**: Auto-detected via `nproc` — `N_WORKERS=4`, `THREADS_PER_WORKER=4` on C2-16 (4×4 = 16 cores)
- **CPU validation**: `vm_sweep_run.sh` will FATAL error if cores don't match the config
- **Post-optimizer**: Dynamically sized `n2-standard-{8,16,32,48}` based on experiment count. Workers auto-matched to vCPUs.
- SPOT VMs can be preempted — use STANDARD for runs that must complete
- **IP address limit**: 8 external IPs per region (pending increase to 30). Post-optimizer runs **after** all sweep VMs are deleted, so it doesn't compete for IPs. When IPs are the bottleneck, add `"max_concurrent_vms": 8` to the manifest `defaults` to cap concurrent VMs directly. GCP Console quota name: **"In-use regional external IPv4 addresses"**.
- Preferred region: **us-west1** (us-central1 can be saturated)
- Zone fallback: pass comma-separated zones to `-Zone` parameter
- **Wall clock summary**: `wall_clock_summary.md` is auto-generated at the end of every batch run
- **Old manifests**: Frozen manifests in `reports/batch_runs/*/manifest.json` may still reference `n2-highcpu-48`. Update `machine_type`, `vcpus_per_vm`, and `max_concurrent_vcpus` before re-running.
