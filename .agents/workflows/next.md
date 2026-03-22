---
description: Propose the next experiment based on prior results and research backlog
---

1. Read the agent context file for current project state:
   - File: `AGENT_CONTEXT.md`

2. Read the experiment tracker for all prior results:
   - File: `experiment_tracker.json`
   - Pay attention to `data_integrity` flags — experiments marked `"leaked"` used datasets with future lookahead bias and their metrics are unreliable
   - Check `current_best` — if null, the priority is establishing a clean baseline

3. Read the research backlog for queued ideas:
   - File: `research_backlog.json`
   - Focus on items with `"status": "ready"` and no unmet prerequisites
   - Prioritize by: critical > high > medium > low

4. Analyze and decide:
   - What has been tried and what the results show
   - What is the current clean baseline (if any — check `current_best`)
   - Which backlog items are ready (prerequisites met)
   - Which high-priority items haven't been tried

5. Propose the top 1-3 experiments to run next, with:
   - **What**: Specific experiment to run (dataset, target, direction, method)
   - **Why**: What gap it fills or improvement it targets
   - **Effort**: Estimated hours and compute requirements
   - **Success criteria**: What metric improvement would matter
   - **Backlog ID**: Which IDEA-NNN it addresses

6. Wait for user to pick one, then execute it via `/run-experiment` or `/research`.
