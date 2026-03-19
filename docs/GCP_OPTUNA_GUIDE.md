# GCP Optuna Deployment Guide

> Run Optuna hyperparameter searches on GCP high-CPU VMs instead of your local machine.

## Quick Reference

| Item | Value |
|------|-------|
| **VM Name** | `optuna-runner` |
| **Machine Type** | `c2d-highcpu-56` (56 vCPUs, AMD EPYC Milan) |
| **Zone** | `us-central1-a` |
| **Project** | `cltrainer` |
| **GCS Bucket** | `gs://cltrainer-optuna-results` |
| **Cost** | ~$1.30/hr on-demand |
| **Scripts** | `gcp/` directory |

## Prerequisites

1. **Google Cloud SDK** installed and authenticated:
   ```powershell
   gcloud auth login
   gcloud config set project cltrainer
   ```
2. **Compute Engine API** enabled (already done for this project)
3. **CPU Quota** ≥ 56 for `CPUS_ALL_REGIONS` and `C2D_CPUS` in `us-central1`

## Workflow Overview

```
[1] gcp_setup.ps1        → Create VM + install deps (~1 min)
[2] gcp_deploy_run.ps1   → Upload code + data, launch Optuna in tmux
[3] gcp_check_status.ps1 → Monitor progress (safe to disconnect)
[4] gcp_teardown.ps1     → Download results + delete VM
```

## Step-by-Step

### 1. Create the VM

```powershell
.\gcp\gcp_setup.ps1
# Override machine type:
.\gcp\gcp_setup.ps1 -MachineType c2d-highcpu-32
```

This creates the VM, installs Python + ML packages via startup script, and creates a GCS bucket for results.

### 2. Deploy & Run

```powershell
# Long model search
.\gcp\gcp_deploy_run.ps1 `
    -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet" `
    -Target "TARGET_TRIPLE_2x1_24H_LONG" `
    -MlMetric logloss `
    -NTrials 100 `
    -NJobs 4

# Short model search
.\gcp\gcp_deploy_run.ps1 `
    -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet" `
    -Target "TARGET_TRIPLE_2x1_24H_SHORT" `
    -MlMetric logloss `
    -NTrials 100 `
    -NJobs 4

# Skip data re-upload (already on VM)
.\gcp\gcp_deploy_run.ps1 -DataPath "..." -SkipDataUpload
```

### 3. Monitor (Safe to Disconnect)

The search runs in a `tmux` session on the VM. You can close your laptop and come back later.

```powershell
# Check status
.\gcp\gcp_check_status.ps1

# View live output
gcloud compute ssh optuna-runner --command="tmux attach -t optuna"
# (Ctrl+B then D to detach without stopping)

# Check from any machine
gcloud compute instances describe optuna-runner --zone=us-central1-a --format="get(status)"
```

### 4. Download Results & Tear Down

```powershell
.\gcp\gcp_teardown.ps1
# Downloads results from VM + GCS to local, then deletes VM
```

## How It Works

### File Upload (Minimal)
Only **8 Python files** (~130KB) are uploaded — not the entire project:
- `agent/optuna_lgbm_search_v2.py` — main search script
- `agent/experiment_runner.py` — experiment logging (not imported, kept for reference)
- `agent/backtest_engine.py` — for sharpe mode
- `agent/__init__.py`, `src/__init__.py` — package inits
- `src/util.py` — feature/target column helpers
- `experiments.json` — experiment log
- `gcp/vm_run_optuna.sh` — tmux runner

Plus the data parquet file (~1.3GB, takes ~16 min to upload).

### tmux Persistence
The Optuna script runs inside a `tmux` session, so it survives SSH disconnections. Even if your internet drops or you close your laptop, the search continues.

### Result Safety (GCS Auto-Upload)
When the search finishes, `vm_run_optuna.sh` automatically uploads results to GCS:
- `.db` file (Optuna study database)
- `.json` reports (best params, trial summary)
- `.csv` reports
- Log files

Results persist in GCS even if the VM is deleted.

### VM Console
See your VM at: [GCP Console → Compute Engine → VM Instances](https://console.cloud.google.com/compute/instances?project=cltrainer)

## Cost Guide

| Machine | vCPUs | $/hr | 100-trial est. time | 100-trial est. cost |
|---------|:-----:|:----:|:-------------------:|:-------------------:|
| e2-highcpu-8 | 8 | $0.20 | ~50 hrs | $10.00 |
| c2d-highcpu-32 | 32 | $0.75 | ~12 hrs | $9.00 |
| **c2d-highcpu-56** | **56** | **$1.30** | **~7 hrs** | **$9.10** |
| c3-highcpu-88 | 88 | $3.00 | ~3 hrs | $9.00 |

Total cost is roughly the same (~$9) regardless of machine size — bigger machines finish proportionally faster.

**Stop vs Delete:**
- **Stop** the VM when not in use → ~$4/month for disk storage only
- **Delete** the VM when done → $0 (results safe in GCS)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `Quota exceeded` | Increase quota in GCP Console → IAM → Quotas |
| `gcloud not found` | Add to PATH: `$env:PATH = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin;" + $env:PATH` |
| SSH timeout | The search takes time; check with `gcloud compute ssh optuna-runner --command="tail /tmp/smoke.log"` |
| Startup not complete | Wait 1-2 min, check: `gcloud compute ssh optuna-runner --command="cat /tmp/startup.log"` |
| Data upload slow | ~1.4 MB/s is normal for compressed SCP; use `--compress` flag |

## Scripts Reference

| File | Location | Purpose |
|------|----------|---------|
| `gcp_setup.ps1` | `gcp/` | Create VM + GCS bucket |
| `gcp_deploy_run.ps1` | `gcp/` | Upload code/data + launch search |
| `gcp_check_status.ps1` | `gcp/` | Monitor progress |
| `gcp_teardown.ps1` | `gcp/` | Download results + delete VM |
| `vm_startup.sh` | `gcp/` | VM boot-time package installer |
| `vm_run_optuna.sh` | `gcp/` | tmux runner + GCS auto-upload |
| `requirements-gcp.txt` | `gcp/` | Minimal pip dependencies |
