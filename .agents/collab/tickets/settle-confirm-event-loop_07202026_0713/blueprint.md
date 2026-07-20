# Ticket Resolution Blueprint — settle-confirm-event-loop_07202026_0713
**Ticket Directory:** `.agents/collab/tickets/settle-confirm-event-loop_07202026_0713/`

> ## ✅ OPERATOR-AUTHORIZED 2026-07-20 (human-authorization gate cleared)
> The Impact-Reviewer required Human Authorization (Refactor Veto on a live
> real-money order-routing path). The operator authorized Direction A **with the
> Reviewer's 4 binding conditions attached** and will restart the fleet to deploy
> once implemented + validated. Deploy remains operator-gated. **Canary required
> before the fleet redeploys** (standing "canary before pipeline change" rule).

## Bug Summary

**Incidents:** 2026-07-19 16:00 PT (GC/MGC, event `..._800d842eb78e`) and
2026-07-20 07:00 PT (CL, event `..._e778ce9f6b31`) — the FIRST TIME BARRIER exit
each child took since commit `a1464d2` (2026-07-16) deployed. Deterministic: every
TIME BARRIER exit hits it.

**Root cause (verified):** `a1464d2`'s TIME BARRIER exit confirm-gate calls
`_confirm_settled_position` → `ExecutionInterface.get_position_settled`
(`adapters/ibkr_execution.py:121`) → `IBKRClient.get_position_settled`
(`ibkr_client.py:652`), whose body does `self.ib.run(asyncio.wait_for(
self.ib.reqPositionsAsync(), ...))`. `ib.run()` = `loop.run_until_complete()`,
which **requires the asyncio event loop to be idle**. But `_check_time_barrier` is
reached ONLY via the ib_insync `updateEvent` bar-update callback chain
(`_on_bar_update_5m/_1h` → `_on_new_bar` → `_check_time_barrier`), which fires
**from inside the already-running event loop**. Re-entering the loop via `ib.run()`
raises `RuntimeError: This event loop is already running`
(`asyncio/base_events.py:622`). `_confirm_settled_position` catches it
(`except Exception → return None`), so the gate **fail-closes on every call** and
can never actually confirm. The position is left to the hourly housekeeping sweep,
which OOB-closes it with a **NULL exit price** (the ledger side-effect: trade_21,
trade_116; a "possible orphan brackets" warning also fires — operator TWS-verify).

**Fail-SAFE, not naked:** both incidents ended 0-naked (kill switch / exit-fill /
housekeeping held). The defect is that the confirm-gate's *intended* function
(confirm fill → book proven price → or cleanly re-arm on non-fill) never runs.

**Architectural crux (why the fix MUST be structural, not a patch):** even if the
`RuntimeError` were avoided, an in-loop cache read (`self.ib.positions()`) would be
WRONG — the just-submitted exit's fill event is queued *behind* the executing
callback, so the loop has not turned; a same-tick read returns the STALE pre-exit
position and would misroute (cancel a real fill / re-arm onto a flat book). **The
settled confirm can only be meaningful on a LATER, genuinely-idle tick.** The
codebase already solved this exact class once: `_deferred_resubscribe`
(`live_trader.py:~4195-4202`) documents the identical "This event loop is already
running" failure and defers to a later idle iteration.

**Recent regression:** YES — from `a1464d2` (2026-07-16). Per protocol, not
fast-tracked; went through Auditor + Impact-Reviewer.

## Target Files

- `src/live_execution/live_trader.py` — **the only source file changed** (Direction
  A needs no signature/interface/adapter/`ibkr_client.py` changes; the Reviewer
  confirmed zero signature changes, single component).
- `tests/test_time_barrier_exit_fill_confirmation.py` — extend (see Tests; the
  existing suite mocks `get_position_settled` to a plain int, which is exactly why
  this shipped uncaught).
- **NEW** `tests/test_settle_confirm_loop_deferral.py` — loop-aware regression
  coverage (Binding Condition 4).

## The three hazardous call sites (all must stop calling `_confirm_settled_position` in-loop)

`_confirm_settled_position` has 5 call sites. **Two are SAFE** and MUST NOT change —
`_recover_inherited_position` (`:2216`) and `_cancel_orphaned_orders_on_startup`
(`:2715`) run at `start()` **before** subscriptions/`_event_loop` wire up, so the
loop is genuinely idle. **Three are HAZARDOUS** (all reached only via the in-loop
`_check_time_barrier`):

| Site | Current role | Origin |
|---|---|---|
| `:1657` | first-flat-read confirm before an OOB close (reconnect-false-flat) — triggered by a flat cache read for a tracked trade **with NO pending exit** | `246c5989` 07-08 |
| `:1806` | A1 post-exit confirm (gate the book/reset) | `a1464d2` 07-16 |
| `:1875` | `_route_retired_time_barrier_exit` re-confirm after retiring the exit | `a1464d2` 07-16 |

## Required Changes

### The invariant this ticket establishes

> **`_confirm_settled_position` (and therefore `get_position_settled` /
> `self.ib.run()`) is NEVER called from inside the ib_insync bar-update callback
> (`_check_time_barrier` and anything it calls in-tick). Every settled-based
> decision for a tracked trade is made on a genuinely-idle main-loop tick, by a
> single reconciler.** `_check_time_barrier`, running in-callback, may only
> *submit* orders (non-blocking `placeOrder`/`cancelOrder`) and *record intent*
> (set state); it must never confirm, book, or re-arm off a settled read.

### Change 1 — New idle-loop reconciler `_reconcile_pending_position_state()`

Add a method invoked from the main event loop (`_event_loop`, near `:5576`) in the
**genuinely-idle context** where `_run_hourly_housekeeping()` and
`_attempt_pending_roll_resolution()` already run safely between `ib.sleep()` calls.
It owns ALL settled-based confirmation for a tracked trade. Each invocation, in this
order:

1. **BINDING CONDITION 1 (ordering, load-bearing): the reconciler MUST run BEFORE
   `_run_hourly_housekeeping()` each poll.** If housekeeping's OOB-closer (or the
   5-min kill switch) acts on the same pending exit first, the NULL-price row
   persists — just sooner. Wire the reconciler call in immediately *above*
   `self._run_hourly_housekeeping()`.
2. **Pending-exit branch** — if `_pending_exit_order_id is not None`: run the
   settled-decision logic that a1464d2 currently has inline at `:1802-1865` +
   `_route_retired_time_barrier_exit` (`:1867-1895`), **byte-for-byte identical
   decision logic**, only relocated to this idle context. That is: A1
   `_confirm_settled_position` (now succeeds — loop is idle and has turned since
   submission, so the fill is reflected and the snapshot is authoritative);
   `None` → fail-closed defer; `0` → `_book_time_barrier_flat` (proven execution
   price, NULL if unmatched, NEVER `current_price`); `!= 0` → A2 cancel the exit →
   **BINDING CONDITION 1 (never re-arm while the exit could still fill)**: on
   `cancel_count == 0` route on a fresh settled read; on `cancel_count >= 1`
   re-scan `get_open_trades` and only once the exit has LEFT the book take the
   settled read STRICTLY AFTER, else defer. Preserve `_note_time_barrier_deferral`
   / `_MAX_TIME_BARRIER_EXIT_ATTEMPTS` escalation unchanged.
3. **Flat-read branch (BINDING CONDITION 3 — the gap)** — else if
   `_active_trade_id is not None and _pending_exit_order_id is None`: read the
   cached `get_position()`; if it reads **flat**, this is the `:1657`
   reconnect-false-flat case → do the settled confirm HERE (idle) and, on a
   confirmed flat, perform the OOB-close booking (the `:1668+` block). This branch
   MUST have its own trigger (a flat cache read for a tracked trade), NOT be gated
   on `_pending_exit_order_id` — folding it under the pending-exit gate would
   silently drop OOB-close confirmation in the common case (the Reviewer's
   explicit finding). On `settled is None` → fail-closed (retain, defer), exactly
   as `:1658-1666` does today.
4. Never-raises boundary: like housekeeping/rollover, the reconciler must not throw
   into the event loop (wrap its body so a failure logs + defers, never crashes the
   child) — but this is the ONLY permitted catch, and it must NOT swallow-and-guess
   a position value (no cheap fix); an internal failure defers to the next tick,
   never books/re-arms on a guess.

### Change 2 — `_check_time_barrier` becomes submit-and-defer (in-callback)

Rewrite `_check_time_barrier` so it makes **no** `_confirm_settled_position` call:

- **Time-barrier-hit path** (currently `:1760-1865`): keep the inline exit
  SUBMISSION exactly as-is through setting `_pending_exit_order_id = _exit_oid`
  (cancel legs, `close_position`, capture `_exit_oid`, A0 never-submitted hard-fail,
  clear `_sl_order_id`/`_tp_order_ids`, register the exit id). Then **return
  (defer)** — delete the inline A1 gate + A2 + route block (`:1802-1865`); that
  logic now lives in the reconciler (Change 1 step 2). The A0 never-submitted branch
  (`:1784-1794`) stays inline (it does not call `_confirm_settled_position` — it
  re-arms because no live exit exists — safe in-callback).
- **Flat-read path** (currently `:1652-1667`): remove the inline
  `_confirm_settled_position` call. On a flat cache read for a tracked trade with no
  pending exit, **defer** — return without booking; the reconciler's flat-read
  branch (Change 1 step 3) owns the confirm + OOB-close. (Do not book an OOB close
  in-callback off an unconfirmed flat.)

### Change 3 — BINDING CONDITION 2: `_check_time_barrier` re-entrancy guard

While `_pending_exit_order_id is not None`, subsequent bar callbacks must NOT
re-submit a second exit or otherwise re-drive the exit path (a second
`close_position` or a repeat settled read — the latter would re-crash). Add an
early guard: if a pending time-barrier exit is outstanding, `_check_time_barrier`
returns immediately (the reconciler is resolving it). Trailing-stop / normal
in-position management that does NOT touch the settled read may continue per
existing behavior, but no new exit is submitted until the reconciler clears
`_pending_exit_order_id`.

### Change 4 — Deferral window / safety net (unchanged behavior, verify)

The submission→confirm gap is now one poll interval (`_POLL_INTERVAL = 5.0`s,
`live_trader.py:137`) instead of "up to ~1h until housekeeping." During that gap the
position is tracked with `_sl_order_id` cleared, so the **5-minute kill switch**
remains the safety net exactly as today — do not weaken or bypass it.

## Tests (Binding Condition 4 — loop-aware; NO test loosening)

The existing `tests/test_time_barrier_exit_fill_confirmation.py` sets
`exec_client.get_position_settled.return_value = 1/0/None` — it **mocks away the
`ib.run()` call that raises**, so it cannot catch this bug class. Do not weaken it;
its decision-logic assertions still apply to the reconciler (the logic moved, not
changed). Add coverage:

1. **NEW `tests/test_settle_confirm_loop_deferral.py`** — the regression that would
   have caught this:
   - A fake exec client whose `get_position_settled` **raises `RuntimeError("This
     event loop is already running")` when invoked from within a running asyncio
     loop** (or an asyncio-loop-aware harness that makes a real `reqPositionsAsync`
     require the loop to turn). Assert that driving `_check_time_barrier` from
     inside a running loop (the real call context) **submits the exit but makes NO
     settled call inline, no crash, no NULL-price book, no reset** — i.e. it
     defers.
   - Then invoke `_reconcile_pending_position_state()` in an idle context with
     `get_position_settled` returning `0` + a matching `get_executions` record →
     asserts the ledger books the **proven** execution price (never
     `current_price`), position state resets, `_pending_exit_order_id` cleared.
   - `settled is None` in the reconciler → fail-closed (no book, no reset, no
     re-arm, stays tracked).
   - **Binding Condition 1 preserved:** `settled != 0` then `cancel_orders_by_ids →
     0` books the proven price and does NOT re-arm; `cancel → >=1` with the exit
     still in `get_open_trades` defers (no re-arm).
   - **Binding Condition 2:** a second `_check_time_barrier` call while
     `_pending_exit_order_id` is set does NOT submit a second exit and does NOT call
     `get_position_settled`.
   - **Binding Condition 3:** a tracked trade whose cached `get_position()` reads
     flat with NO pending exit → `_check_time_barrier` defers (no inline confirm);
     the reconciler's flat-read branch then confirms and books the OOB close; a
     `None` settled there fails closed (retains position).
2. **Ordering assertion:** the reconciler runs before `_run_hourly_housekeeping`
   each poll (unit-test the wiring, or assert call order with mocks).
3. Full fast suite green: `conda run -n trader python -m pytest tests/ -m "not slow"`.

## Hard constraints (violating any fails the ticket)

- **NO CHEAP FIXES.** Forbidden: catching the `RuntimeError` and returning a
  guessed/default position (the tempting shortcut — explicitly banned); `try/except:
  pass`; defaulting a missing required field to None/fallback (must RAISE); blind
  retries/sleeps; loosening/skipping tests or widening assertions; hardcoding
  today's data. The reconciler's never-raise boundary defers on failure — it must
  never book or re-arm on an unconfirmed/guessed value.
- **Parity:** LiveTrader-only machinery; the backtest byte-identical gate is
  untouched. Do not touch `round_to_tick` / bracket-child pricing.
- **All 5 children** share this path — the fix is systemic; the canary must exercise
  a real TIME BARRIER exit end-to-end (confirm → proven-price book, no NULL row, no
  crash) before the fleet redeploys.
- Confine source changes to `src/live_execution/live_trader.py`. If a
  signature/interface/base-class change appears necessary, STOP and report — that
  would exceed the authorized Direction A scope.
- **Deploy is operator-gated.** Commit with "deploy pending operator restart";
  never claim DEPLOYED. Branch = current fleet working branch
  (`git branch --show-current`); stage file-by-file, leave operator WIP untouched.

## Out of scope (separate, operator-gated — do NOT touch here)

- The already-written corrupted/NULL ledger rows (trade_21, trade_116, trade_98,
  and the ES-scale GC trade_27 = 7484.75). trade_27 has repair SQL prepared; the
  NULL rows are the project's honest-unknown convention, not corruption. This fix
  stops NEW NULL rows; it does not backfill old ones.
- The TWS bracket-orphan verification for trade_21 / trade_116 (operator action).
