"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/fleet_runner.py
Target Class/Function: FleetRunner (load_manifest / validate / launch_all / poll_once / shutdown)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: multi-config-fleet-runner_07042026_0646

RED-PHASE CONTRACT (blueprint: .agents/collab/tickets/multi-config-fleet-runner_07042026_0646/blueprint.md)

These tests define the public interface the Coder must implement:

    from src.live_execution.fleet_runner import FleetRunner

    runner = FleetRunner(
        manifest_path=<str|Path>,   # path to fleet manifest JSON
        popen=<callable>,           # dependency-injected subprocess.Popen-compatible
        sleep=<callable>,           # dependency-injected time.sleep-compatible
        max_restarts=<int>,         # per-instance restart cap (has a sane default)
    )
    runner.load_manifest()   # parse manifest; raise on missing/invalid required fields
    runner.validate()        # client_id + capacity checks; raise BEFORE any launch
    runner.launch_all()      # spawn one child per ENABLED instance, stagger between
    runner.poll_once()       # single supervision pass: restart dead children w/ backoff
    runner.shutdown()        # terminate() every live child

Project rule (no silent null defaults): every missing required manifest/config
field must RAISE (ValueError or KeyError), never default to None.

Each instance consumes TWO IBKR client ids: cid (data) and cid+1 (exec)
(cli.py lines 276-277), hence the >=2 spacing rule and the 2*N <= 32 capacity rule.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.live_execution.fleet_runner import FleetRunner


# =============================================================================
# HELPERS
# =============================================================================

DATA_PORT = 4002
EXEC_PORT = 4003  # deliberately != DATA_PORT so command assertions are unambiguous
STAGGER = 60


def write_strategy_config(dir_path, name, client_id=1400, include_client_id=True):
    """Write a minimal strategy JSON with live_config.client_id; return its path."""
    payload = {"strategy_name": name, "live_config": {}}
    if include_client_id:
        payload["live_config"]["client_id"] = client_id
    path = dir_path / f"{name}.json"
    path.write_text(json.dumps(payload))
    return path


def make_instance(config_path, enabled=True, extra_args=None):
    return {
        "config": str(config_path),
        "enabled": enabled,
        "extra_args": list(extra_args or []),
    }


def write_manifest(dir_path, instances, stagger_seconds=STAGGER,
                   data_port=DATA_PORT, exec_port=EXEC_PORT, omit=()):
    """Write a fleet manifest JSON; `omit` removes required keys to test validation."""
    manifest = {
        "instances": instances,
        "stagger_seconds": stagger_seconds,
        "data_port": data_port,
        "exec_port": exec_port,
    }
    for key in omit:
        manifest.pop(key)
    path = dir_path / "fleet_manifest.json"
    path.write_text(json.dumps(manifest))
    return path


class FakeLauncher:
    """Records popen/sleep calls in order; never launches real processes."""

    def __init__(self):
        self.events = []   # ("popen", flat_cmd_tuple) | ("sleep", seconds)
        self.procs = []    # MagicMock child processes, in spawn order

    def popen(self, cmd, *args, **kwargs):
        flat = tuple(str(c) for c in cmd)
        self.events.append(("popen", flat))
        proc = MagicMock(name=f"proc{len(self.procs)}")
        proc.poll.return_value = None  # running
        proc.pid = 10000 + len(self.procs)
        proc.args = flat
        self.procs.append(proc)
        return proc

    def sleep(self, seconds):
        self.events.append(("sleep", seconds))

    @property
    def popen_cmds(self):
        return [e[1] for e in self.events if e[0] == "popen"]

    @property
    def sleep_calls(self):
        return [e[1] for e in self.events if e[0] == "sleep"]


def make_runner(manifest_path, **kwargs):
    launcher = FakeLauncher()
    runner = FleetRunner(
        manifest_path=str(manifest_path),
        popen=launcher.popen,
        sleep=launcher.sleep,
        **kwargs,
    )
    return runner, launcher


def load_and_validate(manifest_path, **kwargs):
    """Run the full pre-launch pipeline (load + validate) — no launching."""
    runner, launcher = make_runner(manifest_path, **kwargs)
    runner.load_manifest()
    runner.validate()
    assert launcher.popen_cmds == [], "validation must never spawn processes"
    return runner, launcher


def launched_fleet(tmp_path, cids=(1400, 1402), extra_args_map=None):
    """Build a valid fleet of len(cids) enabled instances and launch it."""
    extra_args_map = extra_args_map or {}
    instances = []
    for i, cid in enumerate(cids):
        cfg = write_strategy_config(tmp_path, f"strat_{i}", client_id=cid)
        instances.append(make_instance(cfg, extra_args=extra_args_map.get(i)))
    manifest = write_manifest(tmp_path, instances)
    runner, launcher = make_runner(manifest)
    runner.load_manifest()
    runner.validate()
    runner.launch_all()
    return runner, launcher


# =============================================================================
# 1. MANIFEST LOADING & VALIDATION (no silent null defaults)
# =============================================================================

class TestManifestValidation:

    @pytest.mark.parametrize("missing_key", [
        "stagger_seconds", "data_port", "exec_port", "instances",
    ])
    def test_missing_required_manifest_field_raises(self, tmp_path, missing_key):
        """Missing top-level required field must RAISE, never default silently."""
        cfg = write_strategy_config(tmp_path, "strat_a", client_id=1400)
        manifest = write_manifest(
            tmp_path, [make_instance(cfg)], omit=(missing_key,),
        )
        runner, _ = make_runner(manifest)
        with pytest.raises((ValueError, KeyError)):
            runner.load_manifest()
            runner.validate()

    def test_instance_entry_missing_config_path_raises(self, tmp_path):
        """An instance entry without a 'config' key must raise."""
        instance = {"enabled": True, "extra_args": []}  # no "config"
        manifest = write_manifest(tmp_path, [instance])
        runner, _ = make_runner(manifest)
        with pytest.raises((ValueError, KeyError)):
            runner.load_manifest()
            runner.validate()

    def test_referenced_strategy_config_file_missing_raises(self, tmp_path):
        """A manifest pointing at a non-existent strategy JSON must raise."""
        ghost = tmp_path / "does_not_exist.json"
        manifest = write_manifest(tmp_path, [make_instance(ghost)])
        runner, _ = make_runner(manifest)
        with pytest.raises((ValueError, KeyError, FileNotFoundError)):
            runner.load_manifest()
            runner.validate()


# =============================================================================
# 2. CLIENT ID VALIDATION (fail fast BEFORE launching anything)
# =============================================================================

class TestClientIdValidation:

    def test_config_missing_client_id_raises(self, tmp_path):
        """Strategy config without live_config.client_id must raise (no default cid)."""
        cfg = write_strategy_config(
            tmp_path, "strat_no_cid", include_client_id=False,
        )
        manifest = write_manifest(tmp_path, [make_instance(cfg)])
        runner, launcher = make_runner(manifest)
        with pytest.raises((ValueError, KeyError)):
            runner.load_manifest()
            runner.validate()
        assert launcher.popen_cmds == [], "must fail before launching anything"

    def test_duplicate_client_ids_raise(self, tmp_path):
        cfg_a = write_strategy_config(tmp_path, "strat_a", client_id=1400)
        cfg_b = write_strategy_config(tmp_path, "strat_b", client_id=1400)
        manifest = write_manifest(
            tmp_path, [make_instance(cfg_a), make_instance(cfg_b)],
        )
        runner, launcher = make_runner(manifest)
        with pytest.raises((ValueError, KeyError)):
            runner.load_manifest()
            runner.validate()
        assert launcher.popen_cmds == []

    def test_client_ids_spaced_less_than_two_raise(self, tmp_path):
        """1400 and 1401 collide: instance A consumes 1400 (data) AND 1401 (exec)."""
        cfg_a = write_strategy_config(tmp_path, "strat_a", client_id=1400)
        cfg_b = write_strategy_config(tmp_path, "strat_b", client_id=1401)
        manifest = write_manifest(
            tmp_path, [make_instance(cfg_a), make_instance(cfg_b)],
        )
        runner, launcher = make_runner(manifest)
        with pytest.raises((ValueError, KeyError)):
            runner.load_manifest()
            runner.validate()
        assert launcher.popen_cmds == []

    def test_client_ids_spaced_two_apart_pass(self, tmp_path):
        """1400 and 1402 do not collide (1400/1401 vs 1402/1403) — must validate."""
        cfg_a = write_strategy_config(tmp_path, "strat_a", client_id=1400)
        cfg_b = write_strategy_config(tmp_path, "strat_b", client_id=1402)
        manifest = write_manifest(
            tmp_path, [make_instance(cfg_a), make_instance(cfg_b)],
        )
        # Must NOT raise.
        load_and_validate(manifest)


# =============================================================================
# 3. CAPACITY VALIDATION (2 x N <= 32 connections per gateway)
# =============================================================================

class TestCapacityValidation:

    @staticmethod
    def _fleet_of(tmp_path, n):
        instances = []
        for i in range(n):
            cid = 1400 + 2 * i  # valid >=2 spacing throughout
            cfg = write_strategy_config(tmp_path, f"strat_{i:02d}", client_id=cid)
            instances.append(make_instance(cfg))
        return write_manifest(tmp_path, instances)

    def test_seventeen_enabled_instances_raise(self, tmp_path):
        """17 instances = 34 connections > 32 per gateway — must raise."""
        manifest = self._fleet_of(tmp_path, 17)
        runner, launcher = make_runner(manifest)
        with pytest.raises((ValueError, KeyError)):
            runner.load_manifest()
            runner.validate()
        assert launcher.popen_cmds == []

    def test_sixteen_enabled_instances_pass(self, tmp_path):
        """16 instances = exactly 32 connections — boundary must validate."""
        manifest = self._fleet_of(tmp_path, 16)
        load_and_validate(manifest)  # must NOT raise


# =============================================================================
# 4. LAUNCH ORCHESTRATION (popen + sleep fully mocked — no real processes)
# =============================================================================

class TestLaunchOrchestration:

    def test_one_subprocess_per_enabled_instance_only(self, tmp_path):
        """Disabled instances are skipped; exactly one popen per enabled entry."""
        cfg_a = write_strategy_config(tmp_path, "strat_a", client_id=1400)
        cfg_b = write_strategy_config(tmp_path, "strat_b", client_id=1402)
        cfg_c = write_strategy_config(tmp_path, "strat_c", client_id=1404)
        manifest = write_manifest(tmp_path, [
            make_instance(cfg_a, enabled=True),
            make_instance(cfg_b, enabled=False),   # must be skipped
            make_instance(cfg_c, enabled=True),
        ])
        runner, launcher = make_runner(manifest)
        runner.load_manifest()
        runner.validate()
        runner.launch_all()

        assert len(launcher.popen_cmds) == 2
        launched_configs = " ".join(" ".join(c) for c in launcher.popen_cmds)
        assert str(cfg_a) in launched_configs
        assert str(cfg_c) in launched_configs
        assert str(cfg_b) not in launched_configs, "disabled instance was launched"

    def test_spawn_command_shape(self, tmp_path):
        """Command must run the existing CLI module with config + ports from manifest."""
        cfg = write_strategy_config(tmp_path, "strat_a", client_id=1400)
        manifest = write_manifest(tmp_path, [make_instance(cfg)])
        runner, launcher = make_runner(manifest)
        runner.load_manifest()
        runner.validate()
        runner.launch_all()

        assert len(launcher.popen_cmds) == 1
        cmd = list(launcher.popen_cmds[0])

        assert "-m" in cmd
        assert "src.live_execution.cli" in cmd
        assert "--config" in cmd
        assert cmd[cmd.index("--config") + 1] == str(cfg)
        assert "--data-port" in cmd
        assert cmd[cmd.index("--data-port") + 1] == str(DATA_PORT)
        assert "--exec-port" in cmd
        assert cmd[cmd.index("--exec-port") + 1] == str(EXEC_PORT)

    def test_stagger_sleep_between_consecutive_launches(self, tmp_path):
        """A sleep(stagger_seconds) must occur between every pair of launches."""
        runner, launcher = launched_fleet(tmp_path, cids=(1400, 1402, 1404))

        popen_positions = [
            i for i, e in enumerate(launcher.events) if e[0] == "popen"
        ]
        assert len(popen_positions) == 3
        for a, b in zip(popen_positions, popen_positions[1:]):
            between = launcher.events[a + 1:b]
            assert ("sleep", STAGGER) in between, (
                f"no sleep({STAGGER}) between launch at event {a} "
                f"and launch at event {b}: {launcher.events}"
            )
        # At least N-1 stagger sleeps overall.
        assert launcher.sleep_calls.count(STAGGER) >= 2

    def test_extra_args_appended_to_command(self, tmp_path):
        """Manifest extra_args must be appended to that instance's command only."""
        runner, launcher = launched_fleet(
            tmp_path, cids=(1400, 1402),
            extra_args_map={0: ["--dry-run"]},
        )
        assert len(launcher.popen_cmds) == 2
        first, second = launcher.popen_cmds
        assert "--dry-run" in first
        assert "--dry-run" not in second


# =============================================================================
# 5. SUPERVISION (restart crashed children with capped backoff)
# =============================================================================

class TestSupervision:

    def test_crashed_child_is_restarted(self, tmp_path):
        runner, launcher = launched_fleet(tmp_path, cids=(1400,))
        assert len(launcher.procs) == 1

        launcher.procs[0].poll.return_value = 1  # child died (nonzero exit)
        runner.poll_once()

        assert len(launcher.procs) == 2, "dead child was not restarted"
        # Restarted with the same command (same config instance).
        assert launcher.popen_cmds[1] == launcher.popen_cmds[0]

    def test_restart_uses_backoff_sleep(self, tmp_path):
        """Restarting must invoke the injected sleep with a positive backoff."""
        runner, launcher = launched_fleet(tmp_path, cids=(1400,))
        launcher.events.clear()  # discard launch-phase stagger sleeps

        launcher.procs[0].poll.return_value = 1
        runner.poll_once()

        restart_sleeps = [s for kind, s in launcher.events if kind == "sleep"]
        assert any(s > 0 for s in restart_sleeps), (
            "no positive backoff sleep before restart: "
            f"{launcher.events}"
        )

    def test_restarts_are_capped_no_infinite_hot_loop(self, tmp_path):
        """A child that dies every cycle must stop being restarted at max_restarts."""
        cfg = write_strategy_config(tmp_path, "strat_a", client_id=1400)
        manifest = write_manifest(tmp_path, [make_instance(cfg)])
        runner, launcher = make_runner(manifest, max_restarts=3)
        runner.load_manifest()
        runner.validate()
        runner.launch_all()

        for _ in range(20):  # far more supervision cycles than the cap
            for proc in launcher.procs:
                proc.poll.return_value = 1  # always dead
            runner.poll_once()

        # 1 original launch + at most max_restarts restarts.
        assert 2 <= len(launcher.popen_cmds) <= 1 + 3, (
            f"expected between 1 restart and the cap of 3; "
            f"got {len(launcher.popen_cmds) - 1} restarts"
        )


# =============================================================================
# 6. SHUTDOWN (terminate all live children)
# =============================================================================

class TestShutdown:

    def test_shutdown_terminates_all_live_children(self, tmp_path):
        runner, launcher = launched_fleet(tmp_path, cids=(1400, 1402))
        assert len(launcher.procs) == 2

        runner.shutdown()

        for proc in launcher.procs:
            assert proc.terminate.called, (
                f"live child pid={proc.pid} did not receive terminate()"
            )

    def test_shutdown_spawns_nothing_new(self, tmp_path):
        """Shutdown must never launch or restart children."""
        runner, launcher = launched_fleet(tmp_path, cids=(1400, 1402))
        n_before = len(launcher.popen_cmds)

        launcher.procs[0].poll.return_value = 1  # one child already dead
        runner.shutdown()

        assert len(launcher.popen_cmds) == n_before
