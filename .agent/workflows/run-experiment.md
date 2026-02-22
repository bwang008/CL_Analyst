---
description: How to run a model improvement experiment for CL_Analyst
---

# Run Experiment Workflow

// turbo-all

## Prerequisites
- Conda environment `trader` is activated
- Working directory is `c:\Users\bwang\Documents\GitHub\CL_Analyst`
- Data exists at `data/processed/CL_set_03.parquet`

## Steps

1. Check the strategy queue for the next pending strategy:
```bash
python -c "import json; q=json.load(open('agent/strategy_queue.json')); [print(f'{s[\"id\"]}: {s[\"name\"]} [{s[\"status\"]}]') for s in q['strategies']]"
```

2. Read the strategy details and hypothesis from the queue output.

3. If the strategy requires code changes (new features, new targets), make those changes first.

4. Run the experiment using the experiment runner:
```bash
python agent/experiment_runner.py --experiment <STRATEGY_ID>
```

5. Check the results in the experiment log:
```bash
python -c "import json; log=json.load(open('agent/experiment_log.json')); e=log['experiments'][-1]; print(json.dumps(e, indent=2, default=str))"
```

6. Review the verdict:
   - `promising`: Signal F1 > 10% — investigate further, tune hyperparameters
   - `improvement`: Precision or recall > 10% — worth combining with other strategies
   - `marginal`: Any signal metric > 2% — note but move to next strategy
   - `no_improvement`: No change — move to next strategy

7. Update the strategy queue status based on the result.

8. If `promising`, consider running follow-up experiments with:
   - Different balance modes (weight vs downsample)
   - Probability threshold sweeping
   - Hyperparameter tuning

9. Log findings to `agent/experiment_log.json` — what worked, what didn't, and why.

## Quick Smoke Test
```bash
python agent/experiment_runner.py --quick-test
```

## Termination Criteria
Stop when vault backtest shows:
- Sharpe Ratio > 1.0 (annualized)
- Profit Factor > 1.5
- No single fold with catastrophic drawdown > 15%
