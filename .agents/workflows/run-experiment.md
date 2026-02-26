---
description: Run a model improvement experiment for CL_Analyst
---

// turbo-all

1. Review the current experiment registry to understand the baseline:
   ```bash
   dir /B models\registry
   ```

2. Process the dataset through the data pipeline:
   ```bash
   conda run -n trader python -m src.data_processor
   ```

3. Run walk-forward validation to evaluate the model:
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
   ```

4. Run any experiment-specific scripts (training, hyperparameter tuning, evaluation) as needed.

5. Compare results against the baseline and summarize findings with metrics.

6. If the experiment improves on the baseline, commit the results:
   ```bash
   git add -A && git commit -m "EXP-NNN: <experiment description>"
   ```
