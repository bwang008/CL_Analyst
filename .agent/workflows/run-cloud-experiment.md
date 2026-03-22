---
description: How to run an Optuna hyperparameter search on GCP using the canary deploy + monitor wrapper
---

# Run Cloud Experiment Workflow

// turbo-all

## Prerequisites
- Google Cloud SDK installed and authenticated (`gcloud auth login`)
- Project set: `gcloud config set project cltrainer`
- Data uploaded to GCS: `gs://cltrainer-optuna-results/data/cl-5m_bk_set_10.parquet`
- Working directory: `c:\Users\bwang\Documents\GitHub\CL_Analyst_Development`

## Canary Run (20 trials, ~30 min)

1. Delete any existing canary VM to free CPU quota:
```powershell
gcloud compute instances delete optuna-runner-canary --zone=us-central1-a --quiet
```

2. Deploy the canary VM with STANDARD pricing (no preemption):
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_canary.ps1 -ProvisioningModel STANDARD
```

3. Start the local monitor wrapper to auto-track progress and download results:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_monitor.ps1 -VmName optuna-runner-canary -GcsPrefix canary -PollIntervalSeconds 120
```

4. Wait for the monitor to complete. It will:
   - Poll VM status + STATUS.json heartbeat every 2 minutes
   - Auto-download all artifacts when VM terminates
   - Generate `reports/canary/run_report.md`

5. Review the run report:
   - Report: `reports/canary/run_report.md`
   - Logs: `reports/canary/logs/`
   - Models: `reports/canary/registry/`

6. If the run had OOM failures (exit 139), consider:
   - Reducing `N_JOBS` in `gcp/vm_canary_run.sh` (fewer workers = less memory)
   - Switching to a higher-memory machine type in `gcp/gcp_deploy_canary.ps1`

7. Clean up the VM when done:
```powershell
gcloud compute instances delete optuna-runner-canary --zone=us-central1-a --quiet
```

## Production Run (200 trials, ~3-4 hours)

1. Delete any existing production VM to free CPU quota:
```powershell
gcloud compute instances delete optuna-runner --zone=us-central1-a --quiet
```

2. Deploy the production VM (use SPOT for cost savings on long runs, STANDARD if budget allows):
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_run.ps1 -DataPath "C:\CL_Analyst_Data\data\processed\cl-5m_bk_set_10.parquet" -E2E -Shutdown
```

3. Start the local monitor wrapper (longer poll interval for production):
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_monitor.ps1 -VmName optuna-runner -GcsPrefix production -PollIntervalSeconds 300
```

4. Wait for the monitor to complete. Same behavior as canary:
   - Auto-downloads artifacts on VM termination
   - Generates `reports/production/run_report.md`

5. Review production results:
   - Report: `reports/production/run_report.md`
   - Logs: `reports/production/logs/`
   - Models: `reports/production/registry/`

6. Clean up:
```powershell
gcloud compute instances delete optuna-runner --zone=us-central1-a --quiet
```

## Quick Status Check (without monitor)
```powershell
# Canary
.\gcp\gcp_check_status.ps1 -VmName optuna-runner-canary
# Production
.\gcp\gcp_check_status.ps1 -VmName optuna-runner
```

## View Live Output (SSH)
```powershell
# Canary
gcloud compute ssh optuna-runner-canary --zone=us-central1-a --command="tmux attach -t canary"
# Production
gcloud compute ssh optuna-runner --zone=us-central1-a --command="tmux attach -t optuna"
# Ctrl+B then D to detach without stopping
```

## Important Notes
- Machine: `n2-standard-64` (64 vCPUs, 256 GB RAM, ~32 GB per worker)
- Current config: `N_JOBS=8`, `NUM_THREADS=8` (8 × 8 = 64 cores)
- CPU validation will FATAL error if cores don't match the config
- If changing machine type, update N_JOBS/NUM_THREADS in the shell scripts
- SPOT VMs can be preempted — use STANDARD for runs that must complete
