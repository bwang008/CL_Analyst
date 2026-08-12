# Ticket Resolution Blueprint — exit-fill-observability_08112026_1749
**Ticket Directory:** `.agents/collab/tickets/exit-fill-observability_08112026_1749/`
**Status:** Auditor RCA complete; Impact-Reviewer APPROVED with 5 mandatory modifications (M1-M5) and 2 additions (A1-A2). Ready for `/tdd-manager`.
**Severity:** MEDIUM. **Not a regression** (blame: `128dc202` 2026-06-13, `6cafa6c0` 2026-06-23, `942de02f` 2026-07-02; none of the 5 most recent `live_trader.py` commits touch this branch).

---

## Bug Summary

A CL take-profit filled live on 2026-08-11 at 01:08:50. The entire operator-visible record of that exit was ONE line in `reports/fleet/fleet_20260811.log`:

```
2026-08-11 01:08:50 [INFO] [CL cid=1400] LiveTrader: [CL ] [OCA] cancelled 0 resting protective order(s) after TP_HIT
```

No fill price, no qty, no side, no entry price, no bars held, no PnL, no trade id. The heartbeat's realized PnL jumped `-7,403.28 -> -3,958.00` (+$3,445.28) with nothing in between explaining it. No Telegram ping arrived.

### Root cause

**Nothing was muted — the exit-fill log line was never written.** The TP/SL branch of
`LiveTrader._on_standard_execution_event` (`src/live_execution/live_trader.py:7235-7268`)
contains exactly one `log.*` call on the success path: the `[OCA] cancelled ...` line at 7245.
`_reset_position_state` (1384-1454) logs nothing either. Contrast the ENTRY path in the same
method, which logs `[TRADE] ENTRY FILLED: ...` at 7534 and sends an `*ENTRY FILLED*` Telegram at
7517. The exit path has no equivalent.

### Verified secondary findings

1. **The trade booked correctly.** Fleet telemetry DB `active_positions`: `trade_212`, cid 1400,
   LONG 1, entry 80.63, exit 84.08, CLOSED, `TP_HIT`, bars_held 17, close_time
   `2026-08-11T08:08:51.476288`. `telemetry.close_position` succeeded. The ledger is truthful;
   only the operator log is blind.
2. **The PnL jump has a silent source.** `tradebook_events` shows `COMMISSION 08:08:51.480
   order 213 commission 2.36 realized_pnl 3445.28`. The heartbeat's `real=` is
   `telemetry.realized_pnl_total()` (live_trader.py:6603), a SUM over that column.
   `_on_commission_event` (1313-1335) logs only on failure — so the number moved because a row
   silently landed in the DB. This is the direct answer to "the realized changes, but there's no
   real event being captured between".
3. **TP and SL take the IDENTICAL code path** (`if is_tp_fill or is_sl_fill:` at 7235 — one
   branch, one Telegram, one ledger close). The operator's impression that SL hits are chattier
   is an artifact of `cancelled`: when our sweep actually cancels a leg (`cancelled == 1`),
   *ib_insync itself* emits a fat `cancelOrder: Trade(...)` record plus a benign 10148 error
   (see `fleet_20260810.log:1248-1250` and `:1497-1499` around the SI `SL_HIT` of `trade_1125`).
   This CL TP had `cancelled == 0`, so the broker library printed nothing and the silence was
   total. **The "notification" the operator had been relying on was the broker library's, not
   ours.**
4. **Why the Telegram ping is missing is currently unprovable — and that is itself a defect.**
   `TelegramAlerter.send` (`src/live_execution/utils/telegram_alert.py:83-163`) returns `False`
   on failure and the exit site discards the return. Its two likeliest failure branches log at
   **DEBUG** (timeout, line 157; catch-all, line 162), and `setup_fleet_logging`
   (`fleet_log.py:163`) attaches the root handler at **INFO**. Only the non-200 branch (line 148)
   is WARNING, and no such line appears near 01:08:50. So the send either succeeded and was
   missed, or failed invisibly. Telegram was definitely enabled for all five children (the
   14:15 `TELEGRAM SUPPRESSED` watchdog lines prove it).
5. **The watchdog throttle is a red herring.** `_send_watchdog_telegram` / `_WATCHDOG_TG_COOLDOWN_SECONDS`
   (~6707) is a separate helper; the exit branch calls `self._telegram.send` directly.
6. **`cancelled 0` is expected and benign.** Brackets carry native IBKR OCA (`ocaType=2`), so on
   a TP fill the broker pulls the sibling SL server-side before our symbol-scoped sweep runs.
   `cancelled 0` means "the broker already did it". Do NOT promote it to a warning — record it
   as a field (see R1). Consistent with the existing "OCA 10148 benign" finding.

### The hazard that makes this MEDIUM rather than LOW

`src/live_execution/adapters/ibkr_execution.py:112-113` dispatches order-status callbacks with
**no exception isolation**:

```
for cb in self._order_callbacks:
    cb(event)          # bare — contrast _on_commission_report at 90-97, which DOES isolate
```

Any exception raised inside the exit branch propagates out **and aborts the branch before line
7268**, leaving the position flat broker-side while `_position_side`, `_active_trade_id` and the
TP/SL id tracking stay populated in memory — the phantom-position class this fleet has been
burned by before. This constrains every requirement below (see **M1**).

---

## Target Files

- `src/live_execution/live_trader.py` — primary (7235-7268; also 1313-1335, 2552, 2580-2581, 2634-2638, 7012)
- `src/live_execution/utils/telegram_alert.py` — 156-163
- `src/live_execution/telemetry.py` — 1194-1214
- `tests/` — new red-first coverage (see Test Requirements)

Context only, **no change**: `src/live_execution/fleet_log.py:163` (handler level INFO),
`src/live_execution/adapters/ibkr_execution.py:112-113` (deferred — see Out of Scope).

---

## Required Changes

### R1 — `[TRADE] EXIT FILLED` operator line  *(live_trader.py, exit branch)*

Emit ONE INFO line per exit fill, positioned **after** `exit_reason` is set (7238) and **after**
the OCA sweep (so `cancelled` is available), but **before** the Telegram and ledger blocks — so it
survives a failure in either. Style it as a peer of the ENTRY FILLED line at 7534 (`%`-style lazy
formatting, price precision consistent with `_price_decimals(self._tick_size)` as the `[PNL]` line
at 5877 already does).

Required fields — **all are already in scope at 7238**; nothing needs to be captured earlier
(instance state is not cleared until `_reset_position_state` at 7268; attribute names verified
against the entry-seeding block at 7435-7450 and the reset at 1404-1414/1441):

| Field | Source |
|---|---|
| `orderId` | `order_id` (7163) |
| `leg` (TP/SL) | `is_tp_fill` (7226-7233) |
| `reason` (TP_HIT/SL_HIT) | `exit_reason` (7238) |
| `action` (BUY/SELL) | `action_str` (7170) |
| `fill` price | `avg_price` (7164) |
| `qty` | `qty` (7165) |
| `symbol` | `self._execution_symbol` |
| `trade_id` | `self._active_trade_id` |
| `side` (LONG/SHORT) | `self._position_side` |
| `entry` price | `self._entry_price` |
| `bars_held` | `self._position_bars_held` |
| `trailing` latch | `self._trailing_activated` — distinguishes a trailed stop from the original SL |
| `oca_cancelled` | `cancelled` (7244) — folds in finding 6; NOT a warning |
| `gross_est` | computed, see R2 |

### R2 — Realized PnL: what may and may not be sourced here

- **Forbidden:** any blocking broker read inside this callback. The A-2 constraint is documented
  verbatim at `live_trader.py:7363` — *"cached read ONLY (A-2) - a blocking get_position /
  settled read here would deadlock the broker event loop."* Do NOT call `get_account_summary`,
  `get_executions`, or `get_position` from this branch.
- **The authoritative figure does not exist in this callback.** It arrives ~0.7s later as
  `CommissionReport.realizedPNL`, surfaced as `evt.realized_pnl` in `_on_commission_event`.
- **Therefore two lines, not one:**
  - **R2a** — the EXIT FILLED line carries a gross estimate from in-memory state:
    `(avg_price - self._entry_price) * self._position_side * qty * <multiplier>`, where the
    multiplier comes from `self._execution_instrument.multiplier`. **Label it `gross_est=` and
    mark it as excluding commission** — it must never be mistakable for the booked figure.
    (Note: the multiplier property is documented as RAISING on an unknown symbol / missing seam —
    `live_trader.py:4525-4539` — so this computation is exactly the kind of thing M1 must isolate.)
  - **R2b** — add an INFO line to `_on_commission_event` (1313-1335) carrying `order_id`,
    `exec_id`, `commission`, `realized_pnl`. This is the broker's own truth, already in hand, zero
    extra I/O, no blocking read. It reconciles against `gross_est` (3445.28 booked vs 3450.00
    gross - 2.36 commission for `trade_212`) and permanently closes the "realized moved with no
    narrative" complaint. Its caller IS exception-isolated (`ibkr_execution.py:90-97`), so this
    one is low-risk.

### R3 — Telegram send: log the failure, and check the return  *(exit branch, 7249-7257)*

- Replace `except Exception: pass` with an except that **logs at WARNING with `exc_info=True` and
  still swallows**. Non-blocking behavior preserved exactly; only visibility changes.
- **Keep the `except`.** The auditor's claim that it is dead code is wrong: `to_ascii(...)`
  (telegram_alert.py:119) and `datetime.now(tz)` (114) sit OUTSIDE `send()`'s internal try.
- **Additionally capture `send()`'s boolean return** and `log.warning` when it is `False`
  (e.g. "EXIT alert NOT delivered for trade X - the fill IS booked"). This is the single change
  that would have made the 01:08 incident self-explaining.

### R4 — Ledger-close failure must be loud  *(exit branch, 7258-7267)*

Replace `except Exception: pass` around `telemetry.close_position` with:
`log.error(..., exc_info=True)` naming trade_id and exit price → `self._emit_health_event(
"exit-ledger-close-failed", detail)` (helper at 5194, signature `(self, kind, detail)`; it is
opt-in gated and already swallows its own exceptions, so do NOT re-wrap it) → a CRITICAL Telegram.

Hard constraints:
- **Must NOT re-raise** — an escaping exception in a broker callback breaks the event loop.
- **Must NOT skip `_reset_position_state`** — the position IS flat broker-side, so in-memory
  state must still clear. Failure *visibility* only; no control-flow change.

Rationale: a silent failure here leaves the `active_positions` row `OPEN` **forever**, and
`get_open_position` plus the restart-recovery and kill-switch paths read that ledger.

### R5 — Delete the `self._active_trade_id or "unknown"` fallback  *(7260)*

`close_position` is `UPDATE ... WHERE trade_id='unknown' AND status='OPEN'` — it matches nothing,
commits cleanly, and **raises nothing**, so even R4's logging is structurally blind to it. If
`_active_trade_id` is `None`, log an ERROR stating the exit cannot be booked to any ledger row and
do not pass a fabricated id. (Project rule: no silent null defaults.)

### R6 — Raise silent ledger-close failures from DEBUG to ERROR

Three sites log ledger-close failure at `log.debug`, invisible under the INFO fleet handler, all
carrying the same permanently-OPEN-row hazard. Raise all three to `log.error(..., exc_info=True)`:
- `_book_time_barrier_flat` (~2580-2581)
- `_book_retired_leg_close` (~2634-2638)
- `_flatten_book_and_reset` (~7012, `log.debug("[FLATTEN] Failed to close ledger")`) — **M4**;
  fixing two of three would be arbitrary.

### R7 — `telegram_alert.py`: stop hiding lost alerts  *(156-163)*

Raise both swallowed failure paths from DEBUG to WARNING:
- line 157 timeout → e.g. `"Telegram send timed out (%.1fs) - message NOT delivered"`
- line 162 catch-all → e.g. `"Telegram send failed - message NOT delivered"` with `exc_info`

Each occurrence is a **lost operator alert**, exactly the failure class this ticket exists to
expose, and the non-200 branch two lines above (148) is already WARNING — the current split is
inconsistent, not deliberate. `send()`'s "Never raises" contract (docstring line 99) is untouched.

Spam risk assessed and accepted: `TelegramAlerter` has ~33 call sites; the highest-frequency is
the **hourly** heartbeat (1003, 6274). A total Telegram outage yields on the order of 5 WARNING
lines/hour fleet-wide against ~120 routine lines/hour.

### R8 — `telemetry.close_position` returns its rowcount  *(telemetry.py:1194-1214)*

Execute through a cursor and `return cur.rowcount`. Purely additive: this is the ONLY telemetry
implementation (the other `close_position` hits are `exec_client`'s unrelated
`(symbol, exit_mode, current_price)` method), all 6 internal callers discard the return, and
`repair_closed_position` (1223) already returns `int`.

Then have the exit branch WARN on rowcount 0 — the only way to detect the "WHERE matched nothing"
class (row already CLOSED by another path, or the shared-fleet-DB `_client_scope()` binding did
not match) that R4 cannot see.

---

## Mandatory Modifications (Impact-Reviewer — binding)

- **M1 (blocking).** Every new statement inside the 7235-7268 branch — R1's line, R2a's
  `gross_est`, R4's health event and Telegram, R8's rowcount check — must be **individually
  exception-isolated** so nothing new can prevent `_reset_position_state` from running. Build the
  EXIT line from pre-formatted strings (no `%.2f` against a possibly-`None`), and wrap the whole
  emission so a formatting error degrades to a minimal `orderId/reason/fill` line rather than
  escaping. Rationale: the unisolated dispatch at `ibkr_execution.py:112-113`.
- **M2.** `gross_est` renders `n/a` when `_entry_price is None` **or** `_position_side == 0`
  **or** the multiplier lookup raises. A `0.0` here would be a fabricated number on a money line.
- **M3.** Reorder R4: `log.error(exc_info=True)` → `_emit_health_event` → `_reset_position_state`
  → CRITICAL Telegram (capture `_active_trade_id` into a local first, since the reset clears it).
  No blocking network I/O between exit detection and the state reset. **Cap the branch at TWO
  Telegram sends total** — each costs up to 3s and a Markdown-400 retry doubles that.
- **M4.** Extend R6 to the third site (`_flatten_book_and_reset` ~7012). Folded into R6 above.
- **M5.** R8's rowcount-0 message is **WARNING only** — no Telegram, no health event — and its
  text must name the two benign causes so it does not read as an alarm. Guard the comparison
  against non-int returns from `MagicMock` telemetry in tests.

## Additions (Impact-Reviewer)

- **A1 — `_book_time_barrier_flat` (~2552) logs NOTHING on success.** It closes the ledger and
  resets state in total silence; this path is *blinder than the one the operator reported*. Add
  one INFO line carrying `trade_id`, reason, resolved exit price (or `n/a`), and `bars_held`.
  Small, in a method R6 already opens, no new state. Every other exit path was checked and is
  already covered: `_book_retired_leg_close` WARNs with reason + price (2620-2624); OOB recovery
  INFOs `[RECOVERY] OOB exit recovered for trade %s: %s @ %s` (3357-3360) and WARNs the
  unrecovered case; `_flatten_book_and_reset` CRITICALs the flatten.
- **A2 — add a red test that `_reset_position_state` is still reached when the EXIT line's inputs
  are degenerate** (`_entry_price=None`, `_position_side=0`, `_execution_instrument` raising).
  This is the M1 regression guard.

## Test Requirements (red-first)

Mirror the conventions in `tests/test_exit_reason_and_fill_routing.py` and
`tests/test_commission_capture.py` (both already exercise this branch and the commission callback).

1. A TP fill emits an EXIT FILLED record at INFO containing order id, reason, fill, qty, entry,
   bars_held, trade_id, `gross_est`, `oca_cancelled` (use `caplog`).
2. Identical assertions for an SL fill — locks in "same path".
3. `_entry_price is None` renders `n/a`, never `0.0`, and does not raise.
4. `telemetry.close_position` raising → ERROR logged, health event emitted, `_reset_position_state`
   still called, **no exception escapes** the callback.
5. `close_position` returning rowcount 0 → WARNING logged (no Telegram, no health event).
6. `_telegram.send` returning `False` → WARNING; `send` raising → WARNING and fill handling
   completes.
7. `_active_trade_id is None` → ERROR logged, `"unknown"` never passed to the ledger.
8. `_on_commission_event` emits an INFO line carrying commission and realized_pnl.
9. All new operator strings are ASCII-only (`assert s.isascii()`) — project rule; `ascii_safe.py`
   is a net, not a license.
10. **(A2)** Degenerate EXIT-line inputs still reach `_reset_position_state` and raise nothing.

## Out of Scope (explicitly deferred)

- **Follow-up ticket:** mirror the per-callback exception isolation from `_on_commission_report`
  (`ibkr_execution.py:90-97`) into `_on_order_status` (112-113). Right long-term fix, but it
  changes fill-handling failure semantics fleet-wide and needs its own canary.
- **Follow-up ticket (low priority):** duplicate `EXECUTION_FILL` tradebook rows — the
  `_processed_exit_order_ids` dedupe return at 7213 sits *after* the tradebook write at 7193-7211.
  Distinct `event_id`s, no constraint violation, nothing double-booked; cosmetic DB noise only.
- A full EXIT-detail line for the TIME_BARRIER path beyond A1's single INFO line.
- No change to exit semantics, order routing, or what gets booked.

## Deployment Note (requires operator scheduling — NOT part of implementation)

Every change is inert until a **manual fleet restart**. Restarts on this system are an
operator-scheduled event (no systemd on Windows, 5/5 restart caps, the Saturday gateway re-auth
window). Land the code, run the suite, and let the operator pick the restart slot. **Natural
canary: the next TP or SL fill on any of the five bots** — the new EXIT FILLED line plus a
COMMISSION line should both appear, and a Telegram should arrive.
