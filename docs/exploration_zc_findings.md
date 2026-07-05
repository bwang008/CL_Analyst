# ZC Exploration Findings — branch `exploration` (2026-07-05)

**Question asked:** "I found edge on ZC but the big gains were at the start and died out.
Was the model overfitting, regime-specialized, or is the alpha gone? Can we build a
complementary low-vol ZC model?"

**Short answer:** None of the three. The model's predictive skill never changed — but it
was never predicting *direction*, and the "edge" was an artifact of running ZC through
the evaluation chain at **CL economics**. At true ZC costs the strategy loses money.
The 2022-heavy PnL shape is volatility scaling of a vol-harvesting exit engine, plus an
optimizer that was allowed to scalp because modeled costs were ~25x too low.

---

## 1. The economics bug (root cause of the mirage)

Every stage after training — `strategy_optimizer` (the Optuna objective that picks
TP/SL/thresholds), `batch_post_optimizer`, `generate_ensemble_artifacts`, the
`backtest_engine` CLI defaults, and the cloud baseline backtests — used
`contract_multiplier = 1000 $/pt` (CL) and `slippage_per_side = 0.01` (1 CL tick)
for **every symbol**.

| Symbol | True $/pt | True tick | Manifest slippage 0.01 = | Cost distortion |
|--------|-----------|-----------|--------------------------|-----------------|
| CL     | 1000      | 0.01      | 1 tick                   | correct         |
| ZC/ZS  | 50        | 0.25      | 1/25 tick                | ~25x understated |
| ES/NQ  | 50/20     | 0.25      | 1/25 tick                | ~25x understated |
| GC     | 100       | 0.10      | 1/10 tick                | ~10x understated |
| SI     | 5000      | 0.005     | 2 ticks                  | ~2x OVERstated  |
| NG     | 10000     | 0.001     | 10 ticks                 | ~10x OVERstated |

Measured on the batch_20260704_2215 ZC Sortino E01 ensemble:

| Economics                        | Net PnL   | PF   | WR    |
|----------------------------------|-----------|------|-------|
| As reported (CL econ)            | +$603,167 | 1.30 | 54.6% |
| True ZC, 1-tick slippage/side    | **-$15,843** | 0.88 | 33.9% |
| True ZC, half-tick slippage/side | +$5,564   | 1.05 | 45.5% |

Average gross capture was 0.43 pts/trade ≈ 1.7 ticks vs ~2.4-tick round-trip cost.
The optimizer selected sub-spread scalps because it couldn't feel the spread.

**NG corollary:** NG costs were 10x overstated — NG models were unfairly penalized and
deserve a re-look after the fix.

**Fixed** (commit d05cebe + d176bf2, tickets `per-symbol-economics_07052026_0930`,
`exec-data-index-zeros_07052026_0935`):
- `instrument_master.dollars_per_point()` / `default_slippage_points()`; symbol threaded
  manifest → post-optimizer → optimizer → engine, fail-loud when unresolvable (no CL default).
- CL resolves byte-identically (1000.0 / 0.01) — ledger-parity gate safe (137 affected-suite
  tests + 18 new tests pass).
- The non-CL "all baseline metrics = $0" bug (int64-indexed `<SYM>_raw.parquet` reindexed
  against DatetimeIndex) is also fixed — this had degraded top-pair selection to
  tie-breaking noise for every non-CL batch, and was why ZC_01B looked "failed".
- ZC manifests' slippage corrected 0.01 → 0.25. Other symbols' manifests left for owner
  review (deliberately — they change fleet-visible numbers).

**Live-fleet check:** ES01B_Sharpe_E03 (MES, currently dry-run in the fleet) was re-scored
at true ES economics: **survives** — PF 1.23, +$89.5k ES-sized (÷10 for MES) over 4.5y,
positive 4/5 years, avg gross 2.2 pts ≈ 9 ticks/trade. Its reported dollar figures were
~27x inflated, but the model is genuinely viable. HS14B (CL) was always evaluated at
correct economics.

## 2. Was the ZC model overfit / regime-bound / decayed?

Diagnostics on the E01 predictions (2022→2026, fully out-of-sample — train cutoff was
2022-01-01):

- **Skill is flat, not decaying.** AUC vs its own triple-barrier target, by year:
  0.775 / 0.772 / 0.807 / 0.785 / 0.779. The worst PnL year (2024) had the *best* AUC.
- **The skill is not directional.** corr(prob_Buy, prob_Sell) = 0.88; prob_Buy predicts
  the SHORT target as well as it predicts the LONG target (cross-AUC 0.75-0.80 = own-AUC);
  restricted to bars where exactly one barrier fired, direction AUC = 0.50-0.53.
  The model learned "will corn move within 3 hours" — the grain-session activity clock —
  not which way it moves.
- **CL (HS14A: direction-AUC 0.506) and ES (01A/01B: ~0.54) have the same signature.**
  The whole fleet's PnL engine is asymmetric exits (TP >> SL + trailing) harvesting vol
  expansions flagged by a movement-timing model. That's a legitimate edge only where
  costs per trade are small vs the harvested move (true for CL, marginal for ES, false
  for ZC scalps).
- **The 2022-heavy PnL shape** is what a vol harvester does: ZC entry-ATR averaged 3.2 in
  2022 (Ukraine invasion) vs 1.46 in 2024. $473k of the $603k (fake-econ) came from the
  top-tercile vol months; corr(monthly PnL, ATR) = 0.47. Same trades in points terms were
  ~7x smaller in 2024.

So: **not** overfit in the classical sense (OOS skill stable), **not** decayed alpha
(nothing directional existed to decay), and "regime-specialized" only in the sense that
a vol harvester earns in vol regimes — 2022 richness was real vol, monetized at fake costs.

## 3. Can the existing ZC models be rescued by strategy parameters? — NO

Re-ran the full post-optimizer (200 trials × 4 pairs × both objectives, seed 42, same
machinery as the cloud) on the existing 01A predictions at TRUE economics
(`batch_20260704_2215_ZC_01A_TRUECOST`):

- The optimizer immediately fled activity: 1047 trades → 97 / 39 / 274 / 4.
- Best optimizer-window PnLs: $2-12k over 4 years (vs $530k+ under fake costs).
- **Holdout (2026-01→07): -$1,006 / $0 (no trades) / -$33 / $0 (Sortino);
  +$219 (7 trades) / -$3,684 / -$3,066 / -$1,563 (Sharpe).**

No configuration of the existing ZC 01A models is deployable. Per the "discard bad
models, don't add crutches" rule: these are discards.

Also fixed en route: `--min-trades` on `batch_post_optimizer` is accepted but never
reaches the objective (pre-existing no-op — the real floor is `TRADES_PER_YEAR_FLOOR`
= 100/yr ensemble, 50/yr single-side); and concurrent post-opt batches collided on the
Optuna study `.db` (PID-hashed now, commit 4c44438).

## 4. Trade floor & statistical significance (your question)

Raising the trade floor points the wrong way: more trades = thinner per-trade edge, and
under honest costs thin edges are exactly what dies. You want *fewer, larger* captures.

On "2-year backtest + 6-month holdout — is that significant?" (bootstrap on the actual
E01 holdout ledger, 20k resamples):
- The best-looking holdout you had (PF 1.48 on 164 trades, fake econ) carries a 90% CI
  of **[0.97, 2.24]** — it never statistically excluded zero edge (p ≈ 0.057 against a
  demeaned null). A 6-month holdout at scalping cadence is a *veto gate*, not a
  significance test.
- At long-horizon cadence (~40 trades per 6 months) the CI balloons to [0.85, 4.79] —
  a single holdout is nearly uninformative alone.
- The 2y optimizer window gives the post-optimizer only ~18 monthly-PnL points to rank
  200 trials — selection-bias risk is higher than with the 4y window.
- Practical protocol: keep the full OOS window for the optimizer, treat the 6-month
  holdout as a *veto* (reject if negative), and require the sign of the edge to persist
  across vol terciles rather than trying to hit p<0.05 on holdout PF alone.
- (Exact CIs for the new runs are computed in §5 once trades exist.)

## 5. Can a better ZC model be built? (cloud experiments, 2/10 scout budget)

- **S1 `batch_20260705_0840`** — new models on big-capture targets
  (5x1/8x2 × 36H/48H), train cutoff 2022-01-01, true economics end-to-end.
  Hypothesis: multi-point captures clear the ~0.6 pt round-trip cost.
- **S2 `batch_20260705_0842`** — identical targets, train cutoff **2024-07-01**
  (+2.5y more training data including the 2022 regime; 2y OOS; 6mo holdout).
  Tests the "raise the training window, narrow the backtest" idea as a clean A/B vs S1.
- **Local 01B re-opt** — pruned-feature models under true costs (also validates that the
  "failed" 01B batch was just the zeros bug).

**01B result (batch_20260705_0458_ZC_01B_TRUECOST):** the "failed" batch was indeed
healthy — models train fine; only the baseline display was zeroed by the exec-index bug.
At true costs the pruned-feature models are marginally better than 01A but still not
deployable: optimizer-window PnL $2.4k-$12k over 4y; holdouts -$1,724 to +$1,483 on
27-54 trades (noise). The 4x1_36H long side is again the least-bad component —
consistent with the long-horizon hypothesis S1/S2 are testing.

**S1 result (long-horizon, cutoff 2022):** training completed cleanly (and validated the
zeros fix in the cloud — first non-CL batch with real baseline metrics). The cloud
post-optimizer VM crashed on a missing `src/core` in the optimizer deploy whitelist
(fixed, c2c2adf); post-opt re-run locally with identical parameters (200 trials, seed 42,
both objectives, true economics). Verdict: 16 individually-optimized models all collapse
to tiny books (best: $7.5k over 4y on 234 trades, holdout -$692). Ensembles: optimizer
window $24-$7.5k, **holdouts -$2,384 to +$950** — noise.

**S2 result (long-horizon, cutoff 2024-07 — the "raise training window, narrow backtest"
variant):** same protocol. 2.5y more training data (including the 2022 regime) changed
nothing: individual holdouts -$2,944 to +$284; ensemble holdouts **-$1,923 to +$433**.

### Verdict across all four arms

| Arm | Models | Training window | Eval window | Best honest holdout |
|-----|--------|-----------------|-------------|---------------------|
| 01A re-opt | existing 3H/6H/36H | 2010→2022 | 4y + 6mo ho | +$219 (7 trades) |
| 01B re-opt | pruned features | 2010→2022 | 4y + 6mo ho | +$1,483 (27 trades) |
| S1 (new) | 5x1/8x2 × 36H/48H | 2010→2022 | 4y + 6mo ho | +$950 (23 trades) |
| S2 (new) | 5x1/8x2 × 36H/48H | 2010→2024-07 | 18mo + 6mo ho | +$433 (39 trades) |

48 individually-optimized models and 24 ensemble optimizations, two objectives, two
feature sets, five target horizons (3H→48H), two training cutoffs: **nothing clears real
ZC transaction costs out-of-sample.** Per the discard-don't-crutch rule, ZC should be
dropped from the fleet roadmap until the feature set contains something actually
directional for grains (COT positioning, seasonality, WASDE calendar — see §6). The
scout budget spent: 2 of 10 authorized runs; the other diagnoses were local and free.

## 6. On the "low-vol specialist" idea

The honest read: with the current feature set there is no directional signal in *any*
regime (direction-AUC ≈ 0.5 across all years and vol buckets), so training the same
features on low-vol samples is unlikely to produce a real complement — and low-vol
periods are where fixed costs bite hardest. The constructive version of your idea:
1. **Longer-horizon captures** (S1/S2, running) — fewer trades, bigger points, and if
   the 36H/48H models show *any* directional tilt it monetizes there.
2. **Directional grain features** as a follow-up HourSet_01C: CFTC COT positioning
   (adapter already exists), month-of-year seasonality (planting/pollination/harvest),
   and days-to-WASDE calendar distance — all leakage-safe (public calendars) and trivially
   available live. Worth a scout only if S1/S2 shows the exit engine can at least run
   cost-positive on ZC.
3. Conflict handling for two concurrent ZC models is deferred per your note.
