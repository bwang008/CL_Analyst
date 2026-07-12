# Ticket Resolution Blueprint — autoresume-supervision-selfheal_07112026_1736
**Ticket Directory:** `.agents/collab/tickets/autoresume-supervision-selfheal_07112026_1736/`

## Proposal Summary
A request proposed extending the `.agents/workflows/run-cloud-batch.md` "Supervision (do not
fire-and-forget)" guidance so the supervising agent, upon DETECTING a stalled cloud batch (the local
driver `gcp/run_sweep_batch.ps1` externally killed mid-run — `batch_progress.json` stuck below `total`,
the batch's sweep/opt VMs TERMINATED-but-uncollected, and no post-opt reports present), would
**AUTOMATICALLY** invoke `scripts/resume_batch.ps1 -BatchId <id>` to self-heal instead of requiring a
human to run it manually.

**Verdict: ADOPT-WITH-SAFEGUARDS.** Severity **MEDIUM** (a documentation/guidance edit, but it opens a
new *autonomous* surface). **Not a regression** (the stall gap is original to the local-orchestrator
design; the 2026-07-05 TTL work bounded cost, not completion). **Review verdict: APPROVED** — doc/guidance
change only, no code file touched; Interface / Base-Class / Refactor rules all clear; no human
authorization required to make the doc edit.

### Root cause of the risk (why the proposal as-worded is unsafe, and what to change)
The proposed trigger — "`batch_progress.json` stuck below `total`" — is **NOT an observable dead-driver
signal**:
- `gcp/run_sweep_batch.ps1` writes `batch_progress.json` **only when a sweep SLOT COMPLETES**
  (`~L1000-1013`, via `Save-Progress` `~L233-237`). While a sweep is in progress the driver only prints a
  **console heartbeat** (`~L991-994`), polling every 30s (`~L1017`) — nothing is written to the JSON.
- The **post-optimizer stage writes NO progress entry at all**.
- There is **no PID, no `last_update`/`completed_at`, no heartbeat, and no lock file** anywhere (the
  recovered NG reference file even lacks `completed_at`).

Therefore an incomplete `batch_progress.json` is **indistinguishable** from (i) a legitimately long
in-progress sweep, or (ii) a running post-optimizer. Firing recovery on this signal is a **high**
false-positive risk (question (a)).

There is also a **collect→post-opt race** (question (b)): a sweep VM can be already **TERMINATED** while
the **still-alive** driver has not yet collected it. In that window the resume script's **RUNNING-VM guard
would NOT fire** (no RUNNING VM exists) even though the driver is alive — so auto-invoke could cause a
**double post-optimizer deploy** and **two processes writing `batch_progress.json` concurrently**
(last-writer-wins **corruption of a HEALTHY batch**). The blueprint's running-VM guard is therefore
**necessary-but-NOT-sufficient**.

Cost/orphan risk (question (c)) is bounded — every VM carries a `--max-run-duration` DELETE/STOP TTL
(`run-cloud-batch.md:29-31`) — and the `resume_batch.ps1` script mitigations (both-or-neither
recoverability, never-flip-DEPLOY_FAILED/TIMEOUT→COMPLETED, backup-before-mutate, targeted VM ops,
idempotent) cover the *script's* internal safety. **The unmitigated surface is the AGENT-SIDE decision of
*WHEN* to auto-invoke** — the script mitigations do not cover mis-timing of the invocation itself.

The fix splits **DETECTION (may be autonomous)** from **RECOVERY-MUTATION (gated)** and documents an
honest, observable stall criterion. The two Impact-Reviewer non-blocking conditions are folded in below
(the human-gate is BINDING until the heartbeat signal exists; the DETECT path persists nothing).

## Target Files
- `.agents/workflows/run-cloud-batch.md` — the "Supervision (do not fire-and-forget)" note (~L156-163).
- `.agents/collab/tickets/batch-resume-recovery_07112026_0924/blueprint.md` — append the
  "necessary-but-not-sufficient" note about the running-VM guard.

> **Documentation/guidance changes ONLY. No code file is modified by this ticket.** (The optional
> heartbeat instrumentation of `gcp/run_sweep_batch.ps1` is explicitly a SEPARATE future ticket — see
> "Optional follow-up ticket" below — and is out of scope here.)

## Required Changes

### Change 1 — Extend the "Supervision (do not fire-and-forget)" note in `.agents/workflows/run-cloud-batch.md`
Add guidance that governs how the supervising agent may respond to a *suspected* stall. It MUST establish
the following, consistent with (and not contradicting) the existing "Orphaned-VM prevention (RULES)"
section (~L21-44) and its "Agents MUST NEVER broad-kill processes" rule:

1. **"Stuck-below-total is NOT, by itself, a stall signal" caveat.** Explicitly state that an incomplete
   `batch_progress.json` is indistinguishable from a legitimately long in-progress sweep or a running
   post-optimizer, because progress is written only on sweep-slot completion (`run_sweep_batch.ps1`
   ~L1000-1013 / `Save-Progress` ~L233-237), the post-optimizer writes no progress entry, and there is no
   PID / `last_update` / heartbeat / lock to prove the driver is dead. An agent MUST NOT treat "the JSON
   looks stuck" as proof of a dead driver.

2. **Split DETECT (autonomous) from RECOVER (gated).**
   - **DETECT — autonomous, allowed:** the agent MAY, on a suspected stall, (i) run
     `scripts/resume_batch.ps1 -BatchId <id> -DryRun` (read-only; the script's DryRun contract performs
     zero filesystem mutations, zero `.bak` writes, zero deploys/VM ops) and (ii) ALERT the human with the
     resulting plan. **The autonomous DETECT path persists NOTHING** — no `.bak`, no progress write, no
     download/extract, no deploy — so detection can never itself corrupt a healthy batch. *(Reviewer
     condition #2.)*
   - **RECOVER (live mutate/deploy) — GATED.** Before any live (non-DryRun) `resume_batch.ps1` run, ALL of
     the following must hold:
     - **(i) Proven-dead driver:** the `run_sweep_batch.ps1` process / its console is *confirmed gone*
       (e.g. the owning terminal/process is verified absent) — NOT merely "batch_progress.json looks stuck
       below total."
     - **(ii) All batch VMs TERMINATED or absent:** every `optuna-sweep-*` / `opt-post-*` instance for this
       batch id is TERMINATED or gone (closes the collect→post-opt race where a TERMINATED VM coexists with
       a live driver; the script's running-VM guard alone does not cover this).
     - **(iii) Settle/dwell window:** wait ≥ 2 monitor poll cycles + one collect window (~2-3 min) during
       which `batch_progress.json` does NOT change, to rule out an in-flight slot collection.
     - **(iv) `-DryRun`-first:** always run and review the DryRun plan before any live run.

3. **BINDING human-gate on the live mutate/deploy.** The live (non-DryRun) `resume_batch.ps1` invocation is
   **human-gated**: the agent detects + DryRuns + alerts; a human authorizes the live run. State
   **explicitly that this human-gate is BINDING until the follow-up PID/heartbeat-signal ticket lands** —
   the four "live auto-invoke" preconditions above are the criteria a *future* autonomous path would need,
   and MUST NOT be misread as already authorizing autonomous live runs today (no observable dead-driver
   signal exists yet). *(Reviewer condition #1.)*

4. Keep it consistent with the existing orphan rules: reference `scripts/reap_orphan_vms.ps1` for
   reconciliation and reiterate "never broad-kill processes / targeted VM ops only."

### Change 2 — Append a note to `.agents/collab/tickets/batch-resume-recovery_07112026_0924/blueprint.md`
Add a short note (near the Step-6 "Running-VM guard" description) stating that the running-VM guard is
**necessary-but-NOT-sufficient** as a race protection: it blocks only when a batch VM is RUNNING, and does
**not** cover the collect→post-opt window in which a sweep VM is already TERMINATED while the original
`run_sweep_batch.ps1` driver is still alive collecting it. The safe timing of a live invocation is enforced
by the agent-side gating documented in `run-cloud-batch.md` (Change 1), not by the script's guard alone.

## Optional follow-up ticket (OUT OF SCOPE for this ticket — do NOT implement here)
Add a **PID + `last_update` heartbeat** to `gcp/run_sweep_batch.ps1`'s `Save-Progress` (~L233-237) so
`batch_progress.json` carries an observable liveness signal (process id + monotonically-updated timestamp).
Only once such an observable dead-driver signal exists could the BINDING human-gate above be relaxed toward
autonomous live recovery. File this as a separate ticket; it is explicitly not part of this change.

## Scope / Non-goals
- Documentation/guidance edits only; no code, no test changes; no `tdd-manager` handoff required by this
  ticket (this exercise stops at the reviewed verdict/blueprint).
- Do NOT mutate NG batch `batch_20260711_061128` or live SI batch `batch_20260711_094042`; touch no VMs.
