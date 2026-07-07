# TDD Result — screen-config-generator_07072026_1541

**Branch:** `training-update` (uncommitted — left for human review)
**Outcome:** GREEN. New generator implemented + validated via TDD (Red -> Green).

## Files changed
- `scripts/build_screen_config.py` — NEW. Standalone CLI generator that emits a
  VALIDATED MasterConfig-shaped screen config (`training_workflow.mode="screen"`,
  no `execution_workflow`) from EITHER a v2 batch manifest OR a dataset.
  - `--from-manifest <path>`: reads `baseline.symbol` /
    `baseline.data_workflow.dataset_version` /
    `baseline.training_workflow.train_cutoff_date`, and the **deduped, first-seen
    order-preserved union** of every
    `experiments[].overrides.training_workflow.target_columns`.
  - `--from-dataset <ver>` (requires `--symbol`): resolves the parquet via
    `get_data_root()/processed/<symbol>_<ver>.parquet` (or `<ver>.parquet` if the
    version already starts with the symbol — mirrors `vm_e2e_pipeline.main()`), and
    collects all `TARGET_TRIPLE_*` ending in `_LONG`/`_SHORT` (`_MULTI` excluded).
  - Common args: `--symbol`, `--train-cutoff-date` (override; required for
    `--from-dataset`, no silent default), `--holdout-cutoff-date` (default null =>
    2-way), `--random-seed` (default 42), `--out`
    (default `configs/sweeps/screen_<symbol>_<dataset>.json`).
  - **Validation gate:** builds the dict, constructs `MasterConfig(**cfg)`, and on
    ANY failure (invalid config OR zero targets) prints the error and exits
    non-zero WITHOUT writing (fail loud, no half-written config). Sources are a
    required, mutually-exclusive argparse group.
- `.agents/workflows/cloud-target-batch.md` — replaced step 1 ("copy the template
  and edit") with the two generator invocations (dataset / manifest), the default
  out-path note, the fail-loud validation note, and kept the template link +
  hand-edit fallback. The `--output-dir reports/screens/<name>` convention is
  unchanged.
- `tests/test_build_screen_config.py` — NEW. 14 tests (see below).

## Scope adherence
- No `run_screen` / `--output-dir` changes. No `signals/yr` metric change. No cloud
  orchestration. No change to any existing config or manifest. Generator writes only
  the requested screen config after successful validation.

## Test coverage (tests/test_build_screen_config.py — 14 tests)
- `--from-dataset`: LONG/SHORT triples collected, `_MULTI`/non-target excluded,
  `mode==screen`, `MasterConfig(**cfg)` validates; missing parquet -> non-zero + no
  write; missing `--symbol` -> error; zero targets -> non-zero + no write;
  symbol-prefixed version resolution.
- `--from-manifest`: union deduped + first-seen order preserved; symbol / dataset /
  cutoff read from `baseline`; `--train-cutoff-date` override; `--symbol` conflict ->
  error + no write; empty targets -> non-zero + no write; smoke against the real
  `batch_manifest_v2_si_hourset01b_scout.json` (8 unique targets).
- Source args: neither -> error; both -> error (mutually exclusive).
- `build_config()` direct-import: valid MasterConfig shape (no `execution_workflow`);
  empty targets raises.

## Test outcome (full fast suite)
`conda run -n trader python -m pytest tests/ -m "not slow"`
=> **10 failed, 1721 passed** (213s).

The 10 failures are the KNOWN pre-existing ES01B sentinels ONLY, unrelated to this
ticket (confirmed by module):
- `test_config_generator_symbols.py::TestES01BPatchedConfig` (4)
- `test_hourly_only_equity_session.py::TestES01BFlagPatch` (3)
- `test_instrument_context.py::TestShippedConfigs::test_es01b_shipped_config_resolves_as_es` (1)
- `test_shallow_5m_bootstrap.py::TestLiveTraderShallowWiring` (2)

All 14 new tests pass; no previously-passing test was broken.
