# Bug Report (symptoms) — ensemble-objective-report-parity_07032026_2055

**Ticket Directory:** `.agents/collab/tickets/ensemble-objective-report-parity_07032026_2055/`
**Status:** Awaiting triage. This is the bug-log INPUT for `/ticket-manager` — intentionally symptom-only (no root cause, no `Required Changes`). The Ticket-Manager (Auditor + Impact-Reviewer) should determine root cause and produce `blueprint.md` here.

> Follow-up to `optimizer-objective-report-parity_07032026_0830`. That ticket fixed a **different, narrower** defect (guard-triggered rows in the **individual-target** path leaking the discarded trial's params into the Opt columns) and explicitly declared the **ensemble** path out of scope / "already correct." This report shows the ensemble path still exhibits the objective-invariance the user originally flagged — **on a run where the earlier fix is deployed and verified.**

## Bug Summary
For a two-objective post-optimizer run (Sharpe and Sortino), the **ensemble** optimized reports are byte-identical between the two objectives — same selected trades, PnL, profit factor, optimized params, and selected trial — *including for ensembles that were genuinely optimized (NOT reverted to baseline by the regression guard)*. The objective function is scored distinctly per objective (the internal `best_obj_score` differs), but the objective appears **not to change which trial is selected** for the ensemble, so both objective reports are duplicates. This makes the Sortino ensemble report add no information over the Sharpe one.

## Evidence (run `reports/batch_runs/batch_20260703_2026/` — NG canary, POST-fix)
This run has the prior fix deployed: individual-target guard rows now correctly show blank `-` Opt columns. The ensemble reports were unaffected.

### A. Ensemble reports are duplicates
`diff batch_summary_optimized_ensembles_sharpe.md batch_summary_optimized_ensembles_sortino.md` differs **only** on title, `Generated` timestamp, `Objective:` label, and per-row `Wall Time` (±0.3s). Every ensemble's trades/PnL/PF/params/`Best Trial` are identical.

### B. (Decisive) The invariance holds for NON-guard, genuinely-optimized ensembles
Per-ensemble comparison of `optimization_results_ensembles_sharpe.json` vs `…_sortino.json`:

| Ensemble (Best Trial) | `regression_guard_triggered` (S/So) | selected `trial_number` (S/So) | `best_obj_score` differs? | `params` differ? | `metrics` differ? |
|---|---|---|---|---|---|
| #1, #2 — `baseline (guard)` | True / True | 2 / 2 | yes | no | no |
| **#3 — `#2/3`** (AP_LONG+AP_SHORT) | **False / False** | 2 / 2 | **yes** | **no** | **no** |
| **#4 — `#2/3`** (AP_LONG+LL_SHORT) | **False / False** | 2 / 2 | **yes** | **no** | **no** |

- Rows #1/#2 hitting the guard → reverting to the objective-independent baseline → identical is expected/correct.
- **Rows #3/#4 did NOT hit the guard** — they applied a real optimization (opt≠pre: e.g. #3 trades 1337→1602, params `0.53`, PF 0.55→0.63). Yet **both objectives selected trial #2 with identical params and identical metrics**, even though `best_obj_score` differs per objective (Sharpe `best_obj_score=-3.1234` vs a different Sortino value). This is the exact "objective scored differently but selection unchanged" signature that motivated the original report — now isolated to the ensemble path with the guard explanation ruled out.

### C. Artifacts don't persist per-trial scores
Each ensemble record's `optuna_info` stores only the *selected* trial (`trial_number`, `best_obj_score`, `consistency_score`, `long_params`/`short_params`) — there is no per-trial score array. So whether trial #2 is legitimately the arg-max under BOTH objectives (coincidence) or selection ignores the objective (bug) **cannot be decided from the artifacts** and requires reading the optimizer's trial-ranking code.

## Scope / Impact
- Ensemble Sortino reports duplicate Sharpe reports; the second objective yields no differentiated ensemble output. Observed on NG (3 trials); ES earlier showed the same duplicate-ensemble signature — likely objective-/symbol-agnostic. Not a data/COT/manifest issue.
- With only 3 trials and 2 non-guard ensembles both landing on trial #2, "coincidence" is possible but unverified — the RCA must confirm from code.

## Candidate Files (starting points, not a diagnosis)
- `agent/generate_ensemble_artifacts.py` — ensemble optimization + record assembly.
- `agent/strategy_optimizer.py` — trial scoring / selection (does the objective enter the ensemble trial ranking, or only the reported score?).
- `agent/batch_post_optimizer.py` — ensemble report writer (`generate_optimized_report` ensemble branch).
- Prior ticket for context: `.agents/collab/tickets/optimizer-objective-report-parity_07032026_0830/blueprint.md`.

## Hypotheses to Investigate (for the ticket manager)
1. **Objective drives scoring but not ensemble selection** — the ensemble "best trial" may be chosen by a shared/secondary key (e.g. `consistency_score`, or `study.best_trial` from a single shared study) so both objectives resolve to the same trial regardless of `best_obj_score`.
2. **Single shared ensemble study reused across objectives** — the two objective runs may share one Optuna study / seed and only re-label the objective on the persisted record, so the selected trial (and its params/metrics) is identical by construction.
3. **Genuine coincidence** — with 3 trials, trial #2 may truly be arg-max under both objectives for these two ensembles; confirm by logging/inspecting per-trial per-objective scores. To make this decidable in future, consider persisting per-trial scores in `optuna_info`.

## Reproduction
```
cd reports/batch_runs/batch_20260703_2026
diff batch_summary_optimized_ensembles_sharpe.md batch_summary_optimized_ensembles_sortino.md   # only title/ts/objective/walltime differ
# Programmatic: for each ensemble key, compare optuna_info.regression_guard_triggered / trial_number / best_obj_score / params
#   and metrics between the _sharpe and _sortino ensemble JSONs.
#   Expect: ensembles #3 and #4 have guard=False, best_obj_score differs, but trial_number/params/metrics identical.
```

## Status
Reported per user request (symptoms only). No code changed. Distinct from and downstream of the already-fixed individual-path ticket.
