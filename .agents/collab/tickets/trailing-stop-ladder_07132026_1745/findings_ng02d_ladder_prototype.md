# Trailing-Stop Ladder Prototype Findings — batch_20260713_005758_NG_02D_SCOUT
**Ticket:** trailing-stop-ladder_07132026_1745
**Date:** 2026-07-13
**Method:** Engine-exact per-trade replay (same TP/SL intrabar checks, SL-wins-ties, gap fill at open, exit slippage, ladder advance AFTER exit checks so a moved stop is effective next bar, moved stop relative to `entry_price` unrounded — all mirrored from `agent/backtest_engine.py:_on_in_position`). **Validation: 262/262 (E01) and 193/193 (E02) trades reproduced exactly with the existing single rung**, so per-trade counterfactuals are exact; only re-entry/cooldown knock-ons are unmodeled. Script: `ladder_prototype.py` (this folder).

## What exists today
The engine's "trailing stop" is a **one-shot ratchet**: when the extreme-since-entry reaches `entry ± trailing_atr_mult×ATR`, the SL moves ONCE to `entry ± trailing_sl_atr_offset×ATR` and never moves again (`_trailing_activated` latch, [backtest_engine.py:810-828]). The proposed feature = generalize to an ordered ladder of (activation, lock) rungs.

## Key results (12mo holdout)

### Adding an UPPER rung above the existing one (keep existing lock, ratchet higher later)
**E01 LONG** (TP 4.25, existing rung 2.55→lock 0.51; side actual $9,167):
| a_hi | o_hi | delta | trades changed |
|---|---|---|---|
| 3.0 | 2.0 | **+$6,561 (+72% of side)** | 7 |
| 3.0 | 1.0 | +$636 | 3 |

Concentration check on (3.0, 2.0): **+$5,087 of the +$6,561 is ONE trade** (2026-01-30, a −$3,205 give-back-to-lock loss converted to +$1,882). Remaining 6 trades: +$1,101, +$470, +$364 vs −$361, −$88, −$13. Mechanism real, magnitude 1-trade-driven on this holdout.

**E01 SHORT** (TP 7.25, existing rung 1.45→lock 0.29; side actual $18,918):
| a_hi | o_hi | delta | trades changed |
|---|---|---|---|
| 3.0 | 1.0 | +$1,108 | 20 |
| 4.0 | 1.0 | +$478 | 7 |

The (3.0, 1.0) result is **systematic, not lucky**: 17 near-uniform improvements (+$54…+$329 each — all trades that ran ≥3 ATR then gave nearly everything back to the 0.29 lock, now keeping 1 ATR) vs 3 TIME_BARRIER truncations (−$386, −$1,256, −$299). This is exactly the intuition behind the feature.

E02: upper rung changes zero trades (long: nothing travels that far; short: existing activation 6.4 already sits under TP 8 — no room).

### Adding a LOWER rung below the existing one (early small-profit lock)
- **E01, both sides: harmful in every configuration.** Long: best case +$1.7k only when snugged just under the existing rung (1.5/0.50); at activation 0.5–1.0 it destroys −$12k to −$16k. Short (existing activation already 1.45): every candidate loses −$11.6k to −$16.6k. Early locks strangle the winners that pay for the whole book.
- **E02 SHORT** (existing rung far away: 6.4/5.12, TP 8 — a wide unprotected zone): lower rung (1.0, 0.5) → **+$21,337**; the user's exact example (2.0 → lock 1.0) → **+$20,041**. This is the biggest number in the study **and the no-optimizer-crutch trap**: it rescues a *failing* ensemble (−$330 short side), with the rung picked on the same holdout being scored.

## Placement principle (the transferable insight)
Ladder value lives in the **unprotected gap between the current lock and the TP**. Where the existing rung is tight (E01: activation 1.45–2.55, locks near breakeven), a lower rung only truncates winners; an upper rung adds modest, systematic value. Where the geometry leaves a wide gap (E02 short: 6.4 activation under TP 8; **live NG long: lock 1.68 under TP 8**), a rung in the gap changes many trades — that is also where regime luck and crutch-rescue risk concentrate, so it must be validated across seeds/symbols, not hand-picked.

**Live fleet relevance:** `NG01B_Sharpe_E03` live config = LONG TP 8 / trail 2.4→1.68, SHORT TP 6 / trail 4.8→4.8. The live long already has the user's proposed low rung (≈2 ATR → lock ≈1.7); what it lacks is the upper rung (e.g., 5.5 → lock 3) covering the 1.68→8.0 gap. That geometry cannot be tested on the 02D scout artifacts (different params); it needs its own replay before any conclusion.

## Addendum 2026-07-13 (later): fixed geometric rule — user-proposed, ZERO new tuned parameters
Rule: `trigger₂ = a₁ + 0.5·(TP − a₁)` (midpoint of the remaining distance), `lock₂ = a₁` (stop ratchets to the previous trigger's level). Script: `fixed_rule_ladder_test.py` (this folder).

| Side | rung2 derived | delta | changed |
|---|---|---|---|
| E01 LONG | (3.40 → 2.55) | **+$5,823** (89% of the swept optimum) | 5 |
| E01 SHORT | (4.35 → 1.45) | +$721 (65% of swept optimum) | 7 |
| E02 LONG | (3.40 → 2.55) | $0 — no trades travel that far | 0 |
| E02 SHORT | (7.20 → 6.40) | +$2,026 | 9 |

- **Positive-or-neutral on all 4 sides; no side degraded.** Combined: E01 +$6.5k (+23% of holdout), E02 +$2.0k.
- **Crutch-resistant by construction:** because rung placement follows the tuned geometry instead of being fitted, the rule cannot reproduce the E02 +$21k low-rung rescue — it placed the rung at 7.2, not 1.0.
- Softer lock (`lock₂ = 0.8·a₁`) tested consistently worse. A 3-rung extension of the same halving rule added nothing (E01 long +$5.1k vs +$5.8k; more truncations) — **2 rungs suffice**.
- Caveats unchanged: single symbol/holdout; the E01 long gain is still dominated by the same 2026-01-30 giveback trade.

## Conclusions
1. Feasible and cheap: the ladder is a strict generalization of the existing one-shot ratchet; a 1-rung ladder is byte-identical to today's behavior (proven by the 455/455 validation).
2. Evidence supports **upper rungs on healthy configs** (modest, systematic gains; short side +$1.1k across 20 trades with near-uniform per-trade profile). Lower rungs below tight existing rungs are value-destroying.
3. The big dollar swings (E02 short +$21k) are weak-ensemble rescues = the crutch trap. Rung placement must be an Optuna-searched, holdout+seed-gated parameter, never a hand-picked constant.
