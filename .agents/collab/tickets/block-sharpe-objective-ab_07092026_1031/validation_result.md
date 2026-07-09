# Validation Result — block-sharpe-objective-ab_07092026_1031 (2026-07-09)

| Gate | Verdict | Evidence |
|---|---|---|
| 1. Full test suite | **PASS** | 1983 passed / 0 failed (`-m "not slow"`; red phase was 54 failed / 1929 passed) |
| 2. Baseline invariance | **PASS** | Reran frozen `batch_20260709_045452/manifest.json` (ES 01B canary, seed 42, sharpe-only) on post-change code → `batch_20260709_124801_ES_RUN`. `optimization_results_sharpe.json`, `optimization_results_ensembles_sharpe.json`, `top_pairs.json` **numerically identical** (best trials, params, PnL, trades, pair selection); only additive `block_sharpes`/`block_bounds` diagnostics + timestamps/wall-times differ (by design). `compare_parity.py` → PARITY PASS. |
| 3. A/B canary chain | **PASS** | `batch_20260709_122657_CL_CANARY_OBJAB` (`-Objective "sharpe,block_min"`, 15B canary manifest, holdout 12): full per-arm artifact sets incl. `top_pairs.json` + `top_pairs_block_min.json` + `block_min_ensemble_backtests.md`; self-describing headers (objective_metric / n_blocks 3 / λ 1.0 / min_block_months 10 / holdout 12); sharpe-set PARITY PASS vs CANARY_V1; `block_sharpes` present in both arms; arms provably optimized independently (differing winners on logloss targets); folder auto-stamped; zero VMs left after teardown. |
| 4. Preflight block gate | **PASS** | Dry run prints `[OK] block layout for ['block_min']: in-sample 2022-01..2025-06 = 42 months -> 14/14/14` (boundaries 2022-01..2023-02 / 2023-03..2024-04 / 2024-05..2025-06); inert when no block metric requested. |
| 5. 15B scout 4-arm A/B | **NOT RUN** | The experiment itself — launch command below. Human-gated (cost + potential collision with the operator's own scout retry of failed `batch_20260709_085842`). |

## Gate-5 launch command (when ready)
```powershell
& .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\batch_manifest_v2_hourset15b_scout.json" `
    -Objective "sharpe,block_min,block_median,block_mean_std" `
    -OptimizerMaxRunDurationMinutes 720 `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun   # then re-run without -DryRun
# Readout: conda run -n trader python scripts/compare_objective_arms.py --batch-dir <stamped dir>
```

## Outstanding
- `gcp/run_sweep_batch.ps1` (-Objective forwarding, dry-run gate 6b, auto-stamp) and `.agents/workflows/run-cloud-batch.md` doc updates: implemented + validated in the working tree but **uncommitted** — entangled with the operator's uncommitted GCS-preflight changes; commit both efforts together.
- `configs/batch_manifest_v2_hourset15b_canary.json` (untracked): holdout bumped 6→12 for gate 3.
- Step-5 config validation gate (`validate_batch_configs.py`) not run on the OBJAB canary — its configs are not production candidates.
