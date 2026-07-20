# TDD Result — settle-confirm-event-loop_07202026_0713

**Outcome:** GREEN. Full fast suite **2439 passed, 1 skipped** (Red baseline: 2421
passed + 18 failing; the 18 now pass, zero other movement). Verified by the
TDD-Manager running `conda run -n trader python -m pytest tests/ -m "not slow"`.

**Operator-authorized 2026-07-20 (Direction A + 4 binding conditions). Deploy
(fleet restart) remains operator-gated — the fix is INERT until the operator
restarts the fleet.**

## What the fix does

Moves ALL settled-based confirmation off the in-callback path. The TIME BARRIER
exit's `_confirm_settled_position` → `get_position_settled` → `self.ib.run()` no
longer fires from inside the ib_insync bar-update callback (where the loop is
running and `ib.run()` raises). `_check_time_barrier` now **submits the exit and
defers**; a new **idle-loop reconciler** (`_reconcile_pending_position_state`)
runs the confirm/book/re-arm decision on a genuinely-idle main-loop tick, where
`ib.run()` is safe and the loop has turned so the fill is reflected.

## Source changes — `src/live_execution/live_trader.py` only (+383 / −164 region)

| Change | Detail |
|---|---|
| `_check_time_barrier` → submit-and-defer | Keeps the inline exit submission (cancel legs, `close_position`, capture `_exit_oid`, A0 never-submitted hard-fail, clear `_sl_order_id`/`_tp_order_ids`, set `_pending_exit_order_id`), then returns. Deleted the inline A1/A2/route block. Flat-read path now defers (no inline confirm/OOB-book). |
| **BINDING CONDITION 2** re-entrancy guard | Early `return False` when `_pending_exit_order_id is not None` — no second exit, no settled read while one is pending. |
| NEW `_reconcile_pending_position_state()` | Idle-loop reconciler, wired into `_event_loop` **immediately before `_run_hourly_housekeeping()`** (**BINDING CONDITION 1** ordering). Pending-exit branch = a1464d2's A1/A2/route logic relocated byte-for-byte (settled None→fail-closed; 0→`_book_time_barrier_flat` proven price; !=0→retire exit, **BC1** cancel-count 0-vs-≥1 re-scan + strictly-after settled, route). Flat-read branch (**BINDING CONDITION 3**, own trigger = flat cache read, not pending-exit-gated) confirms + books OOB close. Inert on healthy/non-flat/untracked (no `ib.run` per tick). Never-raises boundary: logs + defers, never books/re-arms on a guessed value. |
| NEW `_book_out_of_band_close()` | The `:1668-1725` OOB block relocated. **Books `exit_price=None`** (see behavior note below). |
| 2 SAFE startup call sites untouched | `_recover_inherited_position`, `_cancel_orphaned_orders_on_startup` (run before the loop starts — correct). No signature/interface/adapter changes. |

## Behavior note (accepted, flagged) — OOB close now books `exit_price=None`

The old in-callback OOB close booked `exit_price=current_price` (the bar price at
detection). The relocated `_book_out_of_band_close` books **`None`** — the idle
reconciler has no bar price and, per the project's honest-unknown rule (the exact
principle this ticket family enforces — never fabricate a price), writes an
explicit unknown. **Consequence:** `fleet_health` flags any CLOSED row with NULL
`exit_price` as `incomplete-close` (`fleet_health.py:174`, regardless of reason),
so **out-of-band closes (reconnect-false-flat / manual TWS closes) will now
generate an `incomplete-close` health finding** where the old fabricated price
suppressed it. Judged a correctness-preserving relocation (honest None > fabricated
price), not a regression — 0-naked either way. **Recommended follow-up (not in this
ticket):** have `_book_out_of_band_close` resolve the PROVEN exit price from broker
executions (as `_book_time_barrier_flat` already does) and book NULL only when
unmatched — that eliminates the new health noise and gives the true price when
available.

## Tests (no assertion weakened)

- **NEW** `tests/test_settle_confirm_loop_deferral.py` — 13 cases incl. the
  loop-aware regression that would have caught this (a fake `get_position_settled`
  that genuinely raises `RuntimeError("This event loop is already running")` inside
  a running loop; the existing suite's plain-int mock could never catch it).
- Adapted (submit-then-reconcile flow, every assertion preserved at full strength,
  `_pending_exit_order_id` fixture inits added): `test_time_barrier_exit_fill_
  confirmation.py`, `test_cooldown.py`, `test_exit_reason_and_fill_routing.py`,
  `test_live_trader_bugs.py`, `test_reconnect_false_flat_recovery.py`,
  `test_hourly_order_housekeeping.py`.
- **$296k false-flat guard preserved AND strengthened:** false-flat (settled≠0) and
  unconfirmed (settled raises→None) both assert NO close / NO cancel / NO reset at
  BOTH layers (`_check_time_barrier` defer + reconciler re-verify); the reconciler
  fires the LOUD `position-flat-unconfirmed` health event on the unconfirmed case.

## CANARY REQUIRED before fleet redeploy

Per the standing "canary before pipeline change" rule and the Reviewer's condition:
before the fleet redeploys widely, exercise a real TIME BARRIER exit end-to-end and
confirm — the reconciler books the proven price (no `event loop is already running`
crash, no NULL-price row on a filled exit), and no naked window. The operator will
restart to deploy; the first live TIME BARRIER exit post-restart is the canary.

## Out of scope (unchanged from blueprint)

Corrupted/NULL legacy rows (trade_21/116/98, ES-scale GC trade_27 — SQL prepared);
the TWS bracket-orphan verification for trade_21/trade_116.
