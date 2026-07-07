# TDD Result — cloud-target-screen-core_07072026_1223

**Branch:** `training-update` (uncommitted, left for review)
**Outcome:** GREEN. New target-screen CORE implemented via strict Red→Green TDD.

## Final test outcome (fast suite: `pytest tests/ -m "not slow"`)
- **1707 passed, 10 failed** (0:04:36).
- The 10 failures are the KNOWN pre-existing ES01B sentinels (unchanged, untouched):
  `test_config_generator_symbols.py` (4), `test_hourly_only_equity_session.py` (3),
  `test_instrument_context.py` (1), `test_shallow_5m_bootstrap.py` (2).
- Baseline before this ticket was 1692 passed / same 10 failed. Net delta: +15 new
  passing tests (this ticket's module), zero regressions.
- New module `tests/test_target_screen_core.py`: **15/15 pass** in ~5s.

## Files changed
- `src/config/schemas.py`
  - `TrainingWorkflowConfig.mode: Literal["optimize", "screen"] = "optimize"`
    (optional, explicit default → existing manifests keep "optimize", no blast radius).
- `gcp/vm_e2e_pipeline.py`
  - New imports: `copy`, `sklearn.metrics.roc_auc_score`.
  - `SCREEN_LGBM_PARAMS` — documented fixed mid-box param dict, one config for all targets.
  - `_safe_auc(y_true, scores)` — ROC-AUC guarded to return `nan` on single-class splits.
  - `_screen_one_target(df_train, df_vault, feature_cols, target_col, direction, params, random_seed)`
    — trains ONE LGBM (params passed as `copy.deepcopy` per call since `train_final_model`
    `.pop`s keys), evaluates AUC/PR-AUC/pos-rate on the FULL non-downsampled train+vault splits,
    computes prob spread (q95−q05) and the tradeability proxy (ref_thr = holdout q80,
    precision@ref, signals/yr from the holdout calendar span). Model pickle is written to a
    throwaway `tempfile.TemporaryDirectory` and discarded (no repo pollution). Returns a flat
    dict with all documented keys.
  - `_split_train_vault(df, train_cutoff_date, holdout_cutoff_date)` — shared split helper
    mirroring `run_pipeline`'s 2-way/3-way semantics WITHOUT modifying `run_pipeline` itself.
  - `write_auc_report(rows, output_path, meta)` — markdown report sorted by `auc_holdout` desc,
    all 10 columns + meta header (symbol/dataset/cutoffs/params/seed) + edge legend.
  - `run_screen(data_path, train_cutoff_date, targets, symbol, output_dir, holdout_cutoff_date=None, random_seed=42)`
    — seeds numpy, splits, loops targets (direction inferred from `_LONG`/`_SHORT` suffix,
    per-target dropna), writes `AUC_Model_Report.md`, returns rows sorted by holdout AUC desc.
  - `main()` — added `--mode {optimize,screen}` CLI flag (default None → falls back to
    manifest `training_workflow.mode`). Screen branch runs `run_screen` and returns early;
    `execution_workflow` is only required on the optimize path. **`--mode optimize` (default)
    behavior is byte-identical** — `run_pipeline`'s body and signature are unchanged.
- `tests/test_target_screen_core.py` (new) — synthetic ~800-row hourly parquet fixture with one
  learnable target (`TARGET_TRIPLE_2x1_6H_LONG`) and one pure-noise target
  (`TARGET_TRIPLE_2x1_6H_SHORT`); LGBM trains in ~1s/target.

## Test coverage
- Schema: `mode` defaults to "optimize"; accepts "screen"; rejects "bogus".
- `_screen_one_target`: returns all documented keys; `auc_holdout ∈ [0,1]`; learnable target's
  holdout AUC materially > noise (and > 0.55); degenerate single-class label → `nan` (no crash);
  shared `SCREEN_LGBM_PARAMS` never mutated across calls.
- `run_screen`: writes report with both targets + all column headers; rows sorted by holdout AUC
  desc; deterministic across two same-seed runs (identical AUCs); direction inferred from suffix.
- `write_auc_report`: hand-built rows → sorted desc, every column + meta header present.

## Deviations from blueprint
- One test-fixture fix (not an implementation weakening): the schema's REQUIRED nested field
  `optuna.post_optimizer_holdout_months` must be supplied for `TrainingWorkflowConfig` to
  instantiate at all; the mode tests now pass `optuna={"post_optimizer_holdout_months": 3}`.
  This is orthogonal to the `mode` assertions and does not soften them.
- `_screen_one_target` writes its (discarded) model pickle to a `tempfile.TemporaryDirectory`
  rather than a persisted path — the screen needs only metrics, and this avoids polluting the
  repo/output dir with per-target pickles. No behavioral impact on the returned metrics.

## Out of scope (untouched, per blueprint / S4)
- `gcp/run_sweep_batch.ps1`, `gcp/vm_sweep_run.sh`, `gcp/vm_post_optimize.sh`, any `.ps1`.
- `.agents/workflows/cloud-target-batch.md` (S5 doc).

**Tree is uncommitted.** Left for review on `training-update`.
