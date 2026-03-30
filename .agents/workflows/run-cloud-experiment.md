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

## Large Scale Continuous Sweep (300 Trials, ~6-8 hours)

**Standard Practice**: Always launch the `gcp_monitor.ps1` heartbeat after deploying a VM sweep. The VM will natively shut down after finishing and uploading to GCS, but the local monitor acts as a required heartbeat that will auto-download all models/reports directly to your local file system as soon as the VM drops.

1. Deploy the Sweep VM (e.g. for a 5-minute dataset):
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_sweep.ps1 -VmName "optuna-runner-5m" -Zone "us-central1-a" -MachineType "n2-standard-48" -GcsDataPath "gs://cltrainer-optuna-results/data/cl-5m_bk_set_11c.parquet" -StrategyConfig "configs/strategies/production_lean_dual.json" -Metrics "logloss average_precision" -TargetLong "TARGET_TRIPLE_2x1_24H_LONG" -TargetShort "TARGET_TRIPLE_2x1_24H_SHORT"
```

2. **CRITICAL**: Launch the local monitor to track pipeline state and automatically collect results:
```powershell
# GcsPrefix should match the --job-name your sweep generates (usually 'sweep_' + the dataset name, e.g. sweep_set_11c)
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_monitor.ps1 -VmName optuna-runner-5m -GcsPrefix sweep_set_11c -PollIntervalSeconds 300
```

3. Wait for monitor to conclude and download all `.zip` artifacts into `reports/<GcsPrefix>`.

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
# Sweep
gcloud compute ssh optuna-runner-5m --zone=us-central1-a --command="tmux capture-pane -t sweep -p"
# Ctrl+B then D to detach without stopping
```

## Important Notes
- Machine: `n2-standard-64` (64 vCPUs, 256 GB RAM, ~32 GB per worker)
- Current config: `N_JOBS=8`, `NUM_THREADS=8` (8 × 8 = 64 cores)
- CPU validation will FATAL error if cores don't match the config
- If changing machine type, update N_JOBS/NUM_THREADS in the shell scripts
- SPOT VMs can be preempted — use STANDARD for runs that must complete
