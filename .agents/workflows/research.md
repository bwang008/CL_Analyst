---
description: Research new methods from a report and implement them in a separate branch
---

// turbo-all

1. Read and analyze the provided research report or paper.

2. Compare the methods described against the current CL_Analyst codebase to identify techniques NOT already implemented.

3. Create a feature branch for the experiment:
   ```bash
   git checkout -b research/<method-name>
   ```

4. Implement the new method(s) in the appropriate modules (features, model, data pipeline).

5. Write tests for the new implementation:
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
   ```

6. Run the full experiment pipeline to measure performance:
   ```bash
   conda run -n trader python -m src.data_processor
   ```

7. Run walk-forward validation:
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short
   ```

8. Log and compare results against the baseline model. Summarize what worked, what didn't, and recommended next steps.

9. Commit the results:
   ```bash
   git add -A && git commit -m "Research: <method description> - results summary"
   ```

10. Return to main branch:
    ```bash
    git checkout main
    ```
