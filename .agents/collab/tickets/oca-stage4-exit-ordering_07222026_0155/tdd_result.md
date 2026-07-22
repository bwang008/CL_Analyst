# TDD Result — oca-stage4-exit-ordering_07222026_0155

**Scope:** Stage 4 of parent `oco-leg-race-audit_07212026_1935` (operator-authorized 2026-07-22 "Proceed with the next stages"). Prerequisites Stage 1 (`7795e1a`) and Stage 2 (`be44ca5`) landed first.

## Outcome: GREEN
- RED: full fast suite `31 failed / 2486 passed / 1 skipped` — 19 new-file tests + 12 tester-re-adjudicated ordering pins across six existing files (all tagged `# re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)`; 18 tagged edit groups total incl. mechanical stub repairs), zero collateral.
- GREEN: full fast suite `2517 passed / 1 skipped / 0 failed` (175s). Ticket file `tests/test_oca_exit_ordering.py`: 25/25 (19 features + 6 fences incl. 4 getattr stub-safety pins).

## What changed (`src/live_execution/live_trader.py`)
- **R1 retire-then-submit:** the TIME BARRIER tick now only RETIRES the legs — `_retiring_leg_ids` (+`_retiring_sl_id`) armed BEFORE the cancels transmit (kill-switch guard coverage over the cancel-transmit gap is load-bearing), signal row `TIME_BARRIER_EXIT_PENDING`/`order_id=None`, no in-tick `close_position`; re-entrancy guard widened.
- **R2 reconciler retiring-legs branch** (first, inside the never-raise boundary): leg-still-resting -> defer with NO settled read (budget++); settled None -> fail closed; settled 0 -> `_book_retired_leg_close` books the truthful SL_HIT/TP_HIT at the proven execution price (or CLOSED_OOB with NULL price); settled reversed -> Stage-2 `_flatten_book_and_reset` (`REVERSED_POSITION_KILL_SWITCH`); settled sign-OK -> exit submitted NOW (exit_mode + freshest rolling close, market/0.0 fallback), oid registered + `_pending_exit_order_id` set, lifecycle handed to the UNCHANGED pending-exit branch; A0-relocated no-oid path re-arms settled-sized.
- **R3 kill switch:** deferral guard widened to `pending-exit OR retiring-legs` under the SAME 291a9fd budget/release arithmetic; new cancel-confirm — pre-cancel, re-scan `get_open_trades`, defer up to `_KILL_SWITCH_CANCEL_CONFIRM_MAX=3` ticks while an order still rests, then proceed LOUDLY (scan failure also proceeds — the ultimate net is never permanently suppressed).
- **R4:** a retiring-leg `Filled` event logs WARNING ("retirement") and returns — no longer UNRECOGNIZED-FILL ERROR noise, never the Stage-2 reversal branch (trade still tracked); booking stays with the reconciler.
- **R5:** attrs initialized in `__init__`, cleared in `_reset_position_state`; all reads outside `__init__` are getattr-with-default (stub-safety contract pinned by 4 fences).

## Files changed
- `src/live_execution/live_trader.py` (implementation).
- `tests/test_oca_exit_ordering.py` — NEW (TDD-Tester, Strict-Lock, 25 tests).
- Re-adjudicated (tester-owned, tagged, per-assertion): `tests/test_time_barrier_exit_fill_confirmation.py`, `tests/test_settle_confirm_loop_deferral.py`, `tests/test_oob_entry_state_recovery.py`, `tests/test_exit_reason_and_fill_routing.py`, `tests/test_cooldown.py`, `tests/test_live_trader_bugs.py`.

## DEPLOY: NOT DEPLOYED — OPERATOR-GATED
Same paper-canary/restart train as Stages 1-2; nothing rides the pending 291a9fd/394fa68 restart. Canary addition for this stage: force a time-barrier exit on paper and observe the two-tick retire-then-submit lifecycle + a kill-switch drill with a stuck resting order.
