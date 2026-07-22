# TDD Result — backtest-tranche-exits_07222026_1608

**Scope:** operator-authorized Stage-3 prerequisite (parent oco-leg-race-audit_07212026_1935): true multi-tranche (scale-out) exit support in the backtest engine + decision-gate study. Live/sim untouched.

## Outcome: GREEN
- RED: full fast suite `39 failed / 2566 passed / 1 skipped` at HEAD 7b5321c — all 39 the new features; 9 identity/schema fences green pre-change (goldens captured from the pre-change engine, incl. exact float literals).
- GREEN: full fast suite `2605 passed / 1 skipped / 0 failed`. Ticket file `tests/test_backtest_tranche_exits.py`: 48/48. Engine-consumer spot-check: 225/225 across 14 suites.

## Files changed
- `src/live_execution/strategies/execution_models.py` — `allocate_tranche_lots()`: exact replica of the live rung allocator (banker's rounding pinned: 5 lots x [0.5,0.5] -> [2,3]); the single shared function live Stage 3 must later adopt.
- `agent/backtest_engine.py` — `_parse_tranche_exits` validation (qty_pct sum, strictly-increasing tp mults, multi-rung+concurrent refused "single-position"); multi-tranche activation only when >= 2 nonzero allocated tranches (1-lot ladders collapse to the existing path bit-for-bit); `_on_in_position_tranche` per-bar logic (SL-pessimistic remainder close, per-rung gap-aware fills, fill-consumes-bar, barrier/flatten/trailing on the remainder); `_book_tranche_close`/`_finalize_tranche_trade` booking (one TradeRecord; summed tranche PnL; 2*per_side*total_lots commission; weighted exit price/fill; final-event reason drives cooldown/on_exit once); `TradeRecord.tranche_exits` optional field excluded from to_dataframe/to_csv; `_close_trade` routes engine-level exits (SIGNAL_EXIT) to the remainder-closer when a ladder is active.
- `scripts/study_tranche_exits.py` — decision-gate study runner (Manager-authored; run post-commit).

## Canary note
The engine is optimizer-pipeline code: per standing rule a canary manifest run is owed before the next cloud scout/prod batch. The byte-identity golden fences (single-rung/absent tiered_exits unchanged to the float) make the expected canary delta ZERO.

## Semantics contract
Blueprint S1-S5 in this ticket folder is the single contract; live Stage 3 (per-tranche OCA pairs) must match it and adopt `allocate_tranche_lots` verbatim.
