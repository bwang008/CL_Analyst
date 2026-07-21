# Ticket Resolution Blueprint — killswitch-pending-exit-guard_07202026_1805
**Ticket Directory:** `.agents/collab/tickets/killswitch-pending-exit-guard_07202026_1805/`
**Status:** Impact-Reviewer APPROVED (veto loop iter 2). Ready for TDD-Manager.
**Severity:** MEDIUM change-size / HIGH live urgency. **Regression** — no fast-track.

## Bug Summary
Live real-money incident 2026-07-20 17:00:05 PT, cid=2010/MES. The naked-position kill switch
(`_check_naked_position`) prematurely flattened a healthy MES position that was mid-exit, racing
the settle-confirm reconciler (`_reconcile_pending_position_state`).

Root cause: `_check_time_barrier` submits a TIME BARRIER exit, clears `self._sl_order_id` (:1715),
sets `self._pending_exit_order_id` (:1769), and defers the confirm/book/re-arm to the idle-loop
reconciler (which runs every poll at :5710 and owns the exit lifecycle). `_check_naked_position`
(runs on the heartbeat via `_log_heartbeat` :5877) guards a pending ENTRY (:6205) and a tracked SL
(:6208) but has **NO guard for a pending EXIT**. During the deferral window (`_sl_order_id` None,
exit in-flight), it sees "SL None + position != 0", declares a false NAKED emergency, and market-
flattens — racing the reconciler's cancel-and-defer. The MES exit went non-marketable and rested,
so it did not fill within one poll cycle and the kill switch fired.

Provenance: the submit-and-defer machinery (`_pending_exit_order_id`) landed in a1464d2 (07-16);
commit 731ebed (07-20) relocated the settled confirm to the idle reconciler, turning the deferral
into a durable multi-poll window the kill switch can race. Regresses from 731ebed. Repeats on every
time-barrier exit that does not fill within one poll cycle; all 5 fleet children share this path.
Impact: defeats the clean modeled exit with an emergency market order, fires false CRITICAL "naked"
alarms, and carries a genuine double-close / position-reversal risk (reconnect-false-flat-oob /
$296k class).

## Design decision (settled by Auditor + Impact-Reviewer)
**BOUNDED skip**, not unconditional. The kill switch defers to the reconciler while a TIME BARRIER
exit is pending AND the reconciler's retry budget is not yet spent, then — only if the exit is
genuinely stuck (SL still None, exit unresolved past `_MAX_TIME_BARRIER_EXIT_ATTEMPTS` = 6 polls) —
flattens as the ultimate net. This keeps a SINGLE budget / SINGLE ceiling / SINGLE escalation point:
the kill switch releases at exactly the moment the reconciler's A4 pages a human. Unconditional skip
was rejected (would leave a stuck exit backstopped only by human escalation, never an auto-flatten).

Critical correctness requirement (grounded the iter-1 REJECT): the bound
`_time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS` must be **monotonic per poll** — it
must advance on EVERY unresolved reconciler poll, including one whose broker calls throw. Otherwise a
persistent selective broker-API failure (settled read succeeds, but `cancel_orders_by_ids` /
`get_open_trades` keeps throwing) freezes the counter below `_MAX` forever, suppressing BOTH the kill
switch AND A4 — strictly worse than status quo.

## Target Files
- `src/live_execution/live_trader.py` (source — the ONLY source file to change)
- `tests/test_time_barrier_exit_fill_confirmation.py` (tests — via TDD-Tester)
- `tests/test_oob_entry_state_recovery.py` (mechanical fixture repair — via TDD-Tester)

## Required Changes

### Source — `src/live_execution/live_trader.py` (3 coordinated edits, no refactor, no signature change)

**Edit A — restore the monotonic bound (the load-bearing repair).** In
`_reconcile_pending_position_state`'s never-raise except boundary (currently :1914-1925), KEEP the
existing `log.error(...)`, then BEFORE `return False` add a guarded budget-advance:
- If `self._pending_exit_order_id is not None`, call `self._note_time_barrier_deferral(self._pending_exit_order_id)`.
- Read the id off the ATTRIBUTE `self._pending_exit_order_id`, NOT the local `exit_oid` (which may be
  unbound if the throw preceded its assignment at :1802).
- Wrap that call in its OWN `try/except` that degrades to `log.debug(..., exc_info=True)`, so a
  telemetry/Telegram failure inside `_note_time_barrier_deferral` can never turn the never-raise
  boundary into a raising one.
- Do NOT weaken the boundary's contract: it must still ONLY log + advance the counter (+ possibly fire
  A4). It must NEVER book, reset, or re-arm on an unconfirmed value.
- Rationale: every pending-exit poll then hits exactly one of — resolves+clears
  (`_book_time_barrier_flat` -> `_reset_position_state`), normal-defer -> `_note_time_barrier_deferral`
  (:1823/:1868/:1995/:2007) then `return` (never reaches the except), or throws -> except boundary ->
  `_note_time_barrier_deferral` (new). So `_time_barrier_exit_attempts` advances exactly once per
  non-clearing poll unconditionally and reaches `_MAX` in <=6 polls in ALL cases. When
  `_pending_exit_order_id` is None (flat-read branch threw), the guard skips the increment (that branch
  owns no time-barrier budget).

**Edit B — the bounded kill-switch guard.** In `_check_naked_position`, immediately AFTER the existing
`if self._sl_order_id is not None: return` guard (:6208-6209), add:
```python
if (
    self._pending_exit_order_id is not None
    and self._time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS
):
    return
```
with an explanatory comment (reconciler owns the pending TIME BARRIER exit lifecycle; skip only until
its bounded budget is spent; the budget advances on every unresolved poll incl. throws, so this can
never skip forever; at `_MAX` the kill switch releases as the ultimate net exactly when A4 pages a
human). No other logic in `_check_naked_position` changes — it still verifies IBKR `pos != 0` at :6213
before flattening.

**Edit C — comment correction.** Update the now-inaccurate comment in `_check_time_barrier` at
:1710-1714: clearing `_sl_order_id` no longer "arms the kill switch to cover any deferral window"; the
kill switch covers the deferral window only AFTER the reconciler's bounded budget is exhausted.

### Tests (authored by the TDD-Tester under THIS ticket; both target test files are Strict-Lock: TRUE)
Specify CONTRACTS to assert (behavior-level — unit tests mock get_position_settled/broker calls, so
loop/timing behavior must be tested, not just int-return mocks):

1. **Incident regression (false-flatten):** `_pending_exit_order_id` set + `_time_barrier_exit_attempts
   < _MAX` + MES-shaped state (settled != 0, `cancel_orders_by_ids` -> 1, exit still in
   `get_open_trades` = Binding Condition 1 defer) => `_check_naked_position()` does NOT flatten
   (`close_position` / `telemetry.close_position` NOT called).
2. **Ultimate-net preserved:** `_pending_exit_order_id` set + `_time_barrier_exit_attempts == _MAX` +
   `_sl_order_id is None` + IBKR pos != 0 => `_check_naked_position()` DOES flatten (market close +
   `NAKED_POSITION_KILL_SWITCH` ledger close).
3. **NEW persistent-broker-failure termination (the hole that veto'd v1):** drive
   `_reconcile_pending_position_state` where `_confirm_settled_position` returns settled != 0 but
   `cancel_orders_by_ids` (or `get_open_trades`) RAISES every poll. Assert `_time_barrier_exit_attempts`
   strictly increments per poll despite the throw, reaches `_MAX` within the bound, and that at `_MAX`
   the system terminates in an auto-flatten (kill switch releases).
4. **Genuine-naked fence:** `_pending_exit_order_id is None` + `_sl_order_id is None` + IBKR pos != 0 =>
   kill switch fires exactly as today (preserve the existing
   `test_kill_switch_real_close_still_fires_cooldown_and_bulk_cancel` contract).
5. **Contract correction:** re-author the existing
   `tests/test_time_barrier_exit_fill_confirmation.py::test_kill_switch_fires_for_free_because_trade_stays_tracked`
   (it asserts the buggy immediate-fire behavior) to contracts 1+2. This is a deterministic contract
   correction (flatten-at-`_MAX` replaces flatten-on-poll-1), NOT a loosening/widening.

**Mechanical fixture repair:** `tests/test_oob_entry_state_recovery.py::_kill_switch_stub()` builds the
trader via `object.__new__` (bypasses `__init__`); add `lt._pending_exit_order_id = None` and
`lt._time_barrier_exit_attempts = 0` or the new guard raises `AttributeError`. Semantically a no-op
(a genuine naked position has no pending exit).

## Constraints (project law — enforce)
- NO CHEAP FIXES: no try/except: pass (the Edit A inner except must log at debug, not silently pass —
  and exists solely to preserve the never-raise contract, not to swallow logic errors); no defaulting a
  missing required field to None/fallback (RAISE); no loosening/skipping tests or widening assertions;
  no blind sleeps/retries; no hardcoding today's conditions.
- Live real-money order routing, all 5 children. Live/backtest parity must not regress (live-only net).
- Source changes confined to `src/live_execution/live_trader.py`.
- Full fast suite green: `conda run -n trader python -m pytest tests/ -m "not slow"`.
- TDD-Coder may modify ONLY `live_trader.py`; it may NOT modify the Strict-Lock test files.

## Deferred (separate follow-up ticket — reviewer-confirmed does NOT weaken this fix)
`_route_retired_time_barrier_exit` (:1980-2008) re-arms the SL on non-fill (A3) but never clears
`_pending_exit_order_id`, which (a) blocks the next-bar time-barrier retry via the :1665 re-entrancy
guard and (b) can drive a false A4 escalation on a healthy re-armed position. Not part of this patch
(in the re-armed state `_sl_order_id` is set, so :6208 already blocks any flatten).
