# Ticket Resolution Blueprint — bb-f-exit-bar-semantics_07032026_2045
**Ticket Directory:** `.agents/collab/tickets/bb-f-exit-bar-semantics_07032026_2045/`

## Human Authorization (2026-07-03)
> "Authorize B(b)+F: backtest adopts live's intrabar SL/TP-before-barrier precedence, live adopts
> backtest's exit-bar evaluation semantics, accepting that historical backtest metrics shift and
> ensembles may need re-scoring."

Supersedes the earlier deferral of B(b) (parity-exit-signal ticket). Historical backtest exit
prices/reasons on same-bar-conflict trades WILL change, with second-order trade-sequence changes
via cooldown flavor; ensembles may need re-scoring. Accepted.

## Bug Summary
Last two attributed parity blockers (see `validate-ledger-parity.md` baseline of 2026-07-03 evening:
bt=15 lt=17 matched=14, 1 violation + 4 unmatched, all B(b)/F):

**B(b) — same-bar exit precedence inversion.** `agent/backtest_engine.py::_on_in_position` checks
TIME_BARRIER (~:688, exits at bar_open) BEFORE evaluating TP/SL breach. Live/IBKR reality: resting
SL/TP orders fill INTRABAR in real time; the time-barrier check runs at bar close only if still in
position. Live is the correct engine; the backtest changes.

**F — exit-bar evaluation semantics (3 parts).**
1. `ConfigurableStrategy.on_exit` resets the per-side counter to 0; with the pre-gate increment the
   exit-bar evaluate reads 1 where the backtest reads 0 → live releases cooldown one bar early
   (same-bar re-entry after TP when tp_cooldown=0).
2. `scripts/livetest_engine.py::run_simulation` flushes deferred fill callbacks AFTER the bar's
   evaluation (`updateEvent.fire`), so the exit-bar evaluate sees a flat sim position with stale
   counters — production delivers fills in real time BEFORE the bar-close evaluation.
3. `LiveTrader._on_new_bar` returns immediately when `_check_time_barrier` exits, SKIPPING the
   exit-bar evaluation; the backtest evaluates every bar including exit bars → one-bar shifts in
   consecutive-signal gating (e.g. the 05-28 07:00-vs-08:00 entry).

## Required Changes
### B(b) — `agent/backtest_engine.py::_on_in_position`
Reorder: (1) bars_held++/extremes as today; (2) evaluate TP/SL breach FIRST (preserve pessimistic
SL-wins-on-same-bar and all price math EXACTLY); (3) only if no TP/SL exit and
`bars_held > horizon` → TIME_BARRIER exit at bar_open (price basis unchanged); (4) trailing
upgrade only if still in position (relative order to TP/SL unchanged). NO other changes — entry
logic, fill basis (B(a)), trailing math untouched.

### F(1) — `src/live_execution/strategies/configurable_strategy.py::on_exit`
Reset the exited side's counter to **-1** (was 0) for ALL exit reasons. With F(3) the exit-bar
evaluation always runs and its pre-gate increment yields 0 on the exit bar — matching the
backtest's exit-bar read of 0 (blocked for any cooldown ≥ 0), release at exit+N+1 reading N+1.

### F(2) — `scripts/livetest_engine.py::run_simulation`
Add `sim_exec.flush_deferred_callbacks()` BETWEEN the bar feed (resting-order matching) and
`updateEvent.fire`, so exit fills (and their on_exit resets) land before the bar-close evaluation.
KEEP the existing post-fire flush (delivers same-bar ENTRY fills placed during evaluation).

### F(3) — `src/live_execution/live_trader.py::_on_new_bar`
When `_check_time_barrier` returns True (barrier exit), do NOT return early — continue to signal
evaluation on the exit bar (backtest convention). Position is flat post-exit; the exited side is
gated by its own cooldown (counter reads 0); the opposite side may enter same-bar exactly as the
backtest allows. Do NOT touch `_on_bar_update_5m`/trailing scheduling (strict-locked).

### Test updates (authorized semantics change)
`tests/test_parity_cooldown_single_authority.py` encodes the pre-F(1) convention ("first call
after on_exit reads 1"). Under the new convention the first call after on_exit IS the exit-bar
call and reads 0; releases shift one call later. Update the affected assertions and the docstring;
note the authorization in the file header. `tests/test_exit_reason_and_fill_routing.py`
CLOSED-flavor tests unaffected (blocked either way).

## Expected validation outcome
336-bar trailing-symmetric replay: the 05-26 violation reconciles (both engines TP_HIT 94.30);
the 05-26 13:00 / 05-28 07:00↔08:00 / 06-10 11:00 unmatched pairs collapse. Target: PARITY PASS
(0 violations, equal trade counts) or residuals attributable ONLY to C/trailing (out of scope).
Quantify the backtest ledger shift (old vs new precedence) for the replay window as the
historical-impact sample.

## Out of scope
- Trailing-stop math/scheduling (strict-locked; 5m-harness ticket).
- `execution_models.py`.
- Full-history ensemble re-scoring (flagged for follow-up after merge).
