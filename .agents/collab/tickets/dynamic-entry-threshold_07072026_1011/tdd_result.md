# TDD Result — dynamic-entry-threshold_07072026_1011

**Outcome:** GREEN. Feature implemented via strict Red -> Green TDD.
**Branch:** `training-update` (unchanged; NOT committed/pushed — left in working tree for human review).
**Test env:** `conda run -n trader`.

## What was built
Per-model, per-side **dynamic** `entry_threshold` search bounds derived from the
model's own prediction distribution, expressed as a signal-firing band
`[f_min, f_max]` (defaults `FIRING_FRAC_MIN = 0.05`, `FIRING_FRAC_MAX = 0.45`).
Every OTHER param stays on the static `_PARAM_RANGES` grid. The static
`entry_threshold` tuple is retained as the fallback for empty/missing prob series.

Firing fraction `f(t) = P(prob >= t) = 1 - CDF(t)`, inverted through the model's
quantiles:
- `low  = quantile(1 - f_max)` (most-permissive, fires ~f_max)
- `high = quantile(1 - f_min)` (most-selective,  fires ~f_min)
- `step = max((high - low) / 10, 1e-3)`

## Files changed
- **`agent/strategy_optimizer.py`** (modified, +245 / -9):
  - Added module constants `FIRING_FRAC_MIN = 0.05`, `FIRING_FRAC_MAX = 0.45`
    (+ guard-rail constants `_ENTRY_THR_MIN_SPAN`/`_HALF_SPAN`/`_MIN_STEP`).
  - Added `_entry_threshold_bounds(prob_series, f_min, f_max) -> (low, high, step)`:
    dropna before quantiling; widens degenerate/compressed ranges symmetrically to
    a minimum span around the midpoint; clamps to `[0,1]`; guarantees `high > low`;
    `step` floored at `1e-3`.
  - Added `_compute_entry_thr_bounds(predictions_df, f_min, f_max)` -> per-side
    `{"long": ...(prob_Buy)..., "short": ...(prob_Sell)...}` with static fallback
    (`None`) when a side's prob series is absent/empty.
  - `make_objective`: new kwargs `f_min`, `f_max` (default to the module constants)
    and `entry_thr_bounds`; precomputes bounds once (not per trial); logs a per-side
    `[ENTRY-THR]` line; applies dynamic bounds to `entry_threshold` in BOTH
    `_suggest_side_params` and the non-tiered objective loop (long-side bounds).
  - `_extract_warm_start_params`: new `entry_thr_bounds` kwarg; snaps
    `entry_threshold` onto the **dynamic** grid (new `_entry_threshold_grid` helper)
    — the 3-way consistency invariant — while all other params snap on the static grid.
  - `run_optimization` and `run_hybrid_optimization`: new `firing_frac_min/max`
    params; precompute bounds AFTER the holdout slice and share them across both the
    objective and warm-start extraction.
  - CLI: `--firing-frac-min` / `--firing-frac-max` threaded into `run_optimization`.
  - **Bug fix (surfaced by the warm-start test):** `_snap_to_grid` now re-clamps
    AFTER `round(..., 10)`. On the many-decimal dynamic bounds, rounding could nudge
    a boundary snap ~1e-11 outside `[low, high]`, which would make Optuna's
    `enqueue_trial` reject the warm-start baseline. Static-grid consumers (clean
    2-decimal bounds) are unaffected.
- **`tests/test_dynamic_entry_threshold.py`** (new, strict-locked, 19 tests / 8 classes):
  module constants; quantile+firing identities on uniform/beta/clipped-normal;
  firing<->quantile inversion; degenerate + tiny-jitter; compressed SI-model
  100%-firing bug (floor > 0.30, firing at `low` <= f_max); NaN dropna; warm-start
  dynamic-grid snapping via `_snap_to_grid`; end-to-end Optuna integration asserting
  sampled `entry_threshold_long/short` stay inside per-side dynamic bounds.

Out of scope (untouched, per blueprint / fast-follow ticket 1b): manifest ->
`BatchSweepConfig` -> `vm_post_optimize.sh` plumbing of the firing band.

## Test results (fast suite: `pytest tests/ -m "not slow"`)
- **RED baseline (before impl):** 29 failed, 1659 passed — 19 = the new ticket tests
  (all `AttributeError` on the not-yet-built `FIRING_FRAC_MIN` / `_entry_threshold_bounds`,
  not fixture bugs); the other 10 = PRE-EXISTING, unrelated ES01B config/instrument
  sentinel failures.
- **GREEN (after impl):** **10 failed, 1678 passed** (`140s`).
  - New file: **19/19 pass**.
  - The 10 remaining failures are the identical pre-existing ES01B failures
    (`test_config_generator_symbols`, `test_hourly_only_equity_session`,
    `test_instrument_context`, `test_shallow_5m_bootstrap`) — **zero regressions**
    introduced by this ticket; none of those files are touched here.
  - Optimizer/warm-start/seed-determinism focus suite (reconstruction + parity +
    seed-reproducibility + objective-seed-offset): 43/43 pass, confirming the shared
    `_snap_to_grid` change and dynamic bounds preserve warm-start correctness and
    study determinism.

## Deviations from blueprint
None material. Notes:
- Added a small shared `_compute_entry_thr_bounds` helper and a `_entry_threshold_grid`
  helper (implementation detail, not called out in the blueprint) to keep the 3-way
  consistency DRY between the objective and warm-start.
- Found and fixed the `_snap_to_grid` boundary-rounding bug — required to satisfy the
  warm-start consistency test the blueprint mandated; it was the exact class of
  silent warm-start distortion the blueprint flagged as the main regression risk.

## Working tree
Left **UNCOMMITTED** on `training-update` for human review. `git status`:
`M agent/strategy_optimizer.py`, `?? tests/test_dynamic_entry_threshold.py`,
`?? .agents/collab/tickets/dynamic-entry-threshold_07072026_1011/`.
