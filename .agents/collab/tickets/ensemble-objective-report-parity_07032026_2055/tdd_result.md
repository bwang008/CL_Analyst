# TDD Result — ensemble-objective-report-parity_07032026_2055

## Outcome: ✅ PASS

## Summary
Fixed duplicate Sharpe/Sortino ensemble reports by deriving a per-objective seed offset for the TPE sampler and numpy RNG in `strategy_optimizer.py::run_optimization()`.

## Files Changed

### `agent/strategy_optimizer.py` (3 edits)
1. **L70** — Added module-level constant: `_OBJECTIVE_SEED_OFFSETS = {"sharpe": 0, "sortino": 1}`
2. **L1097-1098** — Compute `effective_seed = random_seed + _OBJECTIVE_SEED_OFFSETS.get(objective_metric, 0)` and use it in `np.random.seed(effective_seed)`
3. **L1219** — Use `effective_seed` in `optuna.samplers.TPESampler(seed=effective_seed)`

### `tests/test_objective_seed_offset.py` (NEW)
22 test cases across 6 classes verifying seed offset correctness, divergence, reproducibility, backward compatibility, and the offset mapping contract.

## Test Results
- **22/22 new tests PASS** (seed offset behavior verified)
- **777 total passed** across the full suite
- **10 pre-existing failures** in `test_exit_bar_semantics.py` — completely unrelated to this change (exit bar precedence logic)

## Follow-Up (out of scope)
`run_hybrid_optimization()` has the same latent pattern (shared seed across objectives) but is not currently invoked from the multi-objective batch path. Recommend a separate ticket.
