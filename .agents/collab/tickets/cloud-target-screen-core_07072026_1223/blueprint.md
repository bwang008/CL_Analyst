# Ticket Resolution Blueprint — cloud-target-screen-core_07072026_1223
**Ticket Directory:** `.agents/collab/tickets/cloud-target-screen-core_07072026_1223/`
**Branch:** `training-update` (do NOT touch `stable-fleet`)
**Epic:** `/cloud-target-batch` Stage-1 target screen. This ticket = the locally-testable
Python CORE (S1 screen fn + S2 report + S3 schema mode). Cloud orchestration (S4) and the
workflow doc (S5) are separate follow-ups.

## Change Summary
Add a **fixed-param, no-Optuna** target-screening path that trains ONE LightGBM per target,
computes out-of-sample edge (holdout ROC-AUC / PR-AUC) + a tradeability proxy, and emits a
ranked `AUC_Model_Report.md`. This is the cheap pre-screen that decides which targets are
worth a full `/run-cloud-batch` Optuna sweep. Motivation (memory `si-01b-edge-and-threshold-floor`):
target choice, not model tuning, drove results — the 4x1_36H target was dead (holdout AUC
~0.50) while 3x1_6H had real edge (0.64), and pos-rate does NOT predict learnability.

## Target Files
- `gcp/vm_e2e_pipeline.py` — new screen functions + a `--mode screen` CLI branch
- `src/config/schemas.py` — `TrainingWorkflowConfig.mode`
- `tests/` — new test module (uses a small synthetic dataset; must run fast)

## Required Changes

### 1. `src/config/schemas.py` — `TrainingWorkflowConfig`
Add `mode: Literal["optimize", "screen"] = "optimize"` (optional, explicit default —
existing manifests keep "optimize", no blast radius). `"screen"` selects the target-screen
path; `"optimize"` is today's full Optuna+backtest E2E.

### 2. `gcp/vm_e2e_pipeline.py` — screen core
- **`SCREEN_LGBM_PARAMS`** module constant: a documented fixed param dict at the middle of the
  Optuna search box, e.g. `{"max_depth":6, "num_leaves":31, "learning_rate":0.02,
  "n_estimators":1500, "min_child_samples":200, "feature_fraction":0.7}`. One fixed config for
  every target so AUCs are comparable across targets.
- **`_screen_one_target(df_train, df_vault, feature_cols, target_col, direction, params, random_seed) -> dict`:**
  - Train with `train_final_model(df_train, feature_cols, target_col, params=copy.deepcopy(params), ...)`.
    ⚠️ `train_final_model` MUTATES `params` (`.pop` of `lookback_window_years`/`n_estimators`) —
    pass a fresh deepcopy per call.
  - Predict on the FULL (non-downsampled) `df_train` and `df_vault`:
    `probs = _sigmoid(model.predict(df[feature_cols]))`.
  - Labels `y = (df[target_col] > 0).astype(int)` on each split (drop NaN targets already done upstream).
  - Metrics (reuse `sklearn.metrics.roc_auc_score` / `average_precision_score`; import
    `roc_auc_score` if not already imported): `auc_train`, `auc_holdout` (ROC on vault),
    `pr_auc_holdout`, `pos_rate_train`, `pos_rate_holdout`, `n_train`, `n_holdout`.
  - Prob-distribution: `prob_spread = q95 − q05` of holdout probs.
  - **Tradeability proxy** (answers "edge AND trades"): reference selective threshold
    `ref_thr = holdout_probs.quantile(0.80)` (top-20% firing); `precision_at_ref =
    mean(y_holdout[probs >= ref_thr])`; `signals_per_yr_at_ref = 0.20 * bars_per_year`
    where `bars_per_year = n_holdout / (holdout_span_days/365.25)`.
  - Guard AUC on degenerate labels (all one class) → return `nan` for that AUC, do not crash.
  - Return a flat dict with all the above + `target`, `direction`.
- **`run_screen(data_path, train_cutoff_date, targets, symbol, output_dir, holdout_cutoff_date=None, random_seed=42) -> list[dict]`:**
  - Reuse `run_pipeline`'s split logic verbatim (2-way default / 3-way when holdout_cutoff set;
    `np.random.seed(random_seed)`; `util.get_feature_columns`; per-target
    `util.get_target_column` + dropna on each split). Refactor the shared split into a helper
    if cleaner, but do NOT change `run_pipeline`'s behavior.
  - `targets`: list of column names; direction = "long" if endswith `_LONG` else "short".
  - Loop targets → `_screen_one_target` → collect rows. Write the report (below). Return rows.
- **`write_auc_report(rows, output_path, meta: dict)`:** markdown to
  `{output_dir}/AUC_Model_Report.md`, one row per (symbol, target, direction), **sorted by
  `auc_holdout` desc**. Columns: target | dir | AUC train | AUC holdout | PR-AUC holdout |
  pos% train | pos% holdout | prob spread | signals/yr | precision@ref. Header meta = symbol,
  dataset, train/holdout split dates, `SCREEN_LGBM_PARAMS`, seed. Add a one-line legend
  (AUC≈0.50 = no edge; ≥~0.55 = real edge).
- **CLI:** extend `main()` so `--mode screen` calls `run_screen(...)` (reads the same
  `--data/--targets/--train-cutoff-date/--holdout-cutoff-date/--symbol/--output-dir/--random-seed`
  args already parsed) instead of `run_pipeline(...)`. Keep default `--mode optimize` behavior
  byte-identical. Do NOT wire run_sweep_batch/vm_sweep_run (that is S4).

## Test Requirements (TDD-tester writes FIRST; red before code)
Build a small **synthetic** parquet fixture (~800 rows, DatetimeIndex, ~6 numeric feature
cols named like real features, plus two targets: `TARGET_TRIPLE_2x1_6H_LONG` constructed to be
learnable from the features, and a pure-noise `TARGET_TRIPLE_2x1_6H_SHORT`). Keep it tiny so
LGBM trains in <~5s.
- `TrainingWorkflowConfig`: `mode` defaults to "optimize"; accepts "screen"; rejects "bogus".
- `_screen_one_target`: returns all documented keys; `auc_holdout` ∈ [0,1]; the learnable
  target's `auc_holdout` is materially > the noise target's; degenerate single-class label →
  `nan` AUC without crashing.
- `run_screen`: writes `AUC_Model_Report.md`; rows sorted by `auc_holdout` desc; report contains
  both targets and the column headers; deterministic across two runs with the same seed
  (identical AUC values).
- `write_auc_report`: given hand-built rows, output is sorted and contains every column.
- Regression: full fast suite `pytest tests/ -m "not slow"` → only the 10 known pre-existing
  ES01B sentinel failures remain (nothing new).

## Out of scope (separate tickets)
- S4 cloud orchestration (`run_sweep_batch.ps1`, `vm_sweep_run.sh`, collect reports, skip
  post-optimizer for screen mode). Cannot be locally validated; needs a human GCP dry-run.
- S5 `.agents/workflows/cloud-target-batch.md` doc.
- Multi-seed AUC mean±std (nice-to-have; single-seed determinism is enough here).
