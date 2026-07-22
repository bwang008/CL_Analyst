# Ticket Resolution Blueprint — oca-stage4-exit-ordering_07222026_0155
**Ticket Directory:** `.agents/collab/tickets/oca-stage4-exit-ordering_07222026_0155/`
**Parent:** Stage 4 of `oco-leg-race-audit_07212026_1935/blueprint.md` (operator-authorized 2026-07-22 "Proceed with the next stages"). Prerequisites: Stage 1 (`7795e1a`) and Stage 2 (`oca-stage2-residual-detection_07222026_0141`) — this ticket MUST NOT start TDD until Stage 2 is GREEN and committed, because it builds on the Stage-2 flatten helper and sign-check net.

## Residual defect after Stages 1-2
`_check_time_barrier` still cancels the protective legs fire-and-forget and submits the closing order IN THE SAME callback tick, sized from the cached position (parent blueprint R4). With Stage 1 the legs are OCA-grouped (cancel of one cancels the group broker-side) and with Stage 2 a resulting reversal is DETECTED and flattened post-hoc (sign check) — but the double-fill itself is still possible and costs real money (double spread + flatten slippage), and a leg that fills during the cancel-in-flight window lands in the UNRECOGNIZED-FILL branch as noise (its ids were cleared at :1731-1732 pre-Stage-1 numbering, without a registry snapshot — `_reset_position_state` has not run). The kill switch (`_check_naked_position`) has the same cancel-then-act shape.

## Design answer to the kill-switch keying question (parent blueprint / Impact-Reviewer risk D)
The deferral guard keys on `_pending_exit_order_id`, which is unset during a legs-cancelled/no-exit-yet transition. Fix: make the transition EXPLICIT state — `_retiring_leg_ids` (list of str order ids) — set BEFORE the cancels are transmitted, and extend the guard to defer while EITHER `_pending_exit_order_id` is set OR `_retiring_leg_ids` is non-empty, under the SAME existing bounded budget (`_time_barrier_exit_attempts` advanced via `_note_time_barrier_deferral`, kill switch releases at `_MAX_TIME_BARRIER_EXIT_ATTEMPTS` exactly as shipped in 291a9fd). No new budget, no new release semantics — the guard's condition widens, its arithmetic does not change.

## Target Files
- `src/live_execution/live_trader.py` only (`_check_time_barrier`, `_reconcile_pending_position_state`, `_check_naked_position`, `_reset_position_state` (clear the new attr), the unrecognized-fill branch (retiring-leg logging), `__init__` (attr init)).
- Existing pinned suites `tests/test_time_barrier_exit_fill_confirmation.py` / `tests/test_settle_confirm_loop_deferral.py`: pins that assert the OLD same-tick cancel-then-submit ordering are consciously RE-ADJUDICATED by the TDD-Tester with a comment naming this ticket (never silently widened; every other assertion stays).

## Required Changes

### R1 — Split the barrier exit into retire-then-submit
`_check_time_barrier`, barrier-due path (replaces the same-tick sequence):
1. Set `self._retiring_leg_ids = [str(x) for x in (tp ids + sl id) if x is not None]` BEFORE any broker call (the guard must already be armed when the cancels go out).
2. `cancel_open_orders(symbol)` (unchanged bulk cancel), clear `_sl_order_id`/`_tp_order_ids` (tracked PRICES survive, as today).
3. Do NOT call `close_position`. Log intent (`[TIME BARRIER] legs retiring — exit submission deferred to idle reconciler`), `log_signal` with `action_taken="TIME_BARRIER_EXIT_PENDING"` and `order_id=None`, return False.
4. Re-entrancy guard at the top: defer when `_pending_exit_order_id is not None` OR `_retiring_leg_ids` (both mean the reconciler owns the lifecycle).

### R2 — Reconciler retiring-legs branch (runs BEFORE the pending-exit branch)
If `_retiring_leg_ids` is non-empty:
1. Scan `get_open_trades(symbol)`: any retiring id still resting -> defer (`_note_time_barrier_deferral(None)`), return False. (Cancel-requested is not dead — the shipped BINDING CONDITION 1 discipline, now applied to the legs.)
2. Legs gone -> settled read (`_confirm_settled_position`; idle context — sanctioned). `None` -> fail closed, defer (budget++), return False.
3. `settled == 0` -> a leg filled during retirement (or OOB): match retiring ids against `get_executions` — on a match book the TRUTHFUL close (TP_HIT/SL_HIT, proven price); no match -> `CLOSED_OOB` with `exit_price=None` (honest unknown). Reset state, clear `_retiring_leg_ids`, return True.
4. `settled != 0`, SIGN MISMATCH vs `_position_side` -> Stage-2 R3 path (CRITICAL + `rearm-sign-mismatch` + `_flatten_book_and_reset(reason="REVERSED_POSITION_KILL_SWITCH", ledger_trade_id=...)`), clear `_retiring_leg_ids`, return True.
5. `settled != 0`, sign OK -> submit the exit NOW: `close_position(symbol, exit_mode=self._exit_mode, current_price=<last close from rolling_df_5m else rolling_df_1h, else fall back to exit_mode="market">)`. On no-oid (A0 relocated): `_rearm_time_barrier_protection(settled)` (settled-sized per Stage 2), clear `_retiring_leg_ids`, defer-note, return False. On success: add oid to `_processed_exit_order_ids`, set `_pending_exit_order_id`, clear `_retiring_leg_ids`, return False — the EXISTING pending-exit branch owns it from the next tick (its logic is untouched).

### R3 — Kill-switch guard widening + cancel-confirm
1. Deferral guard: `(self._pending_exit_order_id is not None or self._retiring_leg_ids) and self._time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS` — same budget, same release, comment updated.
2. Flatten ordering inside `_check_naked_position` (fires only when genuinely naked or at budget release): transmit `cancel_open_orders`, then re-scan `get_open_trades(symbol)`; if an order still rests -> defer THIS tick with a new small bounded counter (`_kill_switch_cancel_confirm_attempts`, max 3, reset on any successful flatten or state reset); at exhaustion log CRITICAL and PROCEED with the flatten anyway (the ultimate net must never be permanently suppressed — a resting order at this point gets the helper's own idempotent cancel). When the book is clear (or budget exhausted) -> Stage-2 `_flatten_book_and_reset` exactly as today.

### R4 — Retiring-leg fill events stop being noise
In the unrecognized-fill branch: if `str(order_id)` is in `_retiring_leg_ids` -> `log.warning` ("leg filled during TIME BARRIER retirement — idle reconciler will book from settled/executions") and return; NOT the `[TRADE] UNRECOGNIZED FILL` ERROR. (Booking stays with the reconciler — the event is informational here.)

### R5 — Hygiene
`_retiring_leg_ids = []` initialized in `__init__`, cleared in `_reset_position_state`, and cleared by every R2 terminal path. `_kill_switch_cancel_confirm_attempts = 0` likewise. No new health kinds (existing `time-barrier-exit-unconfirmed` A4 escalation covers budget exhaustion; R3.2 exhaustion logs CRITICAL through the existing naked-position alert).

## Constraints
- The pending-exit branch of the reconciler, `_route_retired_time_barrier_exit`, `_book_time_barrier_flat`, A4 escalation, and the Stage-2 helper are NOT modified (only called).
- Behavior at `settled` boundaries stays fail-closed everywhere; no fabricated prices (CLOSED_OOB books None).
- ASCII-only strings. No try/except:pass. Bounded budgets only — never a sleep.
- Existing-test re-adjudication is TESTER-owned, explicit, per-assertion, comment-tagged with this ticket id.
- Deploy: same operator gate/canary train as Stages 1-2; nothing rides the pending 291a9fd/394fa68 restart.

## Test cases (RED targets)
1. Barrier fires -> legs cancelled, `_retiring_leg_ids` armed BEFORE cancel transmit (order-of-operations pin via mock side-effect recording), NO `close_position` this tick, signal row `TIME_BARRIER_EXIT_PENDING`.
2. Reconciler: leg still resting -> defer + budget++; legs gone + settled sign-OK -> exit submitted with `self._exit_mode` and a real bar close, `_pending_exit_order_id` set, retiring cleared; next tick flows into the EXISTING pending-exit branch unchanged.
3. Legs gone + settled==0 + execution match on the SL id -> books SL_HIT at the proven price (not TIME_BARRIER, not a fabricated price); no match -> CLOSED_OOB with exit_price None.
4. Legs gone + settled reversed -> Stage-2 flatten path invoked, retiring cleared, True returned, no re-arm.
5. Kill switch: defers while `_retiring_leg_ids` non-empty under budget; releases at `_MAX_TIME_BARRIER_EXIT_ATTEMPTS` (existing release semantics — reuse/extend the 291a9fd suite's scenario shape); cancel-confirm: resting order after cancel -> defer up to 3 ticks then proceeds CRITICAL-loudly; clear book -> immediate flatten.
6. Retiring-leg fill event -> WARNING (not ERROR), no state change, reconciler still books.
7. A0 relocated: reconciler submit returns no oid -> settled-sized re-arm, retiring cleared, trade stays tracked.
8. Regression fences: full existing barrier/settle-confirm/killswitch suites green after the tester's explicit re-adjudication of ordering pins only.
