# GCP Optuna Search — Quick Start Guide

Run Optuna hyperparameter searches on high-CPU GCP VMs. Fire-and-forget: launch the search, close your laptop, results auto-upload when done.

## Prerequisites

1. **gcloud CLI installed** and authenticated:
   ```powershell
   gcloud auth login
   gcloud config set project cltrainer
   ```

2. **Compute Engine API enabled** (should already be active — you see the VM dashboard in GCP Console)

## Quick Start (3 commands)

```powershell
# 1. Create VM (one-time, takes ~3 min)
.\gcp\gcp_setup.ps1

# 2. Deploy code + data, launch search
.\gcp\gcp_deploy_run.ps1 -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet"

# 3. When done — download results + delete VM
.\gcp\gcp_teardown.ps1
```

That's it. Step 2 launches the search in a detached tmux session — safe to close your terminal.

---

## Detailed Usage

### Step 1: Create the VM

```powershell
# Default: c3-highcpu-44 (44 vCPUs, spot pricing ~$0.28/hr)
.\gcp\gcp_setup.ps1

# Bigger VM (if quota allows):
.\gcp\gcp_setup.ps1 -MachineType c3-highcpu-88

# Custom zone:
.\gcp\gcp_setup.ps1 -Zone us-west1-b
```

> [!NOTE]
> The free trial may limit you to 24 CPUs per region. If `c3-highcpu-44` fails, try `c3-highcpu-22`. Check your quota in GCP Console under **IAM & Admin → Quotas** → filter "CPUs".

### Step 2: Deploy & Run

```powershell
# Standard logloss search (recommended defaults: 4 workers, 100 trials)
.\gcp\gcp_deploy_run.ps1 `
    -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet"

# Custom parameters
.\gcp\gcp_deploy_run.ps1 `
    -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet" `
    -NTrials 150 `
    -NJobs 8 `
    -StudyName "exp_wide_logloss" `
    -MlMetric logloss

# Short target
.\gcp\gcp_deploy_run.ps1 `
    -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet" `
    -Target "TARGET_TRIPLE_2x1_24H_SHORT" `
    -StudyName "wf_v2_short_logloss_set08"

# Sharpe metric (requires strategy config)
.\gcp\gcp_deploy_run.ps1 `
    -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet" `
    -MlMetric sharpe `
    -StrategyConfig "ensemble3.json"
```

### Step 3: Monitor

```powershell
# Quick status check (shows recent output)
.\gcp\gcp_check_status.ps1

# Attach to live output (Ctrl+B then D to detach)
.\gcp\gcp_check_status.ps1 -Attach

# Download latest .db mid-run
.\gcp\gcp_check_status.ps1 -DownloadDb
```

### Step 4: Get Results + Tear Down

```powershell
# Downloads all results, then deletes the VM
.\gcp\gcp_teardown.ps1

# Just delete VM (if you already downloaded)
.\gcp\gcp_teardown.ps1 -SkipDownload

# Delete everything including GCS bucket
.\gcp\gcp_teardown.ps1 -CleanAll
```

Results are downloaded to your local project:
- `models/optuna_studies/<study_name>.db` — Optuna SQLite database
- `reports/optuna_best_params_*.json` — Best hyperparameters
- `reports/optuna_trials_*.csv` — All trial results

---

## Cost Estimates

| VM Type | vCPUs | Spot $/hr | 100 trials (~) | Recommended `--n-jobs` |
|---------|:-----:|:---------:|:---------------:|:---------------------:|
| c3-highcpu-22 | 22 | ~$0.15 | ~2.5 hrs = **$0.38** | 2-3 |
| **c3-highcpu-44** | **44** | **~$0.28** | **~1.5 hrs = $0.42** | **4** |
| c3-highcpu-88 | 88 | ~$0.55 | ~50 min = $0.46 | 8 |

> [!TIP]
> The 88-vCPU VM gives the fastest wall time and only costs ~$0.50 for a full 100-trial search. Use it if your quota allows.

## Spotting / Preemption

VMs use spot pricing (70% off). If Google reclaims the VM:
- The VM **stops** (not deleted) — your disk and data persist
- Restart with: `gcloud compute instances start optuna-runner`
- Re-run `gcp_deploy_run.ps1` — Optuna resumes from the SQLite DB automatically

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Quota exceeded" | Try smaller VM: `-MachineType c3-highcpu-22` |
| SSH hangs | Wait 1 min after VM creation for SSH keys to propagate |
| "startup script not done" | Check: `gcloud compute ssh optuna-runner --command='cat /tmp/startup.log'` |
| Preempted mid-run | Restart VM → re-run `gcp_deploy_run.ps1` (Optuna resumes) |
| Want to reuse VM | Skip `gcp_setup.ps1`, just run `gcp_deploy_run.ps1` again |

## Available Parameters

### gcp_setup.ps1
| Parameter | Default | Description |
|-----------|---------|-------------|
| `-VmName` | optuna-runner | VM instance name |
| `-MachineType` | c3-highcpu-44 | GCP machine type |
| `-Zone` | us-central1-a | GCP zone |
| `-DiskSizeGB` | 50 | Boot disk size |

### gcp_deploy_run.ps1
| Parameter | Default | Description |
|-----------|---------|-------------|
| `-DataPath` | *(required)* | Path to parquet dataset |
| `-Target` | TARGET_TRIPLE_2x1_24H_LONG | Target column |
| `-MlMetric` | logloss | f1, f0.5, logloss, or sharpe |
| `-NTrials` | 100 | Number of Optuna trials |
| `-NJobs` | 4 | Parallel workers |
| `-StudyName` | *(auto-generated)* | Optuna study name |
| `-TrainCutoffDate` | 2022-01-01 | Gym/vault split date |
| `-StrategyConfig` | *(none)* | Strategy JSON (required for sharpe) |
