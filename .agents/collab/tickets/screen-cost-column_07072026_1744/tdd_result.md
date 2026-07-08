# TDD Result — screen-cost-column_07072026_1744

**Branch:** `training-update` (tree left uncommitted for review)
**Env:** conda `trader`
**Outcome:** GREEN — cost-awareness column + flag implemented per blueprint; all new tests pass; no new regressions.

## Files changed
- `gcp/vm_e2e_pipeline.py`
  - New module constants `COMMISSION_RT_USD = 4.0`, `COST_FRAC_MAX = 0.06`.
  - New helper `_tp_mult_from_name(name)` — parses the `<TP>` numerator of `<TP>x<SL>`
    from a `TARGET_TRIPLE_...` name; `nan` on no-match/None.
  - `_screen_one_target`: adds `atr_median_holdout` = median of holdout `EXEC_ATR_14`
    (fallback `ATR_14`; `nan` if neither column present or vault empty).
  - `run_screen`: imports `dollars_per_point` / `default_slippage_points` from
    `src.core.instrument_master`, wrapped so an unknown symbol degrades to `nan`
    (never crashes). Per row computes `gross_tp_usd = tp_mult * atr_median * dpp`,
    `rt_cost_usd = 2*slip*dpp + COMMISSION_RT_USD`, `cost_frac = rt_cost/gross`
    (nan-guarded div-by-zero / nan inputs); tp_mult from the DISPLAYED target name.
  - `_screen_flag`: adds a cost gate after RARE — when ROC>=0.53 and a FINITE
    `cost_frac > COST_FRAC_MAX` -> `cost?`; nan `cost_frac` does NOT override the
    ROC verdict (falls through to KEEP/~tune). RARE still wins over the gate.
  - `write_auc_report`: `$win` (integer $, nan->`-`) and `cost%` (cost_frac*100,
    1dp, nan->`-`) columns inserted after `EV_flr`, before `flag`; padded
    alignment preserved. Meta header now shows `$/pt`, `slippage/side`,
    `commission est ($RT)`. Legend documents `$win`, `cost%`, `cost?` and states
    the cost model is approximate (Stage-2 backtest authoritative).
  - `--mode optimize` / `run_pipeline` untouched.
- `tests/test_target_screen_core.py`
  - Fixture: adds a strictly-positive `EXEC_ATR_14` column.
  - New tests: `atr_median_holdout` finite/positive + ATR_14 fallback + nan-when-absent;
    `_tp_mult_from_name` (2x1/6x2/8x2/junk); `run_screen` cost math matches fixture
    economics + unknown-symbol->nan; `write_auc_report` `$win`/`cost%` headers +
    column order + cost? / KEEP / nan-no-override / RARE-wins + nan dash + meta
    economics + legend.

## Test results (fast suite: `pytest tests/ -m "not slow"`)
- Target-screen core file: **42 passed** (was ~26; +16 new).
- Full fast suite: **1760 passed, 13 failed**.
  - The 13 reds are ALL pre-existing and outside this ticket's scope:
    - 10 known ES01B sentinels (test_config_generator_symbols x4,
      test_hourly_only_equity_session x3, test_instrument_context x1,
      test_shallow_5m_bootstrap x2).
    - 3 HourSet15B (test_data_processor::TestHourSet15B) belonging to the separate
      uncommitted `add-hourset-15b_07072026_1724` ticket (its 104-line test block
      was already in the tree; implementation not mine, not complete).
  - Zero new failures introduced by this ticket. RED was confirmed first
    (ImportError on the missing constants/helper), then driven to GREEN without
    weakening any test.

## Deviations
None. Implemented exactly the blueprint scope.

## Tree state
Uncommitted. Only `gcp/vm_e2e_pipeline.py` and `tests/test_target_screen_core.py`
were modified by this ticket (other dirty files — backtest_engine.py, a sweep
config, strategy_config.py, test_data_processor.py — are pre-existing from the
parallel add-hourset-15b ticket and were not touched here).
