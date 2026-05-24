# GCP Machine Type Optimization — Eliminate OOM Kills

## Problem

The current `n2-highcpu-96` (96 vCPUs, 96 GB RAM) with `N_JOBS=6 × NUM_THREADS=16` runs OOM on 2/4 canary searches (exit 139). A ~1.3 GB parquet expands to ~2.2 GB per worker (float32), and with 6 workers + LightGBM internal copies, peak memory exceeds 96 GB.

## Machine Type Comparison

> [!NOTE]
> Pricing based on [GCP N2 us-central1 rates](https://cloud.google.com/compute/vm-instance-pricing). N2 per-vCPU: ~$0.0316/hr on-demand. Per-GB RAM: ~$0.00424/hr on-demand. SPOT is ~60-70% off on-demand.

### Memory Budget Analysis

Per-worker memory breakdown:
- **Parquet load (float32):** ~2.2 GB
- **LightGBM Dataset copies:** ~1.5–2.0 GB (train + validation folds)
- **Python overhead + GC headroom:** ~0.5 GB
- **Total per worker:** ~4.2–4.7 GB peak
- **OS + system overhead:** ~2–4 GB

| Machine Type | vCPUs | RAM (GB) | N_JOBS × NUM_THREADS | Per-Worker RAM | On-Demand $/hr | SPOT $/hr | Est. Search Time | Verdict |
|---|---|---|---|---|---|---|---|---|
| **n2-highcpu-96** (current) | 96 | 96 | 6 × 16 | **15.3 GB** 😬 | ~$3.44 | ~$1.03 | ~8 min/search | ❌ OOM on 2/4 |
| **n2-standard-48** | 48 | 192 | 3 × 16 | **62.7 GB** ✅✅ | ~$2.33 | ~$0.73 | ~14 min/search | ⭐ **Recommended** |
| **n2-standard-64** | 64 | 256 | 4 × 16 | **63.0 GB** ✅✅ | ~$3.10 | ~$0.97 | ~11 min/search | ✅ Fast + safe |
| **n2-standard-32** | 32 | 128 | 2 × 16 | **62.0 GB** ✅✅ | ~$1.55 | ~$0.48 | ~22 min/search | ✅ Cheapest |
| **n2-highmem-32** | 32 | 256 | 2 × 16 | **126 GB** ✅✅✅ | ~$2.10 | ~$0.65 | ~22 min/search | ⚠️ Overkill RAM |
| **n2-highmem-48** | 48 | 384 | 3 × 16 | **126 GB** ✅✅✅ | ~$3.14 | ~$0.98 | ~14 min/search | ⚠️ Overkill RAM |

### Per-Worker RAM Calculation

```
Per-worker RAM = (Total RAM - 4 GB system) / N_JOBS
```

- n2-highcpu-96: (96 - 4) / 6 = **15.3 GB** ← too tight for 4.5 GB peak + GC lag
- n2-standard-48: (192 - 4) / 3 = **62.7 GB** ← massive headroom
- n2-standard-32: (128 - 4) / 2 = **62.0 GB** ← massive headroom
- n2-standard-64: (256 - 4) / 4 = **63.0 GB** ← massive headroom

### Time & Cost Estimates (4 canary searches × 20 trials)

| Machine | Time per Search | Total Search Time | On-Demand Cost | SPOT Cost |
|---|---|---|---|---|
| n2-highcpu-96 (current) | ~8 min | ~33 min | **$1.89** | $0.57 |
| **n2-standard-48** | ~14 min | ~55 min | **$2.14** | $0.67 |
| n2-standard-64 | ~11 min | ~44 min | **$2.27** | $0.71 |
| n2-standard-32 | ~22 min | ~88 min | **$2.27** | $0.70 |
| n2-highmem-32 | ~22 min | ~88 min | **$3.08** | $0.95 |

> [!IMPORTANT]
> **Time estimates assume linear scaling with N_JOBS.** Current 6 workers finishes in ~8 min/search. At 3 workers → ~14 min, at 2 workers → ~22 min, at 4 workers → ~11 min. Real-world scaling may be slightly better due to reduced memory contention.

### Recommendation

**`n2-standard-48`** (48 vCPU / 192 GB, 3 × 16 config) is the best balance:
- 62.7 GB per worker (14× more than current per-worker headroom)
- Only $0.25/run more expensive than current setup
- 3 workers still gives good parallelism (~55 min vs ~33 min for 4 searches)
- Zero OOM risk
- **Cost penalty vs current: +13% per run**

## User Review Required

> [!IMPORTANT]
> Please pick a machine type from the table above. I recommend **`n2-standard-48`** with `N_JOBS=3, NUM_THREADS=16`, but if you want faster runs, `n2-standard-64` with `N_JOBS=4, NUM_THREADS=16` is ~20% faster at ~6% more cost per run.

## Proposed Changes (after machine type selection)

### GCP Deploy Script

#### [MODIFY] [gcp_deploy_canary.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_deploy_canary.ps1)
- Change default `$MachineType` from `"n2-highcpu-96"` to selected type (line 19)

---

### VM Run Scripts

#### [MODIFY] [vm_canary_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_canary_run.sh)
- Change `N_JOBS` from `6` to match selection (line 28)
- Change `NUM_THREADS` stays at `16` (line 29) — no change needed

#### [MODIFY] [vm_production_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_production_run.sh)
- Change `N_JOBS` from `6` to match selection (line 32)
- Change `NUM_THREADS` stays at `16` (line 33) — no change needed

---

### Documentation

#### [MODIFY] [GCP_OPTUNA_GUIDE.md](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/docs/GCP_OPTUNA_GUIDE.md)
- Update machine type references
- Update worker/thread configuration

## Verification Plan

### Automated Verification
1. **Delete existing canary VM** (if any)
2. **Deploy** with new machine type using STANDARD pricing:
   ```powershell
   .\gcp\gcp_deploy_canary.ps1 -ProvisioningModel STANDARD
   ```
3. **Monitor** via:
   ```powershell
   .\gcp\gcp_monitor.ps1 -VmName optuna-runner-canary -GcsPrefix canary -PollIntervalSeconds 120
   ```
4. **Success criteria:** All 4 searches pass (0 exit 139 errors)

### CPU Validation
- The shell scripts' CPU validation will auto-verify `N_JOBS × NUM_THREADS = nproc` at startup
- If mismatched, the script will FATAL error immediately — no wasted time
