# Ticket Resolution Blueprint — heartbeat-bar-log-collision_07212026_0815
**Ticket Directory:** `.agents/collab/tickets/heartbeat-bar-log-collision_07212026_0815/`

## Bug Summary
The console log meshes the periodic `alive` heartbeat lines in with the `NEW 5M BAR`
data-bar lines, making the fleet log hard to read. Five (up to 16) `live_trader`
instances run via `fleet_runner.py`.

**Root cause:** The `alive` heartbeat is *wall-clock anchored*. Each child fires at
`(offset mod 300s)` seconds past every 5-minute boundary. `fleet_runner.py` assigns
enabled instance `i` an offset of `HEARTBEAT_OFFSET_SPACING_SECONDS (=5.0) * i` =
`0, 5, 10, 15, 20…s` ([fleet_runner.py:333](src/live_execution/fleet_runner.py#L333)),
plumbed via `--heartbeat-offset` → `cli.py` → `LiveTrader._heartbeat_offset`, and
consumed at exactly **one** point:
[live_trader.py:5692](src/live_execution/live_trader.py#L5692)
(`hb_offset = float(getattr(self, "_heartbeat_offset", 0.0))`).

Meanwhile IBKR delivers 5-min bars at ~T+5s, so `NEW 5M BAR`
([live_trader.py:4788](src/live_execution/live_trader.py#L4788)) bursts around `:05`.
The offset-0 child fires at `:00` (just before the burst) and the offset-5 child at
`:05` (inside it) — the exact collision in the user's log. The grid math is correct;
its base phase is simply `0s`, sitting on top of the bar burst instead of after it.

**Fix intent:** Shift the whole heartbeat grid ~15s past each 5-min boundary so the
fleet's `alive` block prints as a clean, separate block *after* the bar burst clears,
while preserving wall-clock anchoring and the fleet rotation order/spacing.

**Review status:** Auditor severity LOW; NOT fast-tracked because the fix modifies the
recently-shipped (07-19, commit `e9dd50a`) heartbeat grid. Impact-Reviewer
independently mapped the blast radius and **APPROVED** — no Interface / Base-Class /
Refactor rule triggered.

## Target Files
- `src/live_execution/live_trader.py` (production change — the entire fix)
- `tests/test_heartbeat_phase.py` (test footprint — TDD companion)

## Required Changes

### 1. `src/live_execution/live_trader.py` — add a base grid-delay constant
Add a new module-level constant immediately after `_HEARTBEAT_MIN_SLEEP` (~line 149).
Value: **`15.0`** seconds. Name: `_HEARTBEAT_GRID_DELAY`.

The accompanying comment MUST state that this constant shifts the entire heartbeat
**gate** (not merely the `alive` line) — the same gate also runs the once-per-UTC-day
contract-rollover check and the stale-bar watchdog, which harmlessly ride the +15s
shift. Also document: it is applied *on top of* each child's per-child phase
(`fleet_runner --heartbeat-offset`); `total = delay + phase` must stay within
`_HEARTBEAT_INTERVAL`; at the 16-instance ceiling that is `15 + 15*5 = 90s`,
comfortably inside 300s; and that bars land ~T+5s so 15s clears the burst.

### 2. `src/live_execution/live_trader.py` — apply the delay at the sole consumer
At the single offset-consumption site (~[line 5692](src/live_execution/live_trader.py#L5692)),
add `_HEARTBEAT_GRID_DELAY` to the per-child offset when computing `hb_offset`, i.e.
`hb_offset = _HEARTBEAT_GRID_DELAY + float(getattr(self, "_heartbeat_offset", 0.0))`.
This is the **only** production logic line that changes.

**Do NOT touch:** `fleet_runner.py` offset assignment / the `HEARTBEAT_OFFSET_SPACING_SECONDS`
value, the `--heartbeat-offset` CLI plumbing in `cli.py`, the grid-math functions
(`_initial_heartbeat_deadline` / `_advance_heartbeat_deadline` / `_heartbeat_sleep` —
their signatures and bodies stay identical; they merely receive a larger offset value),
and `fleet_health._HEARTBEAT_RE` (it parses line *text*, not firing phase — confirmed
independent).

### 3. `tests/test_heartbeat_phase.py` — keep mechanism guards pure, add one policy test
- `TestDeadlineMath`, `TestRunnerOffsetAssignment`, `TestCliWiring` must stay green
  unchanged (pure functions with explicit offsets; `fleet_runner`/`cli.py` untouched).
- The 3 `TestEventLoopWallClockHeartbeat` tests assert exact fire times derived from
  `_heartbeat_offset` and would otherwise shift by +15s. Neutralize the new policy in
  those 3 with `patch.object(<live_trader_module>, "_HEARTBEAT_GRID_DELAY", 0.0)` so
  they remain pure *mechanism* guards (wall-clock anchoring / stall-skip /
  no-rephase-on-reconnect) with their original numbers.
- **Add one new test** asserting that with the delay active (default `15.0`) and a
  per-child offset of `0`, the first heartbeat fires at `boundary + 15s` — the minimal
  guard for the new policy.

### Expected behavior after fix
Children fire at `:15, :20, :25…` past each 5-min boundary — a clean `alive` block
starting ~10s after the ~`:05` bar burst clears. The `_HEARTBEAT_GRID_DELAY` constant
is the single tuning knob if IBKR bar delivery ever runs later.

### Deployment note (for the operator, not the coder)
The heartbeat grid is inert until fleet restart (per the 07-19 rotation ship). This
timing change likewise takes effect only after the next `fleet_runner` restart.

## Handoff
Ready for `/tdd-manager` with **Ticket ID `heartbeat-bar-log-collision_07212026_0815`**.
