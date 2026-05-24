# GCP Optuna Deployment Guide — E2E Alpha Factory Edition

> Run Optuna hyperparameter searches + automated training + backtesting on GCP VMs.

## Quick Reference

| Item | Value |
|------|-------|
| **Canary VM** | `optuna-runner-canary` |
| **Production VM** | `optuna-runner` |
| **Machine Type** | `n2-standard-64` (64 vCPUs, 256 GB RAM) |
| **Zone** | `us-central1-a` |
| **Project** | `cltrainer` |
| **GCS Bucket** | `gs://cltrainer-optuna-results` |
| **Cost** | ~$3.40/hr STANDARD, ~$1.02/hr SPOT |
| **Workers** | `N_JOBS=8` × `NUM_THREADS=8` = 64 cores |
| **Data** | `C:\CL_Analyst_Data\data\processed\cl-5m_bk_set_10.parquet` (GCS: `gs://cltrainer-optuna-results/data/`) |

## Workflow Overview

### Canary (Light) Run — 20 trials × 4 searches (~30 min)
```
[1] gcp_deploy_canary.ps1   → Create VM + upload code + data + launch
[2] gcp_monitor.ps1         → Auto-monitor, download results when done
```

### Production Run — 200 trials × 4 searches (~3-4 hours)
```
[1] gcp_deploy_run.ps1      → Create VM + upload code + data + launch
[2] gcp_monitor.ps1         → Auto-monitor, download results when done
```

## Canary Run (Recommended First)

### 1. Deploy & Launch

```powershell
# STANDARD pricing (guaranteed, no preemption) — recommended for canary
.\gcp\gcp_deploy_canary.ps1 -ProvisioningModel STANDARD

# SPOT pricing (cheaper, can be preempted) — use for low-priority runs
.\gcp\gcp_deploy_canary.ps1
```

### 2. Monitor (Local Wrapper)

```powershell
# Auto-polls VM status, downloads artifacts when done, generates report
.\gcp\gcp_monitor.ps1 -VmName optuna-runner-canary -GcsPrefix canary -PollIntervalSeconds 120
```

The monitor will:
- Poll VM status every N seconds
- Read `STATUS.json` heartbeat from GCS (shows search progress)
- Auto-download logs, studies, reports, and artifacts zip when VM terminates
- Parse logs for pass/fail, OOM detection, agent ID
- Generate `reports/canary/run_report.md`

### 3. Review Results

After the monitor finishes:
- **Report**: `reports/canary/run_report.md`
- **Logs**: `reports/canary/logs/`
- **Studies**: `reports/canary/studies/`
- **Artifacts**: `reports/canary/registry/` (unzipped models + predictions)

## Production Run

```powershell
# Deploy production
.\gcp\gcp_deploy_run.ps1 -DataPath "C:\CL_Analyst_Data\data\processed\cl-5m_bk_set_10.parquet" -E2E -Shutdown

# Monitor
.\gcp\gcp_monitor.ps1 -VmName optuna-runner -GcsPrefix production -PollIntervalSeconds 300
```

## Memory Optimization

The Optuna search includes these memory-saving measures (in `optuna_lgbm_search_v2.py`):
1. **float32 downcast** — Features loaded as float32 instead of float64 (~50% RAM savings)
2. **free_raw_data=True** — LightGBM releases pandas copies after building histograms
3. **gc.collect()** — Aggressive garbage collection per-fold and per-trial

### CPU Validation
Both `vm_canary_run.sh` and `vm_production_run.sh` validate that `N_JOBS × NUM_THREADS == nproc`. If there's a mismatch (e.g., running on a different machine type), the script will **FATAL error** and exit.

> **Important**: If you change the machine type, you MUST update `N_JOBS` and `NUM_THREADS` in both shell scripts to match.

## Structured Logging

Every run logs:
- **ISO timestamps** on each search header
- **Agent ID** (`canary_bot` or `production_bot`)
- **STATUS.json** uploaded to GCS after each search (for monitor polling)
- **CPU validation** results

## Cost Guide

| Machine | vCPUs | RAM | $/hr (STANDARD) | $/hr (SPOT) | Workers Config | RAM/Worker |
|---------|:-----:|:---:|:---:|:---:|:---:|:---:|
| **n2-standard-64** | **64** | **256 GB** | **$3.40** | **$1.02** | **8×8** | **~32 GB** |
| n2-highcpu-96 | 96 | 96 GB | $3.44 | $1.03 | 6×16 | ~16 GB |
| n2-standard-48 | 48 | 192 GB | $2.55 | $0.77 | 3×16 | ~64 GB |

> **SPOT warning**: SPOT VMs can be preempted at any time. Use STANDARD for runs that must complete. Use SPOT for fault-tolerant jobs with resume logic.

## Scripts Reference

| File | Purpose |
|------|---------|
| `gcp_deploy_canary.ps1` | Deploy canary VM (20 trials, 4 searches) |
| `gcp_deploy_run.ps1` | Deploy production VM (200 trials) |
| `gcp_monitor.ps1` | **Local monitor wrapper** — polls, downloads, reports |
| `gcp_check_status.ps1` | Quick status check |
| `gcp_setup.ps1` | Create VM + GCS bucket (standalone) |
| `gcp_teardown.ps1` | Download results + delete VM |
| `vm_canary_run.sh` | Canary run orchestrator (on VM) |
| `vm_production_run.sh` | Production run orchestrator (on VM) |
| `vm_e2e_pipeline.py` | E2E pipeline (train + backtest + package) |
| `vm_startup.sh` | VM boot-time package installer |

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `Quota exceeded` | Increase quota in GCP Console → IAM → Quotas |
| Exit 139 (SIGKILL/OOM) | Reduce `N_JOBS` or switch to higher-memory machine |
| `instance-termination-action` error | Use `STANDARD` provisioning (flag only valid for SPOT) |
| SPOT preempted quickly | Switch to `-ProvisioningModel STANDARD` |
| `gcloud not found` | `$env:PATH = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin;" + $env:PATH` |
