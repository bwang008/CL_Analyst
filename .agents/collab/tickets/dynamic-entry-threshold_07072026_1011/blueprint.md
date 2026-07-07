# Ticket Resolution Blueprint — dynamic-entry-threshold_07072026_1011
**Ticket Directory:** `.agents/collab/tickets/dynamic-entry-threshold_07072026_1011/`
**Branch:** `training-update` (do NOT touch `stable-fleet` — live fleet runs there)

## Change Summary
The post-optimizer searches a **static** entry-threshold range for every model:
`_PARAM_RANGES["entry_threshold"] = (0.30, 0.70, 0.04, "float")` in
`agent/strategy_optimizer.py`. Because different models emit probabilities on completely
different scales (one SI long model lived in 0.09–0.83, another in 0.32–0.65), a fixed
`0.30` floor can sit **below a model's entire probability mass** — the optimizer then picks
a threshold that fires on ~100% of bars ("always-on"), which (a) throws away the model's
ranking edge and (b) in a single-position engine starves the opposite side. Root-caused in
`si-01b-edge-and-threshold-floor` memory: E04 buy-thr 0.34 → holdout −$158k; the same model
at 0.52 → +$25k. **Fix: make the entry-threshold search bounds per-model, derived from that
model's own prediction distribution, expressed as a target signal-firing band.**

## The rule (exact math)
Firing fraction at threshold `t` is `f(t) = P(prob ≥ t)` = the upper tail = `1 − CDF(t)`.
A firing band `[f_min, f_max]` maps to a threshold search range by inverting through the
model's prediction quantiles:
- `threshold_floor  = quantile(1 − f_max)`   (lowest/most-permissive threshold, fires f_max)
- `threshold_ceiling = quantile(1 − f_min)`  (highest/most-selective threshold, fires f_min)
- `step = (ceiling − floor) / 10`  (matches the existing 0.04-over-0.40 convention, but per-model)

**First-run band (LIBERAL, per user 2026-07-07): `f_min = 0.05`, `f_max = 0.45`**
→ threshold range `[quantile(0.55), quantile(0.95)]` per side. Verified this clears the
trade-floor penalty for the SI edge models (10%-fire end still gives ~102 long / ~225 short
trades/yr vs the 50/100 floors), so a 5%-fire ceiling has ample headroom.

Which distribution to use, per side:
- **long** suffix → quantiles of `predictions_df["prob_Buy"]`
- **short** suffix → quantiles of `predictions_df["prob_Sell"]`
- Compute on the **optimizer-window** predictions that `make_objective` already receives
  (the holdout is already sliced off upstream in `run_optimization`). Never recompute on the
  holdout.

## Scope
- **In scope (this ticket):** the per-side dynamic bounds inside `agent/strategy_optimizer.py`,
  with the firing band supplied as parameters; explicit module-constant defaults
  `FIRING_FRAC_MIN = 0.05`, `FIRING_FRAC_MAX = 0.45`.
- **Out of scope (fast-follow ticket 1b):** threading the band from the v2 manifest
  (`execution_workflow`) through `BatchSweepConfig` → `vm_post_optimize.sh` →
  `run_optimization`. This ticket must accept the band as function/CLI params so 1b only wires
  the source; do not hardcode the band inside the objective.

## Target Files
- `agent/strategy_optimizer.py` (primary)
- `tests/` (new test module, per TDD)

## Required Changes (logical — TDD-coder implements)
1. **Add a helper** `_entry_threshold_bounds(prob_series, f_min, f_max) -> (low, high, step)`:
   - `low = prob_series.quantile(1 - f_max)`, `high = prob_series.quantile(1 - f_min)`.
   - Guard degenerate/compressed distributions: if `high - low` is below a small epsilon
     (e.g. `< 0.01`), widen symmetrically to a minimum span (e.g. ±0.005 around the midpoint)
     so Optuna gets a valid non-empty range; clamp both to `[0.0, 1.0]`; ensure `high > low`.
   - `step = max((high - low) / 10, 1e-3)` (avoid a zero/absurdly-fine step).
   - Drop NaNs before quantiling.
2. **Precompute once** (not per trial). In `make_objective`, after `predictions_df` is known,
   build `entry_thr_bounds = {"long": _entry_threshold_bounds(predictions_df["prob_Buy"], ...),
   "short": _entry_threshold_bounds(predictions_df["prob_Sell"], ...)}`. Pass `f_min/f_max`
   into `make_objective` (new kwargs, defaulting to the module constants) and forward from
   `run_optimization` (also new kwargs + CLI args).
3. **Use dynamic bounds for `entry_threshold` only**, in ALL THREE consumers of
   `_PARAM_RANGES["entry_threshold"]` — keep every other param on `_PARAM_RANGES`:
   - `_suggest_side_params(trial, suffix)`: when `key == "entry_threshold"`, use
     `entry_thr_bounds[suffix]` for `(low, high, step)` instead of the static tuple.
   - The **non-tiered** path in `objective()` (the `for key ... in _PARAM_RANGES` loop ~line
     1003): use the long-side bounds (non-tiered configs are single-model long).
   - **Warm-start** `_extract_warm_start_params` / `_snap_to_grid`: `entry_threshold` must be
     snapped to the **same dynamic grid**, not the static `_PARAM_RANGES` grid — otherwise the
     baseline threshold gets clamped to the wrong range and `enqueue_trial` distorts/rejects
     it. Thread the computed bounds into the warm-start extraction (compute bounds in
     `run_optimization` where `predictions_df` is available, pass to both `make_objective` and
     the warm-start call).
   ⚠️ This 3-way consistency is the main regression risk — a mismatch silently corrupts the
   warm start. Tests must cover it.
4. **Keep `_PARAM_RANGES["entry_threshold"]` as the fallback** used only when a prob series is
   empty/missing (e.g. a truly single-side config with no short predictions) — do not delete it.
5. **Logging:** print the derived per-side threshold range + implied firing band once per
   optimization (parity with the existing `[WARM-START]` prints), so batch logs show what was used.

## Test Requirements (TDD-tester writes FIRST; must be red before code)
- `_entry_threshold_bounds`: for a known distribution (e.g. uniform, and a realistic skewed
  one), asserts `low == quantile(1-f_max)`, `high == quantile(1-f_min)`, and that
  `P(prob ≥ low) ≈ f_max`, `P(prob ≥ high) ≈ f_min` within tolerance.
- Degenerate distribution (near-constant probs) → returns a valid range with `high > low`,
  both in `[0,1]`.
- Compressed distribution (e.g. all in 0.32–0.65) → floor is NOT below the distribution min
  (i.e. never yields ~100% firing); assert firing at `low` ≤ `f_max + tol`.
- Warm-start consistency: a baseline `entry_threshold` outside the dynamic range is snapped
  into `[low, high]`, and the snapped value is on the dynamic grid (not the static 0.04 grid).
- Integration: `make_objective` with a small synthetic `predictions_df` runs an Optuna trial
  and every sampled `entry_threshold_long/short` lies within the per-side dynamic bounds.
- Regression guard: the full fast suite stays green (`pytest tests/ -m "not slow"`).

## Validation (post-green)
- Re-run the SI E04 threshold intuition offline: confirm the optimizer can no longer select a
  threshold that fires >45% or <5% of bars for either side.
- Confirm trades/yr at the chosen thresholds stay above the trade floor (single 50 / ens 100).

## Notes for downstream
After green + commit on `training-update`, the fast-follow **ticket 1b** wires
`execution_workflow.firing_frac_min/max` (required manifest fields, no silent default per the
project rule) through `BatchSweepConfig` and `vm_post_optimize.sh`. Then `/cloud-target-batch`
(separate epic) is the target pre-screen that feeds this optimizer good targets.
