# TDD Result — block-sharpe-objective-ab_07092026_1031 (Phase 1: Python core)

**Final outcome:** full fast suite `conda run -n trader python -m pytest tests/ -m "not slow" -q` → **1983 passed, 0 failed** (1929 pre-existing + 54 new; red phase was 54 failed / 1929 passed, verified by Manager before Coder spawn).

## Files changed
| File | Change |
|---|---|
| `agent/strategy_optimizer.py` | `BLOCK_OBJECTIVE_METRICS`, `_block_sharpe_score` (calendar 0-fill reindex, remainder-to-earliest partition, per-block ±5.0 clip, min/median/mean−λ·std), block routing in Optuna objective + `_compute_objective_score` (sharpe/sortino numerically unchanged — regression-pinned), seed offsets block_min:2/block_median:3/block_mean_std:4, `n_blocks`/`lambda_dispersion`/`min_block_months` kwargs + hard ValueError window guard on `run_optimization` and `run_hybrid_optimization`, per-block diagnostics into `optuna_info.block_sharpes`, CLI `--objective` choices extended |
| `agent/batch_post_optimizer.py` | `parse_objectives` comma-list (`both`→sharpe,sortino; loud rejection), `--n-blocks` (3) / `--lambda-dispersion` (1.0) / `--min-block-months` (10) threaded through `run_single_optimization`, report header stamps block params |
| `agent/unified_pair_optimizer.py` | `--objectives` (default `sharpe`); per-arm: reads only `batch_summary_optimized_<arm>.md`, writes `top_pairs.json` (sharpe) / `top_pairs_<arm>.json` (other arms); cross-objective pooling removed |
| `scripts/preflight_holdout_check.py` | importable `check_block_layout(...) -> (ok, msg)`: inert without block metrics; fails when (window − holdout) < n_blocks×min_block_months; reports block layout |
| `tests/test_block_sharpe_objective.py` | NEW — 55 tests (contracts above) |
| `tests/test_objective_seed_offset.py` | +9 tests (new offsets, uniqueness, seed threading); all 22 pre-existing assertions untouched |

## Notes
- Blueprint naming correction: `run_ensemble_optimization` does not exist; the second entry point is `run_hybrid_optimization` — guard + kwargs applied there.
- Phase-2 (shell layer) threading contract: `batch_post_optimizer.py --objective <comma-list> --n-blocks N --lambda-dispersion F --min-block-months N`; `unified_pair_optimizer.py --objectives <single-arm>` per arm; `generate_ensemble_artifacts.py --objectives <comma-list>` (pre-existing arg).
- Working tree also carries UNRELATED uncommitted changes (GCS-dataset preflight in `gcp/gcp_deploy_sweep.ps1`, `gcp/run_sweep_batch.ps1`, `.agents/workflows/run-cloud-batch.md`; fleet edits in `src/live_execution/*`) — excluded from the phase-1 commit.
