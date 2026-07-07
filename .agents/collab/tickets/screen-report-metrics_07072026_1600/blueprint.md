# Blueprint — screen-report-metrics_07072026_1600
**Ticket Directory:** `.agents/collab/tickets/screen-report-metrics_07072026_1600/`
**Branch:** `training-update` (do NOT touch `stable-fleet`)

## Change Summary
Upgrade the target-screen report ([reports/screens/…/AUC_Model_Report.md]) per reviewer
feedback: add calibration (Brier), minority-class support (positive count + a rarity warning),
and reward:risk / a pessimistic EV floor; sort **best→worst by PR-AUC (holdout)** (safer than
ROC-AUC for base-rates <20%); make the table **column-aligned** (padded cells) so the raw `.md`
is readable; and a per-row carry/drop **flag**. Drop the non-informative constant `signals/yr`.

## Target Files
- `gcp/vm_e2e_pipeline.py` — `_screen_one_target` (new metrics) + `write_auc_report` (columns,
  sort, alignment, flag, legend) + the per-target print line + the two sort sites
- `tests/test_target_screen_core.py` — UPDATE existing expectations (columns/sort changed) + add
  new-metric tests
- (after commit, I re-run the CL screen to regenerate the report — not part of the code ticket)

## Required Changes

### `_screen_one_target` — add to the returned dict (keep all existing keys except drop `signals_per_yr_at_ref`):
- `brier_holdout` = `mean((prob_holdout - y_holdout)**2)` (float; nan if holdout empty).
- `n_pos_holdout` = `int(y_holdout.sum())` (minority-class support count).
- `reward_risk` = parse from the target name `TARGET_TRIPLE_<TP>x<SL>_<H>H_<DIR>`:
  `RR = TP / SL` (e.g. `5x1`→5.0, `6x2`→3.0, `8x2`→4.0). Use a regex; if it doesn't match,
  `nan`. NOTE: the name is passed in as `target_col`, but `run_screen` overrides `row["target"]`
  with the caller name afterward — compute RR from the caller-facing target name, so do the
  regex on the name that ends up in `row["target"]` (compute in `run_screen` after the override,
  or pass the caller name in). Ensure RR reflects the displayed target.
- `pr_lift` = `pr_auc_holdout / pos_rate_holdout` (nan-guard div-by-zero; >1 = better than a
  random classifier at that base rate).
- `ev_floor_at_ref` = `precision_at_ref * reward_risk - (1 - precision_at_ref) * 1.0`
  (pessimistic: treats every non-win as a full −1R stop; ignores timeouts/partial exits).

### `write_auc_report` — new table
- **Sort rows by `pr_auc_holdout` descending** (nan last). Update BOTH sort sites currently
  keyed on `auc_holdout` (lines ~619, ~761) to `pr_auc_holdout`.
- **Columns, in order:** `target | dir | PR-AUC | ROC-AUC | PR-lift | Brier | pos% | n_pos |
  prec@ref | RR | EV_flr | flag`.
  - Numbers: AUC/PR-AUC/Brier/prec@ref to 3 dp; PR-lift/RR/EV_flr to 2 dp; pos% as `%.1f%%`;
    n_pos integer; nan → `-`.
- **Alignment:** pad every cell to the max width in its column so columns line up in monospace
  (still valid Markdown: keep the `| … |` pipes and the header separator row). Left-align
  `target`/`dir`/`flag`, right-align the numeric columns.
- **`flag` logic (evaluate in order):**
  1. `n_pos_holdout < 75` → `RARE` (support too thin to trust — the reviewer's 5x1_6H_SHORT trap).
  2. else `roc_auc_holdout >= 0.55` → `KEEP`.
  3. else `roc_auc_holdout >= 0.53` → `~tune` (borderline; may clear the gate after Optuna).
  4. else → `drop`.
  Use plain ASCII tokens (no emoji/unicode) to avoid Windows encoding issues.
- **Legend** (below the table) — document each column and the caveats:
  - Sorted by PR-AUC (holdout); for base rates <20% PR-AUC is more trustworthy than ROC-AUC.
  - `PR-lift` = PR-AUC ÷ base rate (>1 beats random).
  - `Brier` = calibration (lower better) of the **fixed-param, focal-trained screen model** —
    treat as a rough/relative signal, not the tuned Stage-2 model's calibration.
  - `n_pos` = positive holdout samples; `<75` → `RARE` (AUC on hyper-rare targets is unreliable).
  - `RR` = reward:risk from the target name. `EV_flr` = **pessimistic** expected R per signal at
    the top-20% threshold, assuming every non-win is a full −1R stop (ignores timeouts). A
    trap-detector, NOT a profitability estimate — true EV comes from the Stage-2 backtest.
- Keep the existing meta header (symbol, dataset, cutoffs, seed, `SCREEN_LGBM_PARAMS`).

## Test Requirements (TDD-tester first; RED before code)
Reuse the synthetic-parquet fixture already in `tests/test_target_screen_core.py`.
- `_screen_one_target`/`run_screen` rows include `brier_holdout` (∈[0,1]), `n_pos_holdout`
  (== positive count), `reward_risk` (e.g. a `5x1` target → 5.0, `6x2` → 3.0), `pr_lift`,
  `ev_floor_at_ref`; `signals_per_yr_at_ref` is gone.
- Report rows are ordered by `pr_auc_holdout` desc.
- Report contains the new column headers; a target with `n_pos < 75` renders `RARE`; a
  high-ROC-AUC well-supported target renders `KEEP`.
- Alignment: every data row has the same number of `|`-delimited cells as the header, and each
  column's cells share a common width (assert padding, not exact whitespace content).
- UPDATE any existing assertions that referenced `signals/yr` or the ROC-AUC sort.
- Regression: full fast suite → only the 10 known pre-existing ES01B sentinels remain.

## Out of scope
No `run_screen` signature / output-dir change. No true-EV backtest. No new CLI flags. The CL
report regeneration is a manual re-run I'll do after this commits.
