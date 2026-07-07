# Ticket Resolution Blueprint — warmup-state-continuity-test_07072026_0455
**Ticket Directory:** `.agents/collab/tickets/warmup-state-continuity-test_07072026_0455/`

## Bug Summary
The live-trader startup "catch-up" mechanism — `LiveTrader._warmup_inference_state()`
([src/live_execution/live_trader.py:2523](../../../../src/live_execution/live_trader.py)) — replays
the last `warmup_bars` (default 24) historical bars through `strategy.evaluate()` and discards the
output, purely to rebuild the strategy's internal `_consecutive_long_signals` /
`_consecutive_short_signals` counters so the consecutive-signal-threshold gate resumes as if the bot
had run continuously. It is invoked on startup (~line 921-923) and again after a reconnect backfill
(~line 3331).

**This mechanism has ZERO test coverage.** The only reference to `_warmup_inference_state` in the
entire suite is `tests/test_reconnect_recovery_fixes.py:158`, which **mocks it out**. The
strategy-level counting is unit-tested in isolation (`tests/test_isolated_strategy.py:170-200`,
`tests/test_joint_strategy.py:194`), but nothing asserts that the **startup replay restores that
state to match a continuously-running bot**.

- **Severity:** LOW. Coverage gap, not a runtime bug. Not a recent regression (path unchanged since
  commit `4a01e5c`, 2026-06-24 — verified via safe `git log`).
- **Scope:** TEST-ONLY. **Do NOT modify production inference logic.** The three known faithfulness
  caveats (warmup uses `macro_overrides={}`; `warmup_bars` vs `consecutive_signal_threshold` is
  unguarded; the `entry_crossed`/`mock_position` heuristic) are explicitly OUT OF SCOPE for this
  ticket and are not to be "fixed" here.

## Target Files
- `tests/test_warmup_state_continuity.py`  **(NEW FILE)**
- Reference-only (read, do not modify): `src/live_execution/live_trader.py`,
  `src/live_execution/strategies/execution_models.py`,
  `src/live_execution/strategies/configurable_strategy.py`,
  `tests/test_reconnect_recovery_fixes.py` (stub pattern), `tests/test_cooldown.py` (df/stub pattern),
  `tests/test_isolated_strategy.py` (`_make_config` pattern).

## Required Changes
Add a **LiveTrader-level equivalence / state-continuity test** in the new file
`tests/test_warmup_state_continuity.py`. The test must exercise the REAL `_warmup_inference_state`
and the REAL strategy — no live IBKR connection — and prove that a restarted-then-warmed-up bot holds
the same consecutive-signal counters as a bot that ran the same bars continuously.

1. **Strategy under test.** Construct a real `ConfigurableStrategy` wrapping a real
   `IsolatedAsymmetricalStrategy` with `long.consecutive_signal_threshold = 3` (reuse the
   `_make_config` pattern from `test_isolated_strategy.py`). Make signal probabilities deterministic
   by controlling inference — stub `ConfigurableStrategy._run_inference` (or monkeypatch the
   learners) so each synthetic bar yields a KNOWN `buy_prob`/`sell_prob`. No real LGBM inference.

2. **Trader harness.** Build the trader via `object.__new__(LiveTrader)` following the seam-only stub
   patterns in `test_reconnect_recovery_fixes.py` (`_reconnect_stub`) and `test_cooldown.py`
   (`_make_trader_stub`). Attach ONLY the seams `_warmup_inference_state` reads:
   `self.strategy`, `_bar_size="5m"`, `rolling_df_5m` = a synthetic OHLC DataFrame with a
   `DatetimeIndex` (cf. `test_cooldown.py:88-95`), `data_manager_1h = None`, `feature_names`,
   `_lean_features = False`, `_atr_period*`, `_brain_instrument`, `_needs_macro = False`.
   Broker seams: `exec_client.get_position` → `0` (flat), `exec_client.get_account_summary` →
   `{"cl_avg_cost": 0.0}`.
   To keep the test about STATE CONTINUITY rather than feature math, monkeypatch
   `src.live_execution.feature_pipeline.build_live_features` (imported inside the method at ~line
   2558) to return a controlled last-N feature frame whose rows map 1:1 to the scripted probabilities.

3. **Equivalence assertion.** For each scenario:
   - **(a) Continuous baseline:** run the identical scripted bar sequence straight through a fresh
     strategy (via its normal per-bar path), and snapshot `_consecutive_long_signals` /
     `_consecutive_short_signals` at the final bar T.
   - **(b) Warmup reconstruction:** build a fresh strategy + trader, call
     `_warmup_inference_state(num_bars=len(sequence))` over the same bars, and read the reconstructed
     counters off the strategy's execution model.
   - **(c) Assert reconstructed == baseline** for BOTH long and short counters.

4. **Required scenarios (at minimum):**
   - **Mid-streak-below-threshold:** 2 consecutive buy signals with `threshold=3` ⇒ reconstructed
     `_consecutive_long_signals == 2` (NOT 0, NOT gated-to-fired).
   - **Reset-on-gap:** buy, buy, non-signal (gap), buy ⇒ reconstructed `_consecutive_long_signals == 1`.

5. **TDD note for the coder.** `_warmup_inference_state` is real, working code, so this test is a
   **characterization / regression lock** and is expected GREEN on HEAD once the harness is wired
   correctly. To honor the RED→GREEN discipline, first assert a deliberately-wrong expected value
   (e.g. expect `0` for the mid-streak case), confirm RED, then flip to the correct expected value
   for GREEN. **No production code changes.**
