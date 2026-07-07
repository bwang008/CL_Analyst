# Ticket Resolution Blueprint — hourly-order-housekeeping_07072026_0435
**Ticket Directory:** `.agents/collab/tickets/hourly-order-housekeeping_07072026_0435/`
**Authorization:** HUMAN-AUTHORIZED 2026-07-07 ~06:00 PT (operator-requested
feature; Reviewer verdict "design sound, recommend authorization"; amendments
below are BINDING, including the operator's A-1(b) choice).

## Bug Summary
Broker-vs-ledger reconciliation runs ONLY at startup (`start()` step 7b/7c;
`_recover_oob_close`'s sole call site is startup recovery). While children run,
nothing re-checks that IBKR's resting orders/positions match the ledger. Proven
consequences (2026-07-06/07): mid-session orphaned protective orders rest until
the next restart (software-OCA sibling cancel is a single warning-swallowed
attempt — anchor `"[OCA] Failed to cancel"`); OOB exits lose their true fill
price forever once the IBKR current-day execution window closes (trade_8:
permanent `exit_price=NULL`); the ONLY existing mid-session drift path,
`_check_time_barrier`'s broker-flat branch, writes a SYNTHETIC bar price as
`exit_price`, uses the symbol-scoped bulk cancel that already missed the
trade_8 orphan, debug-swallows cancel failures, and is bar-gated (no bars = no
detection).

Feature: an in-child hourly housekeeping sweep at ~:15 wall-clock (after the
:00 signal bar and the :06 read-only monitor) that auto-cleans PROVABLE
inconsistencies, repairs ledger truth from same-day executions, and
detect-alerts everything ambiguous — composing a per-hour loop: fleet_health
detects (:06) → housekeeping cleans (:15) → next fleet_health verifies.

## Target Files
- `src/live_execution/live_trader.py`
- `src/live_execution/interfaces/execution_interface.py`
- `src/live_execution/adapters/ibkr_execution.py`
- `src/live_execution/adapters/simulated_execution.py`
- `src/live_execution/telemetry.py`
- `.agents/skills/fleet-error-monitor/SKILL.md`
- NEW `tests/test_hourly_order_housekeeping.py`
- UNCHANGED by design: `src/live_execution/fleet_health.py`, all bulk
  `cancel_open_orders` call sites (039208d A8 stays in force),
  `_check_time_barrier` (v1 leaves it; housekeeping repairs its rows after).

## Required Changes

### 1. Scheduling (live_trader.py) — A-7
- New latch `_last_housekeeping_slot` (init None). In the main event-loop poll
  body — at the same level as the rollover check (anchor: the comment "Runs
  here (between ib.sleep() calls)"), NOT inside the
  `poll_count % _HEARTBEAT_CYCLES` block (poll_count resets on reconnect) —
  fire `_run_hourly_housekeeping()` when `now_utc.minute >= 15` and
  `(date, hour) != _last_housekeeping_slot`. Latch BEFORE the sweep body
  (a crash must not hot-loop; same pattern as `_last_rollover_check_date`).

### 2. `_run_hourly_housekeeping()` (live_trader.py)
Never-raises contract (mirror `emit_crash_event`): any internal failure →
`housekeeping-error` health event + log, trading untouched. Runs under
`_ledger_lock`. Skip silently (no event) when `exec_client.is_connected()` is
False, `_rollover_in_progress`, or emergency-halted.
- **A-2 (deadlock guard):** re-check `is_connected()` before EACH broker
  touch; abort the remainder of the sweep on any disconnect. ONLY local-cache
  primitives (`get_open_trades`, `get_executions`, position from cached
  portfolio) and targeted `cancel_orders_by_ids` are permitted inside — never
  `ensure_connected`-routed calls, never qualify/reqContractDetails/place/
  close/modify. (get_position routes through ensure_connected → possible
  blocking reconnect → event-loop pump under the lock → re-entrant bar
  callback deadlock. Use a cache-read equivalent.)
- **A-3 (budget):** time the sweep; > ~10s → include a slow-sweep note via a
  `housekeeping-error` health event. Batch ALL findings of one sweep into at
  most ONE Telegram send.
- Every action/detection emits `_emit_health_event(kind, detail)` (existing
  live-gate applies) with kinds listed below; also one summary log line.

**Auto-clean actions (provable + idempotent only):**
(a) **Orphaned protective orders** → kind `housekeeping-orphan-cancelled`.
    Preconditions ALL required: no in-position state (`_active_trade_id is
    None`) AND no pending entry AND cached broker position == 0 AND the
    resting order id matches a recent CLOSED ledger row's tp_order_id /
    sl_order_id. **A-4:** ids present in the live `_tp_order_ids` /
    `_sl_order_id` / `_pending_entry_order_id` sets are NEVER cancelled
    regardless of any CLOSED-row match (broker id reuse after TWS id-sequence
    resets). Cancel via `cancel_orders_by_ids` (targeted; never blanket).
(b) **Broker-vs-ledger drift** (ledger OPEN + cached broker flat) → kind
    `housekeeping-drift-detected`. **A-5:** skip (detect-only note, no action)
    if the trade shows tradebook/fill/order activity within the last 10
    minutes (constant mirroring fleet_health's FILL_PRICE_GRACE_MINUTES) — a
    fill callback may be in flight. Otherwise reuse `_recover_oob_close`
    (refactored to return `(reason, price)`), then
    `_reset_position_state(reason=<truthful reason>)` for cooldown parity
    with the legacy path.
(c) **Ledger repair** → kind `housekeeping-ledger-repaired`. For recent
    CLOSED rows (48h helper below): match the row's own tp/sl order ids
    against cached `get_executions()`. **A-1(b):** repair BOTH
    `exit_price IS NULL` rows AND synthetic-price rows, but overwrite is
    allowed ONLY under a strict whitelist: `close_reason IN ('CLOSED_OOB',
    'CLOSED_OOB_UNRECOVERED')` and the matched execution belongs to that
    row's own tp/sl ids; reason upgrades to TP_HIT_OOB/SL_HIT_OOB
    accordingly. Rows with reasons TP_HIT/SL_HIT/anything else are NEVER
    touched. No execution match → leave as-is, never synthesize. Write the
    A5-idempotent tradebook rows via the shared booking helper (below).

**Detect-and-alert ONLY (health event + batched Telegram; never act):**
- `housekeeping-naked-position`: cached broker position != 0 with no resting
  SL among session orders (auto-placing protection = order-routing semantics
  = human gate; also rate-limit interaction with the 10-orders/60s halt).
- `housekeeping-untracked-position`: ledger has no trade, broker position != 0.
- `housekeeping-ambiguous`: pending-entry/partial-fill ambiguity (reuse the
  A1-039208d partial-fill detection).
- **A-6** `housekeeping-unknown-order`: a resting session order (while
  ledger-flat) matching neither a recent CLOSED row's brackets nor the
  pending entry id — unknown bot-origin order, alert only (entry orders
  legitimately rest while flat).

### 3. Shared booking helper (live_trader.py)
Extract the recovery tradebook-booking block of `_recover_oob_close` (anchor:
the A5 `EXECUTION_FILL_<execId>` / `COMMISSION_<execId>` writes) into
`_book_recovered_executions(...)` used by both startup recovery and
housekeeping. `_recover_oob_close` return type changes `None → (reason,
price)`; its single startup call site adapts; startup behavior must remain
byte-identical (FENCE-tested).

### 4. Interface widening — A-10
`get_open_trades(symbol: Optional[str])` in the ABC + both adapters: the
parameter stays REQUIRED; `symbol=None` explicitly means "all symbols on this
session" (needed for old-contract orphans after instrument reconfigs — the
trade_8 class). All existing call sites keep passing a symbol. Sim adapter
implements honestly (039208d A4 precedent).

### 5. telemetry.py — A-9
- `repair_closed_position(trade_id, *, exit_price, reason, allow_overwrite_reasons)`:
  UPDATE scoped to trade_id AND `status='CLOSED'` AND (exit_price IS NULL OR
  close_reason in the overwrite whitelist) AND the instance's client/symbol
  binding on the shared fleet DB; returns rowcount. Tests must prove a
  fleet-mate's identical trade_id is untouched (54dc110 collision precedent).
- `get_recent_closed_positions(hours=48)`: client-scoped read helper.

### 6. SKILL.md
New `housekeeping-*` triage bullet-set: `-orphan-cancelled` /
`-drift-detected` / `-ledger-repaired` are INFORMATIONAL (action already taken
in-child; the :06 monitor verifies via the next fleet_health run, files to
done/, does NOT re-act; RISING `occurrences` on the same event = something
repeatedly manufactures orphans → ticket). `-naked-position` /
`-untracked-position` / `-ambiguous` / `-unknown-order` = highest severity,
human notification, never auto-act.

## Test Contract
NEW Strict-Locked `tests/test_hourly_order_housekeeping.py` (house
conventions: object.__new__ stubs, documented seams, tmp_path only, never the
production queue/log/DB, `_health_events_enabled` left unset). Red-mandatory
coverage: gate/latch incl. crash-no-hot-loop + reconnect-independence (A-7);
disconnect-abort mid-sweep + forbidden-primitive guard (A-2); duration budget
+ single batched Telegram (A-3); A-4 live-id exclusion (CLOSED-row id
colliding with live bracket id → NOT cancelled); A-5 grace skip; each
auto-clean case incl. idempotency (second run = no-op) and truthful reasons;
A-1(b) whitelist (synthetic CLOSED_OOB row repaired to proven fill; TP_HIT row
NEVER overwritten; no-match rows untouched); detect-only cases place/cancel
NOTHING; A-6 unknown-order alert; never-raises under a throwing exec_client;
`get_open_trades(None)` in both adapters; telemetry repair scoping incl.
fleet-mate collision; startup `_recover_oob_close` byte-identical FENCE.
Full fast suite green except the 10 known ES01B-Sortino sentinel reds.

## Deployment
Inert until fleet restart. Deploy on the operator's next restart; verify via
the :06 monitor across the following cycles (first :15 sweep observed live).
