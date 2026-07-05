# Ticket Resolution Blueprint — ng-trade-collapse_07042026_1933
**Ticket Directory:** `.agents/collab/tickets/ng-trade-collapse_07042026_1933/`

## Bug Summary
The trade-floor penalty in `_apply_trade_floor_penalty` only penalizes **positive** objective scores. For unprofitable symbols (e.g., NG) where all Optuna trials produce negative Sharpe/Sortino, the penalty is entirely bypassed. This causes the optimizer to converge on micro-trade configs (5–34 trades) that minimize losses rather than finding robust strategies with meaningful trade counts. The fix: for negative scores below the trade floor, **divide** by the sigmoid weight (making scores more negative = worse), which is the mathematical inverse of the positive-score multiplication treatment.

## Target Files
- `agent/strategy_optimizer.py` — function `_apply_trade_floor_penalty` (lines 234–239)
- `tests/test_regression_guard_consistency.py` — test `test_apply_trade_floor_penalty_negative_raw_score` (lines 40–47)

## Required Changes

### 1. `agent/strategy_optimizer.py` — `_apply_trade_floor_penalty` (L234–239)

Replace the current function body with symmetric penalty logic:

- **If `raw_score == 0`**: return 0 (no penalty needed).
- **Compute `weight`** via existing `_trade_floor_weight(trade_count, trade_floor)`.
- **If `weight < 1e-9`** (near-zero, meaning trade count is far below floor): return `-9999.0` for negative scores (sentinel for degenerate trials, consistent with L1000/L1013/L1023/L1033), or `0.0` for positive scores.
- **If `raw_score > 0`**: return `raw_score * weight` (existing behavior — shrinks toward 0).
- **If `raw_score < 0`**: return `raw_score / weight` (NEW — pushes away from 0, making score more negative = worse).

Update the docstring to document the dual behavior:
- Positive scores: multiply by weight (shrinks toward 0 = worse).
- Negative scores: divide by weight (pushes away from 0 = worse).

**Do NOT change the function signature** `(raw_score: float, trade_count: int, trade_floor: float) -> float`. No callsite modifications needed.

### 2. `tests/test_regression_guard_consistency.py` — `test_apply_trade_floor_penalty_negative_raw_score` (L40–47)

The existing test asserts `penalized == raw_score` for negative scores below the trade floor. This must be updated to assert:
- `penalized < raw_score` (i.e., the penalized score is MORE negative than the raw score when trade count is below the floor)
- `penalized == raw_score` still holds when trade count is AT or ABOVE the floor (weight = 1.0)

Add additional test cases:
- Negative score with trades AT the floor → no penalty (score unchanged)
- Negative score with trades far below the floor → heavily penalized (much more negative)
- Negative score with zero trades → returns `-9999.0` sentinel
- Zero score → returns `0.0` regardless of trade count
- Verify that positive-score behavior is unchanged (regression guard)
