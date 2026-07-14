# Ticket Resolution Blueprint — trailing-stop-ladder_07132026_1745
**Ticket Directory:** `.agents/collab/tickets/trailing-stop-ladder_07132026_1745/`
**Type:** Feature design (not a bug). Evidence: `findings_ng02d_ladder_prototype.md` + `ladder_prototype.py`, validated engine-exact (455/455 trades reproduced) on `batch_20260713_005758_NG_02D_SCOUT`.
**Status: APPROVED by operator 2026-07-14** (condition: ladder exposed as an Optuna-searchable per-side on/off boolean — confirmed in Phase 2 item 5). D1 resolved per recommendation: backtest-only first, live-loader guard refuses ladder configs until Phase 3. D2 (A/B scout VM spend) still open — canary required before any scout.
**Related:** `.agents/collab/tickets/prob-path-exit-feature_07132026_1401/` (independent exit-management feature; this ladder is the simpler of the two and should ship first — it generalizes a mechanism that already exists in both backtest and live).

## Feature Summary
Generalize the engine's one-shot trailing ratchet (single `trailing_atr_mult` activation → single `trailing_sl_atr_offset` lock, latched by `_trailing_activated`) into an ordered **N-rung trailing ladder**: `[(activation₁, lock₁), (activation₂, lock₂), …]`. When the max favorable excursion (in entry-ATR units) crosses activationᵢ, the stop ratchets to `entry ± lockᵢ×ATR`. A 1-rung ladder is byte-identical to today's behavior.

Evidence in one line: on the healthy NG 02D E01, an upper rung above the existing one is modestly and *systematically* positive (short side: +$1.1k across 20 trades, 17 near-uniform improvements; long side +$6.6k but 78% from one trade); rungs *below* a tight existing rung are always harmful (−$12k…−$17k); the spectacular numbers (+$21k on E02 short) are weak-ensemble rescues = the no-crutch trap. Rung placement is therefore an Optuna-searched, guard-gated parameter.

## Target Files
- `agent/backtest_engine.py` — ladder state machine in BOTH exit paths: `_on_in_position` (single-position) and `_check_position_exit` (concurrent mode); `Order` per-trade override plumbing; `from_config` mapping
- `src/live_execution/config_loader.py` — `trailing_ladder` schema + monotonicity validation (crash on violation; no silent defaults)
- `src/live_execution/strategies/execution_models.py` (`TieredEnsembleStrategy`) — pass-through of ladder params per side/tier
- Live executor stop-modification logic (the code that today performs the one-shot resting-stop modify; same mechanism, N modifies) + reconnect-recovery state rebuild
- `agent/strategy_optimizer.py` — Phase-2 Optuna dims + guards
- Tests: byte-parity sentinel, ladder unit tests, config-validation tests, concurrent-mode tests

## Required Changes

### Phase 1 — Engine ladder (opt-in via config shape, default = legacy)
1. **Config schema.** Per side: `"trailing_ladder": [{"activation_atr": float, "lock_atr": float}, ...]`. Rules enforced at load, crash on violation: activations strictly increasing; locks strictly increasing; `lock_i < activation_i` for every rung; last activation < that side's `tp_atr_mult`. Legacy scalar `trailing_atr_mult`/`trailing_sl_atr_offset` auto-convert to a 1-rung ladder. A config supplying BOTH the ladder and the legacy scalars must crash (ambiguity). Incomplete rung objects must crash.
2. **Engine mechanics.** Replace the `_trailing_activated` bool with a rung index. Each bar, AFTER the TP/SL/time-barrier/flatten checks (preserving the existing "moved stop effective next bar" semantics), advance through all rungs whose activation the extreme-since-entry has crossed (a single bar may advance multiple rungs), setting `sl = entry_price ± lock×ATR` (unrounded, matching current behavior). Implement identically in the concurrent-mode path. `Order` overrides carry the full ladder, not just rung 1.
3. **Exit labeling & cooldowns.** Keep `ExitReason.TRAILING_BE` for any ladder-stop exit (live-label and report compatibility). Pin the current TRAILING_BE→cooldown mapping in a test first and preserve it for all rungs. Add additive trade-ledger columns: `trail_rung_at_exit` (0 = never activated) and `max_favorable_atr` (diagnostic gold per the findings; must not break ledger-parity tooling — verify or sidecar).
4. **Byte-parity sentinel.** Legacy configs (auto-converted 1-rung ladder) must produce byte-identical ledgers vs the pre-change engine on a fixture AND on the NG 02D batch replay. This is the canary-critical invariant.

### Phase 2 — rung-2 policy (REVISED 2026-07-13, operator-proposed fixed rule)
5. **Primary variant — fixed geometric rule, ZERO new tuned parameters:** `trigger₂ = a₁ + 0.5·(tp_mult − a₁)`, `lock₂ = a₁` (stop ratchets to the previous trigger's level), derived at config-materialization time from the already-tuned `(a₁, lock₁, tp_mult)`. Evidence (findings addendum): positive-or-neutral on ALL four ensemble-sides of the 02D batch (+$5.8k / +$0.7k / $0 / +$2.0k), captures ~89% of the swept optimum on the best side, and is crutch-resistant by construction (rung placement follows geometry, so it cannot reproduce the E02 low-rung rescue). Softer locks (0.8·a₁) and a 3-rung extension both tested worse — 2 rungs, full lock. **Exposure (operator-confirmed 2026-07-14): a per-side BOOLEAN dim `ladder_enabled` in the post-optimizer Optuna search space** — the optimizer searches on/off per side/cell; when True the emitted config carries the derived 2-rung ladder, when False it carries today's single rung. Placement itself is never searched.
6. **Fallback variant (only if the fixed rule fails the A/B):** searched dims `a₂ = a₁ + f_a·(tp_mult − a₁)`, `lock₂ = lock₁ + f_o·(a₂ − lock₁)`, `f_a, f_o ∈ [0.2, 0.9]`, plus `ladder_enabled`. Do NOT expose lower-rung placement below the tuned rung 1 (evidence uniformly negative on tight geometries).
7. **Guards (mandatory, from project history):** holdout-only selection; seed-consistency survival (harness already in the batch pipeline); **no-crutch rule** — the ladder may not promote an ensemble that fails holdout without it; canary batch before any scout carrying the change.

### Phase 3 — Live parity (human-gated)
8. Live already performs the one-shot resting-stop modification; extend to N sequential modifies driven by the same rung state machine. **Reconnect recovery must rebuild the rung index deterministically from bars since entry** (rung = f(extremes since entry) — pure function, no persisted latch), with explicit tests; precedent: the reconnect false-flat and `_bars_since` recovery bugs. Full /validate-parity run required. Until this lands, live-loader guard refuses configs whose ladder length > 1.

## Human Gates (operator decisions before TDD handoff)
- **D1:** Scope of first implementation — backtest-only with live-loader guard (recommended), or include live in the same pass since the mechanism is identical?
- **D2:** A/B vehicle + VM spend — rerun an NG 02D-style scout with ladder dims, and/or a targeted replay of the live `NG01B_Sharpe_E03` geometry (LONG TP 8 / 2.4→1.68 has a wide unprotected 1.68→8 gap; cannot be judged from the 02D artifacts)?
- **D3:** ~~Fix N=2 vs general N~~ — largely answered by evidence: a 3-rung extension of the same rule added nothing, so ship the general-N data model but materialize only the fixed 2-rung rule. Remaining decision: fixed rule only (recommended), or also carry the searched-dims fallback in the same A/B?

## Expected Outcome / Success Criteria
- Phase 1: 455/455-style byte parity for legacy configs; ladder unit tests green in both engine paths.
- Phase 2: keep the dims only if holdout improvement is seed-stable on already-viable ensembles; expected magnitude is modest (single-digit % of side PnL on tight geometries) — the +$21k-style rescues on failing sides are explicitly NOT success.
- Phase 3: /validate-parity green including reconnect-recovery rung rebuild.
