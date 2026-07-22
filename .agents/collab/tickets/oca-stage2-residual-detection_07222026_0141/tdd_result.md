# TDD Result — oca-stage2-residual-detection_07222026_0141

**Scope:** Stage 2 of parent `oco-leg-race-audit_07212026_1935` (operator-authorized 2026-07-22 "Proceed with the next stages").

## Outcome: GREEN
- RED: full fast suite `10 failed / 2482 passed / 1 skipped` — all 10 failures the new features; D2's wrong behavior (re-arm on reversed settled sign, stale-sized re-arm) reproduced exactly.
- GREEN: full fast suite `2492 passed / 1 skipped / 0 failed` (176s). Ticket file `tests/test_oca_residual_detection.py`: 16/16.

## Files changed
- `src/live_execution/live_trader.py` — R1 un-gated `_flatten_book_and_reset` helper (extracted from `_check_naked_position`, which keeps all guards and delegates; behavior byte-identical); R2 single-slot recently-closed-legs registry (snapshot in `_reset_position_state` before clearing, 6h age bound, cleared on next entry) + reversal branch in the unrecognized-fill path (CRITICAL + `oca-race-reversal` health event + Telegram + `OCA_RACE_REVERSAL` tradebook event + `get_cached_position`-only residual read + flatten with `ledger_trade_id=None`); R3 sign check in `_route_retired_time_barrier_exit` (reversed settled sign -> flatten `REVERSED_POSITION_KILL_SWITCH`, return True, never re-arm) + settled-sized re-arm; R4 partial-fill observability (once per (order_id, filled_qty), `protective-leg-partial-fill`, dedupe cleared per trade).
- `tests/test_oca_residual_detection.py` — NEW (TDD-Tester, Strict-Lock, 16 tests).
- `tests/test_hourly_order_housekeeping.py` — additive-only: 3 new kinds in `_ALLOWED_KINDS` (registry update, ticket-tagged).
- `.agents/skills/fleet-error-monitor/SKILL.md` — triage entries for `oca-race-reversal` (CRITICAL, auto-flatten already taken), `rearm-sign-mismatch` (CRITICAL, auto-flatten already taken), `protective-leg-partial-fill` (informational, verify broker-side sibling resize).

## DEPLOY: NOT DEPLOYED — OPERATOR-GATED
Same canary/restart train as Stage 1 (`7795e1a`); nothing rides the pending 291a9fd/394fa68 restart. Stage 4 ticket `oca-stage4-exit-ordering_07222026_0155` is unblocked by this GREEN.
