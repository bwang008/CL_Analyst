# Case Report — ES baseline metrics all zero in batch reports
**Ticket ID:** `es-baseline-zero-metrics_07042026`
**Ticket Directory:** `.agents/collab/tickets/es-baseline-zero-metrics_07042026/`
**Date:** 2026-07-04 · **Mode:** read-only investigation (no fixes applied)
**Workflow:** ticket-manager (hub) → Ticket-Auditor (RCA, locally reproduced) → Ticket-Impact-Reviewer (APPROVED fix A+B)

---

## 1. Symptom

In the baseline batch report `batch_summary.md`, every per-model table (Long/Short Model × Logloss/Average Precision, plus "Ensemble (Both Metrics)") shows **0 trades / 0.0% WR / 0.00 PF / $0 PnL** for both ES batches, while equivalent CL batches show normal nonzero values. The downstream ensemble optimization for the same ES batches (`batch_summary_optimized_ensembles_sharpe.md`) shows healthy nonzero trades and PnL, proving the models and predictions themselves are fine — only the VM-side baseline per-model backtests produced zeros.

**Affected report files (originally flagged):**
- ZEROED: `reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/batch_summary.md`
- ZEROED: `reports/batch_runs/batch_20260704_2016_ES_01A_SCOUT/batch_summary.md`
- HEALTHY comparators: `reports/batch_runs/batch_20260704_0310_CL_14A_SCOUT_PASS/batch_summary.md`, `reports/batch_runs/batch_20260702_0038_SCOUT_14B_V2/batch_summary.md`

The full affected set is much larger — see Blast radius (§3).

---

## 2. Root cause

**Verdict: producer-side (VM), not the collector.** The zeros are already present in every ES `pipeline_summary.json`; `gcp/collect_batch_results.ps1:197-213` renders them faithfully. Verified directly:

```
reports/sweep_es01b_2x1_6h_scout_20260704-0701/pipeline_summary.json → backtest_results:
  long_logloss:             trades=0  pnl=0
  long_average_precision:   trades=0  pnl=0
  short_logloss:            trades=0  pnl=0
  short_average_precision:  trades=0  pnl=0
  ensemble_logloss:         trades=0  pnl=0
  ensemble_average_precision: trades=0  pnl=0
```

(`agent/generate_batch_report.py` — the script with the hardcoded CL dataset and `except: pass` blocks — has **no callers** anywhere in the repo. It is legacy and not in the VM path; it is *not* the culprit.)

### Failure chain

1. **Data shape mismatch (the trigger).** All seven new-symbol execution parquets (`ES/NG/NQ/ZC/GC/ZS/SI_raw.parquet` in `C:\CL_Analyst_Data\data\processed\` and their GCS copies) store `DateTime` as a **column** over an **int64 RangeIndex** (0..95733). The healthy `CL_raw.parquet` has a proper `datetime64[ns]` index named `DateTime` with no `DateTime` column. The standup workflow itself seeded the error: `.agents/workflows/build-symbol-pipeline.md:49` instructs builders to "mirror `CL_raw_1h.parquet`" — a file that has the *wrong* (int-indexed) shape.

2. **Unnormalized read (the latent code trap).** `gcp/vm_e2e_pipeline.py:271-273` (`run_backtest`) and `:896-898` (baseline ensemble block) do a raw `pd.read_parquet(exec_data_path)` with **no** DateTime-column→index promotion, then call `engine.run(signals, exec_df)` (`:279`, `:904`):

   ```python
   # gcp/vm_e2e_pipeline.py:271-273
   if exec_data_path:
       if str(exec_data_path).endswith('.parquet'):
           exec_df = pd.read_parquet(exec_data_path)   # ← no index normalization
   ```

   The repo already has a normalizing loader — `agent/backtest_engine.py:load_ohlcv` (`:1739-1741` promotes a `DateTime` column to the index and validates OHLCV columns) — but `vm_e2e_pipeline.py` doesn't use it. The healthy local post-optimizer path does, which is exactly why the optimized-ensemble reports were fine.

3. **Silent miss (why nothing crashed).** `agent/backtest_engine.py:1206` — `ts = pd.Timestamp(row.Index)` converts int64 index values into 1970-epoch nanosecond timestamps; `:1221-1222` — `prob_buy_lookup.get(ts, 0.0)` / `prob_sell_lookup.get(ts, 0.0)` silently default every bar's probability to 0.0. No signal ever fires → 0 trades, 0 PnL, no warning, exit code 0. The VM log (`reports/sweep_es01b_2x1_6h_scout_20260704-0701/*.log`) shows a "clean" run: "Trades: 0" for all six backtests.

### Reproduction (definitive)

Run locally in the `trader` env with the real engine, real ES predictions, and real strategy config:
- **Case A** — exec data read exactly as the VM does (raw `pd.read_parquet`): **0 trades, $0.00**
- **Case B** — identical file with `DateTime` promoted to the index: **934 trades, $264,300**

Model probabilities are not the issue: ES long_logloss `prob_Buy` max = 0.674, with 39.5% of bars at or above the 0.5 threshold.

### Regression status: latent since 2026-06-16, not a recent regression

`git blame` places the unnormalized read block at commit `c5111ca1` (2026-06-16), predating all non-CL work; none of the last 5 commits to `vm_e2e_pipeline.py` (ending `6bf3209`, 2026-07-01) touch this read. The trigger was the *data*: the first int-indexed `<SYM>_raw.parquet` (ES, built 2026-07-01) hit a 2.5-week-old latent code path. CL was never affected because its exec parquet happens to have a DatetimeIndex. The very first ES canary (`sweep_es01a_2x1_6h_canary_20260702-0114`) is already all-zero.

---

## 3. Blast radius

### ES top-pair selection: NOT contaminated — selection was INFORMED ✅

The ticket's premise was wrong on one point: `agent/unified_pair_optimizer.py:127-136` parses `batch_summary_optimized_sharpe.md` / `_sortino.md`, **not** the zeroed `batch_summary.md`. Those optimized reports are produced by `agent/batch_post_optimizer.py`, which runs **locally**, loads exec data via the normalizing `load_ohlcv` (`batch_post_optimizer.py:1040`), and *recomputes* baseline ("pre") metrics itself (`agent/strategy_optimizer.py:1194-1196`). The parser scores `robustness = pnl_opt + 6*pnl_holdout` with a pass filter (`pnl_opt>0, pnl_holdout>0, trades>=100`) using **optimized** trades / optimized PnL / holdout PnL columns (`unified_pair_optimizer.py:62-78`) — none of which were zeroed.

Verified: `batch_20260704_0701_ES_01B_SCOUT/top_pairs.json` is exactly the top-2 longs (3x1_6h AP, 2x1_6h LL) × top-2 shorts (2x1_6h LL, 3x1_6h AP) by that score. Example healthy input: ES01B 2x1_6h long_logloss shows 829 pre-trades, $2.45M optimized PnL, $252,581 holdout in the optimized report. **The ES scout A/B pair selections and comparisons are trustworthy.**

### Affected runs: ALL non-CL baselines, since first standup

Scan of all 100+ `reports/sweep_*/pipeline_summary.json`: every non-CL baseline is ALL_ZERO —
- **ES** 01A + 01B, canary + scout (14 summaries)
- **NG** 01A canary ×2 + 01B scout (8)
- **NQ** 01A canary (2)
- **ZC** 01A canary + 01A scout-in-progress (3)
- **GC / ZS / SI** 01A canaries (6)

Batch folders: `batch_20260702_0114`, `batch_20260702_0636_ES_CANARY_01_FAIL`, `batch_20260703_0758`, `batch_20260703_2026`, `batch_20260703_2047`, `batch_20260704_0334`, `_0637`, `_0701_ES_01B_SCOUT`, `_0751`, `_0810`, `_0829`, `_0857_NG_01B_SCOUT`, `_2016_ES_01A_SCOUT`, `_2215`. Every CL run is healthy (three isolated single-model zeros in old CL runs look like genuine no-trade configs, not this pattern).

### Other consumers of the zeroed values

- `batch_summary.md` per-model/ensemble tables — human-facing only.
- `backtest_report.txt` inside every non-CL registry bundle (`vm_e2e_pipeline.py:347`) — zeroed; no automated reader found, but it is misleading bundle provenance.
- VM-side `production_output/backtest_report_*.txt` and `ensemble_backtest_*.txt`.
- **NOT affected:** optimized ensembles, `top_pairs.json`, HourSet A/B scout comparisons (all keyed off post-optimizer output).

---

## 4. Proposed fix options (not implemented — per ticket mode)

Reviewed and **APPROVED (A + B, C as follow-up)** by the Ticket-Impact-Reviewer; blast radius of the fix itself was mapped (see `blueprint.md` and `ticket_audit_log.md`).

**Option A — reuse the normalizing loader (recommended, localized).**
In `gcp/vm_e2e_pipeline.py`, replace both raw exec-data reads (`:271-277` and `:896-902`) with `agent.backtest_engine.load_ohlcv(exec_data_path)` (already handles parquet+CSV, promotes `DateTime` to index, validates required OHLCV columns, raises on failure). ~4 lines; converges VM behavior with the healthy post-optimizer path. For CL's already-correct parquet the promotion is a no-op — no CL regression. *Tradeoff:* doesn't stop the next silent-zero from a *different* alignment bug.

**Option B — fail-loud guard at the true silent-failure point (pairs with A; repo "fail loudly" policy).**
In `BacktestEngine.run` (`agent/backtest_engine.py:~1009-1045`), after building probability lookups, raise `ValueError` if the lookups are **non-empty** yet zero of their timestamps intersect `ohlcv.index` (message naming the dtype mismatch, e.g. "0 of N signal timestamps found in OHLCV index — index misalignment (int64 vs datetime64)"). Would have caught this at the first ES canary on 07-02. *Tradeoff:* `run()` has ~25+ production call sites + 37 test calls — the guard must be scoped exactly (see binding conditions in blueprint) so legitimate no-signal models still report 0 trades honestly instead of crashing. Reviewer verified no legitimate caller breaks (test fixtures build signals with `index=ohlcv.index`; the Optuna fold evaluator wraps `run` in `except Exception: return 0.0`).

**Option C — data-side hygiene (optional follow-up, not a gate).**
Rebuild the seven `<SYM>_raw.parquet` files with a DatetimeIndex, re-upload to GCS, and fix `.agents/workflows/build-symbol-pipeline.md:49` to state the schema contract explicitly. *Tradeoff:* data-only fix without A leaves the latent code trap armed; with A in place, C becomes consistency work plus a one-line doc fix.

**Residual noted by the Reviewer (not a blocker):** `vm_e2e_pipeline.py:756` (`ohlcv = pd.read_parquet(data_path)`) is a third raw read feeding the `:902` ensemble fallback when `exec_data_path` is null — practically dead since the manifest schema now requires `exec_data_path`, and Option B's guard covers it, but implementers should not treat that fallback as fixed by A.

---

## 5. Verification plan

1. **Local, free, definitive:** re-run the Auditor's reproduction against the patched `run_backtest` — call `run_backtest(preds_df, cfg, "long", tmp, exec_data_path="C:\CL_Analyst_Data\data\processed\ES_raw.parquet")` and assert `trade_count > 0` (expected ≈934 trades / ≈$264k for ES01B 2x1_6h long_logloss, slippage 0.01). Repeat with one CL input and assert metrics unchanged vs `sweep_hs14a_2x1_6h_scout_20260704-0310` (no CL regression).
2. **Guard test (Option B):** feed an int-indexed exec frame to `engine.run` with non-empty signals → expect a loud `ValueError`, not 0 trades. Also assert a legitimately signal-free model still returns 0 trades without raising.
3. **Cheap cloud confirmation (when authorized):** one ES 01B canary (n_trials=3) → assert `pipeline_summary.json.backtest_results.*.trade_count > 0` and nonzero `batch_summary.md` tables. No model-training logic is touched, so full scout re-runs are unnecessary.
