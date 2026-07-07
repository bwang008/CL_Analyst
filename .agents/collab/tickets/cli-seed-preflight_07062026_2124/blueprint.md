# Ticket Resolution Blueprint — cli-seed-preflight_07062026_2124
**Ticket Directory:** `.agents/collab/tickets/cli-seed-preflight_07062026_2124/`

## Bug Summary
The fleet runner validates per-symbol data prerequisites before launch
(`FleetRunner._check_requirement` / `validate_data_prerequisites`,
`src/live_execution/fleet_runner.py:321-380`) and prints an actionable remedy on a
missing/stale seed. The **single-instance CLI**
(`python -m src.live_execution.cli --config ...`) has **no such preflight** — it
constructs `LiveTrader` directly and dies with a raw traceback in
`LiveTrader.__init__` (`live_trader.py:525`). This is exactly the failure the
operator hit tonight for `SI01B` (missing 1h seed): a cryptic crash instead of the
clear, self-healing message the fleet path gives.

**Root cause:** the readiness preflight lives only in the fleet supervisor, not in
the shared launch path, so direct-CLI launches bypass it.

## Target Files
- `src/live_execution/fleet_runner.py` — extract the preflight into a reusable
  function (candidate home: `src/live_execution/data_manager.py`, alongside the
  Ticket-1 materialize helper).
- `src/live_execution/cli.py` — `main()` (~line 309): invoke the shared preflight
  BEFORE constructing `LiveTrader`.
- `src/live_execution/live_trader.py` — optionally call the shared preflight at the
  top of `__init__` so ANY construction path is covered.
- `tests/test_fleet_preflight.py` — extend, or add `tests/test_cli_preflight.py`.

## Required Changes
1. **Extract** the fleet runner's data-prerequisite preflight (seed/cache/macro
   existence + freshness) into a single shared function usable by both the fleet
   runner and the CLI, with no behavior change for the fleet path (regression-guard
   the existing fleet preflight tests).
2. **Wire the CLI** so a direct launch runs the same readiness check up front and
   either (a) with `live-seed-1h-loud-materialize_07062026_2124` landed, loudly
   auto-materializes the seed, or (b) fails fast with the same actionable remedy
   message — never a raw `LiveTrader.__init__` traceback.
3. **Consistency:** the CLI and fleet paths must produce identical readiness
   verdicts and messages for the same config (single source of truth).

## Dependencies / Coordination
- **Depends on `live-seed-1h-loud-materialize_07062026_2124`** for the shared
  helper — land that first (or land the extracted preflight here and have Ticket 1
  add the materialize branch to it). Sequence with the TDD-Manager accordingly.

## Severity
LOW / enhancement (not a bug in live behavior) — but high operator-experience value;
it is the exact gap that turned tonight's missing-seed into a cryptic crash.
