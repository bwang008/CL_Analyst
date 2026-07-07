---
name: model-detective
description: Forensic audit of a completed batch run — reproduces the reported PnL, attributes it to individual trades, then stress-tests each model for genuine out-of-sample EDGE (holdout AUC vs its real target), threshold fragility, signal-firing balance, and cross-config agreement, to decide "luck vs signal" and whether a model is safe to trade. Point it at a `reports/batch_runs/<batch>` folder.
---

# /model-detective — Model Performance Forensics

You are the **Model Detective**. Given a completed batch folder
(`reports/batch_runs/<batch>/`, e.g. `batch_20260706_0925_SI_01B_SCOUT`), your job is
to decide whether a strategy's headline result is **real edge or luck/artifact**, and to
quantify what would make it robust. You produce `model_detective_report.md` inside that
folder with a verdict and evidence.

> **Golden rule:** never trust a report's dollar figure until you have (a) reproduced it
> from the raw trades, (b) confirmed the underlying price move is real, and (c) shown the
> entry signal has out-of-sample ranking skill against the label it was trained on. A big
> number with holdout AUC ≈ 0.50 is exit-luck, not alpha.

## Environment & known gotchas (read before running anything)
- **Use the `trader` conda env** — global Python lacks `joblib`/`pandas_ta`/`lightgbm`.
  Run snippets with `conda run -n trader python - << 'PY' … PY` (or activate it).
- **Exec/raw prices:** `<SYM>_raw.parquet` may not exist locally (it ran on the VM). The
  `<SYM>_HourSet_*.parquet` embeds `EXEC_Open/High/Low/Close` (== the raw continuous
  contract). `load_ohlcv_dual(data)` returns `(features, exec)` from those columns, so you
  can reproduce PnL **without** `--exec-data`. Verify `EXEC_Close == si_continuous_master`.
- **Threshold override gotcha:** `TieredEnsembleStrategy` reads its entry threshold from
  `cfg["<side>"]["tiers"][*]["min_prob"]`, **NOT** `cfg["models"]["<side>"]["threshold"]`
  (the latter only emits a warning). To sweep thresholds you MUST rewrite the tier
  `min_prob` values.
- **AUC must be vs the model's OWN training target**, on the holdout — not vs forward
  returns. Find each model's target in
  `reports/sweep_*/…/registry/E2E_*_<side>_<metric>/experiment_config.json` (`target`).
- **pos-rate ≠ learnability.** A target with more positive labels is not more predictable
  (SI 3x1_6H had 7.8% positives but holdout AUC 0.64; 4x1_36H had 17% but AUC 0.50).

## Step 0 — Inventory & pick the target of the investigation
1. Read `batch_summary*.md`, the `{sharpe,sortino}_ensemble_backtests.md`, `manifest.json`,
   and the per-ensemble `configs/*.json`. Note the dataset, `execution_symbol`,
   `contract_multiplier`, `slippage_per_side`, and `holdout_months`.
2. State the **claim under investigation** in one line (e.g. "Ensemble 2 holdout month
   2026-01 = +$172k while every other ensemble was flat/negative"). Everything below is
   evidence for/against that claim.

## Step 1 — Reproduce the headline & dump the trades (is it real, and which trades?)
Replay the exact holdout via the engine and export per-trade rows. This must match the
report to the cent; if it doesn't, stop — the report is stale or the inputs differ.
```python
# conda run -n trader python - << 'PY'
import pandas as pd
from src.live_execution.config_loader import load_strategy_config
from agent.backtest_engine import BacktestEngine, load_ohlcv_dual, load_predictions, _resolve_prob_column
BATCH='reports/batch_runs/<batch>'; ENS='<E0x>'; DATA='data/processed/<SYM>_HourSet_XX.parquet'
cfg=load_strategy_config(f'{BATCH}/configs/<CONFIG>.json')
ohlcv,ex=load_ohlcv_dual(DATA)
d=load_predictions(f'{BATCH}/predictions/<PRED>.csv')
b=_resolve_prob_column(d,'buy'); s=_resolve_prob_column(d,'sell')
preds=d[[b]].rename(columns={b:'prob_Buy'}).join(d[[s]].rename(columns={s:'prob_Sell'}),how='outer').fillna(0.0)
cut=preds.index.max()-pd.DateOffset(months=cfg.get('holdout_months',6)); hp=preds[preds.index>=cut]
bt=BacktestEngine.from_config(cfg, slippage_per_side=<SLIP>, contract_multiplier=<MULT>)
t=bt.run(hp,ohlcv,ohlcv_exec_df=ex).to_dataframe()
t['entry_time']=pd.to_datetime(t['entry_time']); t['mo']=t['entry_time'].dt.to_period('M').astype(str)
print('holdout net', round(t.net_pnl_dollars.sum(),2))
print(t.groupby('mo').net_pnl_dollars.agg(['count','sum']).round(0))
print(t.sort_values('net_pnl_dollars').tail(5)[['signal_side','entry_time','exit_time','entry_fill','exit_fill','atr_at_entry','exit_reason','net_pnl_dollars']])
PY
```
Record: monthly PnL, the **top ± trades**, their exit_reason (TP/SL/TRAILING/TIME), and
**concentration** = top-1 and top-3 net PnL as a % of the month/holdout. A month carried by
1–3 trades is a red flag regardless of the total.

## Step 2 — Real move vs data artifact
For the window of the big trade(s): print raw `EXEC_*` OHLC hour-by-hour and confirm the
move is coherent (multi-bar, volume rising) — not a single bad print. Cross-check the
`<SYM>_continuous_master_1h.parquet` and (if possible) an external source. Sanity checks:
largest 1h returns of the whole series (is the anomaly an outlier vs history?), and the
weekday calendar (real futures = no Saturdays, Sunday-evening reopen). Note the **price
level** — inflated levels (e.g. silver at $100+) magnify dollar PnL for the same % move.

## Step 3 — Exit-math verification (ATR / TP / SL)
Recompute Wilder ATR on the **raw exec** prices at the entry bar with the side's
`atr_period`; confirm it equals `atr_at_entry`. Verify the exit reconciles: e.g. short
`TP = entry − tp_atr_mult×ATR`, and the first bar whose low ≤ TP is the exit bar/fill.
If the fill is at the barrier (not the bar extreme) the engine is being conservative — good.
Flag any TP/SL that does **not** reconcile with the price path (that is a real bug).

## Step 4 — Directional EDGE: holdout AUC vs the real target (the gate)
For each model in the ensemble, AUC of its probability against the exact triple-barrier
label it trained on, split train vs holdout. **This is the single most important number.**
```python
# ... load preds E[...] and target columns from the HourSet ...
# y=(df[TARGET_COL]>0); auc(prob[train], y[train]) and auc(prob[holdout], y[holdout])
```
Heuristic: **holdout AUC ≥ ~0.55 = real edge; ~0.50 = none (PnL is exit/vol-timing luck).**
Also compute forward-return AUC (1/3/6h) as a cross-check. If holdout AUC ≈ 0.50, the model
should be discarded no matter how good the backtest dollars look.

## Step 5 — Signal distribution & firing balance (drowning-out detection)
Per model, print the probability distribution (mean, p50/p90/p99/max) and the **firing rate**
`P(prob ≥ threshold)` on train and holdout, plus long vs short trade counts from Step 1.
Because the engine is single-position, a side that fires on ~all bars **starves the other
side** (once in a position, opposite signals are HOLD). A threshold below the model's whole
prob mass (→ ~99% firing) converts a ranker into an "always-on" bet and throws its edge away.
Healthy firing rate ≈ **15–70%** of bars.

## Step 6 — Threshold fragility sweep
Re-run the holdout while overriding only the entry threshold (rewrite `<side>.tiers[*].
min_prob`), e.g. `{0.34,0.45,0.50,0.52,0.55}`, reporting NET and long/short split for the
**optimizer window vs holdout** side-by-side. Two failure signatures to look for:
- **Sign flip on a small nudge** (e.g. holdout −$158k → +$34k between 0.34 and 0.55) ⇒ fragile.
- **Optimizer-best == holdout-worst threshold** ⇒ the search overfit the in-sample regime
  (this is the fingerprint of a too-low threshold floor; see the code note below).

## Step 7 — Cross-config / cross-ensemble agreement
Identify which ensembles **share a model** (same sweep+side+metric ⇒ identical prob column;
verify with `np.allclose`). Configs that share a model but differ in threshold/exits should
**agree in sign** on the holdout. Catastrophic disagreement (same short model → one ensemble
+$160k, another −$158k) means the dollar outcome is not a property of the model → luck.

## Step 8 (optional) — Structural trade-pattern scan
For day-of-week / holiday / toxic-hour biases, run the existing analyzer and fold its
findings in:
```bash
conda run -n trader python scripts/analyze_trade_patterns.py --config <CONFIG>.json --data <DATA>.parquet --output reports/batch_runs/<batch>/<ENS>_trade_patterns.md
```

## Step 9 — Write the verdict
Write `reports/batch_runs/<batch>/model_detective_report.md`:
```markdown
# Model Detective — <batch>
**Claim investigated:** …
**Verdict:** REAL EDGE | LUCK/FRAGILE | DATA ARTIFACT | MIXED

## Reproduction
- Holdout net reproduced: $… (report $…) ✅/❌
- Concentration: top-1 = …% of month, top-3 = …%

## Edge (holdout AUC vs real target)
| model | used by | AUC train | AUC holdout | verdict |
## Signal balance & firing rate
## Threshold fragility (optimizer vs holdout sweep)
## Cross-config agreement
## Real-move / data check
## Conclusion & recommendations
- keep/discard which models & why; threshold/firing fixes; target swaps to screen next.
```
End by messaging the user the verdict, the report path, and the 2–3 highest-value follow-ups
(e.g. "discard 4x1_36H target (AUC 0.50)"; "raise threshold floor / add firing-rate gate";
"run /cloud-target-batch to screen replacement targets").

## Interpretation cheat-sheet
| Signal | Edge | Luck/fragile |
|---|---|---|
| Holdout AUC vs own target | ≥ 0.55 | ≈ 0.50 |
| Top-1 trade share of month | small | > ~50% |
| Firing rate at chosen threshold | 15–70% | ~99% (always-on) or ~0% |
| Threshold sweep | stable sign | flips sign on small nudge |
| Cross-config sharing a model | agree in sign | opposite signs |

## Related code (for fixes this audit motivates)
- Entry-threshold search space: `agent/strategy_optimizer.py` — `PARAM_SPACE`
  `"entry_threshold": (0.30, 0.70, 0.04, "float")` (this is the "0.3–0.7 floor"). Making
  the floor **distribution-relative** (per-model, cap firing ≤70% / floor ≥15%) is a change
  here, in the **post-optimizer**, not in model training (`optuna_lgbm_search_v2.py`).
- Trade-floor penalty / objective cap: `agent/strategy_optimizer.py`
  (`TRADES_PER_YEAR_FLOOR`, `OBJECTIVE_SCORE_CAP`).
- Target pre-screen (edge without a full sweep): `/cloud-target-batch`.
