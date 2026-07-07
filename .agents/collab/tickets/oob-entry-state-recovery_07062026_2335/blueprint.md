# Ticket Resolution Blueprint — oob-entry-state-recovery_07062026_2335
**Ticket Directory:** `.agents/collab/tickets/oob-entry-state-recovery_07062026_2335/`
**Authorization:** HUMAN-AUTHORIZED 2026-07-07 (~00:15 PT) after Impact-Reviewer
verification; amendments A1–A8 below are BINDING. Reviewer verdict: RCA accurate,
architecture sound; gate was scope authority, not correctness.
**Caution:** line numbers cited below were verified on 2026-07-06 HEAD but commits
34bba58/eb0c059 (margin heartbeat, perf tools) have since landed — locate by the
quoted ANCHOR strings, not line numbers.

## Bug Summary (three related defects, one live-path state machine)

**D1 — OOB close-out orphans protective orders and loses the exit price (HIGH).**
Recovery branch (`live_trader.py`, anchor `"filled out-of-band"` / `CLOSED_OOB`):
when the ledger says OPEN but IBKR is flat, it (i) cancels protective orders via
the SYMBOL-scoped `exec_client.cancel_open_orders(symbol=self._execution_symbol)`
— which silently matched nothing on 2026-07-06 because the instance had been
reconfigured MGC→GC between restarts (orders rest on the OLD contract symbol);
success is logged only `if cancelled > 0` and the except is `log.debug` (silent by
construction); the startup backstop `_cancel_orphaned_orders_on_startup` reads the
callback-populated `self._open_orders` (empty at startup) and is symbol-filtered
too; and (ii) calls `telemetry.close_position` without `exit_price` (the signature
accepts it) and generic reason `CLOSED_OOB`, never consulting broker executions.
Real-world result: orphaned GTC TP (naked-short trap) a human had to cancel, and a
permanently NULL exit price.

**D2 — trade/trailing state initialized at order PLACEMENT, not FILL (HIGH).**
Submission path (anchor: where `_pending_entry_order_id` is assigned alongside
`_entry_price = current_price`, `_atr_at_entry`, `_position_side`, extremes seeds,
`_position_entry_bar_time`) sets in-position state BEFORE any fill.
`_check_trailing_stop` gates only on that pre-fill internal state — never on a
confirmed fill — and runs on every 5m/1h bar; the `_sl_order_id is None` early
return precedes the `_trailing_activated` latch, so an unfilled GTC entry re-fires
the warning every bar (observed: NG order 19, 2026-07-06 23:20/23:25 PT).
Extremes accumulate pre-fill and are NOT re-seeded on fill; in-memory
`_entry_price` is NEVER re-seeded from the actual fill price (ledger gets
avg_price; trailing math runs off submission price on EVERY trade). TTL cancel
(`_check_entry_order_ttl` → `_reset_position_state()` default reason "CLOSED")
fires `strategy.on_exit` with an SL-flavored cooldown for a trade that never
existed.

**D3 — fleet_health false-positive discriminators (LOW-MED; same-day regression
from ticket fleet-health-check_07062026_0640).**
(a) `missing-fill-price` flags every aged EXECUTE ledger row with NULL fill_price
— but NULL is the legitimate permanent state of never-filled/TTL-cancelled
entries. (b) `incomplete-close` has no recency scope (SELECT omits `close_time`)
→ adjudicated old rows nag hourly forever.

## Target Files
- `src/live_execution/live_trader.py` (D1 recovery branch; D2 state machine)
- `src/live_execution/interfaces/execution_interface.py` (D1: new ABC methods)
- `src/live_execution/adapters/ibkr_execution.py` (D1: implementations)
- `src/live_execution/adapters/simulated_execution.py` (A4)
- `src/live_execution/configurable_strategy.py` (A7: cooldown vocabulary)
- `scripts/trade_reconciler.py` (A7: `_EXIT_REASON_MAP`)
- `src/live_execution/fleet_health.py` (D3)
- NEW test file `tests/test_oob_entry_state_recovery.py` (all three defects;
  Strict-Lock conventions). `tests/test_fleet_health.py` is Strict-Locked — its
  pins must keep passing (see A6); add new D3 tests to the NEW file.

## Required Changes

### D1 — OOB recovery: targeted cancel + execution recovery
1. NEW targeted primitive (ExecutionClient method, e.g.
   `cancel_orders_by_ids(order_ids: list[int]) -> int`): look up `openTrades()`
   and cancel exactly those order ids **irrespective of contract symbol**. Do NOT
   modify the bulk symbol-scoped `cancel_open_orders` (live software-OCA exit path
   + TTL depend on it — A8). Per-instance client ids mean `openTrades()` only
   exposes this instance's own orders (verified) — cross-child interference is
   structurally impossible.
2. NEW ExecutionClient method (e.g. `get_executions(symbol: Optional[str]=None)`)
   wrapping ib_insync `ib.fills()` (current-day executions are locally cached by
   connect's reqExecutions; sync viable at startup recovery outside the event
   loop — same pattern as `get_open_trades`). Returns fills with order id, permId,
   price, qty, side, time, execId, and commissionReport when present.
3. Rework the OOB branch: (a) cancel the ledger row's exact
   `tp_order_id`/`sl_order_id` via the targeted primitive, THEN run the existing
   symbol-scoped sweep as belt-and-braces; if an expected protective order is
   neither found open nor provably done (matched execution), log ERROR + send
   Telegram — NEVER `log.debug`-swallow. (b) match executions to the protective
   order ids → determine which leg filled → `close_position` WITH `exit_price`
   and truthful reason `SL_HIT_OOB` / `TP_HIT_OOB`; write tradebook
   EXECUTION_FILL + COMMISSION rows for the recovered fill. (c) if executions are
   unavailable (day boundary), keep exit_price NULL with explicit reason
   `CLOSED_OOB_UNRECOVERED` — no fabricated prices.
4. **A5 (idempotency):** recovery-written EXECUTION_FILL/COMMISSION rows must use
   deterministic event_ids keyed on broker execId (COMMISSION_<execId> pattern
   exists) / order-id+permId for fills — NOT the timestamp-based
   `_build_event_id` — so repeated restarts dedupe via INSERT OR IGNORE.
5. **A7 (vocabulary):** add `SL_HIT_OOB` and `CLOSED_OOB_UNRECOVERED` to the
   SL-flavored cooldown tuples in `configurable_strategy.py` (two tuple sites,
   anchor: existing tuples containing `"SL_HIT"`); `TP_HIT_OOB` is CONSCIOUSLY
   EXCLUDED (TP flavor = absence) — add a comment saying so. Map all three in
   `scripts/trade_reconciler.py` `_EXIT_REASON_MAP`.
6. **A4:** `SimulatedExecution` implements the two new methods honestly for the
   sim domain (its resting-order/trade records) or raises loud
   `NotImplementedError` — never a stub returning fabricated success.

### D2 — pending-entry vs in-position state split
1. Submission path stores a PENDING-ENTRY record only: order id, submission bar
   time, side INTENT, and the signal params needed later (ATR at signal, tp/sl
   offsets, per-trade trailing overrides) — carried in the existing
   per-order decision context. It must NOT set `_entry_price`, `_atr_at_entry`,
   `_position_side`, extremes, or `_position_entry_bar_time`.
2. The confirmed-fill entry branch (`_on_standard_execution_event` →
   `_place_bracket_children_on_fill`) sets ALL in-position state, seeded from the
   FILL: **A3:** `_entry_price := avg_price` (fill, not submission price);
   extremes re-seeded from the fill-time bar; `_position_entry_bar_time` := fill
   bar; `_position_side` from the fill action; `_atr_at_entry` + overrides from
   the stored pending context. If the stored context cannot be resolved for a
   recognized entry fill → ERROR + Telegram (raise-loudly rule), no defaults.
   Clear `_pending_entry_order_id` eagerly here.
3. `_check_trailing_stop` hard-gates on confirmed in-position state
   (`_active_trade_id is not None` — fill-time-only today). With pre-fill state
   gone, the repeating `_sl_order_id is None` WARNING becomes structurally
   impossible — do NOT merely suppress the log line.
4. NEW `_clear_pending_entry()` used by the entry-cancellation paths (TTL,
   rollover, kill-switch): clears ONLY pending state and does NOT fire
   `strategy.on_exit`/cooldowns when no fill ever occurred.
   **A1 (partial fills):** before clearing, check the pending order's
   `orderStatus.filled` — a partial fill is NOT never-filled: route to
   in-position handling (position exists broker-side) with ERROR + Telegram.
   **A2 (scoping):** rollover and kill-switch are also REAL-close paths — when a
   confirmed fill exists they must still run the full in-position reset
   (`strategy.on_exit` + cooldown). Only the never-filled pending case skips
   cooldowns.

### D3 — fleet_health discriminators
1. `main()` additionally queries: `tradebook_events` order_ids where
   `event_type='EXECUTION_FILL'`, and `active_positions.entry_order_id`; passes
   this fill-evidence to `check_positions`.
2. `check_positions` gains an ADDITIVE parameter (existing 3-positional-arg calls
   in the Strict-Locked test file must keep passing). **A6:** the default means
   "evidence unavailable → legacy conservative time-based flagging" (over-flag,
   never silently skip); `main()` ALWAYS passes evidence explicitly. With
   evidence present, flag a NULL-fill EXECUTE row only when a matching fill
   provably exists (EXECUTION_FILL row or a position opened with that
   entry_order_id) — a true update_fill-regression detector.
3. Fetch `close_time` in the active_positions SELECT; scope `incomplete-close`
   to closes within 48h of now. NULL/unparseable `close_time` on a CLOSED row
   still flags (existing "surface it" precedent).

## Test Contract
NEW Strict-Locked file `tests/test_oob_entry_state_recovery.py` following house
conventions (object.__new__ stubs, seams documented, AsyncMock where needed,
pandas 1.5.3-safe). Red-phase mandatory: every requirement above (including each
amendment A1–A8) gets at least one test failing on current HEAD, plus FENCE tests
pinning: bulk `cancel_open_orders` call sites unchanged (A8), locked
test_fleet_health pins still green, `TP_HIT_OOB` absent from SL tuples (A7).
Full fast suite must pass: `conda run -n trader python -m pytest tests/ -v
--tb=short -m "not slow"`.

## Deployment
Live fleet keeps running during implementation (all changes inert until process
restart). Deploy = operator-approved fleet restart AFTER green suite; commit on
stable-fleet only after deploy verification per fleet-error-monitor SKILL.
