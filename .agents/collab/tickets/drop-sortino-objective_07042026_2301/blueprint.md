# Ticket Resolution Blueprint — drop-sortino-objective_07042026_2301
**Ticket Directory:** `.agents/collab/tickets/drop-sortino-objective_07042026_2301/`

## Bug Summary
Change request (approved by Impact-Reviewer 2026-07-04, "approved with conditions"): remove the **Sortino** objective pass from the cloud batch post-optimization chain so only **Sharpe** runs going forward. Decided after the 2026-07-04 A/B scout analysis (Sharpe beat Sortino on holdout in 3 of 4 runs, 14/16 vs 11/16 positive holdout cells; production has only ever shipped Sharpe configs). Halves post-optimizer compute and stops producing never-shipped artifacts.

Root finding (Auditor, independently verified by Reviewer): the objective list `["sharpe","sortino"]` is **code-side only** — not in `BatchSweepConfig` (`src/config/schemas.py`), not in `scripts/generate_v2_manifest.py`, not in any v2 manifest. It flows from two hardcoded deploy-chain defaults (`gcp/gcp_deploy_optimizer.ps1:30` → `gcp/vm_post_optimize.sh:104` → `agent/batch_post_optimizer.py:1199` maps `"both"` → both objectives). Existing manifests therefore run unchanged by construction. Exactly one consumer hard-fails on a sharpe-only run: `scripts/compare_parity.py`.

## Target Files
- `scripts/compare_parity.py`
- `gcp/gcp_deploy_optimizer.ps1`
- `gcp/vm_post_optimize.sh`
- `gcp/run_sweep_batch.ps1`
- `.agents/workflows/run-cloud-batch.md`
- `.agents/workflows/build-symbol-pipeline.md`
- `.agents/workflows/run-vector-cloud-batch.md`
- `agent/unified_pair_optimizer.py` (cosmetic only)

## Required Changes

### 1. `scripts/compare_parity.py` — make sortino artifacts optional-legacy (do this FIRST)
- Split the `REQUIRED_INDIVIDUAL` artifact list (lines ~34-44) into:
  - `REQUIRED`: the 4 sharpe artifacts (`batch_summary_optimized_sharpe.md`, `optimization_results_sharpe.json`, `batch_summary_optimized_ensembles_sharpe.md`, `optimization_results_ensembles_sharpe.json`, plus `sharpe_ensemble_backtests.md`) and `top_pairs.json`.
  - `OPTIONAL_LEGACY_SORTINO`: the 5 sortino files (`batch_summary_optimized_sortino.md`, `optimization_results_sortino.json`, `batch_summary_optimized_ensembles_sortino.md`, `optimization_results_ensembles_sortino.json`, `sortino_ensemble_backtests.md`).
- Semantics: absence of any OPTIONAL_LEGACY_SORTINO file is NOT a failure; when present (historical runs), those files must still flow through the existing content checks (the traceback/slippage loop at ~lines 97-119 already skips missing files via `isfile` — keep both filename sets in that loop).
- Exclude the sortino set from the reference cross-check warning at ~lines 138-142, or annotate the warning as "expected: sortino objective dropped 2026-07-04".
- Result: new sharpe-only runs PASS; old sortino-era runs still PASS with sortino content fully checked.

### 2. `gcp/gcp_deploy_optimizer.ps1` — flip deploy default
- Line ~30: change `[string]$Objective = "both"` to default `"sharpe"`.
- Keep the parameter and its accepted values (`sharpe`, `sortino`, `both`) intact — operational rollback depends on `-Objective both` continuing to work end-to-end.

### 3. `gcp/vm_post_optimize.sh` — flip VM-side default
- Line ~104: change `OBJECTIVE="both"` default to `"sharpe"` (defense-in-depth for manual VM invocations; the managed path always passes `--objective=` explicitly, verified at ps1 line ~293).

### 4. `gcp/run_sweep_batch.ps1` — VM sizing + stale comment (Reviewer Condition 2)
- Lines ~989-998: change optimizer VM task-count math from `completed * 8` to `completed * 4` (2 metrics × 2 sides × 1 objective).
- MANDATORY: fix the now-contradictory comment at ~989-990 ("Both sharpe+sortino run concurrently = ×2") and the associated console message text in the same edit.

### 5. Documentation updates (Reviewer Condition 1 makes the vector disclosure MANDATORY)
- `.agents/workflows/run-cloud-batch.md`: remove sortino rows from the Output layout (~lines 88-93), update the opt_mode table (~line 28) and objective tuning notes (~line 105); add a note that run folders produced before 2026-07-04 contain sortino artifacts (historical, still parity-checkable).
- `.agents/workflows/build-symbol-pipeline.md` (~lines 106-108): remove sortino entries from the artifact checklist.
- `.agents/workflows/run-vector-cloud-batch.md` (~lines 73, 81-86, 94-95): remove sortino rows AND add an explicit statement that the vector chain shares `gcp_deploy_optimizer.ps1`/`vm_post_optimize.sh` and therefore inherits the sharpe-only default (rollback per run via `-Objective both`).

### 6. `agent/unified_pair_optimizer.py` — cosmetic (optional)
- Line ~181: update the hint text `--objective both` → `--objective sharpe`.

## Explicit Non-Changes (guard rails for the coder)
- Do NOT modify `agent/batch_post_optimizer.py`, `agent/strategy_optimizer.py` (sortino implementation, `_OBJECTIVE_SEED_OFFSETS`, `OBJECTIVE_SCORE_CAP` all stay), `agent/generate_ensemble_artifacts.py` (its `sharpe,sortino` default + skip logic is intentional; benign "Skipping sortino" log line is accepted), `src/config/schemas.py`, `scripts/generate_v2_manifest.py`, or any manifest under `configs/`.
- No new manifest field: objectives remain a deploy-chain parameter (Reviewer ratified vs the no-silent-defaults doctrine; a required `objectives` field may be added at the next breaking schema rev).
- Known accepted side-effect: `agent/generate_batch_configs.py` (called from `vm_post_optimize.sh:522`) stops silently no-op-ing and will emit per-experiment `{label}_{metric}_{direction}_sharpe_opt.json` configs into GCS `batch_configs/` and the run's `configs/` dir. Verified inert (nothing routes them to `configs/strategies`, registry, or live). Do not "fix" this.

## Validation Gates (Reviewer Condition 3 — merge blocked until all pass)
1. Full test suite passes unchanged (57 tests at last count; `tests/test_objective_seed_offset.py` and `tests/test_report_best_trial.py` must stay green).
2. One 14B canary batch run completes end-to-end sharpe-only.
3. `python scripts/compare_parity.py --run reports\batch_runs\batch_<canary>` exits 0.
4. Equivalence check: the sharpe artifacts of the sharpe-only canary at a given `--random-seed` are byte-comparable to the sharpe artifacts of a prior both-objectives run at the same seed (valid because `_OBJECTIVE_SEED_OFFSETS["sharpe"] = 0`, confirmed at `agent/strategy_optimizer.py:74`).

## Rollback
- Operational (zero code): pass `-Objective both` to `gcp_deploy_optimizer.ps1` (or `--objective=both` to `vm_post_optimize.sh`); all sortino code paths remain live.
- Full: single-commit `git revert` (restores defaults, sizing math, strict parity list). Historical run folders are never touched either way.
