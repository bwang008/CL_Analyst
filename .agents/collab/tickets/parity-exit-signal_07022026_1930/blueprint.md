# Ticket Resolution Blueprint — parity-exit-signal_07022026_1930

## Bug Summary
Residual backtest-vs-Parity-Mode-livetest ledger divergence remaining AFTER the software-OCA
sibling-cancellation fix. Parity-Mode livetest (`scripts/livetest_engine.py`) replays 336 bars
(2026-05-22 16:00 → 2026-06-12 14:00) through the unmodified production LiveTrader with backtest
predictions injected, and is compared against `agent/backtest_engine.py`'s ledger. Config: scout
ensemble HS14B_Sharpe_E01 (TieredEnsembleStrategy, dual long+short brackets, single 1-lot net,
`conflict_resolution="hold"`, `sl_cooldown_bars:7`, `long/short.cooldown_bars:1`). Of 18 backtest /
17 livetest trades, 13 match to the exact cent. Two root causes are authorized for fix here; two
items are explicitly backlogged.

**Phenomenon A — cooldown double-enforcement (trade-count + entry-timing divergence).**
The livetest path enforces cooldown TWICE: `ConfigurableStrategy.evaluate()` zeroes buy/sell probs
on its OWN per-side since-exit counter (`configurable_strategy.py:418-441`) BEFORE delegating to
`TieredEnsembleStrategy.on_bar`, which then re-applies cooldown via `EngineState.last_exit_bars_ago_*`
(`execution_models.py:754-760`). A harness monkey-patch (`scripts/livetest_engine.py:762-776`,
`_parity_on_exit`) compensates for the double-count but is off by one bar at the
SHORT→cooldown-release→LONG boundary. Result on 2026-05-28→05-29: the live engine opens the SHORT one
bar early (12:00 vs 13:00) and never takes the 05-29 follow-on LONG the backtest takes (18 vs 17
trades). Predictions are identical by construction, so this is purely an entry-gating/counter-timing
difference. The **backtest is authoritative** (single cooldown authority, start-of-bar counter
increment).

**Phenomenon B(a) — static SL/TP price basis divergence.**
On the 2026-06-02 07:00 LONG (backtest TIME_BARRIER +1434.99 vs livetest SL_HIT +1015.00, $420 gap),
the live trailing stop is inert in the 1h harness (`_check_trailing_stop` sole call site
`live_trader.py:2617` inside `_on_bar_update_5m`, which the 1h driver never fires), so the live stop
that fired was the STATIC SL. The backtest derives SL/TP from the RAW unrounded entry price
(`backtest_engine.py:661/666`); live derives them from the slippage-adjusted FILL price and
penny-rounds (and double-rounds via an already-rounded `signal.sl_price`, `live_trader.py:1622/1635`,
offset `:3176`). Net: the live static stop sits ~1–1.5 ticks (~$10–15/contract) closer to price and
fires on a shallower dip. **Authorized direction: change the BACKTEST to mimic IBKR reality**
(apply the existing 1-tick slippage + 2dp penny-grid rounding to SL/TP derivation). Accepted
consequence: historical backtest PnL shifts by ≤ ~1 tick on SL/TP-exit trades only (trade
selection/entries/predictions unchanged; seed reproducibility unaffected).

## Target Files
- `src/live_execution/strategies/configurable_strategy.py`  — Phenomenon A (single cooldown authority)
- `scripts/livetest_engine.py`                              — Phenomenon A (delete compensating monkey-patch)
- `agent/backtest_engine.py`                                — Phenomenon B(a) (SL/TP price → IBKR basis)

## Required Changes

### Phenomenon A — make `ConfigurableStrategy.evaluate()` the SOLE cooldown authority
1. **Relocate the per-side since-exit counter increment.** Move the counter advance (currently
   `configurable_strategy.py:434-441`, executed AFTER the gate and only conditionally) to the TOP of
   the cooldown section, executing BEFORE the prob-zeroing gate (`:418-432`). This mirrors the
   backtest's start-of-bar increment convention (`backtest_engine.py:1200-1201`, reset to 0 in
   `_close_trade` `:588-590`) so the live gate reads the SAME "bars since exit" value the backtest gate
   reads on every bar — including the exit bar (value 0) and the SHORT→cooldown-release→LONG boundary
   where the one-bar slip currently occurs. Preserve the existing "only advance the flat/opposite
   side" semantics; only the ordering relative to the gate changes.
2. **Neutralize the downstream re-gate.** When `evaluate()` constructs the `EngineState` passed to
   `TieredEnsembleStrategy.on_bar` (`:443-449`), feed sentinel
   `last_exit_bars_ago_long = last_exit_bars_ago_short = 9999` (any value greater than the maximum
   configured cooldown). This makes `on_bar`'s only cooldown check (`execution_models.py:754-760`,
   `bars_ago <= cooldown_bars`) always false, so cooldown is enforced in exactly ONE place —
   `evaluate()` itself. VERIFIED SAFE: `last_exit_bars_ago_*` is read ONLY at
   `execution_models.py:755` and `:758` and nowhere else in `on_bar`, so the sentinel has zero
   collateral effect on tier-matching, conflict-resolution, or consecutive-signal logic. **Do NOT
   modify `execution_models.py`.**
3. **Delete the compensating monkey-patch.** Remove `_parity_on_exit` and its install
   (`trader.strategy.on_exit = _parity_on_exit`) at `scripts/livetest_engine.py:762-776`. It exists
   solely to cancel the double-count and would re-introduce a one-bar skew once the source is fixed.
4. **Confirm reconciler labeling.** Verify the reconciler's exit-reason mapping does not mislabel a
   cooldown-gated skip as SIGNAL_EXIT (`conflict_resolution="hold"` → TieredEnsembleStrategy emits no
   EXIT order).

**Correctness anchor (test intent):** after this change, over the replay window the livetest ledger
must produce 18 trades matching the backtest, with the 05-28 SHORT entering at 13:00 (not 12:00) and
the 05-29 follow-on LONG taken. Add a live-vs-backtest parity assertion on the
SHORT→cooldown-release→LONG boundary bar (Impact-Reviewer merge caveat).

### Phenomenon B(a) — align backtest SL/TP price to IBKR reality
1. **Change the SL/TP price basis** where the static SL/TP prices are derived in
   `agent/backtest_engine.py` (long SL `~:661`, short SL `~:666`, and the corresponding TP
   derivations) to mirror the live bracket-placement path (`live_trader.py:1622/1635`):
   - Derive SL/TP from the **slippage-adjusted entry FILL price** (reuse the existing 1-tick adverse
     slippage the backtest already applies to fills, e.g. the tick offset at `backtest_engine.py:510`,
     consistent with `simulated_execution.py:615`) — NOT the raw unrounded entry price.
   - **Round the resulting SL and TP to 2 decimals** (CL 0.01 penny grid), matching live's
     `round(..., 2)`.
   - Apply a SINGLE consistent rounding (do not reintroduce a double-round); goal is the backtest
     SL/TP price equals the live static SL/TP price to the cent.
2. **Scope guard.** Change ONLY the SL/TP price derivation used for exit matching. Do NOT change entry
   logic, position sizing, ATR computation, trade selection, or predictions — backtest trade COUNT and
   entry timing must be unchanged; only exit fill prices on SL/TP-exit trades shift (≤ ~1 tick).

**Correctness anchor (test intent):** on the 06-02 07:00 LONG the backtest and livetest STATIC SL/TP
exit prices reconcile to within the ≤ $5/trade tolerance; the isolated 1-tick SL-basis gap is closed.
Do NOT touch the intrabar SL-vs-TIME_BARRIER precedence (see backlog) — a trade whose ONLY remaining
difference is TIME_BARRIER vs SL_HIT exit-reason labeling with otherwise-reconciled PnL stays as-is
and is out of scope.

## Explicitly Out of Scope (backlogged / do NOT touch)
- **Phenomenon B(b)** — intrabar SL-vs-TIME_BARRIER precedence unification (backtest checks
  TIME_BARRIER first at `:688`; live matches resting SL in `simulated_execution.on_bar_feed` before
  `_check_time_barrier` at `live_trader.py:2824`). This re-labels same-bar exit reasons repo-wide;
  **backlogged for a future sprint** per human decision.
- **Phenomenon C** — sub-penny residuals. 06-04 SL ($9.49) and 06-09 SL ($12.52) are
  TRAILING-DEPENDENT and UNMEASURABLE in the 1h harness (live trailing inert post-`922dea5`) —
  re-measure after the trailing-stop-5m ticket. 06-01 TP ($14.04) is accepted sub-tick tolerance and
  will shrink from the B(a) rounding change. No standalone code change.
- **OCA cancellation path** — already fixed, out of scope.
- **In-flight trailing-stop-5m ticket** — `_on_bar_update_5m` / `_on_new_bar` are strict-locked
  (`tests/test_trailing_stop_5m_scheduling.py`); do NOT edit that path.

## Audit Trail
- Auditor RCA + revised RCA, Impact-Reviewer blast-radius reviews (both rounds), and human
  authorizations are logged in `.agents/collab/ticket_audit_log.md` and
  `.agents/collab/ticket_status.md`, all stamped `parity-exit-signal_07022026_1930`.
- Human decision: authorize A (Option B, live-side cooldown) + B(a) (backtest SL/TP → IBKR basis,
  accepting ≤1-tick historical PnL drift). Defer B(b) and C.
