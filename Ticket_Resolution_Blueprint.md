# Ticket Resolution Blueprint

## Bug Summary
The `batch_summary_optimized_ensembles_sharpe.md` summary table lists ensembles in a different order than the `sharpe_ensemble_backtests.md` report, causing holdout PnL values to appear mismatched to the user. The root cause is a non-deterministic sorting bug in `agent/batch_post_optimizer.py`. While the backtest generation correctly relies on `top_pairs.json` to assign a canonical numbering (`E01`, `E02`, etc.), the summary report generator uses a natural sort of experiment labels. Because multiple pairs can share the exact same label (e.g., when the only difference is the objective, like AP vs LL), Python's stable sort falls back to the original dictionary insertion order of `all_results`. Since `all_results` is populated asynchronously via `as_completed()`, the tiebreaker is highly non-deterministic.

## Target Files
- `agent/batch_post_optimizer.py`

## Required Changes
Update the `get_ensemble_sort_key` function within `generate_optimized_report()` (or the global scope, wherever it is defined) so that it respects the canonical `top_pairs.json` order, mimicking the deterministic logic found in `generate_ensemble_artifacts.py`.

1. **Load Canonical Order:** At the beginning of `generate_optimized_report()`, attempt to load the `top_pairs.json` file from the `batch_dir`. Map each `pair_key` to its canonical index (e.g., `1` for the first pair).
2. **Update Sort Key Logic:** Modify `get_ensemble_sort_key(item)` to use this loaded canonical `index` as the primary sort key for ensembles.
3. **Deterministic Fallbacks:** Maintain the natural label sorting and add a metric-aware tie-breaker as secondary fallbacks to ensure determinism for unranked pairs or if `top_pairs.json` is not present.
