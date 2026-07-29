# time-barrier-retire-loop_07282026_2150 — stop the pending-exit hot loop + alert flood

**Live incident 2026-07-28 18:00:16 -> 21:4x PT (operator stopped the fleet;
fix approved for implementation before restart).** MES trade_76's TIME
BARRIER exit (order 81, marketable limit, submitted 18:00:11 after Stage-4
leg retirement) had not filled by the NEXT ~5s idle tick; the pending-exit
branch's A2 step cancelled it ("retire the stranded GTC exit"), and
`_route_retired_time_barrier_exit`'s still-open path re-armed protection but
NEVER CLEARED `_pending_exit_order_id` (the KNOWN follow-up #6 of
exit-fill-confirm-fix). Every subsequent idle tick re-entered the pending
branch on the dead id: settle -> cancel(no-op) -> route -> re-arm -> defer.
~2,090 cycles at ~6s; `_note_time_barrier_deferral` alerts on EVERY attempt
>= _MAX (6), producing ~2,100 Telegram messages + queued health events.
Position was protected throughout (re-arm each cycle) — the defects are the
loop, the churn, and the flood; the exit the model wanted never re-submitted.

## Three fixes (all in src/live_execution/live_trader.py)

1. **Loop-killer — clear pending state on the died-without-filling path.**
   In `_route_retired_time_barrier_exit` (~2479), after
   `_rearm_time_barrier_protection(settled)` + the warning log: set
   `_pending_exit_order_id = None`, `_pending_exit_reason = None`,
   `_pending_exit_submitted_at = None`. The reconciler stops re-processing;
   the NEXT BAR's barrier check re-runs retire-then-submit fresh (bounded by
   the attempts budget). Do NOT clear on the fail-closed (settled None)
   path — its next-tick retry via the pending branch is settle-read-only and
   self-limiting. The settled==0 and REVERSED paths already conclude/reset.

2. **Grace window — stop retiring 5-second-old exits.** New constant
   `_PENDING_EXIT_GRACE_SECONDS = 30.0` and attr
   `_pending_exit_submitted_at` (set to `time.monotonic()` in
   `_register_pending_exit`, the single registration authority; init None in
   `__init__`; cleared with the pending state everywhere it clears). In the
   pending-exit branch, BEFORE the A2 `cancel_orders_by_ids([exit_oid])`
   (~2246): if the exit's age < grace, log (INFO, once-per-tick is fine at
   this severity) and defer WITHOUT cancelling and WITHOUT
   `_note_time_barrier_deferral` (grace waits are not retirement failures).
   `None` timestamp = unknown age = proceed to retire (pre-fix behavior —
   conservative; production always sets it via registration).

3. **Alert throttle.** `_note_time_barrier_deferral`: alert exactly AT
   `_MAX_TIME_BARRIER_EXIT_ATTEMPTS` and then every
   `_TIME_BARRIER_ALERT_EVERY = 120` further attempts (at the observed ~6s
   hot-tick worst case that is ~1 alert / 12 min instead of ~600/h; at the
   intended per-bar cadence it is a rare backstop). CRITICAL log + health
   event + Telegram all share the same throttle gate.

## Tests (TDD, tests/test_time_barrier_retry_loop.py; object.__new__ stub
pattern per tests/test_log_cosmetics.py; cite this ticket in stub repairs)

- Died-without-filling path clears pending id+reason+timestamp (and re-arm
  was called); fail-closed path RETAINS them.
- Grace: young exit (age < 30s) -> no cancel call, no deferral note, returns
  False; old exit (>= 30s) -> cancel path proceeds. None timestamp ->
  proceeds (legacy).
- Throttle: attempts 1..5 silent; 6 alerts; 7..125 silent; 126 alerts
  (telegram mock call-count assertions; health-event emitter same gate).
- Regression fences: settled==0 still books via _book_time_barrier_flat with
  the registered reason; REVERSED path still flattens.
- Existing suites likely needing mechanical stub repair (add
  `_pending_exit_submitted_at`): test_settle_confirm_loop_deferral,
  test_exit_reason_and_fill_routing, test_live_trader_bugs,
  test_oob_entry_state_recovery (any stub arming a pending exit).

## Constraints

Trader env; full fast suite baseline BEFORE (tree is clean at aa9d96f) and
delta-clean after; no cheap fixes; ASCII logs; commit on development
`fix(time-barrier-retire-loop_07282026_2150): ...` with "deploy = operator
fleet restart (operator standing by)" in the body. The fleet is STOPPED by
the operator — no live interference concerns. Post-restart cleanup owed
separately: bulk-file the ~2,105 flood health events in pending/.
