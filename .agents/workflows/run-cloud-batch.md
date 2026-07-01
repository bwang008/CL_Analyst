# Run Cloud Batch Experiment Workflow

// turbo-all

Fully-automated GCP Optuna batch: **deploy → monitor → collect → post-optimize → report**.
Orchestrated by `gcp/run_sweep_batch.ps1` against a **v2 manifest** (`configs/batch_manifest_v2_*.json`,
`baseline`/`overrides` schema validated by `BatchSweepConfig`). The legacy `defaults`/`target_long`
format is retired.

> The **manifest is the single source of truth.** Every operational parameter is required and
> validated; there are no silent code-side defaults. opt_mode, slippage, holdout, and trials all
> come from the manifest, never from CLI flags.

## Tiers

| Tier | Manifest (v2) | Experiments | Sweep `n_trials` | Post-opt trials | Use |
|------|---------------|-------------|------------------|-----------------|-----|
| **Canary** | `batch_manifest_v2_hourset14a_canary.json` | 2 | 3 | 3 | Pipeline validation / parity (~20-30 min) |
| **Scout** | `batch_manifest_v2_hourset14a_scout.json` | 4 | 200 | 200 | Moderate exploration |
| **Production** | generate via `scripts/generate_v2_manifest.py` | 8 | 500 | 1500 | Deep optimization |

## opt_mode — the post-optimizer chain

`baseline.execution_workflow.opt_mode` selects the post-optimizer chain. Required; read from the manifest.

| `opt_mode` | Passes | Selection | Produces | Notes |
|------------|--------|-----------|----------|-------|
| **`individual`** (default) | 2 (individual → ensemble) | `unified_pair_optimizer.py` → **Top 4** (`top_pairs.json`) | per-side `batch_summary_optimized_<obj>.md` **and** `batch_summary_optimized_ensembles_<obj>.md` + `<obj>_ensemble_backtests.md` | Reproduces CANARY_V1. Pass 1 optimizes each side; top individuals are paired; pass 2 re-optimizes the pairs — all in one optimizer-VM call. |
| **`ensemble`** | 1 (brute force) | `select_top_ensembles.py` → Top 8 (`top_8_ensembles.json`) | `batch_ensemble_pre_opt.md` + ensemble reports only | Sweeps all long/short combos; skips per-side optimization. Diverges from CANARY_V1. |

## Date controls — train_cutoff vs holdout_cutoff vs holdout_months

Three distinct controls; getting them wrong silently collapses the OOS window (→ `0/0/0` "pre" trades).
The dry run now **fails** on collapse (see below).

| Field | Type | Stage | Meaning |
|-------|------|-------|---------|
| `train_cutoff_date` | date | sweep | **Training end.** Train = data before it. |
| `holdout_cutoff_date` | date / `null` | sweep | **`null` (default) = 2-way:** vault = all OOS after `train_cutoff`. **Set (3-way):** OOS splits into Validation `[train_cutoff, holdout_cutoff)` + final Vault `[holdout_cutoff, data_end]`; the post-optimizer backtests the **Vault**. |
| `post_optimizer_holdout_months` | length | post-opt | The **last N months** of the backtest window are carved as the post-opt holdout; everything before is "pre". |

> **Collapse rule:** if the backtest window (Vault in 3-way, OOS in 2-way) ≤ `post_optimizer_holdout_months`,
> the post-opt carve swallows the whole window → "pre" = 0 trades. **Default to 2-way (`null`)** unless you
> deliberately need a separate vault; the dry-run guard verifies the window against the real dataset dates.

## 1. Verify no VMs are running
```powershell
gcloud compute instances list
```

## 2. Dry run (schema + sanity gate — no VMs created)
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\batch_manifest_v2_hourset14a_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun
```
The dry run aborts before any deploy if any gate fails:
1. `train_cutoff_date` defined & parseable
2. no leak: `train_cutoff < holdout_cutoff` (when 3-way)
3. `post_optimizer_holdout_months > 0`
4. `slippage_per_side ∈ [0, 0.5]` (absolute price units; guards the −$2.5M class)
5. `opt_mode ∈ {individual, ensemble}`
6. **holdout/OOS collapse** — `scripts/preflight_holdout_check.py` loads the dataset's real date range and fails if the post-opt holdout would swallow the whole backtest window

## 3. Launch
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\batch_manifest_v2_hourset14a_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
```
The orchestrator then: deploys sweep VMs across fallback zones (quota-aware), monitors via background
jobs, runs an artifact-verification gate before deleting each VM, captures crash diagnostics on failure,
deploys the post-optimizer VM (reads `opt_mode`), downloads results, and writes the consolidated reports.

## 4. Validate parity (canary/parity runs)
```powershell
conda activate trader
python scripts/compare_parity.py --run reports\batch_runs\batch_<timestamp>
# exit 0 = PARITY PASS: checks artifact set, Top-4, no FileNotFound/new tracebacks, slippage 0.01, sane PnL
```

## Output (opt_mode=individual layout)
```
reports/batch_runs/batch_<timestamp>/
├── batch_progress.json                          ← live progress tracker
├── batch_summary.md                             ← unoptimized results
├── batch_summary_optimized_{sharpe,sortino}.md            ← per-side individual optimization (MAIN)
├── optimization_results_{sharpe,sortino}.json
├── top_pairs.json                               ← Top 4 ensemble pairs
├── batch_summary_optimized_ensembles_{sharpe,sortino}.md  ← Top-4 ensemble optimization
├── optimization_results_ensembles_{sharpe,sortino}.json
├── {sharpe,sortino}_ensemble_backtests.md       ← full backtest dumps per ensemble
├── wall_clock_summary.md
├── configs/                                     ← backtest-ready config JSONs per ensemble
├── predictions/                                 ← merged prediction CSVs per ensemble
└── manifest.json                                ← frozen config
```
(opt_mode=ensemble instead emits `batch_ensemble_pre_opt.md` + `top_8_ensembles.json`.)

## Objective tuning notes
- **Trade-floor penalty** (`agent/strategy_optimizer.py`): `TRADES_PER_YEAR_FLOOR=100` (ensemble) /
  `50` (single-side); smooth sigmoid weight multiplies positive scores so hyper-selective low-trade
  configs are penalized.
- **`OBJECTIVE_SCORE_CAP = 5.0`**: ceiling on the Sharpe/Sortino *objective* (not the displayed metric).
  Caps the exploding ratio of low-downside (low-trade) configs so the trade-floor penalty stays dominant.

## Infrastructure
- **Sweep machine**: `c2-standard-16` (16 vCPUs, ~64 GB). Threads auto-detected via `nproc`.
- **Concurrency**: vCPU- and VM-count-gated (`max_concurrent_vms` in the manifest `infrastructure`).
- **IP quota**: external-IP-limited per region; the post-optimizer runs **after** all sweep VMs are deleted.
- **STANDARD** provisioning for runs that must complete (SPOT can be preempted).
- Preferred region **us-west1**; pass comma-separated zones to `-Zone` for fallback.

## Key scripts
| Script | Purpose |
|--------|---------|
| `gcp/run_sweep_batch.ps1` | Batch orchestrator (deploy → monitor → collect → post-optimize → report) |
| `gcp/gcp_deploy_sweep.ps1` | Single sweep VM deploy (per-VM zip + upload verify/retry) |
| `gcp/gcp_deploy_optimizer.ps1` | Post-optimizer VM deploy (code-integrity hash gate) |
| `gcp/vm_sweep_run.sh` / `gcp/vm_e2e_pipeline.py` | VM-side sweep |
| `gcp/vm_post_optimize.sh` | VM-side post-optimizer (parses manifest, runs opt_mode chain) |
| `scripts/preflight_holdout_check.py` | Dry-run holdout/OOS collapse guard |
| `scripts/compare_parity.py` | Structural parity check vs a reference run |
| `scripts/generate_v2_manifest.py` | Generate a v2 manifest |
