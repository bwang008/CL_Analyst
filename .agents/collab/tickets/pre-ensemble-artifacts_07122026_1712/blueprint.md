# Ticket Resolution Blueprint — pre-ensemble-artifacts_07122026_1712
**Ticket Directory:** `.agents/collab/tickets/pre-ensemble-artifacts_07122026_1712/`

## Bug Summary
Feature (approved design, Auditor + Impact-Reviewer): batch run folders only contain the pass-2
("optimized") ensemble artifacts; the pass-1 baseline-graft ("pre") configuration of each Top-4
ensemble is never rendered as configs or backtested reports, even though a manual forensic
counterfactual (`reports/batch_runs/batch_20260712_130740_NG_SCOUT/counterfactuals/`,
`model_detective_report.md`) proved the pre geometry flipped that batch's holdout from −$81.4k to
+$61.3k. Productionize that replay as a reusable, strictly additive artifact generator.

First target batch (acceptance run): `reports/batch_runs/batch_20260712_130740_NG_SCOUT`.

## Target Files
- `scripts/generate_pre_ensemble_artifacts.py` (NEW — the only production file)
- `tests/test_pre_ensemble_artifacts.py` (NEW)
- Zero modifications to any existing file. Explicitly forbidden to edit:
  `agent/generate_ensemble_artifacts.py`, `agent/batch_post_optimizer.py`,
  `agent/strategy_optimizer.py`, `gcp/vm_post_optimize.sh` (VM chain + frozen source pins in
  `tests/test_config_generator_symbols.py`).

## Required Changes

### Script `scripts/generate_pre_ensemble_artifacts.py`
CLI: `--batch-dir` (required); optional `--objectives` (default `sharpe`), `--data`, `--exec-data`,
`--slippage-per-side`. Backtests via `sys.executable` subprocess running
`agent/backtest_engine.py` (caller uses the trader conda env).

Data flow (all code-verified by the Auditor):
1. **Param source:** `<batch-dir>/optimization_results_ensembles_<obj>.json` →
   `<long_pred_id>|<short_pred_id>` → `optuna_info.baseline_side_params` (per-side keys:
   `entry_threshold, tp_atr_mult, sl_atr_mult, trailing_atr_mult, trailing_sl_atr_offset,
   cooldown_bars, max_hold_bars, consecutive_signal_threshold, atr_period`). This is the same field
   the pass-2 summary "Baseline" column renders from. Missing/None → ValueError naming the pair
   (legacy batches crash loudly; NO fallback to any global base config).
2. **E-slot order:** import and use `_canonical_pair_order` (and `parse_experiment_key`) from
   `agent.generate_ensemble_artifacts` — never dict insertion order. Then
   `assert_order_matches_existing`: parse the `## Ensemble N: <long_sweep> / <short_sweep>`
   headings of the existing `<obj>_ensemble_backtests.md` and HARD-FAIL on any mismatch.
3. **Config builder** `build_pre_config(shipped_cfg, baseline_side_params, pre_nickname)` — pure
   function: deep-copy the batch's shipped `configs/<TAG>_E0N_<date>.json`; per side set
   `entry_threshold` into BOTH `models.<side>.threshold` and `tiers[0].min_prob`; apply all other
   geometry keys into `cfg[<side>]`; rebuild `tiered_exits=[{qty_pct:1.0, tp_atr_mult}]` and
   `tiers=[{min_prob, lots:1, tp_atr_mult, sl_atr_mult, trailing_atr_mult, max_hold_bars}]`;
   `conflict_resolution: "hold"`; nickname/description marked as PRE (pass-1 baseline graft).
   Fail-fast ValueError if any side-level geometry key of the shipped config is not overwritten by
   the baseline dict (no silent pass-2 leftovers). Do not mutate the input.
4. **Economics/no-silent-defaults:** symbol from manifest `baseline.symbol` (ValueError if absent);
   `contract_multiplier = dollars_per_point(symbol)` from `src.core.instrument_master`; slippage:
   CLI > manifest `baseline.execution_workflow.slippage_per_side` > ValueError.
5. **Data path:** CLI `--data` > manifest `defaults.local_data_path` > the `**Data**:` header line
   of the existing `<obj>_ensemble_backtests.md` (print provenance) > ValueError; existence-check
   before any run.
6. **Exec source:** CLI `--exec-data` > `resolve_exec_data_path` in try/except; on miss fall back to
   embedded `EXEC_*` columns (no `--exec-data` flag passed to the engine) and stamp the exec-source
   string into BOTH report headers (e.g. "EMBEDDED EXEC_* columns (raw exec parquet unavailable
   locally: <attempted path>)").
7. **Outputs (additive only):**
   - Configs → `<batch-dir>/configs/pre/<TAG>_E0N_pre_<date>.json` (e.g.
     `NG_Sharpe_E01_pre_07122026.json`).
   - `<batch-dir>/sharpe_pre_backtests.md` — mirrors `sharpe_ensemble_backtests.md`
     section-for-section: per-ensemble heading (same `## Ensemble N: <long>/<short>` identity),
     Config/Predictions links, Verification Command (with `--slippage-per-side` and
     `--contract-multiplier`; include `--exec-data` only when a real exec file was used), fenced
     full engine output (Historical + HOLDOUT REPORT + A/B COMPARISON).
   - `<batch-dir>/batch_summary_pre_sharpe.md` — Top-4 table (# | experiment labels | long/short
     model | per-side Thr/TP/SL/Trail(x/y)/Cooldown/MaxHold/Consec/ATR | PnL full | PnL opt-window |
     PnL holdout | trades full/opt/ho) + per-ensemble detail embedding the holdout MONTHLY
     BREAKDOWN block and a link to the backtests file.
   - Both headers state: params provenance (`optuna_info.baseline_side_params`), exec source, order
     contract ("E-slots 1:1 with <obj>_ensemble_backtests.md"), and generation command.
8. Predictions: reuse the batch's existing per-ensemble merged
   `predictions/<TAG>_E0N_predictions.csv` (paths already inside the shipped configs — keep them).

### Reviewer conditions (BINDING)
1. `configs/pre/` MUST resolve under `<batch-dir>/configs/pre/` — never repo-root `configs/`.
2. Tests must exercise the REAL `_canonical_pair_order` import path (no re-implementation), so a
   future rename/behavior change fails this suite loudly.
3. On stdout-parse failure of an engine run, summary cells must render a loud `UNPARSED` marker —
   never zero/blank PnL; the raw dump must still land in `sharpe_pre_backtests.md`.
4. Namespace guard: the script must refuse (raise) to write any output file path lacking the
   `_pre_` marker / `pre` outputs set (`batch_summary_pre_sharpe.md`, `sharpe_pre_backtests.md`,
   `configs/pre/*_pre_*.json`), making the additive guarantee mechanical.

### Tests `tests/test_pre_ensemble_artifacts.py`
(a) `build_pre_config` units: tiers[0].min_prob == models.threshold == entry_threshold; all geometry
keys applied per side; input not mutated; `_pre_` nickname; conflict_resolution "hold".
(b) Fail-fast: missing baseline_side_params / manifest slippage / shipped config → loud errors.
(c) E-order: fixture batch dir with shuffled JSON insertion order still emits top_pairs order (via
the real `_canonical_pair_order`); heading-parity assertion fires on mismatch.
(d) Additive guarantee: pre-existing fixture artifacts byte-identical after a run (backtest
subprocess monkeypatched); namespace guard raises on a non-`_pre_` path.
(e) `parse_engine_output` on canned engine text (cf_E01.txt-style) + `UNPARSED` fallback.

## Acceptance Gates
1. Full pytest suite green (existing tests untouched and passing; new suite passing).
2. Live run:
   `conda run -n trader python scripts/generate_pre_ensemble_artifacts.py --batch-dir reports/batch_runs/batch_20260712_130740_NG_SCOUT`
   produces the 4 pre configs + both reports; holdout PnLs ≈ E01 +$25,528 / E02 +$27,935 /
   E03 +$5,582 / E04 +$2,229 (embedded-EXEC replay, matches `counterfactuals/cf_E0*.txt`).
3. Ensemble order in both new files matches `sharpe_ensemble_backtests.md` E01..E04 1:1.
4. No existing file in the batch dir or repo modified (git status shows only the new
   script/tests; batch dir gains only the 3 pre artifact sets).
