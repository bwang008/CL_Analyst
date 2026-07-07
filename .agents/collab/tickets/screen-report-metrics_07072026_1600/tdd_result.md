# TDD Result — screen-report-metrics_07072026_1600

**Branch:** `training-update` (uncommitted — left for reviewer)
**Env:** `trader` conda
**Outcome:** GREEN. Full fast suite `1744 passed, 10 failed`; the 10 are exactly the
known pre-existing ES01B sentinels (`test_config_generator_symbols`,
`test_hourly_only_equity_session`, `test_instrument_context`,
`test_shallow_5m_bootstrap`) — untouched by this ticket.

## Files changed
- `gcp/vm_e2e_pipeline.py`
  - Added `import re`.
  - New `_TRIPLE_RR_RE` + `_reward_risk_from_name()` helper: regex-parses
    `TARGET_TRIPLE_<TP>x<SL>_<H>H_<DIR>` -> `TP/SL` (5x1->5.0, 6x2->3.0, 8x2->4.0;
    nan on no-match or SL==0).
  - `_screen_one_target`: DROPPED `signals_per_yr_at_ref`; ADDED
    `brier_holdout` = mean((prob-y)^2) (nan if holdout empty),
    `n_pos_holdout` = int(y_holdout.sum()),
    `reward_risk` (from target_col name; overridden in run_screen),
    `pr_lift` = pr_auc_holdout / pos_rate_holdout (nan-guarded),
    `ev_floor_at_ref` = prec*RR - (1-prec)*1 (nan-guarded).
    Removed the now-dead signals/yr span computation.
  - `run_screen`: after the `row["target"] = target_name` override, recompute
    `reward_risk` and `ev_floor_at_ref` from the DISPLAYED name (so RR/EV match
    what the report shows); changed the return-sort key from `auc_holdout` to
    `pr_auc_holdout` (sort site ~2).
  - New `_screen_flag()` helper (RARE if n_pos<75 -> KEEP if ROC-AUC>=0.55 ->
    ~tune if >=0.53 -> drop; ASCII-only).
  - `write_auc_report`: sort by `pr_auc_holdout` desc (sort site ~1); new columns
    `target|dir|PR-AUC|ROC-AUC|PR-lift|Brier|pos%|n_pos|prec@ref|RR|EV_flr|flag`;
    per-column width padding (left-align target/dir/flag, right-align numerics),
    valid-Markdown header + alignment separator; nan -> `-`; rewritten Legend
    documenting the sort rationale, PR-lift, the Brier focal-trained-screen-model
    caveat, n_pos/RARE, and the EV_flr pessimistic-floor caveat.
- `tests/test_target_screen_core.py`
  - `DOC_KEYS`: dropped `signals_per_yr_at_ref`, added the 5 new keys.
  - Added `_screen_one_target` tests: signals/yr dropped, brier in [0,1] + n_pos
    match, pr_lift formula, ev_floor formula, reward_risk parsed (2x1->2.0).
  - `run_screen` tests: updated headers assertion (new columns, signals/yr absent),
    renamed sort test to PR-AUC desc, added displayed-name RR test.
  - `write_auc_report` tests: rebuilt `_rows()` with new keys + a RARE/KEEP/drop
    scenario; added flag-logic, ~tune-band, column-alignment (equal cell counts +
    per-column common width), legend-caveat, and nan->`-` tests.

## Notes / deviations
- None. `--mode optimize` / `run_pipeline` untouched. No signature or output-dir
  change. CL report regeneration is the reviewer's manual follow-up (out of scope).
- Pre-existing unused-sklearn-import lint hints in the file are not from this ticket.
