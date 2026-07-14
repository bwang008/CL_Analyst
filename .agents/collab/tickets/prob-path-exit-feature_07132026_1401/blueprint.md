# Ticket Resolution Blueprint — prob-path-exit-feature_07132026_1401
**Ticket Directory:** `.agents/collab/tickets/prob-path-exit-feature_07132026_1401/`
**Type:** Feature design (not a bug). Evidence: `findings_ng02d_prototype.md` + `prob_path_prototype.py` in this folder, validated on `reports/batch_runs/batch_20260713_005758_NG_02D_SCOUT` (E01 reproduced to the cent).

## Feature Summary
Two related capabilities, phased:
1. **Prob-path forensics** — for every backtest trade, quantify the model's probability trajectory over the holding window (entry prob, min/max, drop below threshold, dip timing, opposite-side firing), split winners vs losers. Prototype proves feasibility and already answered the motivating questions (see findings).
2. **Signal-decay exit rule** — an opt-in engine exit that closes a trade when the entry-side probability decays below `threshold − delta`, gated on the trade being currently profitable, with parameters exposed to the post-optimizer (Optuna) search.

**Module-vs-engine decision (the question this ticket answers):** BOTH, split by purpose.
- The *diagnostic* is a **separate module** (`scripts/analyze_prob_paths.py`) — read-only, zero engine risk, reusable in /model-detective.
- The *exit rule* must live **inside `BacktestEngine`** — an external counterfactual replay cannot model single-position knock-ons (an early exit frees the slot, changes cooldown state, and admits entries the replay never sees), so any Optuna tuning on an out-of-engine approximation would optimize a fiction.

**Static-vs-dynamic decision:** start **static delta + `require_profit=True`** (the only variant with evidentiary support; unconditional exits underperform doing nothing on the healthy ensemble). Dynamic variants (ATR-scaled delta, prob-velocity/reversal-speed trigger, distribution-relative delta following the existing `_entry_threshold_bounds` pattern) are Phase-3 extensions, added only if the static A/B shows seed-stable holdout improvement.

## Target Files
- `agent/backtest_engine.py` (TradeRecord telemetry; SIGNAL_DECAY exit; opt-in rule evaluation)
- `scripts/analyze_prob_paths.py` (NEW — diagnostic CLI, modeled on `scripts/analyze_trade_patterns.py`)
- `src/live_execution/strategies/execution_models.py` (`TieredEnsembleStrategy` — live-parity counterpart OR loader guard, per gate D1)
- `src/live_execution/config_loader.py` (schema validation for the new per-side block; crash-on-missing per the no-silent-null-defaults rule)
- `agent/strategy_optimizer.py` (Phase 3 — `_PARAM_RANGES` additions + guards)
- `.agents/workflows/model-detective.md` (add a "Step 5b — prob-path forensics" pointing at the new script)
- Tests: `tests/` — engine unit tests, ledger byte-parity sentinel, config-schema tests

## Required Changes

### Phase 1 — Diagnostics (no behavior change, ship first)
1. **Trade-ledger telemetry (additive columns only).** Extend `TradeRecord` / `BacktestResult.to_dataframe` with nullable fields populated from the engine's existing `prob_buy_lookup`/`prob_sell_lookup`: `prob_at_entry`, `prob_min`, `prob_max`, `prob_at_exit`, `bars_below_threshold`, `first_dip_bar_offset`, `opp_prob_max`. Constraint: the ledger schema feeds live/backtest reconciliation — new columns must be additive and the parity/reconciler tooling must be verified to tolerate (or explicitly ignore) them; if it cannot, emit telemetry to a sidecar frame instead of the ledger.
2. **`scripts/analyze_prob_paths.py` (new CLI).** Inputs: `--config`, `--data`, `--batch-dir` (or explicit `--predictions-long/short`), `--output`. Behavior: replay holdout exactly as the prototype does, join per-bar probs onto trade windows, emit a markdown report with (a) winners-vs-losers prob-path table per side, (b) exit-reason × prob-collapse crosstab, (c) SL-loser split "flipped-first vs stopped-while-confident", (d) winners-deep-below-threshold count/PnL, (e) the counterfactual delta sweep (both unconditional and only-if-profitable) **clearly labeled as an approximation** (no re-entry knock-ons). Reuse the prototype's logic; run in the `trader` conda env.
3. **Workflow hook.** Add optional Step 5b to `/model-detective` invoking the script.

### Phase 2 — Engine exit rule (opt-in, default OFF, parity-gated)
4. **Config block** per side: `"signal_decay_exit": {"enabled": bool, "delta": float, "require_profit": bool, "consecutive_bars": int}`. Absent block = feature fully disabled. Present-but-incomplete block = **hard crash** at load (no silent defaults). `config_loader` validates.
5. **Engine semantics.** Evaluate at bar close using the same prob lookups that drive entries: trigger when entry-side prob < `tier min_prob − delta` for `consecutive_bars` consecutive closes (debounce mirrors `consecutive_signal_threshold`), and — when `require_profit` — floating PnL at that close > 0. Exit via the existing `SIGNAL_EXIT` plumbing with a new `ExitReason.SIGNAL_DECAY`, filled per the engine's standard next-bar/slippage conventions (must match whatever `SIGNAL_EXIT` already does; do not invent a new fill path).
6. **Parity sentinel test.** With the block absent or `enabled=false`, the trade ledger must be **byte-identical** to the current engine on a fixed fixture (this is the canary-critical invariant).
7. **Live parity (gate D1).** Either implement the identical rule in `TieredEnsembleStrategy` and run /validate-parity, or stamp configs `signal_decay_exit` as backtest-only and add a live-loader guard that **refuses** to start on a config with it enabled (precedent: exit-trigger overlays were shipped backtest-first with live deferred/human-gated).

### Phase 3 — Optuna search dims (guarded; only after Phase 2 canary passes)
8. **Search space** (opt-in tier flag, like the aggressive-tier rollout): `decay_delta` ∈ [0.05, 0.30] step 0.05; `consecutive_bars` ∈ {1, 2, 3}; `require_profit` **fixed True** (evidence: unconditional is value-destroying); plus a boolean `decay_enabled` so Optuna can turn the feature off entirely.
9. **Anti-overfit guards (mandatory, from project history):**
   - Selection on holdout only, never in-sample-only (pass-2 joint-reopt lesson).
   - Candidate must survive the seed-consistency harness (the 02D batch already produces `seed_consistency/` runs) — improvement must hold across seeds, not just seed 42.
   - **No-crutch rule:** the feature may not be used to promote an ensemble that fails holdout without it (the E02 +$33k "rescue" in the findings is the trap, not the prize).
   - Canary batch before any scout carrying the new dims.
10. **Dynamic variants (only if static wins):** ATR-relative delta (delta scaled by `atr_at_entry`/rolling ATR ratio), prob-velocity trigger (drop of ≥X within N bars), distribution-relative delta reusing the `_entry_threshold_bounds` firing-rate machinery. Each is a separate A/B; do not bundle.

## Human Gates (operator decisions before TDD handoff)
- **D1:** Phase 2 live path — implement live-parity now, or backtest-only stamp + loader guard? (Recommend: backtest-only first.)
- **D2:** Phase 3 VM spend — rerun the NG 02D scout as the A/B vehicle with decay dims (same manifest, new tier flag)?
- **D3:** Scope of first A/B — static-only (recommended) vs static+dynamic bundled.

## Expected Outcome / Success Criteria
- Phase 1: prob-path report generated for any batch folder; findings for NG 02D match this ticket's prototype.
- Phase 2: default-off byte-parity proven; canary green.
- Phase 3: keep the feature only if holdout PnL/Sharpe improves on ensembles that were **already viable**, across seeds; otherwise document and turn the tier flag off.
