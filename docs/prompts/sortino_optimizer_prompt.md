# Sortino Ratio Post-Optimization Prompt

Copy and paste the following prompt into a fresh agent context:

***

**System Context**: We have a quantitative trading pipeline that optimizes ensemble trading strategies. Currently, the `agent/strategy_optimizer.py` and `agent/batch_post_optimizer.py` scripts optimize for the Annualized Monthly Sharpe Ratio (referred to as the "Consistency Score"). We want to add the ability to optimize for the **Sortino Ratio** instead, without breaking or replacing the existing Sharpe path, so we can compare the results.

**Task Definition:**

1. **Update `agent/strategy_optimizer.py`:**
   - Add an `--objective` CLI argument that accepts `sharpe` (default) or `sortino`.
   - Update `run_optimization` to accept an `objective_metric` parameter.
   - Update `make_objective` to use `objective_metric`. 
   - Currently, `make_objective` computes the Sharpe ratio using monthly PnL aggregation:
     ```python
     monthly_pnls = trades_df["pnl"].resample("ME").sum().dropna()
     ```
     If the objective is `sortino`, you should compute the Sortino ratio on those `monthly_pnls` instead of Sharpe. Note that `strategy_optimizer.py` already contains a `compute_sortino` function, but it is currently written for bar-by-bar equity curves. You will need to implement a Monthly Sortino calculation in `make_objective` for the monthly aggregation (i.e., taking the mean of `monthly_pnls` divided by the standard deviation of only the *negative* `monthly_pnls`, scaled by `sqrt(12)`).
   - Ensure the printed logs reflect whether Sharpe or Sortino is being used.

2. **Update `agent/batch_post_optimizer.py`:**
   - Add an `--objective` CLI argument (choices: `sharpe`, `sortino`, default: `sharpe`).
   - Pass this argument down to the `run_optimization` calls.
   - Modify the output markdown report filename to be dynamic based on the objective. Instead of hardcoding `batch_summary_optimized.md`, make it output to `batch_summary_optimized.md` (if sharpe) and `batch_summary_optimized_sortino.md` (if sortino).

3. **Execute the Sortino Optimization:**
   - Once the code is updated, run the batch post-optimizer on the existing batch directory using the new Sortino objective.
   - Run the following command (or equivalent) to execute the batch:
     `python agent/batch_post_optimizer.py --batch-dir reports/batch_runs/batch_20260518_2321 --n-trials 1500 --holdout-months 6 --workers 16 --no-filter --objective sortino`
   - **Note**: The execution should take ~3.5 hours on the local machine or you can deploy it to a GCP VM if the standard deployment workflow is used. For this task, please deploy it using the standard GCP orchestrator script `gcp/run_post_optimizer.ps1` if available, ensuring the new `--objective sortino` flag is passed into the VM payload.

**Constraints:**
- Do not remove or break the existing `sharpe` optimization path. It must remain the default.
- Maintain the memory safety optimizations (like `BestResultTracker`) that are already in place.
- Ensure the trade floor penalty is still applied to the Sortino score exactly as it is for the Sharpe score.
