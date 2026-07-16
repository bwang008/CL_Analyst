# Ticket Resolution Blueprint — exit-fill-unverified_07152026_1855
**Ticket Directory:** `.agents/collab/tickets/exit-fill-unverified_07152026_1855/`

> ## ⛔ HUMAN AUTHORIZATION REQUIRED BEFORE IMPLEMENTATION
> This changes **order-routing semantics on a LIVE real-money account** (project
> HUMAN GATE: *"the fix would change trading economics, model selection, order
> routing semantics, or manifest risk parameters"*). Severity **HIGH**.
> The Impact-Reviewer APPROVED the design subject to the two BINDING CONDITIONS
> below, but **the operator must authorize before `/tdd-manager` implements.**
> Deploy (fleet restart) is operator-gated regardless.

## Bug Summary

**Incident:** 2026-07-14 16:00:05 PT, live NG child (`NG01B_Sharpe_E03_07052026`,
client_id 3000, NYMEX NGQ26). A TIME BARRIER exit left a **NAKED + UNTRACKED**
live position on a real account for 29+ minutes. No automated layer recovered it;
a human had to intervene in TWS. Queue events:
`NG01B_Sharpe_E03_07052026_8ac2bba87abd` (housekeeping-untracked-position) and
`NG01B_Sharpe_E03_07052026_bb2a0555dd87` (housekeeping-naked-position).
`broker_audit` confirmed: `naked-position | NG/20260729 | pos=+1 has NO resting stop`.

**Root cause — the TIME BARRIER exit is fire-and-forget.** `live_trader.py:1679-1724`
destroys every piece of state that proves a position exists, *before* proving the
exit happened:

| Line | Action | Problem |
|---|---|---|
| `:1679` | `cancel_open_orders(...)` | Cancels SL + TP **first** — creates the hazard |
| `:1682` | `close_position(...)` | Return value used **only** at `:1701`/`:1719` to scrape `orderId`; **fill status never read**. `close_cl_position` (`ibkr_client.py:824`) returns `ib.placeOrder(...)` — a *submitted* order, not a fill — and can return `None` (`:825`) |
| `:1708-1714` | `telemetry.close_position(exit_price=current_price)` | Books the ledger CLOSED with a price **that never traded** |
| `:1724` | `_reset_position_state(...)` | Nulls `_sl_order_id` (`:1219`) and `_active_trade_id` (`:1222`) |

The exit (order 71 @ 2.911, `tif='GTC'`) never filled — the limit was priced off a
**stale bar close** and rested forever as the market drifted to ~2.905.

**Why nothing recovered it — both safety nets were disarmed by the same lines:**

1. **Kill switch** `_check_naked_position` (`:5762`) — detects and *flattens* naked
   positions, runs every 5 min (`_HEARTBEAT_CYCLES=60` × `_POLL_INTERVAL=5.0`).
   First guard `if self._active_trade_id is None: return` (`:5776`). `:1724` had just
   nulled it → returns immediately, **never queries the broker**. Confirmed:
   `grep -ci "KILL SWITCH" reports/fleet/fleet_20260714.log` → **0**. Armed, it would
   have caught this ~16:05 instead of never. The codebase's own comment at `:6015`
   says *"kill switch will flatten the naked position"* — the design **depends** on a
   net this path defeats.
2. **Housekeeping auto-heal** (`:2813`) — requires an OPEN ledger row. `:1708` marked
   it CLOSED → `if open_row is None:` (`:2757`) took the **UNTRACKED** branch, which is
   deliberately detect-only (`:2755`, operator decision 2026-07-08).

**Neither net is broken.** Both are keyed on the bot's *belief* that it holds a
position; the exit path destroys that belief before confirming the exit filled.

**Corrected premise (do not chase this):** the logged `buffer=0.00` is a **`%.2f`
log-format artifact**. `ibkr_client.py:781` computes `buf = 2 * inst.tick_size` =
**0.002** for NG (`tick_size=0.001`, `instrument_master.py:151`); `:788-790` prints it
with `%.2f`. The buffer **was** applied (CL logs `0.02`, GC `0.20`, ES `0.50`, SI
`0.01` — all 2 ticks). A buffer change is aimed at the wrong mechanism.

**Blast radius: all five live children**, not just NG — every fleet config sets
`"exit_mode": "marketable_limit"` (`HS14B_Sharpe_E01_06262026`,
`ES02B_Sharpe_E01_07112026`, `NG01B_Sharpe_E03_07052026`, `GC02B_Sharpe_E04_07102026`,
`SI01B_Sharpe_E02_07062026`). CL took the identical path at 09:00:05 that same day and
filled — **luck, not protection**.

**Not a recent regression:** `85c08ded` (2026-02-27) introduced the cancel→close→reset
shape; `96af1e95` (2026-05-16) added `exit_price=current_price`; `128dc202` /
`c0bddfa9` were adapter threading / a `reason` string, no behavior change. A ~4.5-month
latent defect, exposed by NG's thin tick + a stale-close limit.

**Ledger corruption is permanent by construction:** `_HOUSEKEEPING_OVERWRITE_REASONS`
(`:196`) omits `TIME_BARRIER`, so `repair_closed_position` (`:2730`, skip at `:2712`)
can never overwrite the fabricated price. Late fills are also silently dropped — with
`_tp_order_ids`/`_sl_order_id` already cleared, a late fill hits `:5991`
*"UNRECOGNIZED FILL … ignoring"*. **Not self-healing.**

## Target Files

- `src/live_execution/live_trader.py` — **the only source file changed.**
- `tests/test_cooldown.py` — fake-fidelity repair
- `tests/test_exit_reason_and_fill_routing.py` — fake-fidelity repair
- `tests/test_live_trader_bugs.py` — fake-fidelity repair
- `tests/test_oob_entry_state_recovery.py` — fake-fidelity repair + `exit_price` assertions
- `tests/test_hourly_order_housekeeping.py` — `exit_price` assertions
- `tests/test_time_barrier_exit_fill_confirmation.py` — **NEW** regression test

**Zero signature changes. Zero interface changes. Zero base-class edits. No
`ibkr_client.py` / adapter / `simulated_execution` modelling work.** All primitives
already exist on `ExecutionInterface` **and both** adapters:
`_confirm_settled_position` (`:1856`), `_verify_and_heal_protective_legs` (`:2071`),
`cancel_orders_by_ids` (interface `:154`), `get_executions` (interface `:172`).

## Required Changes

### The invariant this ticket establishes

> **Never book a close, never reset position state, and never re-arm protection
> until the broker has been asked and has answered.** Concretely: (1) book only on
> a *confirmed* flat with a *proven* fill price; (2) re-arm the stop **only** once
> no exit order that could still fill is live **and** the position is confirmed
> still open.

### Site A — `live_trader.py:1679-1725`, the TIME BARRIER exit branch

**KEEP `:1679` `cancel_open_orders` → `:1682` `close_position` exactly as they are,
in that order.** (Reversing them was rejected — see "Explicitly rejected" below.)

**A0 — capture the exit order id immediately.** From the `close_position` return,
take `_exit_oid`. If `trade is None` (the `close_cl_position:825` no-match return) or
it carries no `orderId`, **the exit was never submitted**: do NOT book, do NOT reset,
keep the trade tracked, `log.critical`, re-arm protection (no live exit exists, so
re-arming is safe here), `return False`. A missing `orderId` is a **hard failure** —
no silent-None default.

**A1 — gate on broker truth.** Call `_confirm_settled_position(self._execution_symbol)`
(already fail-closed on `None`; already called in this same method at `:1592`, so the
"main thread, event loop idle" contract at `ibkr_client.py:665-666` is satisfied in
this exact call context). Three branches:

- **`0` — flat, the exit filled.** Resolve the **true fill price** from
  `get_executions(symbol)` matched on `_exit_oid`. Book with that **proven** price;
  if no execution matches, write **NULL** — never `current_price`. Then
  `_reset_position_state(reason="TIME_BARRIER")` and `return True`.
- **`None` — unconfirmed.** Fail closed, mirroring the existing `:1593-1601` precedent:
  no ledger write, no reset, keep `_active_trade_id`, **do NOT re-arm** (the exit is
  still live and can still fill — BINDING CONDITION 1), `return False`.
- **non-zero — the incident: the exit did not fill.** Proceed to A2.

**A2 — retire the exit order before touching protection.**

1. `cancel_orders_by_ids([_exit_oid])` — **always cancel the stranded GTC exit first.**
   Non-negotiable: leaving it resting lets it double-fill against a re-armed stop.
   Note it returns the count of ids **found open**, and
   (`ibkr_execution.py:298-313`) it iterates `openTrades()` and calls `ib.cancelOrder`
   **fire-and-forget** — it does **not** wait for a terminal state.

2. **BINDING CONDITION 1 (Reviewer, binding) — never re-arm while an exit that can
   still fill is live.** Branch on the returned count:
   - **count == 0** → the exit is **not open** (already filled, or never rested) — a
     filled order has already left `openTrades()`, so the cancel is a silent no-op.
     No live exit ⇒ safe to proceed. **Re-confirm settled**, then route:
     `0` → the A1 flat branch (proven fill price, book, reset, `return True`);
     non-zero → the exit died without filling ⇒ **safe to re-arm** (A3);
     `None` → unconfirmed ⇒ no ledger write, no reset, keep tracked, **no re-arm**,
     `return False`.
   - **count == 1** → the exit is only *cancel-requested*, **not dead** — it can still
     fill at the exchange (this codebase documents the race at
     `ibkr_client.py:1583-1588`: *"Fast fills … reliably trigger this race condition"*).
     **Re-scan `get_open_trades` until `_exit_oid` has left the book, and take the
     settled read STRICTLY AFTER that — the ordering is load-bearing.** Once gone,
     route exactly as `count == 0`. **Still open ⇒ defer: stay tracked, no re-arm, no
     ledger write, `return False`** (retry next bar).

   *Rationale:* without this, `settled=1 → cancel=1 → re-confirm=1 (snapshot pre-dates
   the fill) → re-arm SL+TP → exit fills` leaves a **resting STP/LMT on a flat book —
   a stop that *opens* a naked reversal**, bounded only by the next-bar OOB branch
   (**up to an hour on NG's 1H brain**). Deferring is safe: the position stays tracked
   with `_sl_order_id` None, so the **5-minute kill switch covers the gap** — and
   flattening a position already past its time barrier is the desired end state anyway.

**A3 — re-arm and stay tracked.** Only when A2 established that no live exit can fill
**and** the position is confirmed still open: call
`_verify_and_heal_protective_legs(...)` to re-place SL/TP from the ledger's stored
prices. Keep `_active_trade_id`, increment the attempt counter, **no ledger write**,
`return False` → retried next bar.

**A4 — bounded escalation.** Bound retries by **bars/attempts, never sleeps** (blind
sleeps are forbidden and would block the event loop). On exhaustion: `log.critical` +
Telegram + `_emit_health_event`, and **keep the position TRACKED** so housekeeping's
**heal** branch (`:2813`) owns it rather than the detect-only UNTRACKED branch
(`:2757`).

**Free consequence — do not add code for this:** because A1–A4 keep `_active_trade_id`
set while `_sl_order_id` is None, `:5776` and `:5782` both pass and **the kill switch
re-arms for free**. That is why dropping the `:5776` guard was rejected as unnecessary.

### Site B — new tracked state

Declare near `~:643-647` and **clear in `_reset_position_state` (`:1206-1222`)**:
- `_time_barrier_exit_attempts: int`
- `_pending_exit_order_id: Optional[int]`

### Site C — `_HOUSEKEEPING_OVERWRITE_REASONS` (`:196`) — **NO CHANGE. Do not touch.**

Adjudicated: the Auditor's pushback was **upheld** and the Reviewer withdrew its
implied remedy. Post-fix, `TIME_BARRIER` rows carry **proven** prices, placing them
exactly where they already are — the never-overwrite bucket beside `TP_HIT`/`SL_HIT`,
whose comment (`:193-195`) reads *"carry real prices and are NEVER touched."*
**Widening `:196` would license housekeeping to overwrite truthful rows — converting a
correctly-scoped guard into a new corruption vector.** The defect is the fabricating
write at `:1713`; fill-price-or-NULL (the `:2305-2313` precedent — *"exit price stays
NULL — an explicit unknown, never a fabricated price"*) is the whole remedy.

## Explicitly rejected — do NOT implement these

- **Reversing `:1679`/`:1682` ("submit the exit first, keep the stop resting").**
  **REJECTED — actively dangerous.** `ocaType`/`ocaGroup` appear **nowhere in `src/`**
  (verified: zero grep matches). `place_child_orders` (`ibkr_client.py:1610-1634`)
  places TP/SL as standalone orders with no `parentId`; OCA is **software-side**
  (`ibkr_client.py:1590`: *"OCA behavior … is handled in software by
  LiveTrader._on_order_status"*; `live_trader.py:645`, `:3020`, `:5953`). The
  `ocaType=3` in the incident log is the **broker's echoed repr**, binding nothing
  without an `ocaGroup` — the tell is that orders 65/66 render as generic `Order(...)`
  while order 71 (our constructed object) renders as `LimitOrder(...)` with no
  `ocaType`. Without atomicity, both legs are SELL 1 on a +1 long; a double fill goes
  net **−1 naked short**, and the SL fill routes to `:5943-5981` →
  `_reset_position_state` → **the bot believes FLAT while short** — the
  `reconnect-false-flat-oob` catastrophe class ($296k naked short). This trades a
  *bounded* naked window for an *unbounded-loss* reversal.
- **Dropping the `:5776` `_active_trade_id is None` guard.** REJECTED: reverses the
  operator-authorized `:2748-2755` decision (*"UNTRACKED … stays human-only"*,
  2026-07-08) and would auto-flatten a position **a human just took over in TWS** —
  exactly what happened at ~16:29 in this incident. Also redundant (see A4's free
  consequence).
- **Changing the marketable-limit buffer.** REJECTED: aimed at the wrong mechanism
  (the `%.2f` artifact above).
- **Widening `_HOUSEKEEPING_OVERWRITE_REASONS`.** REJECTED — see Site C.

## Tests

### Fake-fidelity repair (NOT test loosening — every assertion keeps its teeth)

9 files call `_check_time_barrier`; the **4 that stub `exec_client.close_position`**
reach the exit branch and **will fail** until their fakes model the newly-required
broker interaction. A bare `MagicMock` auto-attrs `get_position_settled` → returns a
**truthy Mock ≠ 0** → the gate reads "still holding" → reset never runs.

| File | Stub site |
|---|---|
| `tests/test_cooldown.py` | `:182`, `:210` |
| `tests/test_exit_reason_and_fill_routing.py` | `:165` (test `:158-179`) |
| `tests/test_live_trader_bugs.py` | `:150` |
| `tests/test_oob_entry_state_recovery.py` | `:612` (asserts `:1308`, `:1344`) |

**Repair (precedent: `test_exit_reason_and_fill_routing.py:186`, set by the
`reconnect-false-flat-oob` ticket):** add
`exec_client.get_position_settled.return_value = 0  # settled CONFIRMS the exit filled`
plus a `get_executions` stub returning a record matching the exit order id.
**Every assertion stays unchanged** — notably
`_reset_position_state.assert_called_once_with(reason="TIME_BARRIER")` (`:179`), which
must still pass to preserve SL-flavored cooldown parity with the backtest. Teaching a
fake to model a fill that *really happened* **adds fidelity**; it is not widening.

**Must stay green, UNCHANGED** (the new `None` branch mirrors their exact invariant):
`tests/test_reconnect_false_flat_recovery.py:250`, `:262`
(`telemetry.close_position.assert_not_called()`), `:278`.

**`exit_price` assertions to update for Site A's proven-price/NULL rule:**
`tests/test_oob_entry_state_recovery.py:1375`;
`tests/test_hourly_order_housekeeping.py:1594`, `:1657`.

### NEW regression test — `tests/test_time_barrier_exit_fill_confirmation.py`

1. **Reproduce the incident.** +1 long past the barrier; `close_position` returns a
   trade whose order never fills; `get_position_settled` → `1`. Assert: ledger
   **not** closed; `_active_trade_id` **still set**; `_sl_order_id` **re-armed**; the
   exit order id passed to `cancel_orders_by_ids` **before** the re-arm;
   `_reset_position_state` **not** called; `_check_naked_position` **fires** on the
   next poll (proves the free re-arm).
2. **Confirmed fill books the proven price.** Same setup, `get_position_settled` → `0`
   plus a matching execution → ledger closed with the **execution's** price, never
   `current_price`.
3. **BINDING CONDITION 2 (Reviewer, binding) — pin the race branch.**
   `settled=1`, then `cancel_orders_by_ids` → **`0`** (the exit had already filled) ⇒
   the flat branch books the **execution's** price and does **NOT** re-arm. Without
   this the C1 addition rots untested.
4. **Unconfirmed fails closed.** `get_position_settled` → `None` ⇒ no ledger write, no
   reset, stays tracked, no re-arm.

## Hard constraints (violating any of these fails the ticket)

- **NO CHEAP FIXES**: no `try/except: pass` or broad exception swallowing; no
  defaulting a missing config/field to `None`/fallback (missing required fields must
  **RAISE**); no loosening/skipping/deleting tests or widening assertions; no blind
  retries/sleeps masking a deterministic bug; no hardcoding today's data conditions.
- **Bounds are bars/attempts — never sleeps** (a sleep blocks the event loop).
- **Parity intact — verified, not assumed:** the `"CL stays bit-identical"` guarantee
  (`~:2952-2955`) governs `round_to_tick` on **bracket children**, untouched here; the
  live exit path is not part of the byte-identical backtest parity gate.
  `simulated_execution.close_position` sets `_position=0` synchronously (`:568`) and
  cancels its own orders (`:565`), so the confirm-gate is a **no-op in the sim** and
  cancel/close ordering is already irrelevant there.
- Full fast suite must pass:
  `conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"`
- **Deploy is operator-gated.** Commit with *"deploy pending operator restart"* in the
  body; never claim DEPLOYED before it happened. Branch: the operator's current fleet
  working branch (`git branch --show-current`) — stage file-by-file, leave operator WIP
  untouched.

## Out of scope — separate tickets

1. **Native OCA bracket groups** — the real fix for the overfill hazard, but a
   multi-component refactor (`place_child_orders` + `close_cl_position` +
   `ExecutionInterface.close_position` signature `:187` + both adapters incl.
   `simulated_execution` modelling OCA + 3 call sites + the now-redundant software-OCA
   path) → trips the Interface, Base Class, and Refactor vetoes. Compounded by all 5
   configs running strategy `exit_mode="TIERED"` (`_tp_order_ids` is a **list**;
   `ocaType=3` reduce-with-overfill-protection across TP tiers has non-obvious resize
   semantics). **Requires human authorization.**
2. **"UNTRACKED = auto-flatten"** — reverses the documented 2026-07-08 operator
   decision and could fight a human in TWS. **Requires human authorization.**
3. **Repair the existing corrupted ledger rows** (`trade_64` NG, `trade_27` GC) —
   operator-gated one-off data script; the agent cannot write the live DB.
4. **Rollover `:3606-3628`** — same fire-and-forget shape at lower probability
   (`exit_mode="market"` ⇒ near-certain fill). It also **never calls
   `telemetry.close_position` at all**, so its ledger row stays OPEN forever after a
   successful close — a separate pre-existing defect.
5. **`%.2f` log format** (`ibkr_client.py:788-790`) — LOW. A real observability defect:
   it printed a 0.002 buffer as `0.00` and materially misled this investigation.
