# TDD Result — exit-fill-unverified_07152026_1855

**Outcome:** GREEN. Full fast suite **2340 passed, 1 skipped** (Red baseline was
2334 passed + 6 failed; the 6 new tests now pass, zero other movement). Verified by
the TDD-Manager running `conda run -n trader python -m pytest tests/ -m "not slow"`,
not on the Coder's word.

**Authorized by operator 2026-07-16. Deploy (fleet restart) remains operator-gated —
the fix is INERT until the operator restarts the fleet.**

## What the fix does

Converts the fire-and-forget TIME BARRIER exit into a **confirm-before-book** gate.
It no longer marks the ledger CLOSED / resets position state / abandons protection
until the broker confirms the exit actually filled. On a non-fill it cancels the
stranded exit, re-arms the stop, and keeps the position **tracked** so both safety
nets stay armed and retry next bar.

## Source changes — `src/live_execution/live_trader.py` only (+230 / −12)

| Site | Change |
|---|---|
| `~:184` | `_MAX_TIME_BARRIER_EXIT_ATTEMPTS = 6` — bounds the cross-bar retry by attempts, never a sleep. |
| `~:655` (Site B) | New tracked state `_time_barrier_exit_attempts: int`, `_pending_exit_order_id: Optional[int]`. |
| `~:1236` (`_reset_position_state`) | Clears both new fields on every real close. |
| `:1700-1730` (Site A) | Keeps `cancel_open_orders`→`close_position` in place/order; nulls in-memory `_sl_order_id`/`_tp_order_ids` immediately after the cancel (they are dead on the broker — this arms the kill switch during a deferral; the tracked *prices* survive for re-arm); captures `_exit_oid`; A0 never-submitted hard-fail; registers exit id; A1 `_confirm_settled_position` gate (0→book proven price, None→fail-closed defer, non-zero→A2); A2 `cancel_orders_by_ids([_exit_oid])` first + Binding Condition 1. |
| New helpers | `_route_retired_time_barrier_exit` (re-confirm settled STRICTLY AFTER retirement, then flat→book / open→re-arm / None→defer), `_book_time_barrier_flat`, `_resolve_exit_fill_price` (proven price by `str(order_id)` match, else NULL — never `current_price`), `_rearm_time_barrier_protection` (re-place SL/TP from tracked prices via `_verify_and_heal_protective_legs`), `_note_time_barrier_deferral` (A4 bounded escalation). |
| `_HOUSEKEEPING_OVERWRITE_REASONS` (`:196`) | **Untouched** — adjudicated NO-CHANGE (widening would license overwriting future truthful rows). |

**Binding Condition 1** — `cancel_orders_by_ids` count==0 → exit already gone →
re-confirm settled & route; count>=1 → exit only cancel-requested → single
`get_open_trades` check, still-present → defer (the next bar is the re-scan, no
sleep), gone → settled read taken **strictly after** and routed as count==0.
**Binding Condition 2** — pinned by the race-branch regression test (settled=1 →
cancel=0 → book proven price, no re-arm).

Zero signature/interface/base-class changes. Parity intact (confirm-gate is a no-op
in the sim — `simulated_execution.close_position` sets `_position=0` synchronously;
`round_to_tick`/bracket-child pricing untouched).

## Test changes

- **NEW** `tests/test_time_barrier_exit_fill_confirmation.py` — 6 tests across the 4
  blueprint cases (incident repro; proven-price book; NULL-when-no-execution; the
  BC2 race branch; kill-switch-fires-for-free; settled=None fail-closed).
- Fake-fidelity repairs (stubs added, **no assertion weakened**):
  `tests/test_cooldown.py`, `tests/test_exit_reason_and_fill_routing.py`,
  `tests/test_live_trader_bugs.py`. `_reset_position_state.assert_called_once_with(
  reason="TIME_BARRIER")` preserved.
- Blueprint's item-3 line-refs (`test_oob_entry_state_recovery`,
  `test_hourly_order_housekeeping`) reconciled as STALE by both Tester and Coder —
  those files exercise branches Site A does not touch and stay green unchanged; no
  existing test asserted a fabricated `current_price` on the TIME BARRIER branch.

## Known non-blocking follow-up (flagged, NOT fixed here)

**A4 escalation Telegram will 400 and silently not send.** The message embeds
`trade_<id>` (one underscore) and `TelegramAlerter.send` defaults to
`parse_mode="Markdown"` while prepending its own italic `_timestamp_` — the odd
underscore count trips Telegram's Markdown parser (caught at `telegram_alert.py:136`,
never raises). **Non-blocking:** the escalation's reliable channel — `_emit_health_event`
→ error queue → hourly watchdog — still delivers; only the fast Telegram nudge is
lost. This is the same class as the SKILL's documented Telegram-underscore hazard and
is codebase-wide (e.g. the housekeeping send at `:2764`). **Recommend folding into the
LOW observability ticket** (with the `%.2f` buffer log-format fix) — escape underscores
or send the escalation with `parse_mode=None`.

## Out of scope (separate tickets — operator-gated)

Native OCA bracket groups (the real overfill fix); "UNTRACKED = auto-flatten";
repair of the existing corrupted ledger rows (`trade_64` NG, `trade_27` GC — live DB
write, operator only); rollover `:3606-3628` (same shape, lower odds, + never calls
`telemetry.close_position`); `%.2f` buffer log-format (`ibkr_client.py:788-790`).
