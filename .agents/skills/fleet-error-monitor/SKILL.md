---
name: fleet-error-monitor
description: Self-fixing loop for live-fleet crashes — consumes structured error events from .agents/collab/error_queue/, triages IBKR-infra vs code bugs, drives ticket-manager + tdd-manager to a tested fix, deploys (fleet restart), commits, and audit-logs everything.
---

# /fleet-error-monitor — Fleet Health & Self-Fixing Protocol

You are the consumer side of the fleet error queue (protocol overview:
`.agents/collab/error_queue/README.md`). The hourly run (cron `6 * * * *` —
hourly at :06, after the hourly inference bar and first 5m bar complete) is
THREE steps, all mandatory:

1. **Queue pump** — `powershell -File scripts/error_watcher.ps1`:
   auto-files known-infrastructure events, moves the rest to `processing/`.
   Two producers feed the queue: `fleet_runner.py` (one JSON event per
   unique CRASH) and the children themselves (`event_kind: "health"`
   events — stale-bar watchdog firings, exhausted resubscribe retries —
   via `fleet_error_events.emit_child_health_event`).
2. **Health check** — `python -m src.live_execution.fleet_health`
   (read-only): scans the fleet log for new ERROR/CRITICAL lines, flags
   `subs_lost=True` heartbeats, verifies positions/orders line up in the
   telemetry DB, and checks bars are actually arriving. Prints `HEALTH_OK`
   or `HEALTH_EVENT: <kind> | <who> | <detail>` lines.
3. **Triage** — crash/health queue events per the per-event protocol
   below; HEALTH_EVENT lines per the "Health-event triage" section.

**2026-07-06 lesson (do not regress this):** crash-only capture is
insufficient — four children sat alive-but-blind for 17 minutes with ERROR
lines streaming into the log while the monitor saw nothing. The job is
"the fleet is HEALTHY", not "the fleet hasn't died".

## Standing authorization & hard limits

- **Branch:** all work happens on the `stable-fleet` branch. Never merge to
  `main`/`development` yourself — that is always the human's call.
- **You are authorized end-to-end** for bug fixes: investigate → implement →
  test → deploy (fleet restart) → commit. Do NOT stop to ask permission for
  ordinary fixes.
- **HUMAN GATE — stop and ask** when any of these hold:
  - the fix requires a multi-component refactor or design change (the
    Ticket-Impact-Reviewer's "Human Authorization" trigger);
  - the fix would change trading economics, model selection, order routing
    semantics, or manifest risk parameters;
  - the same event has recurred after **2** previous fix attempts (your fix
    isn't working — a human needs to look).
- **NO CHEAP FIXES.** The point is a *valid* fix, not a running process.
  Forbidden band-aids:
  - `try/except: pass` or broad exception swallowing to survive the error;
  - defaulting a missing config/field to `None`/fallback values (project
    rule: missing required fields must RAISE — no silent null defaults);
  - loosening/skipping tests, widening assertions, deleting failing checks;
  - blind retries/sleeps that mask a deterministic bug;
  - hardcoding today's data conditions to dodge the crash.
  If the only fix you can find is on this list, that IS the human gate.

## Health-event triage (HEALTH_EVENT lines + event_kind:"health" queue events)

Judge each finding before opening any ticket — most have a fact-check step:

- `unprotected-position` — HIGHEST severity. Verify on IBKR immediately
  (`get_open_trades` evidence in the log, or ask the human to check TWS).
  If truly naked: alert the human NOW (Telegram + summary) — do NOT place
  or cancel orders yourself beyond the documented recovery/OCA paths. If
  the DB is simply stale (orders exist broker-side), that's a telemetry
  bug → normal ticket flow.
- `stale-bars` / `subs-lost` / `stale-bars-watchdog` — FIRST check market
  hours (weekend/holiday/daily halt = false positive, file as noise; if
  the halt pattern recurs, improve fleet_health's gating via ticket). If
  the market is open: infra vs code judgment exactly like a crash event —
  IBKR-side connectivity → infra close-out; our reconnect/resubscribe
  logic failing → ticket. Recurring self-healed watchdog events (rising
  `occurrences`) mean the fleet is flapping — tell the human even though
  nothing is "down".
- `resubscribe-retries-exhausted` — the child is blind and its own timer
  gave up; the stale-bar watchdog should escalate within ~30 min. If the
  next hourly run still shows it, treat as DOWN: tell the human a restart
  is needed (or restart the fleet yourself per standing authorization if
  the cause is already understood).
- `missing-fill-price` / `incomplete-close` / `duplicate-open-position` —
  telemetry/state code bugs → normal ticket flow (these are exactly the
  "trades don't line up" class the human asked to be caught).
- `log-error` clusters — read the lines; connectivity flaps during a
  known incident are noise (file with a one-line audit note), anything
  novel or repeating → investigate as a code bug.

Close-out: health queue events move pending/ → done/ with an audit line
like crash events. HEALTH_EVENT console lines need no queue file — audit
the judgment (`... | MONITOR | HEALTH — <kind> <who>: <one-line verdict>`)
and notify the human when severity warrants.

## Per-event protocol

For each event JSON in `.agents/collab/error_queue/processing/` (oldest
first, one at a time):

### 1. Triage
1. Read the event. Note `event_id`, `model_name`, `traceback`,
   `classification`, `occurrences`, `gave_up`, `stderr_log_path` (full
   context beyond the captured traceback lives there).
2. Even though the watcher pre-filed infra events, re-check: if your read of
   the traceback matches a signature in `infra_patterns.json`, treat as
   infrastructure → step 5-infra.
3. Append to `.agents/collab/error_queue/audit_log.md`:
   `[TS UTC] | <event_id> | MONITOR | INVESTIGATING — <one-line traceback summary>`

### 2. Investigate — `/ticket-manager`
Invoke the `/ticket-manager` workflow (`.agents/workflows/ticket-manager.md`)
with the full traceback + event context (model, config path, occurrences,
whether restart cap was exhausted). It mints a Ticket ID and produces
`.agents/collab/tickets/<TICKET_ID>/blueprint.md`.

- Audit line: `... | MONITOR | TICKET <TICKET_ID> opened, blueprint ready — root cause: <one line>`
- If the auditor concludes **infrastructure, not code** → step 5-infra.

### 3. Fix — `/tdd-manager`
Hand the Ticket ID to the `/tdd-manager` workflow
(`.agents/workflows/tdd-manager.md`). It drives the Tester/Coder loop until
the new tests AND the full fast suite pass:

```
conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
```

Audit line: `... | MONITOR | FIX implemented under <TICKET_ID> — files: <list>; tests: <N passed>`

### 4. Deploy, verify, commit (in that order)
1. **Deploy:** restart the fleet on the fixed working tree — stop the running
   `fleet_runner` process, then relaunch:
   `python -m src.live_execution.fleet_runner --manifest configs/fleet/fleet_manifest.json`
   (the launch preflight must pass; children must come up).
2. **Verify:** confirm all enabled children are alive after the stagger
   window and that NO new `pending/` event with the same `traceback_hash`
   appears on the next supervision cycles (~10 min). A recurrence = deploy
   failed → back to step 2 with the new evidence; after 2 failed fix
   attempts, human gate.
3. **Commit** (only after successful deploy) on `stable-fleet`:
   `fix(<TICKET_ID>): <summary>` — include the event_id in the body.
4. Audit lines:
   `... | MONITOR | DEPLOYED — fleet restarted, all children healthy`
   `... | MONITOR | COMMITTED <sha> on stable-fleet`

### 5. Close out
- Move the event JSON from `processing/` to `done/`.
- Audit line: `... | <event_id> | MONITOR | DONE — <resolution: fixed|infra|escalated>`
- Notify the human with a short summary (event, root cause, fix, commit sha).
  Optionally push a Telegram note:
  `python -c "from src.live_execution.utils.telegram_alert import TelegramAlerter; TelegramAlerter(prefix='FLEET-AI').send('<summary>')"`

### 5-infra. Infrastructure close-out (no fix attempted)
When an event is infrastructure (IBKR connectivity, gateway restarts, data
farm outages — NOT fixable in this codebase):
1. **Grow the collection:** if the signature is not yet in
   `.agents/collab/error_queue/infra_patterns.json`, append a new entry
   (`name`, `regex`, `notes`). The regex must be SPECIFIC to the
   connectivity failure — never so broad it could swallow a code bug.
2. Move the event to `done/`.
3. Audit line: `... | MONITOR | INFRA — pattern <name> (added|existing), no ticket; <one-line reason>`
4. If `gave_up: true`, the child is DOWN and won't self-heal by restart cap —
   tell the human explicitly that a manual fleet restart is needed once
   connectivity is back.

## Escalation record
When you hit a human gate, before stopping: write the audit line
(`... | MONITOR | ESCALATED — <why>`), leave the event in `processing/`, and
give the human everything needed to decide (blueprint path, proposed fix,
why it tripped the gate).
