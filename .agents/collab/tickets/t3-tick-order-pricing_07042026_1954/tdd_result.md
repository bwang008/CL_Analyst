# TDD Result — t3-tick-order-pricing_07042026_1954

**Outcome: GREEN + PARITY PASS — ticket complete. Reviewer verdict: APPROVE (no human authorization required).**

- Red: 76 tests, ghost-import collection error; tester pre-validated via scratchpad-injected helper (48 CL pins passing / exactly 28 desired-Red). Baseline 1023 (manager-verified).
- Green: **1099 passed, 0 failed** full fast suite, first run, zero coder iterations (manager-verified).
- Blocking parity gate: **PARITY: PASS**, exit 0 — 15=15, 15/15 exact-cent, $0.00 delta ($1,695.01 both engines).

## Key finding preserved for posterity
Naive `round(price/tick)*tick` mismatches `round(price, 2)` BITWISE on ~1.4-1.9% of
inputs (half-cent floats, -0.0). `round_to_tick` routes power-of-ten ticks through
`round(price, n)` — CL byte-identity by construction. Reproduced independently by
auditor AND reviewer with different seeds before implementation.

## Files changed
- `src/core/instrument_master.py` — append-only: `_tick_grid` + `round_to_tick`.
- `src/live_execution/ibkr_client.py` — `_CL_TICK_SIZE` deleted; marketable-limit
  pricing tick-aware via registry (zero signature changes); R1 adaptive-exit snap;
  NYMEX exit injection → registry exchange in close_cl_position(+_market),
  unregistered positions skipped as before.
- `src/live_execution/live_trader.py` — `_tick_size` property (execution instrument;
  seam fallback, raises when unresolvable); trailing SL + six child-price sites +
  R2 recovery re-place snap via round_to_tick.
- `tests/test_tick_order_pricing.py` — NEW, 76 tests (Strict-Lock).
- `tests/test_bracket_order.py` — 4 mechanical contract.symbol="CL" additions.

## Notes
- ES naked-stop scenario now pinned: filled ES entry produces tick-valid TP/SL children.
- Accepted consequence (documented): non-CL live-vs-backtest TP/SL may diverge ≤ ½ tick
  (backtest stays cent-grid); non-CL parity gates will need ½-tick tolerance.
- Q3 (dead live trailing modification, hasattr modify_order guard) deliberately NOT
  fixed here → ticket `live-trailing-modify-order-dead_07042026_2012`, next in queue.
- Entry-price snapping (adaptive/market entries) deferred — rejected entry is fail-safe.
