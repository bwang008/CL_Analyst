# Ticket Resolution Blueprint — ensemble-objective-report-parity_07032026_2055
**Ticket Directory:** `.agents/collab/tickets/ensemble-objective-report-parity_07032026_2055/`

## Bug Summary
For a two-objective post-optimizer run (Sharpe and Sortino), the **ensemble** optimized reports are byte-identical between the two objectives — same selected trades, PnL, profit factor, optimized params, and selected trial — even for ensembles that were genuinely optimized (NOT reverted to baseline by the regression guard).

**Root Cause:** Both objectives receive the same TPE sampler seed (`random_seed`, typically `42`) in `strategy_optimizer.py::run_optimization()`. With the canary trial budget of 3 (well below TPE's `n_startup_trials=10`), all trials fall within the random startup phase where the sampler ignores objective return values and samples purely from the RNG. Identical seed → identical parameter suggestions → identical backtest results → identical reports. The `best_obj_score` differs (scoring IS objective-aware), but both objectives rank the same 3 identical parameter sets, so the arg-max is the same trial by construction.

**Causal chain:**
1. `batch_post_optimizer.py` L908/L942 passes `random_seed=args.random_seed` (global seed) to `run_single_optimization` for every `(task, objective)` pair.
2. `strategy_optimizer.py` L1091: `np.random.seed(random_seed)` — identical for both objectives.
3. `strategy_optimizer.py` L1213: `sampler=optuna.samplers.TPESampler(seed=random_seed)` — identical TPE seed for both objectives.
4. `strategy_optimizer.py` L1221: `study.enqueue_trial(warm_params)` — warm-start trial is objective-independent.
5. With `n_trials ≤ n_startup_trials`, TPE samples purely from the RNG → identical params for both objectives.

## Target Files
- `agent/strategy_optimizer.py`

## Required Changes

### 1. Derive a per-objective effective seed (L1091 region)

Before setting the numpy random seed, compute an `effective_seed` that incorporates the `objective_metric` so that different objectives produce different RNG sequences:

```
_OBJECTIVE_SEED_OFFSETS = {"sharpe": 0, "sortino": 1}
effective_seed = random_seed + _OBJECTIVE_SEED_OFFSETS.get(objective_metric, 0)
```

Then use `effective_seed` instead of `random_seed` for:
- `np.random.seed(effective_seed)` at L1091
- `optuna.samplers.TPESampler(seed=effective_seed)` at L1213

### 2. Constraints

- **DO NOT** change any function signatures. `run_optimization()` keeps all existing args/returns.
- **DO NOT** modify `batch_post_optimizer.py` or any caller. The offset is applied *inside* `run_optimization()`.
- The `_OBJECTIVE_SEED_OFFSETS` dict and `effective_seed` variable must be local to the `run_optimization` function body (or module-level constant).
- Sharpe offset MUST be 0, preserving backward-compatible behavior for single-objective runs.
- The `db_hash` at L1204 already includes `objective_metric`, so study databases are already separate — no changes needed there.

### 3. Verification criteria

- For `objective_metric="sharpe"` with a given seed, results must be identical to the current behavior (offset=0 is a no-op).
- For `objective_metric="sortino"` with the same seed, the TPE sampler must receive `seed+1`, producing different parameter suggestions during the startup phase.
- Reproducibility: the same `(seed, objective)` pair must always produce the same study.

## Follow-Up (out of scope for this ticket)
`run_hybrid_optimization()` at L1496/L1634 has the same latent pattern (identical `np.random.seed(random_seed)` and `TPESampler(seed=random_seed)` with no objective offset). It is **not currently invoked** from the multi-objective batch path, so it is not triggered by the reported bug. Recommend a separate follow-up ticket to apply the same offset pattern for future-proofing.
