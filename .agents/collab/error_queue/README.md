# .agents/collab/error_queue — Fleet Crash → AI Triage Queue

File-based push queue connecting `fleet_runner.py` (producer) to the AI
error-resolution pipeline (consumer). No polling of logs, no HTTP, no broker:
the runner writes one structured JSON event per unique crash; a scheduled
watcher hands events to the agent, which works them through
`/ticket-manager` → `/tdd-manager` → deploy → commit.

Full agent protocol: [`.agents/skills/fleet-error-monitor/SKILL.md`](../../skills/fleet-error-monitor/SKILL.md)

## Lifecycle

```
fleet_runner.py crash detection
        │  writes JSON (atomic tmp+rename, deduped by traceback hash)
        ▼
pending/                    ← new events land here
        │  scripts/error_watcher.ps1  (hourly cron at :06 — after the
        │  hourly inference bar + first 5m bar have completed)
        ├── classification == "infrastructure" → moved straight to done/
        │      + audit_log.md line. NO agent investigation (known IBKR
        │      connectivity signatures from infra_patterns.json).
        ▼
processing/                 ← agent is actively working the event
        │  /ticket-manager → blueprint → /tdd-manager → tests → deploy →
        │  commit (see SKILL.md; audit_log.md line is MANDATORY)
        ▼
done/                       ← resolved events (audit trail)
```

## Event schema (v1)

```json
{
  "schema_version": 1,
  "event_id": "<model_name>_<hash12>",
  "timestamp": "2026-07-05T21:00:00+00:00",
  "last_seen": "2026-07-05T22:00:00+00:00",
  "occurrences": 3,
  "model_name": "HS14B_Sharpe_E01_06262026",
  "config_path": "configs/strategies/HS14B_Sharpe_E01_06262026.json",
  "client_id": 1400,
  "exit_code": 1,
  "restart_count": 2,
  "gave_up": false,
  "traceback": "Traceback (most recent call last): ...",
  "traceback_hash": "a1b2c3d4e5f6",
  "classification": "infrastructure | unknown",
  "matched_infra_pattern": "gateway-unreachable | null",
  "fleet_manifest_path": "configs/fleet/fleet_manifest.json",
  "stderr_log_path": "reports/fleet_stderr/<model_name>.stderr.log"
}
```

`classification` is assigned at write time by matching the traceback against
`infra_patterns.json`. `"unknown"` means "needs agent investigation" — it is
NOT an error state.

## Deduplication

The event filename is `<model_name>_<traceback_hash>.json`, where the hash is
computed over the traceback with volatile tokens (hex addresses, datetimes,
whitespace) stripped, so a crash loop produces ONE event:

- same hash already in `pending/` → the event is updated in place
  (`occurrences`, `restart_count`, `gave_up`, `last_seen`)
- same hash in `processing/` → skipped (agent is already on it)
- same hash only in `done/` → a NEW event is created — a recurrence after a
  supposed fix must re-open investigation.

## Files

| File | Tracked in git | Purpose |
|---|---|---|
| `infra_patterns.json` | yes (whitelisted) | Growable collection of known infra signatures |
| `audit_log.md` | yes | Append-only log of every resolution — reviewed by the human later |
| `pending/`, `processing/`, `done/` `*.json` | no (`*.json` gitignored) | Runtime events |

Audit-log line format (append-only, one line per state change):

```
[TIMESTAMP UTC] | <event_id> | <ROLE> | <message>
```
