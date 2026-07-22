# Ticket Resolution Blueprint — oca-stage2-residual-detection_07222026_0141
**Ticket Directory:** `.agents/collab/tickets/oca-stage2-residual-detection_07222026_0141/`
**Parent:** Stage 2 of `oco-leg-race-audit_07212026_1935/blueprint.md` (operator-authorized 2026-07-22 "Proceed with the next stages"). Stage 1 (broker-native OCA groups) shipped as `7795e1a`. This ticket is the REQUIRED companion: independent app-side verification of the broker-side OCA guarantee, plus the D2 sign-blind-re-arm fix. Stages 3/4 are OUT of scope here (Stage 4 = its own ticket, next; Stage 3 remains gated on multi-lot sizing being wanted).

## Bug Summary
Three residual defects survive Stage 1 (all in `src/live_execution/live_trader.py`):
1. **Silent residual double-fill.** If both legs fill despite the OCA group (broker anomaly, legacy pre-OCA bracket resting across the deploy, replay edge), the second `Filled` event lands in the UNRECOGNIZED-FILL ignore branch (`_on_standard_execution_event`, ~:6499-6516) because `_reset_position_state` already cleared the tracked ids — a REVERSED position goes untracked, kill-switch-blind (`_check_naked_position` early-returns on `_active_trade_id is None`), detected only by hourly detect-only housekeeping.
2. **D2 — sign-blind re-arm.** `_route_retired_time_barrier_exit` branches only on `settled == 0` vs non-zero; a REVERSED settled position is treated as "still open" and `_rearm_time_barrier_protection` re-places protective legs sized from a STALE pre-settled read using the OLD tracked side — same-direction orders against a reversed book (exposure-increasing if filled).
3. **Partial fills are invisible.** Non-`Filled` status events with `filled_qty > 0` on tracked legs produce no log/health signal; with Stage 1 the broker resizes the sibling server-side, but the operator has zero observability that a partial happened.

## Target Files
- `src/live_execution/live_trader.py` (all three fixes)
- `tests/test_hourly_order_housekeeping.py` (ADDITIVE ONLY: three new kind strings into the `_ALLOWED_KINDS` frozenset at ~:274-286 — a conscious registry update, mirroring how `position-flat-unconfirmed` was added; nothing else in that file may change)
- `.agents/skills/fleet-error-monitor/SKILL.md` (triage entries for the three new kinds — docs, updated by the Manager at commit time)

## Required Changes

### R1 — Un-gated shared flatten helper (MANDATORY precondition, operator-confirmed)
Extract the flatten steps from `_check_naked_position`'s body (~:6324-6388: cancel open orders; market `close_position` + register exit oid in `_processed_exit_order_ids`; ledger close; CRITICAL Telegram; `_reset_position_state` + `_clear_pending_entry`) into a new helper, e.g. `_flatten_book_and_reset(*, reason, telegram_text, ledger_trade_id)`:
- UN-GATED: no `_active_trade_id` / `_sl_order_id` / `_pending_*` checks inside the helper.
- Ledger close runs ONLY when `ledger_trade_id` is not None (the reversal branch must NOT re-close/overwrite the already-truthfully-closed row).
- `_check_naked_position` keeps ALL its existing guard clauses and then calls the helper with `reason="NAKED_POSITION_KILL_SWITCH"` and its existing Telegram text — its observable behavior stays byte-identical (existing kill-switch/OOB test suites must stay green unmodified).
- NEVER call `_check_naked_position()` from the new reversal branch — its `_active_trade_id is None` guard makes that a silent no-op by construction.

### R2 — Residual double-fill (reversal) branch
- `_reset_position_state` (~:1297): BEFORE clearing `_tp_order_ids`/`_sl_order_id`, when either is non-empty, snapshot `{trade_id, reason, leg_ids (str set), cleared_at (time.time())}` into a new single-slot registry attr (e.g. `_recently_closed_legs`). One slot suffices (one position per child). Overwritten by the next snapshot; CLEARED on the next recognized entry fill; entries older than a bounded age (e.g. 6h) are ignored at match time.
- `_on_standard_execution_event`, in the `Filled` path's unrecognized-fill branch (BEFORE the generic ignore/log at ~:6499-6516): if the registry is present, fresh, and `str(order_id)` is in `leg_ids` -> CONFIRMED residual double-fill:
  - `log.critical` + `_emit_health_event("oca-race-reversal", detail)` + Telegram CRITICAL (ASCII-only).
  - Tradebook event `event_type="OCA_RACE_REVERSAL"` carrying the second fill's price/qty/order_id (truthful record; the original trade row keeps its TP_HIT/SL_HIT close untouched).
  - Read residual via `get_cached_position` ONLY (NEVER `get_position`, NEVER a settled read — in-callback context, A-2 constraint; test-pinned).
  - If cached residual != 0 -> `_flatten_book_and_reset(reason="OCA_RACE_REVERSAL", ledger_trade_id=None, ...)`. If cached residual == 0 -> events only (no flatten order), state already flat.
  - The branch keys on `status == "Filled"` only — sibling CANCELLED/expired events must not trip it.

### R3 — D2 sign check + settled-sized re-arm
In `_route_retired_time_barrier_exit` (~:2052-2080), after the settled read succeeds and `settled != 0`:
- If `self._position_side != 0` and `sign(settled) != sign(self._position_side)` -> REVERSED: `log.critical` + `_emit_health_event("rearm-sign-mismatch", detail)` + Telegram + `_flatten_book_and_reset(reason="REVERSED_POSITION_KILL_SWITCH", ledger_trade_id=self._active_trade_id, ...)`; return True (trade concluded). NEVER re-arm.
- Sign-matching path unchanged EXCEPT: `_rearm_time_barrier_protection(settled)` — sized from the SETTLED value, not the stale pre-settled `current_position`. (The A0 in-callback call site keeps `current_position` — no settled read exists in that context by design.)

### R4 — Partial-fill observability
In `_on_standard_execution_event`, for events with `status != "Filled"` and `filled_qty > 0` whose `order_id` matches a tracked TP/SL leg: once per `(order_id, filled_qty)` pair (dedupe set, cleared in `_reset_position_state`): `log.warning` + `_emit_health_event("protective-leg-partial-fill", detail)`. NO booking changes, NO cancel, NO state mutation beyond the dedupe set (broker-side ocaType=2 owns the sibling resize).

### R5 — Kind registry + docs
New health kinds: `oca-race-reversal`, `rearm-sign-mismatch`, `protective-leg-partial-fill` — added (additively) to the `_ALLOWED_KINDS` frozenset and documented in SKILL.md triage (severity: oca-race-reversal = CRITICAL auto-flatten already taken, verify broker flat; rearm-sign-mismatch = CRITICAL auto-flatten already taken; protective-leg-partial-fill = informational, verify SL resize on broker).

## Constraints
- No changes to `_check_time_barrier` submission ordering, the reconciler's pending-exit lifecycle, kill-switch DEFERRAL keying, or any Stage-4 surface.
- No try/except:pass; ASCII-only operator strings; existing kill-switch/OOB/settle-confirm test suites stay green UNMODIFIED (the only permitted existing-test edit is the additive `_ALLOWED_KINDS` registry update).
- Deploy remains operator-gated with Stage 1 (same canary/restart discipline; nothing rides the pending 291a9fd/394fa68 restart).

## Test cases (RED targets)
1. Reversal branch: SL `Filled` books the exit normally; then a late TP `Filled` for the registry-matched sibling -> CRITICAL log + `oca-race-reversal` health event + Telegram + `OCA_RACE_REVERSAL` tradebook event + flatten helper invoked (mock: `close_position` called, `cancel_open_orders` called) + original ledger row's close_reason UNTOUCHED; `get_position` NEVER called from the branch (mock-asserted); cached-flat variant -> events but NO flatten order.
2. Registry hygiene: next recognized entry fill clears the registry; stale (aged) registry entry does not trip the branch; a CANCELLED status event for the sibling does not trip it; an unrelated unknown order id still takes the old UNRECOGNIZED-FILL ignore path.
3. Helper extraction: `_check_naked_position` behavior byte-identical (existing suites green); helper itself is un-gated (flattens with `_active_trade_id is None`).
4. D2: settled returns opposite sign -> no re-arm (`_verify_and_heal_protective_legs` NOT called), CRITICAL + `rearm-sign-mismatch` + flatten with `REVERSED_POSITION_KILL_SWITCH` ledger close, returns True. Same-sign settled != 0 -> re-arm sized from SETTLED (not the stale read; pin with differing values).
5. Partial fill: `status="Submitted", filled_qty=2` on the tracked TP -> exactly one warning + `protective-leg-partial-fill` per (order_id, filled_qty); repeat event deduped; full `Filled` afterward books normally.
6. Kind registry: the three new kinds pass the `_ALLOWED_KINDS` gate.
