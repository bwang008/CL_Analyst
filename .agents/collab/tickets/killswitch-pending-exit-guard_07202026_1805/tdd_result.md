# TDD Result — killswitch-pending-exit-guard_07202026_1805

**Outcome:** GREEN. Full fast suite `conda run -n trader python -m pytest tests/ -m "not slow"`
= **2445 passed, 1 skipped, 0 failed** in 182s (no timeout, `--timeout=120 --timeout-method=thread`).
Counts reconcile with RED (RED: 3 failed + 2442 passed + 1 skipped; the 3 now pass, nothing else moved).

## What was implemented (source — src/live_execution/live_trader.py only)
- **Edit B — bounded kill-switch guard** (`_check_naked_position`, after the `_sl_order_id` guard):
  the kill switch skips while `_pending_exit_order_id` is set AND
  `_time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS`; it flattens only once the budget
  is exhausted (the ultimate net, firing exactly when the reconciler's A4 pages a human). Preserves
  the existing IBKR `pos != 0` verification before any flatten.
- **Edit A — monotonic-per-poll budget** (`_reconcile_pending_position_state` never-raise except
  boundary): advances the retry budget via `_note_time_barrier_deferral(self._pending_exit_order_id)`
  even when a broker call throws, so a persistent selective broker failure can't freeze the bound and
  suppress both nets. The ENTIRE advance (attribute access + call) is inside an inner `try/except ->
  log.debug`, so the boundary can never raise (this is what the corrected version fixed — see below).
- **Edit C — comment correction** (`_check_time_barrier` :1710 region): the kill switch no longer
  "arms to cover the deferral window"; it defers to the reconciler and covers the window only after
  the bounded budget is exhausted.

## Tests (authored by TDD-Tester under this ticket)
- `tests/test_time_barrier_exit_fill_confirmation.py` (Strict-Lock header updated):
  - rewrote `test_kill_switch_fires_for_free_because_trade_stays_tracked` ->
    `test_kill_switch_defers_to_reconciler_then_fires_at_budget_exhaustion` (contract correction, not a
    loosening: flatten-at-`_MAX` replaces the buggy flatten-on-poll-1).
  - added `TestKillSwitchBoundedPendingExitGuard`: `..._defers_while_pending_exit_within_budget` (C1),
    `..._flattens_when_budget_exhausted` (C2 fence), `test_persistent_broker_failure_still_advances_
    budget_and_terminates` (C3 — the freeze the Reviewer caught).
- `tests/test_oob_entry_state_recovery.py` (Strict-Lock header updated): `_kill_switch_stub()` now sets
  `_pending_exit_order_id=None` and `_time_barrier_exit_attempts=0` (mechanical fixture repair; the
  genuine-naked fence `test_kill_switch_real_close_still_fires_cooldown_and_bulk_cancel` = C4 still fires).

## Iteration log (RED -> GREEN)
1. RED (full suite): 3 failed / 2442 passed — the intended incident-regression, bounded-fence, and
   persistent-failure-termination tests failed against unfixed code.
2. Coder v1: 3 targets green in isolation, but the full suite HUNG (~93 min, 100% CPU). Diagnosed via
   `pytest-timeout` faulthandler dump: `tests/test_heartbeat_phase.py::test_fires_on_phase_with_
   shortened_final_sleep` (drives the real `_event_loop`) spun forever because Edit A's
   `if self._pending_exit_order_id is not None:` sat OUTSIDE the inner guard; in an `object.__new__`
   stub without that attr, the boundary re-raised AttributeError, breaking the never-raise contract,
   which starved `_log_heartbeat()` (the only setter of `_running=False`).
3. Coder v2: moved the attribute access INSIDE the guarding `try/except`. Never-raise contract now
   absolute; Contract 3 still holds. Full suite GREEN (2445 passed).

## Deploy
Fleet is STOPPED. Deploy = operator relaunch. Committed "deploy pending operator restart" on
`development`. Operator WIP left untouched (`.agents/collab/error_queue/audit_log.md`, data/predictions).

**CANARY after relaunch:** a TIME BARRIER exit whose marketable-limit does NOT fill instantly must be
resolved by the reconciler (book on fill / re-arm on non-fill) with NO false kill-switch flatten and NO
naked/reversal.

## Deferred follow-up (separate ticket, reviewer-confirmed independent)
`_route_retired_time_barrier_exit` (:1980-2008) re-arms the SL on non-fill (A3) but never clears
`_pending_exit_order_id`, which (a) blocks the next-bar time-barrier retry via the :1665 re-entrancy
guard and (b) can drive a false A4 escalation on a healthy re-armed position. Does not affect this fix
(in the re-armed state `_sl_order_id` is set, so the :6208 guard already blocks any flatten).
