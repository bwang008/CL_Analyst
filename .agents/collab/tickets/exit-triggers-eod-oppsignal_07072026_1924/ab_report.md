# Exit-Trigger Enhancements — A/B Report & Next-Step Discussion
**Ticket:** `exit-triggers-eod-oppsignal_07072026_1924`
**Branch:** `training-update` (commits `4dd87f2` weekend baseline, `7d82ffa` Phase 1, `e5f9818` Phase 2) — `stable-fleet` untouched
**Date:** 2026-07-07 · **Status:** backtest-only, default-off, committed; **live parity DEFERRED (human-gated)**
**Audience:** agents picking up follow-on work; humans deciding promotion

---

## 1. What was built

Three independent, config-gated exit enhancements to `agent/backtest_engine.py`. Every existing
config is **byte-identical** when the new keys are absent (pinned by tests). See README
"Exit-Trigger Overlays" for config schemas.

| # | Feature | Config key | ExitReason | Mechanism |
|---|---------|-----------|------------|-----------|
| 1 | Weekend flatten | `weekend_flatten` | `WEEKEND_FLATTEN` | Flatten winner on last bar before a ≥40h gap (data-driven; catches holiday weekends via Thu pre-gap bars) |
| 2 | EOD flatten | `eod_flatten` | `EOD_FLATTEN` | Flatten winner on last bar before a gap in [2h, weekend-threshold) — the daily 17:00–18:00 ET halt; band disjoint from weekend |
| 3 | Opposite-signal profit-close | `conflict_resolution: "close_existing_position_if_profit"` | `SIGNAL_EXIT` | EXIT iff opposite signal fires AND own side stopped confirming AND green (gross, EXEC basis) |

Winner gate for 1–2: unrealized ≥ `profit_atr_mult` × ATR-at-entry, evaluated at bar open;
fires only AFTER TP/SL (intrabar) and TIME_BARRIER — precedence unchanged.

**Design decisions worth knowing before extending:**
- **Price-basis fix.** The original Feature-3 spec compared on_bar's close (brain,
  ratio-adjusted) against the entry fill (exec, raw) — meaningless across bases. The engine now
  publishes exec-basis `EngineState.entry_price` / `floating_pnl_points`; strategies only
  consume the sign. Test `TestEnginePopulatesExecBasis` pins this with diverging bases.
- **Loud-raise guard (impact-review binding condition).** Live DOES reach
  `TieredEnsembleStrategy.on_bar` via `ConfigurableStrategy.evaluate` but never feeds
  `floating_pnl_points`. The new mode raises `RuntimeError` in-position on None rather than
  silently degrading to hold — a config carrying it into today's live path fails fast.
- **EOD band upper bound = weekend threshold even when weekend is disabled**, so an EOD-only
  arm never flattens Fridays; attribution stays clean.

**Verification:** 51 new tests; full fast suite **1815 passed / 10 failed** — the 10 are the
documented pre-existing ES01B sentinel failures (stash-A/B-proven unrelated earlier in this
session). Impact review: APPROVED (`impact_review.md`).

---

## 2. A/B methodology

`python -m agent.ab_exit_triggers --arms ... --gates 0.0,1.0` — for each of the 5
`configs/fleet/fleet_manifest.json` production configs: baseline vs. arm on the **6-month
holdout window only**, same data (per-symbol `<SYM>_HourSet_*` parquet with embedded EXEC_
columns), same predictions, same per-symbol economics from the instrument registry.
Metrics replicate `strategy_optimizer`'s annualized-monthly Sharpe/Sortino. Engine determinism
means deltas are exact, not noisy.

Caveat on the `oppo` arm: it **overrides** each config's existing `conflict_resolution`
(CL/ES/NG/SI baseline = `hold`; GC baseline = `reverse_position`, so for GC the arm replaces
reversal behavior rather than adding to hold).

## 3. Results (holdout, 6mo)

### Aggregate across 5 configs

| Arm | ΔSharpe Σ | ΔPnL Σ | ΔMaxDD Σ (+ = shallower) | improved | exits fired |
|-----|-----------|--------|--------------------------|----------|-------------|
| **eod@0.0** | **+3.72** | **+$95.3k** | **+$46.2k** | 3/5 | 148 |
| eod@1.0 | +0.19 | +$94.3k | +$25.8k | 3/5 | 77 |
| wkd@0.0 | −0.35 | −$45.2k | −$13.8k | 1/5 | 30 |
| wkd@1.0 | −0.00 | −$49.0k | +$0.4k | 3/5 | 15 |
| both@0.0 | +1.70 | +$19.2k | +$38.5k | 3/5 | 182 |
| both@1.0 | −0.42 | +$9.4k | +$4.4k | 3/5 | 92 |
| oppo | +1.02 | +$3.3k | +$2.2k | 2/5 | 37 |

### Per-symbol highlights

| Symbol (baseline Sharpe) | Best arm | Effect | Read |
|---|---|---|---|
| **ES** (0.49) | eod@0.0 | Sharpe **→3.80**, +$30.1k, maxDD −58% | Overnight carry on ES winners was pure downside; banking daily is transformative |
| **SI** (1.17) | eod@1.0 | Sharpe **→2.23**, **+$127.7k**, maxDD −31% | Big winners re-earn entry next day; overnight gives it back |
| **NG** (1.37) | oppo | Sharpe **→2.59**, +$9.8k, 34 profit-closes | Model's opposite signal is real exit information on NG |
| **GC** (1.40) | wkd@0.0 (+0.59) | **eod hurts −0.26…−1.02; oppo −0.26** | GC winners are multi-day trends — do NOT clip them intraday; weekend-only helps |
| **CL** (−0.42) | — | all arms inert (5 holdout trades) | No signal either way; baseline model is the problem, not exits |

### Interpretation

The economic story is coherent: **ES/SI/NG behave like intraday mean-reversion-after-close
markets** (overnight continuation of a winner is worse than re-entry next day), while **GC
trends through halts and weekends** (its optimizer picked `reverse_position` + long trails for
the same reason). The weekend-only hypothesis (the ticket's origin) is mostly a worse version
of the EOD effect — the Friday flatten is dominated by "flatten every day."

## 4. Skepticism ledger (read before promoting)

1. **One 6-month holdout window.** 21–51 EOD events/symbol; a single vol regime. The ES 0.49→3.80
   jump is the kind of number that demands out-of-window confirmation, not celebration.
2. **Per-symbol sign flips (GC vs ES/SI)** mean any fleet-wide default is wrong; this is a
   per-symbol opt-in decision. House rule applies: no tuning to force losers positive.
3. **Turnover +12–26%** on EOD arms; slippage/commissions ARE modeled (1-tick, $2.50/side), but
   live marketable-limit fills near the 17:00 halt may be thinner than modeled.
4. **Gate sensitivity is itself a warning:** SI best at 1.0×ATR, ES/NG at 0.0 — treating the gate
   as a per-symbol free parameter multiplies the overfit surface (Sortino-holdout history).
5. Flatten decision uses the pre-halt bar's OPEN (TIME_BARRIER convention) — live would decide at
   bar close ~1h before the halt; small systematic timing difference to parity-check later.

## 5. Proposed next steps (for discussion)

| # | Step | Cost | Why |
|---|------|------|-----|
| 1 | **Second-window validation**: rerun harness on the pre-holdout optimizer window (and/or holdout_months=12) for eod@0.0/1.0 + oppo | trivial (one command) | Kills or confirms the regime-luck hypothesis before anything else |
| 2 | If (1) holds: stage **per-symbol opt-in configs** — `eod_flatten` for ES/SI/NG (gate per table), nothing for GC/CL; `oppo` candidate only for NG | small | Follows the evidence; default-off keys make this a config-only change |
| 3 | Livetest replay (`scripts/livetest_engine.py`) with an EOD-enabled config | medium | Proves the overlay through the live-simulation path before touching live_trader |
| 4 | **Live-parity ticket** (human-gated): mirror trigger in `live_trader._on_new_bar` (sibling of `_check_time_barrier_exit`), add WEEKEND_FLATTEN/EOD_FLATTEN to `ledger_parity_check.EXIT_REASON_MAP`, decide live cooldown flavor (backtest is flavor-neutral; live SL-list at `configurable_strategy.py:442` would TP-flavor these today), parity test on close decisions | large, touches live | Only after 1–3; blueprint §Live-parity + impact_review §4–5 list the landmines |
| 5 | Optional research: `oppo` interaction with GC's `reverse_position` (should profit-close *veto* reversal instead of replacing it?) | research | GC regression suggests the two modes want to compose, not substitute |

**Recommendation:** run (1) immediately; gate everything else on it.

## 6. Second-window validation results (run 2026-07-07, same day)

Step (1) of §5 executed: `python -m agent.ab_exit_triggers --arms eod,wkd,oppo --gates 0.0,1.0
--window optimizer` — the ~4-year pre-holdout window (2022-01 → 2025-12), non-overlapping with
the decision window. Caveat: baseline params were fit on this window (in-sample for baseline),
but both arms share it, so the DELTA remains the right comparison.

| Arm | Holdout ΔSharpe Σ | 2nd-window ΔSharpe Σ | Verdict |
|-----|-------------------|----------------------|---------|
| eod@0.0 | +3.72 | **−0.33** | Holdout blowout (ES +3.31) did NOT replicate (ES −0.09) — one-window artifact |
| eod@1.0 | +0.19 | −0.18 (but +$28k PnL, 4/5 improved) | Mixed; survives per-symbol (below) |
| wkd@0.0/1.0 | −0.35 / −0.00 | **−1.05 / −1.03** | DEAD both windows — incl. GC's holdout "win" (+0.59 → −0.60) |
| oppo | +1.02 | **−1.64** | NG standout (+1.22) flipped to −0.50/−$52k — regime luck; GC −1.18 |

**Survivors (positive on BOTH windows):**
- **SI + eod@1.0** — holdout +1.06 Sharpe/+$128k; 2nd window +0.27/+$32k, maxDD better both.
  The only robust candidate.
- ES + eod@1.0 — weakly positive both (+0.17/+$0.8k; +0.03/+$3.5k). Marginal.
- CL + eod — tiny positive both windows; immaterial.

**Killed:** weekend flatten (everywhere), oppo (everywhere), eod@0.0 as a fleet default,
GC anything, NG anything.

**Revised recommendation:** the only promotion candidate is `eod_flatten` gate 1.0 on SI
(ES optional/marginal). If execution-style search is added to Optuna (§5 discussion), this
two-window protocol — holdout gate + `--window optimizer` replication — should be the mandatory
promotion check, precisely because the single-holdout numbers above were misleading.

## 7. Artifact map

- Code: `agent/backtest_engine.py`, `src/live_execution/strategy_config.py`,
  `src/live_execution/strategies/execution_models.py`
- Harness: `agent/ab_exit_triggers.py` · Tests: `tests/test_weekend_flatten.py`,
  `tests/test_opposite_signal_profit_close.py`
- Ticket dir: `blueprint.md` (design + empirical gap audit), `impact_review.md` (blast radius +
  binding condition), `ticket_status.md` / `ticket_audit_log.md` (timeline), this report
- Docs: README "Exit-Trigger Overlays" section; `backtest_engine.py` module docstring
