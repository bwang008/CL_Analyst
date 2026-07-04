# Ticket Resolution Blueprint — exit-fill-routing-cooldown_07032026_0930
**Ticket Directory:** `.agents/collab/tickets/exit-fill-routing-cooldown_07032026_0930/`

## Bug Summary
Three residual live-side defects discovered by the 2026-07-03 `/validate-ledger-parity` replay
(documented as D/E/F in `validate-ledger-parity.md` and in ticket
`parity-exit-signal_07022026_1930/tdd_result.md` addendum). Human authorized fix + validation
directly ("proceed and test/validate the changes", 2026-07-03).

**D — exit-reason vocabulary gap (production).** `LiveTrader._reset_position_state()` defaults
`reason="CLOSED"`; the time-barrier exit (`live_trader.py:1267`) and OOB cleanup (`:1205`) both use
the default. `ConfigurableStrategy.evaluate()`'s cooldown flavor tuple (`configurable_strategy.py:423/430`)
recognizes only `SL_HIT/TIME_BARRIER/REVERSE` as SL-flavored → time-barrier exits get
`tp_cooldown_bars` (0) instead of `sl_cooldown_bars` (7). Backtest treats TIME_BARRIER as SL-flavored.
Previously masked by the TieredEnsembleStrategy re-gate; exposed by the 9999 sentinel.

**E — fill misrouting (harness duplication × production fallback).**
(a) `scripts/livetest_engine.py:407-462` explicitly calls `trader._place_bracket_children_on_fill`
after the fill callback, based on a comment claiming `_on_standard_execution_event` doesn't do it —
now false (`live_trader.py:4047` does). Double placement overwrites `_tp_order_ids`/`_sl_order_id`,
orphaning the first child set. (b) `_on_standard_execution_event`'s classification is
"TP/SL-registered → exit, ELSE → entry" (`live_trader.py:3948-3991`); an orphaned SL fill lands in
the entry branch, and because `_place_bracket_children_on_fill` stores the parent decision context
under CHILD order IDs (`:1742`), the entry branch happily places brackets around an exit fill.
`on_exit` never fires → cooldown never engages; exits salvaged one bar late by OOB cleanup with
reason "CLOSED" (feeding D).

**F — exit-bar off-by-one at cooldown=0.** Expected to be an artifact of E's OOB ordering (OOB
delivers the exit BEFORE the bar's evaluation; the proper deferred-fill path delivers AFTER, which
matches the backtest's bar+1 first-post-exit evaluation). No code change; verify empirically via
replay after D+E.

## Target Files
- `src/live_execution/live_trader.py` — D (explicit reasons), E(b) (entry-ID registry + unrecognized-fill guard)
- `src/live_execution/strategies/configurable_strategy.py` — D (conservative SL-flavor for CLOSED/CLOSED_OOB)
- `scripts/livetest_engine.py` — E(a) (remove duplicate child placement, keep `_open_orders` registration)

## Required Changes
### D
1. `live_trader.py:1267` (time-barrier exit): `_reset_position_state(reason="TIME_BARRIER")`.
2. `live_trader.py:1205` (OOB cleanup): `_reset_position_state(reason="CLOSED_OOB")`.
3. `configurable_strategy.py:423/430`: extend the SL-flavor tuple with `"CLOSED"` and `"CLOSED_OOB"`
   (conservative: unknown/OOB closes get the longer cooldown). ROLLOVER/KILL_SWITCH stay unflavored.

### E
4. `live_trader.py` entry submission (`~:3269`): register the entry order id in a new
   `self._entry_order_ids` set (str form; also int-tolerant lookup).
5. `live_trader.py` fill handler else-branch (`~:3991`): only treat the fill as an entry if the
   order id is in `_entry_order_ids`; otherwise log a loud `[TRADE] UNRECOGNIZED FILL` error and
   return WITHOUT touching position state (OOB detection remains the safety net).
6. `scripts/livetest_engine.py:407-462`: delete the duplicate `_place_bracket_children_on_fill`
   call; keep the `_open_orders` registration of the (single) child set placed by live_trader.

## Out of scope
- B(b) same-bar exit precedence (backlogged) — the 05-26 TIME_BARRIER-vs-TP_HIT trade stays divergent.
- Trailing-stop 5m harness resolution (separate ticket); strict-locked tests untouched.
- `execution_models.py` untouched.
