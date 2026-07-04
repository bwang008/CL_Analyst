# Bug Report (symptoms) — optimizer-objective-report-parity_07032026_0830

**Ticket Directory:** `.agents/collab/tickets/optimizer-objective-report-parity_07032026_0830/`
**Status:** Awaiting triage. This is the bug-log INPUT for `/ticket-manager` — it is intentionally symptom-only (no root cause, no `Required Changes`). The Ticket-Manager (via Auditor + Impact-Reviewer) will determine root cause and produce the `blueprint.md` in this folder.

> Symptom report only. Root-cause investigation is for the ticket manager. All evidence below is from the NG canary batch `batch_20260703_0758` (`reports/batch_runs/batch_20260703_0758/`), a 3-trial post-optimizer run.

## Bug Summary
For a two-objective post-optimizer run (Sharpe and Sortino), the **optimized results are effectively identical between the two objectives** — same selected trade counts, PnL, profit factor, and optimized parameters — even in cases where the recorded **selected `trial_number` differs** between the objectives. The objective function *does* run distinctly (the internal objective scores differ per objective), but the objective appears **not to affect which trial's params/metrics are ultimately reported**. This makes the Sharpe and Sortino reports near-duplicates and calls into question whether trial selection is wired to the reported artifact.

## Evidence / Symptoms

### A. Sharpe and Sortino ensemble reports are duplicates
`diff batch_summary_optimized_ensembles_sharpe.md batch_summary_optimized_ensembles_sortino.md` differs **only** on: the title, the `Generated` timestamp, the `Objective:` label, and per-row `Wall Time` (±0.1s). **Every** data value — Trades (pre/opt/holdout), PF, PnL, all optimized parameters, and the `Best Trial` cell — is identical.

### B. The objective *is* computed distinctly (so the objective fn runs)
`diff optimization_results_ensembles_sharpe.json …_sortino.json` shows the per-target `objective`, `consistency_score`, `baseline_obj_score`, and `best_obj_score` all differ. Example (ensemble AP_LONG+AP_SHORT):
- sharpe: `baseline_obj_score=-3.5168`, `best_obj_score=-3.1234`
- sortino: `baseline_obj_score=-2.5985`, `best_obj_score=-2.4675`

So the objective scoring differs, yet the selected trial and its reported params/metrics do not.

### C. (Decisive) Reported params/metrics are decoupled from the selected `trial_number`
Comparing every target's `optuna_info.trial_number`, `params`, and `metrics` between the two objectives:

| Group | records | `trial_number` differs | `params` differ | `metrics` differ | `best_obj_score` differs |
|---|---|---|---|---|---|
| Ensembles | 4 | 0 | 0 | 0 | 4 |
| Individual | 8 | **2** | 0 | 0 | 8 |

The two individual targets where selection diverged (**NG01A 3x1 6H · short · AP and · LL**) recorded **`trial_number` = 0 under Sharpe but 1 under Sortino** — a *different* Optuna trial — yet reported **identical `params` and identical `metrics`**. A different Optuna trial samples different hyperparameters and should produce a different backtest; identical params+metrics under a different `trial_number` indicates the reported params/metrics are not sourced from the selected trial (or all trials collapsed to one param set).

### D. Optimization did apply *something* (not pure baseline)
For ensembles 3 & 4 (`Best Trial #2/3`), the optimized columns *do* differ from pre-optimization (e.g. ens#3 trades 1337→1602, PF 0.55→0.63; params 1.5/3.5 → 4.5/2.5). So these are not "baseline (guard)" rows — a non-baseline trial was applied. But whatever was applied is byte-identical across both objectives.
> Note: this partially refines the original observation that "opt and pre are unchanged" for ensembles 3 & 4 — within a single report the opt *does* change vs pre; the invariance is *between the two objective reports*.

## Scope / Impact
- Two-objective post-optimizer runs cannot be trusted to reflect the requested objective in their reported optimized params/metrics; Sharpe and Sortino outputs are duplicates.
- Observed on NG canary (3 trials). Likely objective-/symbol-agnostic (the ES canary `batch_20260702_0758` should be re-checked for the same signature). Not a data or COT issue — the parquets/manifests are correct; this is in the optimize→report path.
- Low trial count (3) makes "both objectives coincidentally pick the same trial" plausible for the ensemble case, but item **C** (different `trial_number`, identical params/metrics) is not explained by coincidence and is the strongest signal.

## Candidate Files (starting points, not a diagnosis)
- `agent/strategy_optimizer.py` — trial selection, objective scoring, regression guard.
- `agent/batch_post_optimizer.py` — drives the per-objective optimization and writes `optimization_results_*` / `batch_summary_optimized_*`.
- `agent/generate_ensemble_artifacts.py` — ensemble report generation.
- Existing tests in this area worth reading first: `tests/test_report_best_trial.py`, `tests/test_strategy_optimizer_reconstruction.py`.

## Hypotheses to Investigate (for the ticket manager)
1. **Params/metrics reconstruction ignores the selected trial/objective** — the report may reconstruct from a shared source (baseline study, warm-start point, or trial 0) rather than the objective's selected trial. (`warm_start_injected: true` is present — check whether the warm-start params overwrite the selected-trial params in the report.)
2. **Objective drives scoring but not selection** — `best_obj_score` differs per objective, but the "best trial" chosen for the artifact may be picked by a shared/secondary key (e.g. `consistency_score` computed identically, or `study.best_trial` from one shared study), so both objectives resolve to the same params.
3. **Sortino report copies Sharpe outputs** — a writer-level aliasing where only the objective label/scores are re-stamped while trade/PnL/param blocks are carried over.

## Reproduction
```
# From an existing batch:
cd reports/batch_runs/batch_20260703_0758
diff batch_summary_optimized_ensembles_sharpe.md batch_summary_optimized_ensembles_sortino.md   # only title/ts/objective/walltime differ
# Programmatic check (see scratch compare): for each key, compare optuna_info.trial_number / params / metrics
#   between optimization_results_*_sharpe.json and *_sortino.json.
#   Expect: 2/8 individual targets have different trial_number but identical params AND metrics.
```

## Status
Reported per user request (symptoms only). No code changed. NG pipeline build itself is unaffected and complete; this concerns the optimizer→report artifacts.
