# Ticket Resolution Blueprint — optimizer-objective-report-parity_07032026_0830
**Ticket Directory:** `.agents/collab/tickets/optimizer-objective-report-parity_07032026_0830/`

## Bug Summary
Two-objective (Sharpe / Sortino) post-optimizer reports for the NG canary batch `batch_20260703_0758` were near-duplicates, and the "decisive clue" was that two individual targets (`NG01A 3x1 6H | short | average_precision` and `| short | logloss`) recorded a different Optuna `trial_number` under each objective (Sharpe=0, Sortino=1) yet reported identical trade/PnL/metrics.

Root cause (verified against the batch artifacts):

- **The duplicate metrics are CORRECT, not an objective-wiring bug.** Every one of the 8 individual targets — and the ensembles — recorded `optuna_info.regression_guard_triggered == True` for BOTH objectives. When the regression guard fires, `agent/strategy_optimizer.py::run_optimization()` reverts `best_result = baseline_result` and `best_metrics = baseline_metrics`. The baseline backtest is objective-independent (same config, same data), so identical Sharpe/Sortino trade counts, PnL, PF, and holdout metrics are the expected outcome. Sharpe and Sortino use *separate* Optuna studies (the study/db name is keyed on `objective_metric`) and produce distinct `baseline_obj_score` / `best_obj_score`, so the objective genuinely runs and scores distinctly — it simply never changed the selected artifact because nothing beat baseline in a 3-trial warm-started study.

- **The genuine defect is a REPORTING bug in `agent/batch_post_optimizer.py`.** On a guard trigger the optimizer deliberately sets `optuna_info["params"] = {}` (and empties `long_params`/`short_params` for ensembles) so the Optimized columns render blank/`-`. But the individual-target report path falls back to `optuna_info["all_trial_params"]` — the params of the *discarded* Optuna trial — whenever `params` is empty. This fills the "Opt Thr / Opt TP / Opt SL / Opt TrgF / Opt DstF / Opt Cool / Opt Hold / Opt Consec" columns with rejected trial params, even though the same row is labeled `baseline (guard)` and shows pre==opt metrics. Because the discarded trial differs per objective, those columns diverge between the Sharpe and Sortino reports — which is precisely the "different trial_number, decoupled params" signature that made this masquerade as an objective-selection bug.

- **The ensemble path is already correct** (`agent/generate_ensemble_artifacts.py` gates param overrides behind `if not regression_triggered:`, and the ensemble summary in `batch_post_optimizer.py` reads `long_params`/`short_params`, which are `{}` under the guard). No change is needed there.

This fallback violates the guard-aware contract that already governs the sibling `format_best_trial()` helper (documented in `tests/test_report_best_trial.py`): when the guard triggers, the Optimized columns are "intentionally blank (pre==opt)". The fallback was introduced before that guard-aware contract existed and was never updated to honor it (regression seam: unguarded fallback ~`8bbc08bf` 2026-06-14; guard-aware `format_best_trial` + tests ~`ced3642` 2026-07-01).

Out of scope for this ticket (note only, do NOT fix here): the fact that *every* target hit the regression guard reflects an upstream search-quality / trial-budget concern (3 trials), not this report-parity defect.

## Target Files
- `agent/batch_post_optimizer.py` — the only production file to change.
- `tests/test_report_best_trial.py` — extend with a regression test (new test file also acceptable) for the guard-gated fallback. (Test authoring is the TDD-Manager's job; listed here so the coverage requirement is explicit.)

## Required Changes
Do NOT alter `agent/strategy_optimizer.py` (guard logic and the `params={}` / empty per-side params on guard are correct) or `agent/generate_ensemble_artifacts.py` (already guard-gated).

1. **Guard-gate the `all_trial_params` fallback in the individual summary table** (in `generate_optimized_report`, the individual/per-side branch — the block that currently reads roughly `if not params and "all_trial_params" in opt_info:` and reconstructs per-side params by `_{direction}` suffix). Add a condition so the fallback is used only when the regression guard did NOT trigger — i.e. it must also require `not opt_info.get("regression_guard_triggered")`. When the guard did trigger, `params` must remain empty so every `params.get("<param>", "-")` lookup renders `-`, leaving the Opt Thr/TP/SL/TrgF/DstF/Cool/Hold/Consec columns blank and self-consistent with the `baseline (guard)` label and the pre==opt metrics.

2. **Apply the identical guard-gate to the "Optimized Parameters Detail" section** (the second occurrence of the same fallback, the block reading roughly `if not params and "all_trial_params" in opt_info and not is_ensemble:`). Add the same `not opt_info.get("regression_guard_triggered")` condition so the per-parameter Baseline-vs-Optimized rows in the detail section also show the baseline value with an empty/`-` Optimized cell when the guard fired.

3. **Preserve the legitimate fallback behavior.** The `all_trial_params` fallback must still function for its intended non-guard case: a genuinely-improved single-side run where `optuna_info["params"]` happens to be empty but a real trial was applied (`regression_guard_triggered` is False/absent). Only the guard-triggered case is being suppressed. Do not remove the fallback outright.

4. **Regression test.** Add a test asserting that, given an individual-target `opt_info` with `regression_guard_triggered == True`, `params == {}`, and a populated `all_trial_params`, the rendered optimized parameter columns (both the summary table and the Detail section) are blank/`-` and NOT sourced from `all_trial_params`. The test should also confirm the guarded row is objective-invariant (identical Opt columns for two `opt_info`s that differ only in `all_trial_params` / objective). This is the exact gap that let the bug through. Keep the existing `format_best_trial` tests green.

### Expected outcome after fix
- For a batch where all targets hit the regression guard, the individual Sharpe and Sortino reports differ only in the objective label / timestamp / wall-time — the Opt param columns are blank on guarded rows and no longer leak the discarded, per-objective trial params. This makes the reports honestly reflect that no optimized config beat baseline.
- Genuinely-improved (non-guarded) targets are unaffected and still show their applied optimized params.
