---
description: Research new methods from a report and implement them in a separate branch
---

// turbo-all

1. Read the agent context and tracker for current state:
   - File: `AGENT_CONTEXT.md` (project state)
   - File: `experiment_tracker.json` (what's been tried)
   - File: `research_backlog.json` (existing ideas, avoid duplicates)

2. Read and analyze the provided research report or paper.

3. Compare the methods described against the current CL_Analyst codebase to identify techniques NOT already implemented.

4. Create a feature branch for the experiment:
   ```bash
   git checkout -b research/<method-name>
   ```

5. Implement the new method(s) in the appropriate modules (features, model, data pipeline).

6. Write tests for the new implementation:
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
   ```

7. Run the full experiment pipeline to measure performance:
   ```bash
   conda run -n trader python -m src.data_processor
   ```

8. Run walk-forward validation:
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short
   ```

9. Log and compare results against the baseline model. Summarize what worked, what didn't, and recommended next steps.

10. Update `research_backlog.json`:
    - Add concrete experiment ideas discovered during research as new entries
    - Set appropriate priority and category
    - Link prerequisites if needed
    - If this research addresses an existing backlog item, mark it as "completed" with outcome

11. Update `experiment_tracker.json` if any model was trained:
    - Append experiment entry with full metrics
    - Set `data_integrity` appropriately based on dataset used

12. Commit all changes including tracker and backlog updates:
    ```bash
    git add -A && git commit -m "Research: <method description> - results summary"
    ```

13. Return to main branch:
    ```bash
    git checkout main
    ```
