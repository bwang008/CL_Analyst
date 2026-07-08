---
name: fleet-error-monitor
description: Self-fixing loop for live-fleet crashes — consumes structured error events from .agents/collab/error_queue/, triages IBKR-infra vs code bugs, drives ticket-manager + tdd-manager to a tested fix, deploys (fleet restart), commits, and audit-logs everything.
---

# /fleet-error-monitor — Fleet Health & Self-Fixing Protocol

You are the consumer side of the fleet error queue (protocol overview:
`.agents/collab/error_queue/README.md`). The hourly run (cron `6 * * * *` —
hourly at :06, after the hourly inference bar and first 5m bar complete) is
FOUR steps, all mandatory:

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
3. **Broker audit** — `python -m src.live_execution.broker_audit`
   (READ-ONLY broker truth; 2026-07-08): connects to IB Gateway with the
   operator's Master API clientId (626, `readonly=True`, port 4002 — an id
   no fleet child uses) and cross-checks every open position against the
   ACTUAL resting stop orders on its exact contract. This closes the
   `fleet_health` blind spot: the DB check only confirms the ledger *carries*
   an sl_order_id, never that the order is really resting, so a silently
   cancelled/rejected stop reads as protected. Prints `BROKER_OK` /
   `BROKER_EVENT: naked-position | <sym>/<expiry> | <detail>` /
   `BROKER_SUMMARY`, or `BROKER_UNAVAILABLE:` if the Gateway is down (not a
   fault — report and move on; the fleet may be intentionally stopped). NEVER
   places/cancels (readonly by construction). A `BROKER_EVENT: naked-position`
   is HIGHEST severity (like `unprotected-position`): verify it isn't a
   momentary post-fill gap, then alert the operator NOW (Telegram) — do not
   place orders yourself.
4. **Triage** — crash/health queue events per the per-event protocol
   below; HEALTH_EVENT + BROKER_EVENT lines per the "Health-event triage"
   section.

**2026-07-06 lesson (do not regress this):** crash-only capture is
insufficient — four children sat alive-but-blind for 17 minutes with ERROR
lines streaming into the log while the monitor saw nothing. The job is
"the fleet is HEALTHY", not "the fleet hasn't died".

## Standing authorization & hard limits

- **Branch:** all work happens on the operator's CURRENT fleet working
  branch — check `git branch --show-current` and commit there. Do NOT
  assume a branch name: `stable-fleet` was merged and retired 2026-07-07
  (the operator renames/merges working branches as the project evolves;
  you migrate with the worktree). Never merge to `main`/`development`
  yourself — that is always the human's call. If the working tree has
  operator WIP (dirty files you didn't touch), stage your commits
  file-by-file and leave their files alone.
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
- `housekeeping-*` — emitted by each child's in-process hourly sweep at
  ~:15 (after this monitor's :06 run; the NEXT :06 fleet_health run is
  the verification pass). Two severity classes, routed on the exact kind
  string:
  - `housekeeping-orphan-cancelled` / `housekeeping-drift-detected` /
    `housekeeping-ledger-repaired` — INFORMATIONAL: the action was
    already taken in-child (targeted orphan cancel, OOB drift recovery,
    whitelisted exit-price repair). Verify via the next fleet_health
    run, file to done/, do NOT re-act. RISING `occurrences` of the same
    event = something repeatedly manufactures the inconsistency (e.g. a
    path that keeps orphaning brackets) → ticket.
  - `housekeeping-naked-position` / `housekeeping-untracked-position` /
    `housekeeping-ambiguous` / `housekeeping-unknown-order` — HIGHEST
    severity, exactly like `unprotected-position`: notify the human NOW;
    NEVER auto-place, auto-cancel, or auto-close (the sweep itself is
    detect-only for these by design — order-routing semantics are a
    human gate).
  - `housekeeping-error` — the sweep itself failed or ran slow (>10s):
    a code bug in housekeeping, never a market event → normal ticket
    flow; trading is unaffected by construction (never-raises boundary).

Close-out: health queue events move pending/ → done/ with an audit line
like crash events. HEALTH_EVENT console lines need no queue file — audit
the judgment (`... | MONITOR | HEALTH — <kind> <who>: <one-line verdict>`)
and notify the human when severity warrants.

## Known recurring patterns (verify, one audit line, do NOT re-investigate)

Learned from the 2026-07-06/07 incident chain — each earned hours of
investigation once; do not repeat it:

- **~14:15 PT (17:15 ET) daily**: IBKR Gateway restart during the 5-6pm ET
  futures halt — "Peer closed connection" + ConnectionRefused burst on ALL
  children, reconnect within ~30s ("Reconnected successfully on attempt
  1-2"). INFRA noise IF recovery confirmed in the log. A child that does
  NOT reconnect = real event.
- **Error 366 "No historical data query found" / Error 162 "query
  cancelled" clusters**: the resubscription cycle cancelling stale
  requests — noise whenever adjacent to a known reconnect/reopen event.
- **~15:00 PT (18:00 ET) reopen watchdog false-fire**: FIXED by
  cl-watchdog-reopen-grace_07052026_0001 (GLOBEX session_open_anchor).
  If stale-bars-watchdog events reappear AT the reopen after that commit
  is deployed, that is a REGRESSION → investigate, don't file as noise.
- **Nightly (~21:00-03:30 PT) connectivity flaps**: usfarm/ushmds farm
  drops; the resubscribe retry timer + watchdog self-heal. Judge by
  recovery evidence, not by line count (100-line storms have been noise;
  a single silent child has been the real incident).
- **A resting ENTRY order while a child is flat is LEGITIMATE**
  (marketable-limit entry born non-marketable when price ran; entry TTL
  cancels it at the next signal bar, or it fills at the modeled price —
  both fine). Orphaned BRACKET orders are the disease; entry orders are
  not orphans.
- **ALL children stale simultaneously + log silent** = the fleet is DOWN
  or the machine slept — check `Get-CimInstance Win32_Process` for
  fleet_runner and the log tail BEFORE any per-child theory. A clean
  "Received signal 2 → Shutdown complete" cascade = DELIBERATE operator
  stop: do NOT restart against operator intent; Telegram + report, and
  state whether positions are bracket-protected server-side (they are,
  if TP/SL verified on the last recovery).
- **Suite sentinels**: a fixed set of config-pin tests red from the
  operator's intentional model swaps / config removals (ES01B family et
  al.) — enumerate the failure set before and after your change; only
  DELTAS you caused are yours. Never "fix" the sentinels.

## Environment & tooling rules (each cost real time once)

- AGENT-run project python (pytest, fleet_health, Telegram, DB scripts)
  goes via `conda run -n trader python ...`: in the agent's shell, bare
  `python` is a minimal global interpreter without dotenv. This rule is
  about the agent's environment, NOT the operator's — the operator's
  terminal `python` is the Anaconda base env (fully provisioned; the
  fleet itself runs on it). pytest specifically should ALWAYS use the
  trader env (the suite is validated against its pandas 1.5.3 pin).
  `conda run` buffers output until exit: fine for one-shots, WRONG for
  the live fleet — the fleet launch command is deliberately plain
  `python` so the operator sees streaming console output
  (`--live-stream` exists if conda-run streaming is ever needed).
- The agent CANNOT (permission-blocked, by design): stop/start/signal the
  fleet process, write to the live telemetry DB, or open even read-only
  broker sessions. These are operator actions — when one is needed,
  Telegram + report with exactly what to run, and do not retry the
  blocked call.
- Multi-line git commit messages: write the message to a file with a
  no-BOM writer and use `git commit -F <file>` — PowerShell 5.1 mangles
  quoted heredocs into pathspecs, and `Set-Content -Encoding utf8` writes
  a BOM that corrupts the commit subject (breaks `git log --grep` on
  ticket IDs).
- Reading the telemetry DB is always safe read-only via
  `sqlite3.connect("file:...?mode=ro", uri=True)`.
- Log timestamps are PT wall clock; bar/DB timestamps are UTC; heartbeat
  `last_bar` ages are hours. IBKR's realized PnL is per trading day
  (resets 17:00 ET) and net of commissions — it will NEVER equal the
  per-session gross ledger sums.

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
3. **Commit** on the current fleet working branch:
   `fix(<TICKET_ID>): <summary>` — include the event_id in the body.
   Deploy-then-commit is the ideal ordering; when the deploy (fleet
   restart) is operator-gated, committing first with "deploy pending
   operator restart" stated in the body is the accepted convention
   (64ccccb precedent) — never claim DEPLOYED before it happened.
4. Audit lines:
   `... | MONITOR | DEPLOYED — fleet restarted, all children healthy`
   `... | MONITOR | COMMITTED <sha> on <branch>`

### 5. Close out
- Move the event JSON from `processing/` to `done/`.
- Audit line: `... | <event_id> | MONITOR | DONE — <resolution: fixed|infra|escalated>`
- Notify the human with a short summary (event, root cause, fix, commit sha).
  Optionally push a Telegram note:
  `conda run -n trader python -c "from src.live_execution.utils.telegram_alert import TelegramAlerter; TelegramAlerter(prefix='FLEET-AI').send('<summary>')"`
  (MUST be the trader env — global python lacks dotenv. Telegram parses
  Markdown: underscores in module paths / snake_case break the send with
  a 400 "can't parse entities" — avoid them in message text.)

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
