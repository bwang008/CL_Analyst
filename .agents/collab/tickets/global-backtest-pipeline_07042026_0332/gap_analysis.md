# Gap Analysis — global-backtest-pipeline_07042026_0332 (Auditor-role report)

**Ticket Directory:** `.agents/collab/tickets/global-backtest-pipeline_07042026_0332/`
**Feature:** New `/global-backtest` workflow — input: a strategy config (e.g. `configs/strategies/HS14B_Sharpe_E01_06262026.json`); behavior: backtest against ALL available symbols; output: `reports/global_backtest_report_<manifest_name>.md` with a per-symbol summary table + per-symbol "backtest <SYMBOL>" drill-down sections (monthly breakdown + holdout).

---

## Executive summary

Infrastructure is mature and largely reusable. Key findings:

1. **Backtest engine is reusable** — `BacktestEngine.from_config()` / `.run()` (`agent/backtest_engine.py`) are symbol-agnostic; no "CL" checks in core logic.
2. **Symbol registry complete** — `src/core/instrument_master.py` `INSTRUMENT_REGISTRY` has tick_size, tick_value, cftc_code, volatility_index for CL/ES/NG/NQ/ZC/HG/GC/PA.
3. **Multi-symbol data exists** — processed feature parquets on disk for CL, ES, NG, NQ, ZC (HourSet A/B variants). **Raw execution parquets missing for all but CL** (`CL_raw.parquet` only).
4. **No slash-command pattern in repo** — `.agents/workflows/` are agent plans, not CLI extensions; new workflow doc + Python entry point must be built.
5. **Execution params NOT per-symbol** — `slippage_per_side` default 0.01 and `contract_multiplier` default 1000 are hardcoded CLI defaults (`agent/backtest_engine.py:1868-1872`). Wrong for ES (multiplier 50, tick 0.25) and NG (multiplier 10,000, tick 0.001).
6. **Reporting incomplete** — no per-month drilldown or holdout-section formatting exists; must be built.

## 1. Backtest engine

- Entry: `python agent/backtest_engine.py --config <strategy.json> --predictions <csv> --data <parquet> [--exec-data <raw parquet>]`; `main()` at `agent/backtest_engine.py:1812-1887`.
- Reusable API: `BacktestEngine.from_config(cfg)` (lines 375-414) + `run(signals_df, ohlcv_df, ohlcv_exec_df=None)` (lines 935-1084) → `BacktestResult` (trades, equity curve, total_pnl, win_rate, profit_factor, max_drawdown; `to_dataframe()`/`to_csv()` unified ledger export at lines 194-222).
- Holdout: config `holdout_months`; cutoff = pred_end − DateOffset(months=N); dual backtest opt-period vs holdout (lines 2178-2220); warns if holdout has 0 rows (line ~2190).
- Single-position FSM default; `allow_concurrent`/`max_concurrent` supported; `TieredEnsembleStrategy` shared with live trader.

## 2. Strategy config format

- Loaded via `src/live_execution/config_loader.load_strategy_config` → `StrategyConfig.from_dict()` (`src/live_execution/strategy_config.py:112-150`). **No formal schema** (unlike batch manifests, `src/config/schemas.py:186-238`).
- `execution_symbol` is used by the live trader for contract resolution; the **backtest engine ignores it** — no enforcement that config symbol matches loaded data.
- TP/SL/trailing are ATR multiples (scale-free, transferable); `models.{long,short}` carry `model_path` (pkl) + `predictions_path` (csv) + `threshold`.

## 3. Available symbol data

- `data/processed/`: CL_HourSet_14A/14B, ES_HourSet_01A/01B, NG_HourSet_01A/01B, NQ_HourSet_01A/01B, ZC_HourSet_01A/01B; only `CL_raw.parquet` for execution.
- COT: adapter layer in `scripts/download_macro_data.py:312-435` (`COT_REPORT_BY_SYMBOL`, `DisaggregatedAdapter`/`TffAdapter`) emits identical canonical `COT_*` columns for both report families. Pre-baked into parquets; no download at backtest time.
- Vol index per symbol via registry (CL/NG→OVXCLS, ES/NQ/ZC/HG/PA→VIXCLS proxy, GC→GVZCLS). **Feature columns differ by symbol**: CL parquets have `MACRO_OVX_*`; ES parquets have `MACRO_VIX_*`.

## 4. Per-symbol execution config

- Hardcoded CLI defaults: slippage 0.01, multiplier 1000 (`agent/backtest_engine.py:1868-1872`); used in `_apply_slippage()` (496-508) and dollar PnL (line 561).
- Registry has correct metadata; resolve per symbol: `multiplier = tick_value / tick_size` (CL 1000, ES 50, NG 10,000), `slippage = slippage_ticks × tick_size`.

## 5. Hardcoded CL assumptions

- Only in CLI defaults (`agent/backtest_engine.py:1834`), `agent/generate_batch_report.py:16` (`DATASET` constant), and strategy configs' `execution_symbol` (23 files). Core engine clean. No session/timezone hardcoding found.

## 6. Reporting

- `agent/generate_batch_report.py` builds per-experiment tables from `batch_progress.json`/`pipeline_summary.json`. Naming: `reports/batch_runs/batch_<ID>/batch_summary*.md`.
- **No per-month aggregation code exists anywhere** — new build.

## 7. Workflow precedent

- 36 markdown agent plans in `.agents/workflows/`; recommend Python script entry (`scripts/global_backtest.py`) + a `.agents/workflows/global-backtest.md` doc mirroring existing conventions.

## 8. ES canary precedent

- ES stood up by mirroring configs (symbol/dataset_version/execution_data_path only); data build fully config-driven (`process_from_config`, `src/data_processor.py:3152`); COT/TFF wired. Missing: `ES_raw.parquet`; canary reuses CL-derived strategy config.

---

## Proposed build plan (Auditor)

**(B) Modify:**
- `agent/backtest_engine.py` `from_config()` (~30 LOC): resolve slippage + multiplier from `INSTRUMENT_REGISTRY` when `execution_symbol` present; fall back to CLI args for backward compat. [HIGH]
- Build missing raw execution parquets: ES/NG/NQ/ZC `_raw.parquet` (hourly, `[DateTime, Open, High, Low, Close, Volume]`) per build-symbol-pipeline Phase 1 step 6. [HIGH]

**(C) Build new:**
- `scripts/global_backtest.py` (~200 LOC) — CLI entry: `--config <strategy.json> --symbols CL,ES,NG,NQ,ZC --output reports/global_backtest_report_<name>.md`.
- `scripts/backtest_orchestrator.py` (~250 LOC) — `load_symbol_data(symbol, dataset_version)`, `run_global_backtest(config, symbols)`.
- `scripts/generate_global_backtest_report.py` (~300 LOC) — summary table + per-symbol "### backtest <SYMBOL>" sections (monthly table + holdout section).
- `scripts/report_helpers.py` (~150 LOC) — `aggregate_trades_by_month`, markdown formatting.
- Tests: `tests/test_backtest_engine_multi_symbol.py` (~80), `tests/test_global_backtest_report.py` (~200), `tests/test_global_backtest.py` (~150).

**Order:** Phase 0 verify/build raw parquets + fixtures → Phase 1 execution-param resolution (+tests) → Phase 2 report generator (+tests) → Phase 3 orchestrator (+tests) → Phase 4 CLI/workflow doc → Phase 5 edge cases.
**Total: ~1,360 new LOC, ~30 modified LOC.**

## Risks (Auditor)

1. pandas 1.5.3 COT date bug — already fixed; COT pre-baked, non-blocking.
2. Missing raw execution parquets ES/NG/NQ/ZC — blocker for honest PnL; never silently fall back to adjusted series (repo hard rule: raw for execution, ratio for training).
3. `execution_symbol` mismatch config-vs-data — engine ignores it today; warn loudly.
4. Holdout collapse (0-row OOS) — reuse existing validation; skip + flag.
5. Predictions column auto-resolution (`_resolve_prob_column`, lines 1020-1044) — validate upfront.
6. Same ATR params across symbols — by design for this experiment; document, don't tune.
7. Slippage math per symbol — unit test; print per-symbol slippage in report header.

---

## MANAGER ADDENDUM (Ticket-Manager, must be weighed by Impact-Reviewer)

**A. The predictions gap is understated.** The strategy config's `predictions_path` CSVs are outputs of CL-trained models scoring CL feature data. **No predictions exist for ES/NG/NQ/ZC.** A global backtest that "loads predictions per symbol" cannot work until an **inference step** is built: load `models.{long,short}.model_path` (LightGBM pkl) and score each symbol's feature parquet. This is a new component the Auditor's plan sizes at only 100-200 LOC ("predictions loader") — it is likely the largest and riskiest piece.

**B. Feature-schema mismatch will break naive inference.** The CL model was trained on CL columns including `MACRO_OVX_*`; the ES parquet has `MACRO_VIX_*` instead (and possibly other column diffs). LightGBM/sklearn validates feature names — scoring will crash or, worse, silently misalign. Options to evaluate: (1) a feature-slot mapping layer (rename per-symbol vol-index columns to a canonical slot name at inference time), (2) restrict v1 to symbols whose columns are a superset of the model's feature list, (3) fail loudly per symbol and report "N/A" rows. The repo's no-silent-nulls rule applies: any missing feature must crash/flag, never NaN-fill silently.

**C. Blast-radius note for the reviewer:** `agent/backtest_engine.py` is shared with the live trader and the ledger-parity gate (parity PASS baseline 2026-07-04). Any modification to `from_config()` execution-param resolution MUST preserve byte-identical behavior for existing CL configs/CLI invocations (backward-compat default path), guarded by a regression test, or parity is at risk.
