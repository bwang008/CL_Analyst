# Agent Handoff: GCP Machine Type Optimization

## Context

We run Optuna hyperparameter searches for LightGBM on GCP VMs. The current setup uses `n2-highcpu-96` (96 vCPUs, 96 GB RAM) with `N_JOBS=6` workers × `NUM_THREADS=16` LightGBM threads = 96 cores.

### The Problem
We are hitting **OOM kills (exit 139)** on 2 out of 4 searches. A 1.3 GB parquet file expands to ~4.5 GB per worker in float64 (now ~2.2 GB after our float32 fix), plus LightGBM internal copies. With 6 workers, peak memory exceeds 96 GB during parallel training phases.

### Memory Fixes Already Applied (in [agent/optuna_lgbm_search_v2.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py))
1. **float32 downcast** — features loaded as float32 (~50% RAM savings)
2. **free_raw_data=True** — on lgb.Dataset to release pandas copies
3. **gc.collect()** — per-fold and per-trial cleanup via wrapper

These reduced OOM crashes from 3/4 to 2/4 searches, but 6 workers on 96 GB RAM is still too tight.

### Latest Canary Results (STANDARD VM, 33 min)
| Search | Status |
|--------|--------|
| LONG logloss | ✅ Passed (20/20 trials) |
| LONG f0.5 | ❌ Exit 139 at 13/20 (65%) |
| SHORT logloss | ❌ Exit 139 at 4/20 (20%) |
| SHORT f0.5 | ✅ Passed (20/20 trials) |

E2E pipeline completed despite failures (used partial results).

## Your Task

### 1. Machine Type Comparison
Research and present a cost comparison of GCP machine types suitable for this workload. Consider:
- `n2-highcpu-96` (current: 96 vCPUs / 96 GB RAM)
- `n2-standard-48` (48 vCPUs / 192 GB RAM)
- `n2-standard-32` (32 vCPUs / 128 GB RAM)
- `n2-highmem-32` (32 vCPUs / 256 GB RAM)
- Any other options you think are relevant

For each, calculate:
- Hourly cost (STANDARD and SPOT)
- Optimal `N_JOBS` × `NUM_THREADS` config
- Estimated per-worker RAM headroom
- Estimated search time (relative to current ~8 min/search with 20 trials)

### 2. Update Scripts
After the user picks a machine type:
- Update [gcp/gcp_deploy_canary.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_deploy_canary.ps1) — change default `$MachineType`
- Update [gcp/vm_canary_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_canary_run.sh) — change `N_JOBS` and `NUM_THREADS`
- Update [gcp/vm_production_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_production_run.sh) — same changes
- CPU validation in both shell scripts will auto-catch mismatches

### 3. Verify
- Delete any existing canary VM
- Deploy with the new machine type using STANDARD pricing
- Run the monitor: `.\gcp\gcp_monitor.ps1 -VmName optuna-runner-canary -GcsPrefix canary -PollIntervalSeconds 120`
- All 4 searches must pass (0 exit 139 errors)
- Use the `/run-cloud-experiment` workflow for reference

## Key Files
- [gcp/gcp_deploy_canary.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_deploy_canary.ps1) — canary deploy script (supports `-ProvisioningModel STANDARD|SPOT`)
- [gcp/vm_canary_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_canary_run.sh) — canary run orchestrator (has `N_JOBS`, `NUM_THREADS`, CPU validation)
- [gcp/vm_production_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_production_run.sh) — production run orchestrator (same structure)
- [gcp/gcp_monitor.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_monitor.ps1) — local monitor wrapper (auto-downloads results)
- [agent/optuna_lgbm_search_v2.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py) — Optuna search with memory optimizations
- [docs/GCP_OPTUNA_GUIDE.md](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/docs/GCP_OPTUNA_GUIDE.md) — updated guide with current architecture
- Data path: [C:\CL_Analyst_Data\data\processed\cl-5m_bk_set_10.parquet](file:///C:/CL_Analyst_Data/data/processed/cl-5m_bk_set_10.parquet)
- GCS data: `gs://cltrainer-optuna-results/data/cl-5m_bk_set_10.parquet`

## Important Constraints
- LightGBM doesn't scale well past 16 threads per worker (diminishing returns)
- CPU validation in shell scripts will FATAL error if `N_JOBS × NUM_THREADS ≠ nproc`
- The user wants to keep costs reasonable while eliminating OOM entirely
- Use STANDARD pricing for verification runs (no SPOT preemption risk)
- GCP project: `cltrainer`, zone: `us-central1-a`
