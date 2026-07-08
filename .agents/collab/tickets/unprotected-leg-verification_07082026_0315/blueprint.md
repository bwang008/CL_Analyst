# Ticket Resolution Blueprint — unprotected-leg-verification_07082026_0315
**Ticket Directory:** `.agents/collab/tickets/unprotected-leg-verification_07082026_0315/`
**Status:** Auditor RCA + Impact-Reviewer APPROVED (operator-authorized auto-placement; no refactor veto). 5 mandatory conditions. Pending operator go before implementation.

## Bug Summary
An open live position can silently lose its stop-loss. An async IBKR cancel/reject of a resting protective order is caught by NONE of the three guards:
- `_on_ib_error` (~3464) branches only on connectivity codes — no order-reject codes (201/202/…).
- `_on_standard_execution_event` (~5428) acts only on `status == "Filled"` — no Cancelled/Rejected/Inactive branch.
- `_check_naked_position` (~5318) short-circuits on `_sl_order_id is not None` without revalidating the id is actually resting.
Net: an async-cancelled SL leaves `_sl_order_id` + ledger `sl_order_id` + the cached event all believing it rests, with no ERROR/Telegram/heal. Monitor gaps: `fleet_health` (~161) checks only the DB (null id) with no broker session; the `:15` sweep naked check (~2541) only asks "does any STP rest", not "is the LEDGER's expected id resting". Operator observed an NG long apparently missing its SL; a restart healed it (recovery re-places missing legs). Operator wants that heal hourly.

## Approach (operator redirect: AUTO-HEAL, reuse existing recovery re-place; not new detect-only functions)

## Target Files
- `src/live_execution/live_trader.py` — extract-method + sweep wiring + trailing-persist hardening.
- `tests/test_hourly_order_housekeeping.py` — heal cases + new kind in `_ALLOWED_KINDS`/`test_emitted_kinds_stay_in_the_contract_set`.
- Trailing-stop + recovery test suites — persist-loud + extract byte-identical regression.
- `.agents/skills/fleet-error-monitor/SKILL.md` — triage entry for the new kind.
- DO NOT touch `agent/backtest_engine.py` / `alpha_factory.py` (parity).

## Required Changes
1. **Extract-method** the missing-leg re-place block from `_recover_inherited_position` (~1898-2019) into
   `_verify_and_heal_protective_legs(*, trade_id, tp_order_id, sl_order_id, tp_price, sl_price, quantity, position_side)`.
   Preserve internal ordering verbatim (query → gate → price/front-month guard → cancel → round_to_tick → place → register ids → `update_position_brackets`). `_recover_inherited_position` keeps calling it → **startup byte-identical**.
   - IDEMPOTENCY GATE: `resting_ids` from `get_open_trades(None)`; `tp_found`/`sl_found`. If both found → return "verified", do NOTHING.
   - Missing-price/front-month guard: refuse to fabricate a leg → LOUD cannot-heal (`housekeeping-naked-position`), never synthesize a price.
2. **Wire into `_housekeeping_sweep`** replacing the detect-only naked block (~2541-2554): when `position != 0 and pending_id is None and open_row is not None`, call the heal with the ledger row's ids/prices. On successful heal emit new kind `housekeeping-protective-leg-healed` + `log.error/warning` + Telegram.
3. **Harden trailing→ledger persist** (`_check_trailing_stop` ~1496-1504): `except: log.debug(...)` → `log.error(...)` + health event + Telegram. Do NOT roll back the broker modify (broker SL correct; only ledger stale). `update_position_brackets` COALESCEs `initial_sl_price`, so the true original is preserved.
4. **1s TP/SL placement delay: REJECTED** — no placement race (`place_child_orders` sends two independent transmit=True orders, no shared parent, no native OCA); NG SL was placed+verified; a delay opens a post-fill naked window (masking sleep, forbidden).

## Mandatory conditions (Reviewer, all required)
1. **False-miss guard:** `get_open_trades` is a CACHE read (`ib.openTrades()`, no `reqOpenOrders`), which can be transiently stale/partial post-reconnect. Before any cancel+replace, add a confirm-missing recheck / cache-freshness guard so a stale cache cannot churn healthy orders.
2. **Connection guard:** re-check `is_connected()` immediately before the place; abandon on mid-sweep disconnect; never let `place_child_orders`→`ensure_connected()` drive a blocking reconnect under `_ledger_lock`.
3. **Fail-closed rate-limit + non-re-fire:** if `_check_order_rate_limit()`/`_emergency_halt` trips → do NOT place, emit LOUD `housekeeping-naked-position`. The idempotency gate must confirm the newly-placed ids are FOUND on the next sweep so a non-healing heal cannot re-fire every :15 into the 10-orders/60s halt.
4. **Replace-only-the-missing-leg on the hourly path:** leave a verified-resting TP untouched; cancel-both only when >1 leg is missing. (Startup path may keep cancel-both — it runs once.) Prevents cancelling a healthy TP (brief TP-less gap + wasted rate-limit budget). NOTE: re-placed SL id MUST be registered into `_sl_order_id`/`_tp_order_ids` — exit booking `is_sl_fill` (~5495) is id-dependent (unregistered → "UNRECOGNIZED FILL", no SL_HIT booked).
5. **Contract wiring:** add `housekeeping-protective-leg-healed` to `_ALLOWED_KINDS` + `test_emitted_kinds_stay_in_the_contract_set` + SKILL.md triage; no loosening of existing assertions.

## Parity / Severity
Zero economics/model impact; live-execution only (SimulatedExecution-exercised); startup recovery byte-identical. Severity HIGH (writes orders to the live book; extract + persist-hardening + new heal kind). Auto-placement operator-authorized; guards 1-4 make it bounded, non-churning, connection-safe, and visible.

## Test landing spots
- `tests/test_hourly_order_housekeeping.py`: both legs resting → verified, places NOTHING; ledger SL id not resting → heal re-places (only missing leg), registers ids, emits `housekeeping-protective-leg-healed`; leg missing + rate-limit halt → NO place, cannot-heal event; leg missing + price None → refuse, cannot-heal; re-placed ids registered for OCA/booking; new kind in the contract set.
- Trailing suite: `update_position_sl` raising → LOUD ERROR + health event, broker modify NOT rolled back.
- Recovery suite: `_recover_inherited_position` byte-identical after extract.
