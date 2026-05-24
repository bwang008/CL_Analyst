# Re-Running HourSet_08 Production Sweep — Full 201 Features

## How It Works

The system is **entirely manifest-driven**. Nothing is hardcoded in the orchestrator. Here's the flow:

```
You create a manifest.json
        ↓
manifest.json has "strategy_config": "hourly_ensemble_008_2.json"
        ↓
run_sweep_batch.ps1 reads the manifest, passes strategy_config to each VM
        ↓
Each GCP VM runs Optuna search + E2E training using that strategy config
        ↓
Since hourly_ensemble_008_2.json has NO "features" key → trains on all 201 features
```

**You only need to change the manifest.** The orchestrator, deploy scripts, and VM scripts all read from it dynamically.

---

## Step-by-Step

### Step 1: Create the New Manifest

Create a file at `configs/sweep_batch_manifest.json` (the orchestrator's default path) with the same experiments as your last batch, but pointing to the new strategy config.

The manifest below is identical to [batch_20260518_2321/manifest.json](file:///C:/Users/bwang/Documents/GitHub/CL_Analyst_Development/reports/batch_runs/batch_20260518_2321/manifest.json) except:
- **Line 7**: `"strategy_config"` changed from `"hourly_ensemble_008.json"` → `"hourly_ensemble_008_2.json"`

> [!IMPORTANT]
> Everything else (targets, machine types, trial counts, search space) stays the same. The only thing that changes is which strategy config the VMs read — and the new one has no `"features"` filter.

### Step 2: Run the Orchestrator

```powershell
# From the project root:
.\gcp\run_sweep_batch.ps1

# Or with explicit manifest path:
.\gcp\run_sweep_batch.ps1 -ManifestPath "configs\sweep_batch_manifest.json"

# Dry run first (validates manifest, sends Telegram test, no VMs created):
.\gcp\run_sweep_batch.ps1 -DryRun
```

### Step 3: Wait

The orchestrator will:
1. Deploy up to 8 concurrent GCP VMs (`c2-standard-16`, 16 vCPUs each)
2. Each VM runs Optuna search (500 trials) → E2E training → backtest → upload to GCS
3. Results download to `reports/batch_runs/batch_YYYYMMDD_HHMM/`
4. Auto-deploy a post-optimizer VM for strategy parameter tuning
5. Telegram notifications at each milestone
6. Clean up all VMs when done

**Expected wall time:** Similar to last batch (~4-8 hours depending on quota availability). Models will be slightly slower to train (201 features vs 15), but the GPU-free LightGBM training on 100K hourly rows is fast regardless.

### Step 4: Results

When complete, you'll find:
- `reports/batch_runs/batch_YYYYMMDD_HHMM/batch_summary_optimized.md` — consolidated report
- `reports/batch_runs/batch_YYYYMMDD_HHMM/configs/` — optimized strategy configs
- Models deployed to `C:\CL_Analyst_Data\models\registry\E2E_HourSet_08_*\` (after running `collect_batch_results.ps1`)

> [!TIP]
> After the sweep completes, verify the new models were trained on all 201 features:
> ```powershell
> python -c "import pickle; m = pickle.load(open(r'C:\CL_Analyst_Data\models\registry\E2E_HourSet_08_short_logloss\final_model.pkl','rb')); print(f'Features: {m.num_feature()}')"
> ```
> This should now print `Features: 201` instead of `Features: 15`.
