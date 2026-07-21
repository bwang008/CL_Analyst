# TDD Result — heartbeat-bar-log-collision_07212026_0815

**Outcome:** GREEN. Full fast suite `conda run -n trader python -m pytest tests/ -m "not slow"`
→ **2446 passed, 1 skipped, 0 failed** (380s). Target file `tests/test_heartbeat_phase.py`
→ 31/31 passed.

## Problem
Console `alive` heartbeat lines meshed with the `NEW 5M BAR` burst: heartbeats fired
at `(per-child offset mod 300s)` past each 5-min boundary (offsets 0,5,10,15,20s from
`fleet_runner`), while IBKR delivers bars at ~T+5s — so the offset-0 (`:00`) and
offset-5 (`:05`) children printed on top of the bar burst.

## Fix (two-line source change, one file)
`src/live_execution/live_trader.py`:
1. New module constant `_HEARTBEAT_GRID_DELAY = 15.0` (after `_HEARTBEAT_MIN_SLEEP`,
   now ~line 157) — shifts the entire wall-clock heartbeat GATE 15s past each boundary
   (the daily rollover check + stale-bar watchdog ride the same gate, harmlessly).
   Applied on top of the per-child phase; total (delay+phase) maxes at 90s (16
   instances), inside the 300s interval.
2. Sole consumer in `_event_loop` (~line 5702):
   `hb_offset = _HEARTBEAT_GRID_DELAY + float(getattr(self, "_heartbeat_offset", 0.0))`.

Result: children now fire at `:15,:20,:25…` — a clean `alive` block after the bar burst.

## Tests
`tests/test_heartbeat_phase.py` (Strict-Locked, edited by TDD-Tester only):
- Ghost-imported `_HEARTBEAT_GRID_DELAY` (the Red trigger).
- 3 `TestEventLoopWallClockHeartbeat` mechanism guards neutralized with
  `patch.object(lt_module, "_HEARTBEAT_GRID_DELAY", 0.0)`, original fire numbers
  (`1202.0`/`1502.0`/`1902.0`/`2102.0`) preserved.
- New policy test `test_grid_delay_shifts_whole_fleet_past_boundary`: phase-0 child from
  `FakeTime(1000.0)` fires at **1215.0** (boundary 1200 + 15s); pins constant == 15.0.
- `TestDeadlineMath`, `TestRunnerOffsetAssignment`, `TestCliWiring` unchanged/green.

## Files changed
- `src/live_execution/live_trader.py` (+11 / -1)
- `tests/test_heartbeat_phase.py` (+51 / -3)

## Deploy note
Uncommitted. Like the 07-19 heartbeat-rotation ship, this is inert until the next
`fleet_runner` restart. Post-restart canary: watch a mid-hour reconnect / a fresh
5-min boundary and confirm the `alive` block now prints ~:15 past, separated from the
`NEW 5M BAR` burst.

## Untouched (by design)
`fleet_runner.py` offset assignment, `cli.py` plumbing, grid-math functions,
`fleet_health._HEARTBEAT_RE` parser.
