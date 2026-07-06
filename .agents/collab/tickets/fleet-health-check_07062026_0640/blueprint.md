# Ticket Resolution Blueprint — fleet-health-check_07062026_0640
**Ticket Directory:** `.agents/collab/tickets/fleet-health-check_07062026_0640/`

## Bug Summary
User feedback (2026-07-06): the fleet-error-monitor protocol only reacts to
process CRASHES, which "seldom ever happens so this entire skill is a waste
of time if that's all it does." Today's incident proved it: four children
sat alive-but-blind for 17 minutes with ERROR lines streaming into the
fleet log and the hourly monitor saw nothing. Required: an hourly HEALTH
CHECK that (a) surfaces new ERROR/CRITICAL log lines, (b) verifies bars are
actually arriving, (c) verifies trades/orders line up (no unprotected
positions, no duplicate opens, closed trades have exits), and (d) gives the
monitoring agent a deterministic, greppable report to act on.

## Target Files
- `src/live_execution/fleet_health.py` (NEW — stdlib-only, read-only checks)
- `.agents/skills/fleet-error-monitor/SKILL.md` (protocol expansion)

## Required Changes

### R1 — fleet_health module (NEW)
Pure, individually testable functions + a `main()` orchestrator. Read-only:
the health check must never mutate fleet state (log/db opened read-only;
the only write is its own state file `.agents/collab/error_queue/
health_state.json`).

1. `scan_log_errors(log_path, offset) -> (findings, new_offset)` — reads
   the shared fleet log from byte offset, returns records for every
   `[ERROR]`/`[CRITICAL]` line (and `Traceback` marker lines) with the
   child tag when present. Handles the dated-filename scheme: `main()`
   tracks offsets per filename in the state file, scans today's (and, on
   date rollover, finishes yesterday's) `fleet_YYYYMMDD.log`.
2. `parse_heartbeats(lines) -> {child: {last_bar_age_h, subs_lost,
   position, ts}}` — latest HEARTBEAT per child tag; flags
   `subs_lost=True`.
3. `check_positions(active_rows, ledger_rows) -> findings` — from
   fleet_telemetry.db:
   - OPEN active_positions missing tp_order_id or sl_order_id →
     `unprotected-position` (naked position, highest severity);
   - >1 OPEN row per (symbol, client_id) → `duplicate-open-position`;
   - CLOSED rows missing exit_price or close_reason → `incomplete-close`;
   - trade_ledger EXECUTE rows older than 10 minutes with NULL fill_price →
     `missing-fill-price` (regression signal for the update_fill wiring).
4. `check_bar_freshness(bar_rows, now, threshold_minutes=45) -> findings` —
   max(market_bars.timestamp) per (symbol, client_id) older than threshold
   → `stale-bars` finding carrying the age. (Market-closed judgment is the
   AGENT's job per SKILL.md — the check reports facts.)
5. `main()` — argv: `--log-dir` (default reports/fleet), `--db` (default
   <data root>/fleet_telemetry.db via the existing data-path helper),
   `--queue-dir`. Prints exactly one of:
   - `HEALTH_OK (checked: log, heartbeats, positions, bars)` when clean;
   - one `HEALTH_EVENT: <kind> | <child/symbol> | <detail>` line per
     finding (plus a trailing summary line with counts).
   ALWAYS exits 0 (a health-check failure must not look like a fleet
   failure); internal errors print `HEALTH_CHECK_ERROR: <detail>` and
   still exit 0.

### R2 — SKILL.md expansion
- Document the hourly run as THREE steps: (1) `scripts/error_watcher.ps1`
  (crash queue pump — unchanged), (2) `python -m
  src.live_execution.fleet_health` (health check), (3) triage: crash events
  per the existing protocol; HEALTH_EVENT lines per a new section —
  `unprotected-position` → verify on IBKR immediately and alert the human
  (never place/cancel orders autonomously beyond the documented OCA
  protocol); `stale-bars` → check market hours first, then treat as
  infra/code per evidence; `missing-fill-price`/`incomplete-close` →
  telemetry code bug → normal ticket flow; log-ERROR clusters → judge
  infra vs code, open ticket for code.
- Record the 2026-07-06 lesson: crash-only capture is insufficient; the
  monitor's job is "the bot is healthy", not "the bot hasn't died".

## Test Contract (Strict-Locked)
`tests/test_fleet_health.py` — function names in R1 are contract; tests
drive them with fixture text/rows and a tmp sqlite db + tmp log dir through
`main()`.
