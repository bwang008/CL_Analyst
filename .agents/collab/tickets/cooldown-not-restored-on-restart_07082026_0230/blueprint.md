# Ticket Resolution Blueprint — cooldown-not-restored-on-restart_07082026_0230
**Ticket Directory:** `.agents/collab/tickets/cooldown-not-restored-on-restart_07082026_0230/`
**Status:** Auditor RCA + Impact-Reviewer complete. Reviewer verdict = **APPROVED ON MERITS, REQUIRE HUMAN AUTHORIZATION** (changes live entry-gating economics). BLOCKED pending operator sign-off before `/tdd-manager`.

## Bug Summary
The stop-loss re-entry cooldown (`sl_cooldown_bars`) is not enforced after a process restart, so a model can re-enter the same side it was just stopped out of on the next bar. The cooldown gate (`configurable_strategy.py` ~418-457) reads in-memory state (`_last_exit_bars_ago_<side>`, `_last_exit_reason_<side>`) armed only by `ConfigurableStrategy.on_exit()` (~586). Fresh-instance defaults (92-95) are `bars_ago=9999, reason=""` → no cooldown.

Of the two OOB-recovery paths in `live_trader.py`, only the housekeeping sweep (~2377-2384) arms cooldown (`_recover_oob_close` **then** `_reset_position_state`). The **startup ledger-recovery** branch (`_recover_inherited_position`, `if ibkr_pos == 0:` ~1715-1727) calls `_recover_oob_close(...)` then `return`s — never arming cooldown. Compounding it: `_reset_position_state` (~1177-1180) only calls `on_exit` when `self._position_side != 0`, and at startup the position was never loaded, so `_position_side == 0` → even adding that call would no-op. The **ledger row's side must drive `on_exit` directly.**

Live impact (2026-07-08): ES trade_15 (−$2,179.50) and GC trade_20 (−$1,755.04) stopped out OOB while the fleet was down (01:06→01:23), recovered at startup with the gate inert, and both re-entered LONG at 02:00 despite `sl_cooldown_bars: 7`.

**Regression status:** not a single-commit regression — latent since 2e55132e (2026-03-26); the asymmetry became glaring on 2026-07-07 when the housekeeping path got `_reset_position_state` (3109a86) but the startup path did not.

**Parity:** verified empirically (both agents). Seeding `bars_ago = bars_elapsed − 1` reproduces a continuously-running bot's gate timeline byte-for-byte; backtest is untouched; live↔backtest cooldown parity preserved. TP_HIT_OOB stays excluded from the SL tuple.

## Target Files
- `src/live_execution/live_trader.py` — Part 1 (startup OOB branch ~1722) + Part 2 (clean-start branch ~1684) of `_recover_inherited_position`.
- `tests/test_hourly_order_housekeeping.py` — revise `test_startup_oob_tp_leg_byte_identical` (~1522): KEEP its three real fences (truthful close reason+price; targeted-then-bulk cancel; deterministic tradebook ids); ADD a positive assertion that cooldown is armed with the truthful reason + ledger side.
- `tests/test_cooldown.py`, `tests/test_parity_cooldown_single_authority.py`, `tests/test_oob_entry_state_recovery.py` — new cases.
- DO NOT touch `agent/backtest_engine.py` or `alpha_factory.py` — backtest/training parity is sacred.

## Required Changes
**Part 1 — Arm cooldown on the startup OOB branch (`_recover_inherited_position`, `if ibkr_pos == 0:` ~1722).**
- After `reason, price = self._recover_oob_close(...)`, arm the strategy cooldown by calling `self._strategy.on_exit(side_int, reason, bars_held)` **directly** (NOT via `_reset_position_state`, which no-ops at startup).
- `side_int` from the **ledger row's** side ("LONG"→+1, "SHORT"→-1).
- `reason` = the truthful reason returned by `_recover_oob_close` (SL_HIT_OOB / CLOSED_OOB_UNRECOVERED → SL cooldown applies; TP_HIT_OOB → excluded from SL tuple, no SL cooldown — correct).
- Guard with `hasattr(self, "_strategy")` (matching `_reset_position_state` at L1178), not `self._strategy is not None`.

**Part 2 — Reconstruct recent-exit cooldown from the ledger on the clean-start branch (~1684, no OPEN ledger position).**
- Query `telemetry.get_recent_closed_positions(...)` (client-scoped, close_time DESC, returns `[]` not None).
- **Group by side; arm EACH side from its OWN most-recent CLOSED row** (first-per-side from the DESC list) — NOT `rows[0]` (which would under-arm the other side).
- Arm only if that side's exit is recent enough to still be within its configured cooldown window.

**Bars-ago semantics (both parts):** set `_last_exit_bars_ago_<side> = bars_elapsed − 1`, where
`bars_elapsed = (current_bar_time − pd.Timestamp(close_time)) / bar_duration`. The gate's pre-gate `+1` then yields the honest `bars_elapsed`. If `bars_elapsed > cooldown`, arming is inert (no over-block). Startup-OOB has `close_time ≈ now` → `bars_elapsed ≈ 0` → seed `-1` → degenerates exactly to mid-session `on_exit(-1)`.

## Mandatory implementation guards (Reviewer conditions a–d)
- **(a)** `rolling_df_5m` AND `rolling_df_1h` can BOTH be None at startup → None-guard `current_bar_time = rolling_df_5m.index[-1]` (fallback `rolling_df_1h`); define an inert fallback when no bar time exists (do not raise, do not over-arm).
- **(b)** `_bar_minutes` is a LOCAL dict inside `_recover_inherited_position` (~1755) using `.get(size, 5)` — do NOT hard-index `_bar_minutes[self._bar_size]` (KeyError on unknown size); use `.get(...)` or hoist the dict.
- **(c)** Part 1 guard uses `hasattr(self, "_strategy")` (as `_reset_position_state` does at L1178).
- **(d)** Part 2 must group-by-side and arm each side from its own most-recent CLOSED row (first-per-side from the DESC list), NOT `rows[0]`.

## Economics / Authorization
This BLOCKS entries currently admitted (same-side re-entry after a stop, across restarts). It restores intended `sl_cooldown_bars` behavior (fewer whipsaw re-entries) but is a live money-admission change → **operator authorization required before implementation.**

## Severity
HIGH operational; fleet-wide (cooldown is symbol-agnostic). Two real losing re-entries already observed. Patch is small/localized (two additive sites + test updates).
