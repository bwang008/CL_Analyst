# rollover-close-fill-registration_07232026_1920 — book rollover force-closes truthfully

**Operator approval 2026-07-23 ~19:20 PT.** Live incident same day 17:00 PT
(NG NGQ26->NGU26 roll, short -1): the ROLLOVER FORCE-CLOSE worked at the
broker (brackets 116/117 cancelled, MARKET BUY order 120 filled 2.90,
+$455, flat confirmed) but the BOOKKEEPING failed the known follow-up #4 of
exit-fill-confirm-fix: order 120 is never registered with the fill router,
so its fill logged `[TRADE] UNRECOGNIZED FILL ... ignoring` (2x ERROR), the
:15 sweep found trade_115 OPEN vs broker-flat, and the OOB recovery — which
matches broker EXECUTIONS against tracked leg ids only (tp=116/sl=117, both
CANCELLED so no executions) — closed the row `CLOSED_OOB_UNRECOVERED`
`exit_price=None` and flagged the legs "UNACCOUNTED - possible live orphan.
Verify and cancel manually in TWS." Operator repaired the row by hand.

## Required behavior after the fix (a roll WITH an open position)

1. The rollover close order's fill books the close DIRECTLY and truthfully:
   `exit_price=<fill>`, `close_reason="ROLLOVER_FORCE_CLOSE"`, position
   state reset via the normal close path (`_reset_position_state` with that
   reason — the cooldown/on_exit machinery then treats it per
   `cooldown_arming` mode: it is NOT SL-family, so "sl_only" sides do not
   arm; "all" sides do. That is the existing predicate's semantics — do not
   special-case it).
2. No UNRECOGNIZED FILL lines for the close order.
3. The sweep/OOB recovery treats rollover-cancelled legs as ACCOUNTED
   (cancelled-by-rollover), not "possible live orphan".
4. A roll with NO position (CL 07-21 style) stays byte-identical.

## Implementation sites (find exact lines; anchors)

- The rollover force-close block in src/live_execution/live_trader.py
  (log anchors: "ROLLOVER FORCE-CLOSE", "Rollover: market close order
  submitted"). It places the market close via ibkr_client
  ("Exit mode: MARKET BUY") and currently drops the returned order id.
- The TIME BARRIER exit already solved the same problem: study its
  `_pending_exit_order_id` registration + the reconciler/fill-routing
  branch that books a pending exit fill with a proven price
  (settle-confirm-event-loop / exit-fill-confirm lineage). REUSE that
  mechanism (register the rollover close order id + a reason override)
  rather than inventing a parallel one. If the pending-exit path assumes
  reason=TIME_BARRIER anywhere, thread the reason through explicitly — no
  silent defaults.
- Leg accounting: where `_reset_position_state`/the recovery snapshots
  `_recently_closed_legs` — ensure the rollover path records the cancelled
  leg ids (reason "ROLLOVER") so the sweep's unaccounted-leg check can
  recognize them. Anchor: "[RECOVERY] ... UNACCOUNTED (tp=%s sl=%s)".

## Tests (TDD, tests/test_rollover_close_booking.py)

Stub pattern: object.__new__ LiveTrader (see tests/test_log_cosmetics.py
and tests/test_live_trader_bugs.py; recent stub-repair precedents cite
their ticket ids). Cases:
- Rollover close fill books exit_price + ROLLOVER_FORCE_CLOSE via
  telemetry, resets position state, no UNRECOGNIZED FILL log.
- Legs cancelled by rollover are accounted (no orphan warning) while a
  GENUINELY unknown resting order still warns (asymmetry preserved).
- Flat-roll path unchanged (no close order, no registrations).
- Reason threading: the booked close is NOT SL-family (predicate
  exit_reason_arms_cooldown(reason, "sl_only") is False; "all" True).

## Hard constraints (same as live-trailing-ladder-phase3 + worktree)

- You run in an ISOLATED GIT WORKTREE. Commit there on its checked-out
  branch; report branch + sha. Do NOT push, do NOT merge to development,
  do NOT touch configs/ or .agents/collab/error_queue/.
- A PARALLEL agent is editing OTHER regions of live_trader.py (reconnect/
  recovery trailing-latch). Keep your diff surgical to the rollover/fill-
  routing/leg-accounting seams to minimize merge conflicts.
- Trader env for all pytest (`conda run -n trader python -m pytest ...`).
  Baseline the full fast suite BEFORE changes; delta-clean after (your new
  tests the only additions; honest re-adjudications cite this ticket id).
- No cheap fixes. Multi-line commit message via no-BOM file + git commit -F;
  subject `fix(rollover-close-fill-registration_07232026_1920): <summary>`,
  body includes "deploy pending operator fleet restart".

## Definition of done
Tests green, suite delta-clean, committed in the worktree. Report: files +
rationale, test counts before/after, branch + sha, deviations w/ reasoning.
