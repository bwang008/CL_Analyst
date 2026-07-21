"""
Wall-clock-anchored console heartbeat (ticket: fleet heartbeat rotation).

The fleet's `alive |` heartbeats used to be phased by each child's event-loop
entry time (poll_count % 60 gate), so the 30s launch stagger never survived
startup and the phases drifted / re-phased on reconnect. The new contract:

  - fleet_runner assigns enabled instance i a static phase offset of
    HEARTBEAT_OFFSET_SPACING_SECONDS * i, passed as --heartbeat-offset
    (restarts rebuild the same command -> a child keeps its slot);
  - cli.py plumbs the flag into LiveTrader(heartbeat_offset=...);
  - LiveTrader._event_loop fires the heartbeat when the SHARED system
    clock crosses (t - offset) % _HEARTBEAT_INTERVAL == 0: the final poll
    sleep is shortened to wake AT the deadline (jitter << 5s spacing), a
    late fire SKIPS missed ticks (no burst) and rejoins its slot, and
    reconnects do NOT re-phase the schedule.

Companion to the Strict-Locked tests/test_fleet_runner.py (command-shape
assertions there are membership-based, so the appended flag is additive).

TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: module-level _HEARTBEAT_GRID_DELAY (=15.0) applied at the
    sole offset-consumption site in LiveTrader._event_loop
    (hb_offset = _HEARTBEAT_GRID_DELAY + self._heartbeat_offset)
Ticket: heartbeat-bar-log-collision_07212026_0815
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.live_execution.live_trader as lt_module
from src.live_execution.fleet_runner import (
    HEARTBEAT_OFFSET_SPACING_SECONDS,
    FleetRunner,
)
from src.live_execution.live_trader import (
    _HEARTBEAT_GRID_DELAY,
    _HEARTBEAT_INTERVAL,
    _HEARTBEAT_MIN_SLEEP,
    _POLL_INTERVAL,
    _advance_heartbeat_deadline,
    _heartbeat_sleep,
    _initial_heartbeat_deadline,
)

# =============================================================================
# HELPERS (modeled on tests/test_fleet_runner.py, which is Strict-Locked)
# =============================================================================


def write_strategy_config(dir_path, name, client_id):
    path = dir_path / f"{name}.json"
    path.write_text(json.dumps(
        {"strategy_name": name, "live_config": {"client_id": client_id}}
    ))
    return path


def write_manifest(dir_path, instances):
    path = dir_path / "fleet_manifest.json"
    path.write_text(json.dumps({
        "instances": instances,
        "stagger_seconds": 30,
        "data_port": 4002,
        "exec_port": 4002,
    }))
    return path


class FakeLauncher:
    def __init__(self):
        self.cmds = []
        self.procs = []

    def popen(self, cmd, *args, **kwargs):
        self.cmds.append([str(c) for c in cmd])
        proc = MagicMock(name=f"proc{len(self.procs)}")
        proc.poll.return_value = None
        proc.pid = 20000 + len(self.procs)
        self.procs.append(proc)
        return proc

    def sleep(self, seconds):
        pass


def launched_runner(tmp_path, entries):
    """entries: list of (name, client_id, enabled). Returns (runner, launcher)."""
    instances = []
    for name, cid, enabled in entries:
        cfg = write_strategy_config(tmp_path, name, cid)
        instances.append(
            {"config": str(cfg), "enabled": enabled, "extra_args": []}
        )
    manifest = write_manifest(tmp_path, instances)
    launcher = FakeLauncher()
    runner = FleetRunner(manifest_path=str(manifest),
                         popen=launcher.popen, sleep=launcher.sleep)
    runner.load_manifest()
    runner.validate()
    runner.launch_all()
    return runner, launcher


def offset_of(cmd):
    return cmd[cmd.index("--heartbeat-offset") + 1]


# =============================================================================
# 1. FLEET RUNNER — static offset assignment at spawn
# =============================================================================


class TestRunnerOffsetAssignment:

    def test_offsets_follow_manifest_order_in_5s_steps(self, tmp_path):
        _, launcher = launched_runner(tmp_path, [
            ("strat_a", 1400, True),
            ("strat_b", 1402, True),
            ("strat_c", 1404, True),
        ])
        assert HEARTBEAT_OFFSET_SPACING_SECONDS == 5.0
        assert [offset_of(c) for c in launcher.cmds] == ["0", "5", "10"]

    def test_disabled_instances_do_not_consume_offsets(self, tmp_path):
        """Offsets pack the burst by ENABLED index — a parked instance
        leaves no 5s hole in the rotation."""
        _, launcher = launched_runner(tmp_path, [
            ("strat_a", 1400, True),
            ("strat_b", 1402, False),
            ("strat_c", 1404, True),
        ])
        assert [offset_of(c) for c in launcher.cmds] == ["0", "5"]

    def test_restart_preserves_offset(self, tmp_path):
        """A crashed child must rejoin the rotation at its OWN slot."""
        runner, launcher = launched_runner(tmp_path, [
            ("strat_a", 1400, True),
            ("strat_b", 1402, True),
        ])
        launcher.procs[1].poll.return_value = 1  # second child crashed
        runner.poll_once()
        assert len(launcher.cmds) == 3
        assert offset_of(launcher.cmds[2]) == "5"

    def test_flag_precedes_extra_args(self, tmp_path):
        """The runner-owned flag must come before manifest extra_args so an
        operator override in extra_args wins under argparse last-wins."""
        cfg = write_strategy_config(tmp_path, "strat_a", 1400)
        manifest = write_manifest(tmp_path, [{
            "config": str(cfg), "enabled": True,
            "extra_args": ["--dry-run"],
        }])
        launcher = FakeLauncher()
        runner = FleetRunner(manifest_path=str(manifest),
                             popen=launcher.popen, sleep=launcher.sleep)
        runner.load_manifest()
        runner.validate()
        runner.launch_all()
        cmd = launcher.cmds[0]
        assert cmd.index("--heartbeat-offset") < cmd.index("--dry-run")


# =============================================================================
# 2. DEADLINE ARITHMETIC (pure functions)
# =============================================================================


class TestDeadlineMath:

    @pytest.mark.parametrize("offset", [0.0, 5.0, 10.0, 295.0])
    @pytest.mark.parametrize("now", [0.0, 1000.0, 12345.6, 1e9])
    def test_initial_deadline_is_next_on_phase_tick(self, now, offset):
        d = _initial_heartbeat_deadline(now, offset)
        assert now < d <= now + _HEARTBEAT_INTERVAL
        assert (d - offset) % _HEARTBEAT_INTERVAL == pytest.approx(
            0.0, abs=1e-6
        )

    def test_on_time_fire_advances_one_interval(self):
        assert _advance_heartbeat_deadline(1202.0, 1202.0) == 1502.0

    def test_slightly_late_fire_advances_one_interval(self):
        assert _advance_heartbeat_deadline(1202.0, 1212.0) == 1502.0

    def test_stalled_fire_skips_missed_ticks_and_keeps_phase(self):
        """700s stall past a 1502 deadline: skip 2 ticks -> 2102, still on
        the offset-2 grid, strictly in the future — no catch-up burst."""
        d = _advance_heartbeat_deadline(1502.0, 2202.0)
        assert d == 2402.0
        assert d > 2202.0
        assert (d - 2.0) % _HEARTBEAT_INTERVAL == 0.0

    def test_sleep_is_poll_interval_when_deadline_far(self):
        assert _heartbeat_sleep(1000.0, 1202.0) == _POLL_INTERVAL

    def test_sleep_shortens_to_wake_at_deadline(self):
        assert _heartbeat_sleep(1200.0, 1202.0) == 2.0

    def test_sleep_never_below_floor(self):
        assert _heartbeat_sleep(1202.0, 1202.0) == _HEARTBEAT_MIN_SLEEP
        assert _heartbeat_sleep(1300.0, 1202.0) == _HEARTBEAT_MIN_SLEEP


# =============================================================================
# 3. EVENT LOOP — wall-clock firing (fake time; no real sleeping)
# =============================================================================


class FakeTime:
    """Stand-in for the `time` module inside live_trader: sleeping advances
    the clock instantly."""

    def __init__(self, start):
        self.now = float(start)

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_loop_trader(fire_log, fake, stop_after):
    """Bare trader wired like the Strict-Locked loop harnesses
    (tests/test_hourly_order_housekeeping.py)."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._running = True
    lt._needs_restart = False
    lt._restart_count = 0
    lt.data_client = MagicMock()
    lt.exec_client = MagicMock()
    lt._telegram = MagicMock()
    lt._reconnect = MagicMock(return_value=True)
    lt._check_contract_rollover = MagicMock()
    lt._check_stale_bars = MagicMock(return_value=False)
    lt._run_hourly_housekeeping = MagicMock()
    lt._attempt_pending_roll_resolution = MagicMock()

    def _fire():
        fire_log.append(fake.now)
        if len(fire_log) >= stop_after:
            lt._running = False

    lt._log_heartbeat = MagicMock(side_effect=_fire)
    return lt


class TestEventLoopWallClockHeartbeat:

    def test_fires_on_phase_with_shortened_final_sleep(self):
        """offset=2, clock starts at 1000 -> ticks at exactly 1202, 1502:
        anchored to the wall clock, NOT to loop entry, and the final sleep
        shortens (5s grid alone can never land on ...02)."""
        fake = FakeTime(1000.0)
        fires, sleeps = [], []
        lt = make_loop_trader(fires, fake, stop_after=2)
        lt._heartbeat_offset = 2.0

        def _sleep(seconds):
            sleeps.append(seconds)
            fake.sleep(seconds)

        lt.data_client.sleep = _sleep

        with patch.object(lt_module, "time", fake), patch.object(
            lt_module, "_HEARTBEAT_GRID_DELAY", 0.0
        ):
            lt._event_loop()

        assert fires == [1202.0, 1502.0]
        assert 2.0 in sleeps, (
            "the pre-deadline sleep was never shortened — firing would be "
            "up to a full poll interval late, jitter >= the 5s spacing"
        )

    def test_stall_skips_missed_ticks_and_rejoins_slot(self):
        """A 700s stall after the first tick: exactly one (late) fire, then
        the schedule realigns to the offset-2 grid — no catch-up burst."""
        fake = FakeTime(1000.0)
        fires = []
        lt = make_loop_trader(fires, fake, stop_after=3)
        lt._heartbeat_offset = 2.0
        stall = {"armed": False}

        def _fire_then_arm():
            fires.append(fake.now)
            if len(fires) == 1:
                stall["armed"] = True
            if len(fires) >= 3:
                lt._running = False

        lt._log_heartbeat = MagicMock(side_effect=_fire_then_arm)

        def _sleep(seconds):
            if stall["armed"]:
                stall["armed"] = False
                fake.sleep(700.0)
            else:
                fake.sleep(seconds)

        lt.data_client.sleep = _sleep

        with patch.object(lt_module, "time", fake), patch.object(
            lt_module, "_HEARTBEAT_GRID_DELAY", 0.0
        ):
            lt._event_loop()

        assert fires[0] == 1202.0
        assert fires[1] == 1902.0   # late fire: once, off-phase, no burst
        assert fires[2] == 2102.0   # missed 1502/1802 skipped; slot regained
        assert (fires[2] - 2.0) % _HEARTBEAT_INTERVAL == 0.0

    def test_reconnect_does_not_rephase_schedule(self):
        """One dropped-connection poll must not move the next tick (the old
        poll_count reset re-phased the heartbeat on every reconnect)."""
        fake = FakeTime(1000.0)
        fires = []
        lt = make_loop_trader(fires, fake, stop_after=1)
        lt._heartbeat_offset = 2.0
        connected = {"ok": False}
        lt.data_client.is_connected = lambda: connected["ok"]
        lt.exec_client.is_connected = lambda: True

        def _reconnect():
            fake.sleep(30.0)  # reconnect takes real time
            connected["ok"] = True
            return True

        lt._reconnect = MagicMock(side_effect=_reconnect)
        lt.data_client.sleep = fake.sleep

        with patch.object(lt_module, "time", fake), patch.object(
            lt_module, "_HEARTBEAT_GRID_DELAY", 0.0
        ):
            lt._event_loop()

        assert lt._reconnect.call_count == 1
        assert fires == [1202.0], (
            "the first tick moved — reconnect re-phased the wall-clock "
            "schedule"
        )

    def test_grid_delay_shifts_whole_fleet_past_boundary(self):
        """POLICY guard (go-forward): the module-level _HEARTBEAT_GRID_DELAY
        shifts the ENTIRE heartbeat gate 15s past every 5-min boundary so the
        fleet's `alive` block prints after the ~T+5s NEW-5M-BAR burst clears.

        Delay ACTIVE (default 15.0, deliberately NOT patched away) on top of a
        phase-0 child: from a 1000.0 clock the first (and only) fire must land
        at boundary 1200 + 15 = 1215.0, i.e. _initial_heartbeat_deadline with
        an EFFECTIVE offset of 15 = (floor((1000-15)/300)+1)*300 + 15."""
        assert lt_module._HEARTBEAT_GRID_DELAY == 15.0, (
            "the go-forward policy default drifted — bars land ~T+5s, so the "
            "gate must sit +15s past the boundary to clear the bar burst"
        )
        fake = FakeTime(1000.0)
        fires, sleeps = [], []
        lt = make_loop_trader(fires, fake, stop_after=1)
        lt._heartbeat_offset = 0.0

        def _sleep(seconds):
            sleeps.append(seconds)
            fake.sleep(seconds)

        lt.data_client.sleep = _sleep

        with patch.object(lt_module, "time", fake):
            lt._event_loop()

        assert fires == [1215.0], (
            "the grid delay did not shift the phase-0 child to +15s past the "
            "5-min boundary — the alive block would collide with the bar burst"
        )


# =============================================================================
# 4. CLI PLUMBING (source-level, matching the repo's inspect conventions —
#    importing cli.main would need live IBKR infra)
# =============================================================================


class TestCliWiring:

    def test_cli_defines_and_forwards_heartbeat_offset(self):
        src = (Path(__file__).resolve().parents[1]
               / "src" / "live_execution" / "cli.py").read_text(
                   encoding="utf-8")
        assert '"--heartbeat-offset"' in src, (
            "cli.py does not define --heartbeat-offset — the fleet runner's "
            "spawn command would crash argparse"
        )
        assert "heartbeat_offset=args.heartbeat_offset" in src, (
            "cli.py parses --heartbeat-offset but never forwards it to "
            "LiveTrader — every child would silently run at phase 0"
        )
