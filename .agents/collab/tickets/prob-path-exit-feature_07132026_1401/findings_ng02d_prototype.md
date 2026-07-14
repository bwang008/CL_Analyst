# Prob-Path Prototype Findings — batch_20260713_005758_NG_02D_SCOUT
**Ticket:** prob-path-exit-feature_07132026_1401
**Date:** 2026-07-13
**Method:** Replayed baseline holdout via `BacktestEngine.from_config` (trader env, `NG_HourSet_02D.parquet` embedded EXEC_* as exec source, slippage 0.001, mult 10000, 12mo holdout), then joined the per-bar prediction CSVs onto each trade's `[entry_time, exit_time]` window. Prototype script: `prob_path_prototype.py` (this folder).

## Reproduction
- **E01**: $28,084.94 / 262 trades — matches report **to the cent**.
- **E02**: -$4,300.19 / 193 trades vs report -$3,595.20 / 194 — one boundary trade at the holdout cut (cut derived from `preds.index.max()` vs generator window). Immaterial for the diagnostic; flag if Phase 1 tooling needs exact parity.
- Entry-bar prob >= tier `min_prob` for **100%** of trades on both ensembles (entry semantics confirmed: prob at entry bar is the firing signal).

## E01 (healthy ensemble, +$28k holdout)
### LONG (thr 0.5452, n=89)
| | n | PnL | med p_entry | med p_min | ever below thr | >0.20 below | med bars |
|---|---|---|---|---|---|---|---|
| losers | 44 | -$28,520 | 0.589 | 0.317 | 93.2% | 63.6% | 20 |
| winners | 45 | +$37,687 | 0.597 | 0.314 | 97.8% | 82.2% | 20 |

**Prob paths of winners and losers are nearly indistinguishable.** Both routinely collapse ~0.23 below threshold mid-trade.

- **SL losers (17):** 14 flipped below threshold before the stop (-$13,370); only 3 were stopped while still confident (-$3,135). → For longs, the user's "threshold hit, then prediction fell away, then stopped" hypothesis is **confirmed (82% of SL losers)**.
- **Winners deep below threshold:** 37/45 long winners went >0.20 below threshold at some point and still won (+$23,731). → A naive "exit on prob drop" kills most winning longs.

### SHORT (thr 0.5804, n=173)
| | n | PnL | med p_entry | med p_min | ever below thr | >0.20 below | med bars |
|---|---|---|---|---|---|---|---|
| losers | 74 | -$48,870 | 0.603 | 0.586 | 37.8% | 13.5% | 3 |
| winners | 99 | +$67,788 | 0.604 | 0.361 | 75.8% | 50.5% | 9 |

**Opposite picture from longs.** Losing shorts die fast (median 3 bars) with the model **still confident** — median min prob 0.586 ≥ threshold. Of 58 SL losers, **35 never dipped below threshold (-$24,945)** vs 23 that flipped first (-$18,245). → "Does it get stopped out even when the predictions are high?" — **yes, that is the dominant short-side failure (60% of short SL losers).**

- Winners deep below threshold: 50/99 (+$62,114) — again, prob collapse is *more* common in winners (they live longer).
- `opp_fired` ~98% for shorts (win AND loss): the long model fires during almost every short trade, so opposite-side flip is **not** discriminative either.
- TIME_BARRIER trades are 100% "ever below" on both sides — these short-horizon models (1–2H targets) mean-revert their prob mass within hours, so any trade held to max_hold sees decay by construction.

## E02 (failing ensemble, -$4.3k holdout) — same pattern, amplified
- LONG SL losers: 9/10 flipped first (-$13,135). SHORT SL losers: 65/91 flipped first (-$65,995), 26 stopped-while-confident (-$21,020).
- Winners >0.20 below threshold: 10/15 longs, 47/56 shorts (+$76,285 of short wins).

## Counterfactual: exit at next bar open once prob < thr − delta
(approximation: keeps original commission, ignores re-entry/cooldown/position-slot knock-ons — the freed single-position slot would admit new trades the replay can't see)

**E01 (actual +$28,085):**
| delta | unconditional | trades cut | only-if-profitable | trades cut |
|---|---|---|---|---|
| 0.00 | -$2,252 | 175 | +$5,098 | 140 |
| 0.05 | -$8,926 | 157 | +$2,724 | 127 |
| 0.10 | +$2,267 | 140 | +$4,667 | 113 |
| 0.15 | +$6,855 | 127 | +$24,135 | 99 |
| 0.20 | +$7,150 | 120 | +$23,510 | 88 |

**Every variant underperforms doing nothing.** The existing exit geometry (TP/SL/trail/time) already harvests what the signal knows.

**E02 (actual -$4,300):**
| delta | unconditional | trades cut | only-if-profitable | trades cut |
|---|---|---|---|---|
| 0.00 | -$9,465 | 152 | +$19,225 | 124 |
| 0.05 | -$5,416 | 133 | +$24,224 | 111 |
| 0.10 | +$132 | 124 | **+$28,982** | 102 |
| 0.15 | +$1,752 | 112 | +$12,662 | 81 |
| 0.20 | -$7,100 | 103 | +$4,440 | 73 |

A +$33k swing on the weak ensemble — but delta was picked on the same holdout being scored, and rescuing a failing ensemble with an execution rule is exactly the **no-optimizer-crutch** trap. Treat as motivation for a *guarded, seed-stable* Optuna A/B, not as a result.

## Conclusions
1. **The analysis is feasible and cheap** — pure post-hoc join, no engine changes needed to *ask* the question; ~30s per ensemble locally.
2. **"Prob dropped below threshold" alone carries no win/loss information** (winners drop as much or more). The only variant with any life is **prob-decay + trade-currently-profitable** — a profit-taking accelerator, not a loss-cutter.
3. Side asymmetry is real and diagnostic gold: long losses = model-flip-then-stop; short losses = stopped-while-confident (exit geometry too tight for the signal's horizon, or the signal is simply wrong fast).
4. Unconditional early exit is value-destroying on a healthy ensemble. Any engine feature must default OFF and be gated `require_profit=True`.
