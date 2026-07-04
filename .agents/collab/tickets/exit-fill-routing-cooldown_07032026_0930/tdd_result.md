# TDD Result — exit-fill-routing-cooldown_07032026_0930

**Outcome:** ✅ D and E FIXED and verified (unit + 336-bar replay). F re-characterized — needs its
own ticket (see below). Merged to `development` as `fc89b11` (fast-forward, 2026-07-03 19:49);
ticket branch `ticket/exit-fill-routing-cooldown` deleted after merge.

## Tests added (Strict-Lock: TRUE)
`tests/test_exit_reason_and_fill_routing.py` — 9 tests: TIME_BARRIER/CLOSED_OOB reason
propagation, CLOSED-family SL-flavored cooldown, unrecognized-fill guard (incl. the
child-ID-decision-context trap), entry-registry preservation, SL-fill exit-path regression
guard, source scans (registry present; harness duplicate placement gone).
Red: 6 failed / 3 preservation-passed. Green: 9/9. Full fast suite: **751 passed / 0 failed**.

## Implementation
- `live_trader.py` — time-barrier exit resets with `reason="TIME_BARRIER"`; OOB close with
  `reason="CLOSED_OOB"`; new `self._entry_order_ids` registry populated at entry submission;
  fill-handler else-branch only books a NEW trade for registered entry ids, loudly ignores
  `[TRADE] UNRECOGNIZED FILL` otherwise (decision context is stored under child order ids too,
  so it cannot discriminate entries).
- `configurable_strategy.py` — cooldown flavor tuple extended with `"CLOSED"`, `"CLOSED_OOB"`
  (conservative SL-flavor).
- `livetest_engine.py` — harness no longer double-places bracket children; it only registers the
  live_trader-placed set in `_open_orders` for trailing lookups.

## Replay verification (336 bars, trailing disabled symmetrically)
Before (post parity-exit-signal, pre this fix): bt=15 lt=22 matched=15, 7 phantom lt extras, 8 issues.
**After:** `bt=15 lt=17 matched=14, exact-cent 13/14, entry deltas $0.0000, 5 issues.`
Log health: 0 UNRECOGNIZED fills, 0 OOB closes, 7 [OCA] sibling cancels, 18/18 single child sets.

**Every remaining divergence is attributed:**
- 1 violation = the **backlogged B(b)** trade (05-26: BT TIME_BARRIER-at-open vs LT intrabar TP_HIT, $1500).
- 05-26 13:00 lt-extra = B(b) cascade × F (same-bar re-entry after TP with tp_cooldown=0).
- 05-28 07:00 lt-extra + 05-28 08:00 bt-only = the SAME trade shifted one bar — F-family
  (TIME_BARRIER exit-bar evaluation skip in live vs evaluated-with-counter-0 in backtest shifts
  the consecutive-signal/gating sequence by one bar).
- 06-10 11:00 lt-extra = F (same-bar re-entry after a TP both engines took).

## F — refined root cause (for the follow-up ticket)
F is NOT an artifact of E. Two intertwined exit-bar-semantics gaps remain:
1. **Reset value:** `ConfigurableStrategy.on_exit` resets the counter to 0; with the pre-gate
   increment the exit-bar evaluate reads 1 where the backtest reads 0 → with cooldown 0 the live
   side can re-enter on the exit bar (backtest earliest next bar). Fix direction: reset to -1 for
   fill-callback exits (SL_HIT/TP_HIT, where the exit bar still gets an evaluation) and 0 for
   TIME_BARRIER/CLOSED-family (where live skips the exit-bar evaluation) — the old harness
   monkey-patch's discrimination, but in production code.
2. **Harness event ordering:** `run_simulation` flushes deferred fill callbacks AFTER the bar's
   evaluation, so the exit-bar evaluate sees a flat sim position with pre-exit counters. Fix
   direction: flush once between bar-feed (resting-order matching) and `updateEvent.fire`, and
   once after (for same-bar entry fills), mirroring production's real-time callback timing.
3. (Bigger design question, overlaps B(b)): live skips evaluation entirely on TIME_BARRIER exit
   bars; the backtest evaluates them — one-bar shifts in consecutive-signal counting.

## Residual risk noted
`reports/_ledger_parity` is shared across worktrees (junction); a second identically-configured
livetest launched at 20:09 (other session, post-merge code) writes the same output files. This
run's ledger was snapshotted to `livetest_ledger_DE_fix.csv` before reconciling.
