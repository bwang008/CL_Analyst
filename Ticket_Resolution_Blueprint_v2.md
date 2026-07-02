# Ticket Resolution Blueprint (v2)

## Bug Summary

Two ensemble backtest report files (`batch_summary_optimized_ensembles_sharpe.md` and `sharpe_ensemble_backtests.md`) produce **mismatched holdout results** for the same ensemble number. The ES canary batch run (`batch_20260702_0636`) shows "Ensemble 1" as `AP_LONG + AP_SHORT` with holdout PnL=$232,927 in the summary, but as `LL_LONG + LL_SHORT` with holdout PnL=$44,236 in the backtests report.

**Three root causes identified:**
1. **Non-deterministic ensemble ordering** — `batch_post_optimizer.py` uses a different sort than `generate_ensemble_artifacts.py`'s `_canonical_pair_order()`
2. **Independent backtest paths** — summary uses cached Optuna metrics; backtest report runs a fresh subprocess
3. **Missing exec_data resolution for ES** — ES manifest lacks the key that `generate_ensemble_artifacts.py` checks, causing it to skip separate exec data

## Target Files
- `agent/batch_post_optimizer.py`
- `agent/generate_ensemble_artifacts.py`

## Required Changes

### Fix A (CRITICAL): Unify Ensemble Sort Order in `batch_post_optimizer.py`

**Location**: `agent/batch_post_optimizer.py`, around L438-446 (`get_ensemble_sort_key()` or the sorting logic in `generate_optimized_report()`)

**Requirement**:
- Replace the current natural-sort-by-experiment-label logic with a call to `_canonical_pair_order()` imported from `generate_ensemble_artifacts.py`.
- This function reads `top_pairs.json` and returns a deterministic, canonical ordering for all ensemble pairs.
- Both report generators must iterate ensembles in the **exact same order** so that "Ensemble 1" in the summary matches "Ensemble 1" in the backtests.
- The `_canonical_pair_order()` function is already well-tested (5 tests in `test_ensemble_order.py`).

### Fix B: Exec-Data Fallback in `generate_ensemble_artifacts.py`

**Location**: `agent/generate_ensemble_artifacts.py`, around L201-203 (where exec_data path is resolved)

**⚠️ MANDATORY CORRECTION** (from Impact-Reviewer): The correct manifest key is `baseline.execution_workflow.execution_data_path`, NOT `defaults.local_exec_data` (which does not exist in any config).

**Requirement**:
- When resolving the exec_data path, add a fallback chain:
  1. First check the existing resolution path (whatever it currently checks)
  2. If empty/missing, check `baseline.execution_workflow.execution_data_path` from the manifest
  3. If the resolved path is a GCS URI (`gs://...`), convert it to a local path using the same GCS→local resolution logic found in `gcp/orchestrator.py` (around L93-105)
- This ensures that ANY new symbol manifest (not just CL) will correctly resolve separate exec data for trade execution vs. inference.

### Fix C: Startup Schema Validation in `generate_ensemble_artifacts.py`

**Location**: `agent/generate_ensemble_artifacts.py`, in `main()` after manifest loading

**Requirement**:
- Add an assertion/validation check that confirms a non-empty exec_data path was resolved before proceeding with optimization.
- Mirror the validation pattern used in `vm_post_optimize.sh` (around L279).
- If validation fails, raise a clear error: `"FATAL: No exec_data path resolved for symbol {symbol}. Ensure manifest contains 'baseline.execution_workflow.execution_data_path' or equivalent."`
- This is a guardrail to prevent silent data-file mismatches from ever occurring again on future symbol pipelines.
