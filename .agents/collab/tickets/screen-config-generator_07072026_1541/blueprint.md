# Blueprint — screen-config-generator_07072026_1541
**Ticket Directory:** `.agents/collab/tickets/screen-config-generator_07072026_1541/`
**Branch:** `training-update` (do NOT touch `stable-fleet`)

## Change Summary
`/cloud-target-batch` (Stage-1 screen) currently tells the user to hand-copy + edit the
MasterConfig template. Add a generator that builds a **validated** screen config from EITHER a
v2 batch manifest OR a dataset, so the workflow is turnkey for any symbol. No output-directory
change (the user opted to keep the stable `--output-dir reports/screens/<name>` path;
`run_screen` already writes wherever `--output-dir` points).

## Target Files
- `scripts/build_screen_config.py` — NEW standalone generator
- `.agents/workflows/cloud-target-batch.md` — replace "copy the template and edit" with the generator
- `tests/` — new test module

## Required Changes

### `scripts/build_screen_config.py`
A CLI that emits a MasterConfig-shaped screen config (`training_workflow.mode = "screen"`).
- **Inputs (exactly one source, mutually exclusive, required):**
  - `--from-manifest <path>`: a v2 `BatchSweepConfig` JSON. Read `baseline.symbol`,
    `baseline.data_workflow.dataset_version`, `baseline.training_workflow.train_cutoff_date`,
    and **the deduped union of every `experiments[].overrides.training_workflow.target_columns`**
    (screen exactly what that batch intended). Preserve first-seen order.
  - `--from-dataset <dataset_version>`: requires `--symbol`. Resolve the parquet via
    `src.data_paths.get_data_root()/"processed"/f"{symbol}_{dataset_version}.parquet"` (or
    `f"{dataset_version}.parquet"` if it already starts with the symbol — mirror
    `vm_e2e_pipeline.main()`'s resolution). Read columns and collect **all**
    `TARGET_TRIPLE_*` ending in `_LONG`/`_SHORT` (skip `_MULTI`).
- **Common args:** `--symbol` (required for `--from-dataset`; for `--from-manifest` taken from
  the manifest, error if `--symbol` conflicts), `--train-cutoff-date` (optional override;
  required if `--from-dataset` and not otherwise available — no silent default),
  `--holdout-cutoff-date` (optional, default null → 2-way), `--random-seed` (default 42),
  `--out` (default `configs/sweeps/screen_<symbol_lower>_<dataset_lower>.json`).
- **Build** a MasterConfig dict identical in shape to `configs/sweeps/screen_si_hourset01b.json`:
  `symbol`, `data_workflow{dataset_version, resolution:"1h", targets{raw_horizon:120,
  atr_period:14, definitions:[minimal grid stub]}}`, `training_workflow{mode:"screen",
  train_cutoff_date, holdout_cutoff_date, random_seed, gcs_base_dir (derive a sensible path),
  target_columns, optuna{post_optimizer_holdout_months:6}}`. No `execution_workflow`.
- **VALIDATE before writing:** construct `MasterConfig(**cfg)`; if it raises, print the error
  and exit non-zero WITHOUT writing (fail loud, no half-written config). On success write the
  JSON (indent=2), print the out-path + target count + source.
- Errors: neither/both sources → argparse error; `--from-dataset` without `--symbol` → error;
  dataset parquet missing → clear FileNotFoundError; zero targets found → error (don't write).

### `.agents/workflows/cloud-target-batch.md`
Replace step 1 ("copy the template and edit") with two generate options:
```bash
# from a dataset (screen the full target grid it contains)
conda run -n trader python scripts/build_screen_config.py \
  --from-dataset HourSet_14B --symbol CL --train-cutoff-date 2025-06-01
# or from a v2 batch manifest (screen exactly that batch's targets)
conda run -n trader python scripts/build_screen_config.py \
  --from-manifest configs/batch_manifest_v2_hourset14b_scout.json
```
Then the existing dry-run / run steps against the generated `configs/sweeps/screen_*.json`.
Keep the `--output-dir reports/screens/<name>` convention unchanged.

## Test Requirements (TDD-tester writes FIRST; red before code)
- `--from-dataset`: build a tiny synthetic parquet with a couple `TARGET_TRIPLE_*_LONG/SHORT`
  columns (+ a `_MULTI` and a non-target col to exclude); assert the generated config lists
  exactly the LONG/SHORT triple targets, `mode == "screen"`, and `MasterConfig(**cfg)` validates.
- `--from-manifest`: given a v2 manifest dict with two experiments carrying overlapping
  `target_columns`, assert the union is deduped + order-preserved, and symbol/dataset/cutoff are
  read from `baseline`.
- Validation gate: a manifest/dataset that would yield an invalid config (e.g. empty targets)
  exits non-zero and writes NOTHING.
- Mutually-exclusive/missing-arg errors behave as specified.
- Regression: full fast suite `pytest tests/ -m "not slow"` → only the 10 known pre-existing
  ES01B sentinels remain.

## Out of scope
- No `run_screen`/output-dir changes. No `signals/yr` metric change (separate follow-up — needs
  a fixed-threshold vs lift design decision). No cloud orchestration (S4).
