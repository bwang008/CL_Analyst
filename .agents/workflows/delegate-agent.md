# /delegate-agent

**Description:** Use this workflow when handling complex, multi-stage tasks that require extensive research, code modification, and validation. It enforces an "Orchestrator-Subagent" delegation pattern to maintain focus, prevent context window pollution, and ensure high data integrity.

## Core Philosophy
The main agent acts strictly as an **Orchestrator**. The Orchestrator does NOT write code or execute modifying commands directly. Instead, the Orchestrator plans, delegates, reviews, and aggregates. Subagents act as the **Workers** (Researchers, Coders, Validators).

## Workflow Steps

### 1. Planning & Research (Orchestrator)
- Read the user's request and identify the discrete phases of work.
- Launch `research` subagents to investigate the codebase, understand data flows, and gather context.
- **Do not** write modifying code during this phase.
- Draft an `implementation_plan.md` artifact that breaks the work into sequential phases.
- Identify which subagent (Role/Type) will handle each phase.
- Request user approval on the plan.

### 2. Execution & Delegation (Orchestrator -> Subagents)
Once the plan is approved, create a `task.md` artifact to track progress. Then, proceed phase-by-phase:

- **Prompting:** Write a highly detailed prompt for the subagent. Include all necessary context, exact file paths, and strict constraints (e.g., "Must be byte-identical", "Do not modify X").
- **Branching:** If the subagent is modifying code, launch it in a `branch` workspace to prevent accidental main-branch corruption. If it is just running scripts or generating data, `inherit` workspace is fine.
- **Delegation:** Use `invoke_subagent` to launch the worker. The Orchestrator should then wait for the subagent's report.

### 3. Review & Validation (Orchestrator)
When a subagent returns:
- **Validate:** Do not blindly accept the subagent's work. If the subagent modified code, review the diffs. If the subagent generated data, run validation checks (e.g., row counts, diff comparisons, NaN checks).
- **Iterate:** If the subagent made a mistake or the validation fails, send a message back to the subagent with the error logs and ask it to fix the issue.
- **Merge/Accept:** Once validated, accept the changes (if in a branch, guide the user on merging, or apply the validated diffs).

### 4. Aggregation & Reporting (Orchestrator)
- Update the `task.md` checklist.
- Summarize the subagent's findings or accomplishments for the user.
- Proceed to the next phase in the plan, repeating the delegation cycle.
- Once all phases are complete, generate a `walkthrough.md` or final report artifact.

## Why use this pattern?
1. **Context Isolation:** The Orchestrator's context window stays clean and focused on the high-level plan and user directives, rather than getting cluttered with hundreds of lines of code edits or terminal outputs.
2. **Safety:** Code generation and modifications happen in isolated subagent scopes, reducing the risk of catastrophic silent bugs in the main workspace.
3. **Parallelism:** Multiple research or independent tasks can be run simultaneously by launching several subagents at once.
