# TDD Result — exit-fill-observability_08112026_1749

**Outcome: GREEN.** Blueprint implemented in full (R1-R8, M1-M5, A1-A2). No test file was
modified by the Coder; no existing test was weakened.

## Test outcomes

| Stage | Command | Result |
|---|---|---|
| RED (pre-implementation) | `pytest tests/ -m "not slow"` | **32 failed, 2702 passed, 1 skipped** (348s) — all 32 failures in the new file, zero pre-existing tests disturbed |
| New tests only (post) | `pytest tests/test_exit_fill_observability.py -v` | **33 passed** (2.46s) |
| Seam files (post) | 11 files driving the exit branch, commission callback, telemetry, OCA, time-barrier, rollover, trailing latch | **144 passed** (4.79s) |
| Extra safety net (post) | time-barrier fill confirmation, settle-confirm deferral, OOB entry recovery, restart cooldown recovery, cooldown | **80 passed** (2.80s) |
| **GREEN (full fast suite)** | `pytest tests/ -m "not slow"` | **2734 passed, 1 skipped, 279 warnings** (359s) |

## Files changed

**`src/live_execution/live_trader.py`** (+~260 lines, all additive logging / except-body substitutions)
- New module-level helpers beside `_price_decimals`: `_fmt_price(value, decimals)` and
  `_fmt_money(value)`. Both render `"n/a"` for absent/unrenderable input and never fabricate
  `0.0` (M2). Pre-formatting to `str` is also the M1 mechanism — every new log call passes `%s`
  with already-built strings, so a lazy `%`-format error cannot be raised inside `logging`
  outside the try blocks.
- **Exit branch (R1, R2a, R3, R4, R5, R8; M1, M2, M3, M5).** `exit_trade_id` captured as the
  branch's first statement; `state_reset_done` tracks the single reset. Order: OCA sweep ->
  `[TRADE] EXIT FILLED` INFO line -> Telegram -> ledger. The EXIT line carries orderId, leg,
  reason, symbol, action, side, fill, qty, entry, `gross_est` (labelled `excl commission`),
  bars_held, trailing, `oca_cancelled`, trade_id — wrapped so a rendering failure degrades to a
  minimal `orderId/reason/fill` line instead of escaping. `gross_est` is separately isolated and
  only attempted when `_entry_price is not None and _position_side` is truthy, so a raising
  `_execution_instrument`/`.multiplier` yields `n/a`. Position context is read via `getattr`,
  which is what keeps the flagged `test_sl_fill_still_routes_to_exit_path` stub green.
- Telegram: `except Exception: pass` -> `log.warning(exc_info=True)`, plus a `False`-return
  WARNING naming the trade (R3).
- Ledger: `or "unknown"` deleted — a `None` trade id logs ERROR and calls nothing (R5). A raising
  `close_position` runs ERROR -> `_emit_health_event("exit-ledger-close-failed", ...)` ->
  `_reset_position_state` -> CRITICAL Telegram, in that order, with a hard cap of 2 sends in the
  branch (R4, M3). A rowcount of exactly `0`, guarded by `isinstance(rowcount, int)`, is WARNING
  only and names both benign causes (R8, M5).
- `_on_commission_event`: new `[TRADE] COMMISSION:` INFO line with orderId, execId, symbol,
  commission, realized_pnl (`n/a` on the DBL_MAX sentinel), in its own try (R2b).
- R6/M4: the three `log.debug` ledger-close failures raised to `log.error(..., exc_info=True)`,
  each now naming trade id and reason — `_book_time_barrier_flat`, `_book_retired_leg_close`,
  `_flatten_book_and_reset`.
- A1: `_book_time_barrier_flat` gained a `[TIME BARRIER] EXIT BOOKED:` INFO line (trade_id,
  reason, exit price or `n/a`, bars_held, orderId), emitted before the ledger write.

**`src/live_execution/utils/telegram_alert.py`** (R7)
- Timeout and catch-all failure paths raised DEBUG -> WARNING with "message NOT delivered".
- The em-dash in the timeout message replaced with an ASCII hyphen; two further em-dashes in
  emitted operator strings in the same file fixed (the `requests` not-installed WARNING and the
  `alerts DISABLED` INFO). Same crash class, one character each.
- `send()`'s "Never raises" contract untouched.

**`src/live_execution/telemetry.py`** (R8)
- `close_position` executes through a cursor and returns `cur.rowcount` (`-> int`), with a
  docstring naming the two rowcount-0 causes. Purely additive — all existing callers discard the
  return, and `repair_closed_position` already reports rowcount the same way.

**`tests/test_exit_fill_observability.py`** — NEW, 33 tests.

## Ticket-Manager verification (independent of the subagents)

- `_emit_health_event` (live_trader.py:5277-5306) confirmed to swallow all exceptions internally
  and to return early when disabled — so calling it unwrapped inside the ledger-failure `except`
  cannot break M1's guarantee that `_reset_position_state` is reached.
- Diff reviewed in full: no control-flow change outside the added visibility, no change to order
  routing or to what gets booked, and `_reset_position_state` is reachable on every path
  (`state_reset_done` guard).

## Deferred to their own tickets (recorded in the blueprint)

1. Mirror the per-callback exception isolation from `_on_commission_report`
   (`adapters/ibkr_execution.py:90-97`) into `_on_order_status` (112-113). This is the right
   long-term fix for the M1 hazard class; it changes fill-handling failure semantics fleet-wide
   and needs its own canary.
2. Duplicate `EXECUTION_FILL` tradebook rows — the `_processed_exit_order_ids` dedupe return sits
   *after* the tradebook write. Distinct `event_id`s, nothing double-booked; cosmetic DB noise.

## Deployment

**Inert until an operator-scheduled fleet restart** (no systemd on Windows; 5/5 restart caps;
Saturday gateway re-auth window). Canary: the next TP or SL fill on any of the five bots should
produce a `[TRADE] EXIT FILLED` line, a `[TRADE] COMMISSION` line, and a Telegram ping.
