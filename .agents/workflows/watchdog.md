---
name: watchdog
description: The hourly live-fleet health + error-queue check (cron `6 * * * *`). Runs the queue pump, fleet_health, and the read-only broker audit, triages every finding against the accumulated noise patterns in .agents/skills/fleet-error-monitor/SKILL.md, appends an audit line, and reports to the operator with a PT datetime prefix. Read-only by default; naked/untracked positions are a human gate.
---

# /watchdog — Hourly Live-Fleet Health Check

Run the fleet supervision cycle. The job is **"the fleet is HEALTHY"**, not "the
fleet hasn't died" — four children once sat alive-but-blind for 17 minutes with
ERROR lines streaming while a crash-only monitor saw nothing.

**Read `.agents/skills/fleet-error-monitor/SKILL.md` FIRST.** It is the authority
for triage rules, the known daily noise patterns, the per-event protocol, and the
hard limits. This workflow is the runbook; the SKILL is the law. Where they
disagree, the SKILL wins.

## Hard limits (do not violate — these are permission-blocked or human-gated)

- **Broker access is READ-ONLY** (`broker_audit` uses clientId 626,
  `readonly=True`). **NEVER** place, cancel, or modify an order. Not even to fix a
  naked position — that is the operator's call, always.
- **Never write the live telemetry DB.** Reads are `mode=ro` only. If a DB
  correction is needed, hand the operator the SQL; do not run it.
- **You cannot stop/start/signal the fleet.** Deploy = fleet restart = operator
  action. Commit with "deploy pending operator restart" in the body if needed.
- **Never edit the working tree while a cloud batch is in flight** (code is zipped
  at optimizer-deploy time).
- **HUMAN GATE — stop and report** on: naked/untracked/ambiguous positions,
  anything touching order routing / trading economics / model selection, a
  multi-component refactor, or the same event recurring after 2 fix attempts.
- **NO CHEAP FIXES.** No `try/except: pass`, no silent null defaults, no loosened
  tests, no blind retries to mask a deterministic bug. If the only fix you can
  find is on that list, that IS the human gate.

## The live fleet (authority: `configs/fleet/fleet_manifest.json`, port 4002)

| Brain symbol | client_id | Executes | Model config |
|---|--:|---|---|
| CL | 1400 | CL | `HS14B_Sharpe_E01_06262026` |
| ES | 2010 | **MES** (micro) | `ES02B_Sharpe_E01_07112026` |
| NG | 3000 | NG | `NG01B_Sharpe_E03_07052026` |
| GC | 4010 | **MGC** (micro) | `GC02B_Sharpe_E04_07102026` |
| SI | 5000 | **SIL** (micro) | `SI01B_Sharpe_E02_07062026` |

Exec session = `client_id + 1`. Three models trade **micro** contracts while the
DB stores the **brain** symbol — never value PnL at the brain multiplier.
Re-read the manifest each run; the operator changes client_ids when swapping
models, and a stale map is how you misread a retired id as a live one.

## Step 1 — Queue pump

```bash
powershell -File scripts/error_watcher.ps1
```

Auto-files known-infrastructure events, moves the rest to `processing/`.
Outcomes: `NO_EVENTS` / `NO_AGENT_EVENTS (infra-only pass)` / `INFRA_FILED: <id>`
/ `MALFORMED_EVENT: <path>` / `=== N EVENT(S) FOR AGENT INVESTIGATION ===`.

## Step 2 — Health check (read-only)

```bash
conda run -n trader python -m src.live_execution.fleet_health
```

Scans the fleet log for new ERROR/CRITICAL, flags `subs_lost` heartbeats, checks
positions/orders in the DB, and checks bars are arriving. Prints `HEALTH_OK` or
`HEALTH_EVENT: <kind> | <who> | <detail>` plus a `HEALTH_SUMMARY` count.

## Step 3 — Broker audit (read-only broker TRUTH)

```bash
conda run -n trader python -m src.live_execution.broker_audit
```

This closes `fleet_health`'s blind spot: the DB check only confirms the ledger
*carries* an `sl_order_id`, never that the order is really **resting**, so a
silently cancelled stop reads as protected. Prints `BROKER_OK` per position /
`BROKER_EVENT: naked-position | <sym>/<expiry> | <detail>` / `BROKER_SUMMARY` /
`BROKER_UNAVAILABLE:` (gateway down — report and move on, the fleet may be
intentionally stopped; not a fault). Always exits 0.

**`BROKER_EVENT: naked-position` is the highest-severity line in this workflow.**
Verify it isn't a momentary post-fill gap (see the recipe below), then alert the
operator NOW. Do not place the stop yourself.

## Step 4 — Triage

Route on what Steps 1–3 printed:

- **`NO_EVENTS`/`NO_AGENT_EVENTS`/`INFRA_FILED` + only known-benign health + 0
  naked** → report in a few lines, append the audit line, stop. This is the
  common case; do not manufacture work.
- **`MALFORMED_EVENT`** → report to the operator; files stay in `pending/`.
- **`N EVENT(S) FOR AGENT INVESTIGATION`** → follow the SKILL's per-event
  protocol for each event in `.agents/collab/error_queue/processing/`, oldest
  first. `event_kind: "health"` = alive-but-degraded child, not a crash.
- **`HEALTH_EVENT` lines** → SKILL's "Health-event triage" section, but check
  **"Known recurring patterns" FIRST**.
- **`BROKER_EVENT: naked-position`** → diagnose, Telegram, escalate. Never fix.

### Known-benign baseline — expect these EVERY run, do not re-investigate

Report them as a count, not a paragraph. Anything **beyond** this set is real:

- **`stale-bars | ES/2000`** and **`stale-bars | GC/4000`** — **retired
  client_ids**. `fleet_health` enumerates every distinct `client_id` in
  `market_bars`, so the rows those old sessions left behind are frozen forever
  and age one hour per hour. The live ES/GC are **2010/4010**. A stale-bars line
  for an id **in the manifest table above** is real; 2000/4000 are not.
- **`missing-fill-price | NG/3000 | EXECUTE order_id=19 ... 2026-07-07`** — a
  long-adjudicated nag on one historical row.
- Weekend/holiday stale-bars while the market is closed.

### Known recurring patterns (verify recovery, one audit line, move on)

Each of these earned hours of investigation once. Do not repeat it. **All are
noise only if recovery is confirmed** — a child that does NOT come back is a real
event.

- **~14:15 PT daily gateway restart** — IBKR restarts the Gateway during the
  5–6pm ET futures halt. Signature: `Peer closed connection` → `Connection lost:
  Socket disconnect` → `ConnectionRefusedError(... 1225)` "Make sure API port is
  open" → Error 366 resubscribe churn, on all 5 children at once, recovering in
  ~30s. Expect `market=CLOSED (daily halt 5-6pm ET)` heartbeats alongside it.
- **~15:00 PT reopen** — the watchdog false-fire here was FIXED by **65e26d4**
  (GLOBEX `session_open_anchor`). If a stale-bars-watchdog fires at the reopen
  now, that is a **REGRESSION** → investigate, do not file as noise.
- **Nightly ~21:00–03:30 PT connectivity flaps** — usfarm/ushmds farm drops:
  `10182 (disconnected)` → `366` → `1100 (connectivity lost)` → **`1102 ...
  restored - data maintained. All data farms are connected`** → trailing `162`
  resubscribe churn. Judge by **recovery evidence, not line count** — 100-line
  storms have been noise; a single silent child has been the real incident.
- **Error 366 / 162 clusters** — the resubscribe cycle cancelling stale requests.
  Noise whenever adjacent to a known reconnect/reopen.
- **ES/MES daily equity-index halt ~13:15–13:30 PT** — per-symbol calendar;
  energy/metals keep trading. Expected `market=CLOSED`.
- **A resting ENTRY order while a child is flat is LEGITIMATE** (a marketable
  limit born non-marketable when price ran). Orphaned **bracket** orders are the
  disease; entry orders are not orphans.
- **ALL children stale + log silent** = fleet DOWN or machine asleep. Check the
  process (`Get-CimInstance Win32_Process`) and the log tail **before** any
  per-child theory. A clean `Received signal 2 → Shutdown complete` cascade is a
  **deliberate operator stop** — never auto-restart against operator intent.
- **Suite sentinels** — a fixed set of config-pin tests are red from the
  operator's intentional model swaps. Enumerate failures before and after your
  change; only DELTAS you caused are yours. Never "fix" the sentinels.

### The recovery-verification recipe (this is what turns 120 findings into "noise")

A flap/restart dumps ~50–120 `log-error` findings. Do **not** read them all and
do **not** trust `broker_audit` as proof the children recovered — it connects
with its **own** clientId (626), so it proves the *gateway* is up, not that any
child reconnected. Prove recovery per child from the log:

```bash
LOG="reports/fleet/fleet_$(date +%Y%m%d).log"   # PT-dated; NOT under C:\CL_Analyst_Data
# 1. every child reconnected?
grep -iE "Reconnected successfully|1102.*restored" "$LOG" | tail -12
# 2. latest heartbeat per ACTIVE child — want: alive, fresh last_bar, connected=True
for cid in 1400 2010 3000 4010 5000; do
  echo -n "cid=$cid: "; grep "cid=$cid" "$LOG" | grep -i HEARTBEAT | tail -1
done
# 3. silence after the burst ended (substitute the real window)
grep -E "2026-07-14 14:1[6-9]|2026-07-14 14:2[0-9]" "$LOG" | grep -iE "\[ERROR\]|CRITICAL|Traceback"
```

Noise **iff**: all 5 children show reconnect/`1102 restored`, every active child's
latest heartbeat is `connected=True` with a fresh `last_bar`, **and** (3) returns
empty. Then it's one audit line, no ticket, no Telegram. If one child is missing
from (1) or silent in (2) — that is the real incident; that asymmetry is the
whole point of checking per child.

### Position changes are normal — report, don't alarm

Entries/exits/flips between runs are the fleet working. The only question that
matters is **`BROKER_SUMMARY: ... 0 naked`**. A new position with a resting stop
is healthy; note it in one clause and move on.

## Step 5 — Audit line (mandatory, every run)

Append one line to `.agents/collab/error_queue/audit_log.md`:

```
[<ISO8601 UTC>] | health-check-<HH:MM>PT-<Day> | MONITOR | <VERDICT>. <evidence>. broker_audit(626) = <positions> — N positions, M naked. Watcher <result>. Health = N (<benign breakdown>). No new events.
```

Verdicts: `HEALTHY` / `INFRA <pattern>` / `INVESTIGATING —` / `ESCALATED —` /
`DONE —`. Carry the *evidence*, not just the verdict — this file is the memory
that stops the next run from re-investigating a solved thing.

> **Append gotcha (this WILL bite you):** the file is not strictly
> chronological, and `Edit` fails if your `old_string` isn't unique. Anchor on
> the **unique trailing phrase of the actual most-recent line** (find it with
> `grep -n "<today>" audit_log.md | tail -3`), not on a generic phrase like
> "No new events." which repeats on every line.

## Step 6 — Report to the operator

Lead with the PT datetime and the verdict — they want "is it fine?" answered in
the first line.

```markdown
## YYYY-MM-DD HH:MM PT (Day) — <one-line verdict>

- **Step 1 watcher:** `NO_EVENTS`.
- **Step 3 broker truth:** <sym ±N (stop id)> ... — **N positions, 0 naked, all protected.**
- **All 5 children** live, market OPEN, connected; no CRITICAL/Traceback/restart.
- **Health = N, all known-benign:** ES/2000 + GC/4000 false stale-bars (retired client_ids) + NG/19 fill-price nag.

<one line: what changed this hour, or "nothing to act on">
```

Keep a quiet hour to a few lines. Spend words only on what changed or what the
operator must decide.

## Escalation (naked / untracked / ambiguous position)

1. **Diagnose before alerting** — confirm it is not a momentary post-fill gap.
   Reconstruct from the log: what happened to that child's position and its
   protective legs? Confirm the ledger side read-only:
   ```python
   # active_positions.status CLOSED + a live broker position = UNTRACKED divergence
   sqlite3.connect("file:C:/CL_Analyst_Data/data/fleet_telemetry.db?mode=ro", uri=True)
   ```
2. **Audit lines**: `INVESTIGATING —`, then `ROOT CAUSE —`, then `ESCALATED —`.
3. **Telegram the operator NOW** (trader env — global python lacks dotenv):
   ```python
   from src.live_execution.utils.telegram_alert import TelegramAlerter
   TelegramAlerter(prefix="FLEET-AI").send(msg)
   ```
   Telegram parses Markdown: **underscores and asterisks in module paths /
   snake_case break the send** with a 400 "can't parse entities". Write plain
   prose. Say what is exposed, since when, the exact TWS action, and that the
   agent will not place orders.
4. **Leave the events in `processing/`** pending the operator. Move to `done/`
   only on resolution, with a closing audit line.

Auto-heal context: the child's :15 sweep re-places a genuinely-missing **tracked**
leg from the ledger. So `housekeeping-naked-position` means the heal was
**deferred or failed** — still unprotected, still needs a human.
`untracked`/`ambiguous`/`unknown-order` are **detect-only by design** (no ledger
prices to heal from) — never auto-place/cancel/close those.

## Environment gotchas (each cost real time once)

- **Project python is `conda run -n trader python ...`** — the agent's bare
  `python` is a minimal global interpreter without dotenv. (The operator's
  terminal `python` is Anaconda base, which is what the fleet itself runs on —
  this rule is about the agent's shell, not theirs.)
- **`conda run` cannot take a multi-line `-c` script** — it asserts
  `Support for scripts where arguments contain newlines not implemented`. Write
  the script to a scratchpad file and run `conda run -n trader python <file>`.
- **The fleet log is `reports/fleet/fleet_YYYYMMDD.log`** (repo-relative,
  PT-dated, one file per day — dated filenames because Windows can't
  rename-rotate an open log). It is **not** under `C:\CL_Analyst_Data\logs\`.
- **Telemetry DB**: `C:\CL_Analyst_Data\data\fleet_telemetry.db`. Read-only via
  `sqlite3.connect("file:...?mode=ro", uri=True)`.
- **Column names bite**: `market_bars` uses `timestamp` (not `bar_time`);
  `trade_ledger` has `symbol` + `client_id` (no `execution_symbol`, no
  `trade_id`). `active_positions` carries `trade_id`/`status`/`sl_order_id`.
- **Log timestamps are PT wall clock; bar/DB timestamps are UTC**; heartbeat
  `last_bar` ages are hours. IBKR realized PnL is per trading day (resets 17:00
  ET) and net of commissions — it will NEVER equal the gross ledger sums.
- **Multi-line git commit messages**: write to a file with a **no-BOM** writer and
  `git commit -F <file>`. PowerShell 5.1 mangles quoted heredocs into pathspecs,
  and `Set-Content -Encoding utf8` writes a BOM that corrupts the subject.
- **Branch**: commit on the operator's CURRENT fleet working branch
  (`git branch --show-current`) — never assume a name, never merge to
  `main`/`development` yourself. If the tree has operator WIP, stage
  file-by-file and leave their files alone.

## Related

- `.agents/skills/fleet-error-monitor/SKILL.md` — the authority (triage law, per-event protocol).
- `.agents/collab/error_queue/README.md` — queue protocol; `infra_patterns.json` — infra signatures.
- `/analyze-trades` — live trade/PnL report (behavioral cross-check, not health).
- `/diagnose` — data-health / feature-drift diagnostics.
