---
description: Run a model improvement experiment for CL_Analyst
---

// turbo-all

1. Read the experiment tracker and agent context for baseline:
   - File: `AGENT_CONTEXT.md` (project state)
   - File: `experiment_tracker.json` (prior results, current_best)
   - File: `research_backlog.json` (if this experiment addresses a backlog item)

2. Review the current experiment registry:
   ```bash
   dir /B models\registry
   ```

3. Process the dataset through the data pipeline:
   ```bash
   conda run -n trader python -m src.data_processor
   ```

4. Run walk-forward validation to evaluate the model:
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
   ```

5. Run any experiment-specific scripts (training, hyperparameter tuning, evaluation) as needed.
   - **Gotcha**: When backtesting Dual Ensembles with `TieredEnsembleStrategy` using `agent/backtest_engine.py`, ensure the root levels of the config JSON also define `"tp_atr_mult": "Tiered"`, `"sl_atr_mult": "Tiered"`, and a float `"trailing_atr_mult": 100.0`. If omitted, the backtester fallback strings will throw a `TypeError` during the final string formatting of the report footer.

6. Compare results against the baseline and summarize findings with metrics.

7. Log the experiment results to the tracker:
   - Read `experiment_tracker.json`
   - Append a new entry with: id (use `next_experiment_id` field), name, status, date, dataset,
     data_integrity (set to "clean" for set_10+, "leaked" for older), direction, target,
     metrics (pnl, profit_factor, win_rate, trades, max_drawdown), notes, and tags
   - Increment `next_experiment_id`
   - Update `current_best` if this experiment beats the baseline AND uses a clean dataset
   - Write updated `experiment_tracker.json`

8. Update `research_backlog.json`:
   - If this experiment came from a backlog item, mark it as "completed" and fill in `outcome`
   - If results suggest new ideas, add them as new backlog entries

9. If the experiment improves on the baseline, commit all changes including tracker updates:
   ```bash
   git add -A && git commit -m "EXP-NNN: <experiment description>"
   ```
