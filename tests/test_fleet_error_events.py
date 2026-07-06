"""
Tests for the fleet crash → error-event queue pipeline.

Covers src/live_execution/fleet_error_events.py (classification against the
infra-signature collection, traceback-hash deduplication, atomic pending/
writes, gave_up handling, stderr-tail traceback extraction) and its
integration into FleetRunner (stderr sink handed to Popen, event emitted by
poll_once, byte-identical legacy behavior when error_writer is None — the
Strict-Locked supervision tests in test_fleet_runner.py stay authoritative
for that path).
"""

import json

import pytest

from src.live_execution.fleet_error_events import (
    DEFAULT_INFRA_PATTERNS_PATH,
    FleetErrorEventWriter,
    classify_traceback,
    extract_traceback,
    load_infra_patterns,
    traceback_hash,
)
from src.live_execution.fleet_runner import FleetRunner, _Instance

CODE_BUG_TB = (
    "Traceback (most recent call last):\n"
    '  File "src/live_execution/live_trader.py", line 812, in _on_bar\n'
    "    ratio = signal / denominator\n"
    "ZeroDivisionError: float division by zero\n"
)

INFRA_TB = (
    "Traceback (most recent call last):\n"
    '  File "src/live_execution/cli.py", line 300, in main\n'
    "    trader.start()\n"
    "ConnectionRefusedError: [WinError 10061] No connection could be made "
    "because the target machine actively refused it\n"
)


# =============================================================================
# HELPERS
# =============================================================================

def write_patterns(dir_path, patterns=None):
    if patterns is None:
        patterns = [
            {"name": "gateway-unreachable",
             "regex": r"ConnectionRefusedError|\[WinError 10061\]",
             "notes": "test"},
        ]
    path = dir_path / "infra_patterns.json"
    path.write_text(json.dumps({"patterns": patterns}), encoding="utf-8")
    return path


class TelegramRecorder:
    def __init__(self):
        self.messages = []

    def send(self, message, **kwargs):
        self.messages.append(message)
        return True


def make_writer(tmp_path, telegram=None, patterns=None):
    return FleetErrorEventWriter(
        queue_dir=tmp_path / "queue",
        stderr_dir=tmp_path / "stderr",
        manifest_path="configs/fleet/fleet_manifest.json",
        patterns_path=write_patterns(tmp_path, patterns),
        telegram=telegram,
    )


def make_instance(tmp_path, name="strat_a", client_id=1400, restarts=0):
    cfg = tmp_path / f"{name}.json"
    if not cfg.exists():
        cfg.write_text(json.dumps(
            {"strategy_name": name, "live_config": {"client_id": client_id}}
        ))
    inst = _Instance(cfg, [], client_id)
    inst.restarts = restarts
    return inst


def stage_stderr(writer, instance, text):
    path = writer.stderr_path_for(instance.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def pending_files(writer):
    return sorted((writer.queue_dir / "pending").glob("*.json"))


# =============================================================================
# 1. INFRA PATTERN COLLECTION (no silent null defaults)
# =============================================================================

class TestInfraPatterns:

    def test_missing_collection_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_infra_patterns(tmp_path / "nope.json")

    def test_missing_patterns_key_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"not_patterns": []}))
        with pytest.raises(ValueError):
            load_infra_patterns(path)

    def test_entry_missing_regex_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"patterns": [{"name": "x"}]}))
        with pytest.raises(ValueError):
            load_infra_patterns(path)

    def test_broken_regex_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(
            {"patterns": [{"name": "x", "regex": "(unclosed"}]}
        ))
        with pytest.raises(Exception):
            load_infra_patterns(path)

    def test_shipped_repo_collection_loads_and_matches_ibkr(self):
        """The collection that ships in the repo must load and catch the
        canonical IBKR connectivity failures."""
        patterns = load_infra_patterns(DEFAULT_INFRA_PATTERNS_PATH)
        cls, name = classify_traceback(INFRA_TB, patterns)
        assert cls == "infrastructure"
        assert name == "gateway-unreachable"
        cls, _ = classify_traceback(
            "Connectivity between IB and Trader Workstation has been lost",
            patterns,
        )
        assert cls == "infrastructure"

    def test_code_bug_is_unknown_not_infrastructure(self):
        patterns = load_infra_patterns(DEFAULT_INFRA_PATTERNS_PATH)
        cls, name = classify_traceback(CODE_BUG_TB, patterns)
        assert cls == "unknown"
        assert name is None


# =============================================================================
# 2. DEDUP HASH (volatile tokens must not fragment a crash loop)
# =============================================================================

class TestTracebackHash:

    def test_same_crash_different_timestamps_and_addresses_same_hash(self):
        tb_a = ("Traceback (most recent call last):\n"
                "ValueError: no bar since 2026-07-05 10:00:00 "
                "<obj at 0x7f3a2b100> \n")
        tb_b = ("Traceback (most recent call last):\n"
                "ValueError: no bar since 2026-07-05 11:00:00 "
                "<obj at 0x7f3a2c9f8> \n")
        assert traceback_hash("m", tb_a) == traceback_hash("m", tb_b)

    def test_different_exceptions_different_hash(self):
        assert traceback_hash("m", CODE_BUG_TB) != traceback_hash("m", INFRA_TB)

    def test_same_traceback_different_model_different_hash(self):
        assert (traceback_hash("model_a", CODE_BUG_TB)
                != traceback_hash("model_b", CODE_BUG_TB))


# =============================================================================
# 3. TRACEBACK EXTRACTION FROM THE STDERR SINK
# =============================================================================

class TestExtractTraceback:

    def test_last_traceback_block_wins(self, tmp_path):
        path = tmp_path / "s.stderr.log"
        path.write_text(
            "2026-07-05 old log line\n" + INFRA_TB + "restarting...\n"
            + CODE_BUG_TB,
            encoding="utf-8",
        )
        tb = extract_traceback(path)
        assert tb.startswith("Traceback (most recent call last):")
        assert "ZeroDivisionError" in tb
        assert "ConnectionRefusedError" not in tb

    def test_no_marker_falls_back_to_tail(self, tmp_path):
        path = tmp_path / "s.stderr.log"
        path.write_text("fatal: killed by watchdog\n", encoding="utf-8")
        tb = extract_traceback(path)
        assert "no Python traceback marker" in tb
        assert "killed by watchdog" in tb

    def test_missing_file_placeholder(self, tmp_path):
        assert extract_traceback(tmp_path / "ghost.log") \
            == "<no stderr captured>"


# =============================================================================
# 4. EVENT EMISSION (schema, dedup, gave_up, telegram, never-raise)
# =============================================================================

REQUIRED_EVENT_FIELDS = (
    "schema_version", "event_id", "timestamp", "last_seen", "occurrences",
    "model_name", "config_path", "client_id", "exit_code", "restart_count",
    "gave_up", "traceback", "traceback_hash", "classification",
    "matched_infra_pattern", "fleet_manifest_path", "stderr_log_path",
)


class TestEmitCrashEvent:

    def test_emits_valid_event_with_all_required_fields(self, tmp_path):
        writer = make_writer(tmp_path)
        inst = make_instance(tmp_path, restarts=1)
        stage_stderr(writer, inst, CODE_BUG_TB)

        path = writer.emit_crash_event(inst, exit_code=1, gave_up=False)

        assert path is not None and path.exists()
        assert path.parent.name == "pending"
        event = json.loads(path.read_text(encoding="utf-8"))
        for field in REQUIRED_EVENT_FIELDS:
            assert field in event, f"event missing required field {field}"
        assert event["model_name"] == "strat_a"
        assert event["client_id"] == 1400
        assert event["exit_code"] == 1
        assert event["restart_count"] == 1
        assert event["gave_up"] is False
        assert event["classification"] == "unknown"
        assert "ZeroDivisionError" in event["traceback"]
        assert not list(path.parent.glob("*.tmp")), "tmp file left behind"

    def test_infra_traceback_classified_with_pattern_name(self, tmp_path):
        writer = make_writer(tmp_path)
        inst = make_instance(tmp_path)
        stage_stderr(writer, inst, INFRA_TB)

        path = writer.emit_crash_event(inst, exit_code=1, gave_up=False)

        event = json.loads(path.read_text(encoding="utf-8"))
        assert event["classification"] == "infrastructure"
        assert event["matched_infra_pattern"] == "gateway-unreachable"

    def test_crash_loop_deduplicates_into_one_updated_event(self, tmp_path):
        writer = make_writer(tmp_path)
        inst = make_instance(tmp_path)
        stage_stderr(writer, inst, CODE_BUG_TB)

        writer.emit_crash_event(inst, exit_code=1, gave_up=False)
        inst.restarts = 3
        writer.emit_crash_event(inst, exit_code=1, gave_up=False)

        files = pending_files(writer)
        assert len(files) == 1, "crash loop must produce ONE pending event"
        event = json.loads(files[0].read_text(encoding="utf-8"))
        assert event["occurrences"] == 2
        assert event["restart_count"] == 3

    def test_different_crash_produces_second_event(self, tmp_path):
        writer = make_writer(tmp_path)
        inst = make_instance(tmp_path)
        stage_stderr(writer, inst, CODE_BUG_TB)
        writer.emit_crash_event(inst, exit_code=1, gave_up=False)

        stage_stderr(writer, inst, INFRA_TB)  # NEW last traceback
        writer.emit_crash_event(inst, exit_code=1, gave_up=False)

        assert len(pending_files(writer)) == 2

    def test_event_in_processing_is_not_requeued(self, tmp_path):
        writer = make_writer(tmp_path)
        inst = make_instance(tmp_path)
        stage_stderr(writer, inst, CODE_BUG_TB)
        path = writer.emit_crash_event(inst, exit_code=1, gave_up=False)
        # Watcher hands the event to the agent:
        path.rename(writer.queue_dir / "processing" / path.name)

        result = writer.emit_crash_event(inst, exit_code=1, gave_up=False)

        assert result is None
        assert pending_files(writer) == []

    def test_recurrence_after_done_creates_new_event(self, tmp_path):
        writer = make_writer(tmp_path)
        inst = make_instance(tmp_path)
        stage_stderr(writer, inst, CODE_BUG_TB)
        path = writer.emit_crash_event(inst, exit_code=1, gave_up=False)
        path.rename(writer.queue_dir / "done" / path.name)

        result = writer.emit_crash_event(inst, exit_code=1, gave_up=False)

        assert result is not None, "recurrence after done/ must re-open"
        assert len(pending_files(writer)) == 1

    def test_gave_up_flag_recorded_and_telegram_notified(self, tmp_path):
        tg = TelegramRecorder()
        writer = make_writer(tmp_path, telegram=tg)
        inst = make_instance(tmp_path, restarts=5)
        stage_stderr(writer, inst, CODE_BUG_TB)

        path = writer.emit_crash_event(inst, exit_code=1, gave_up=True)

        event = json.loads(path.read_text(encoding="utf-8"))
        assert event["gave_up"] is True
        assert len(tg.messages) == 1
        assert "restart cap exhausted" in tg.messages[0]

    def test_gave_up_transition_on_dedup_update_notifies(self, tmp_path):
        tg = TelegramRecorder()
        writer = make_writer(tmp_path, telegram=tg)
        inst = make_instance(tmp_path)
        stage_stderr(writer, inst, CODE_BUG_TB)

        writer.emit_crash_event(inst, exit_code=1, gave_up=False)
        inst.restarts = 5
        path = writer.emit_crash_event(inst, exit_code=1, gave_up=True)

        event = json.loads(path.read_text(encoding="utf-8"))
        assert event["gave_up"] is True
        # first send on creation + second on the gave_up transition
        assert len(tg.messages) == 2

    def test_emit_never_raises_even_on_corrupt_pending_file(self, tmp_path):
        writer = make_writer(tmp_path)
        inst = make_instance(tmp_path)
        stage_stderr(writer, inst, CODE_BUG_TB)
        tb_hash = traceback_hash(inst.name, extract_traceback(
            writer.stderr_path_for(inst.name)))
        corrupt = writer.queue_dir / "pending" / f"{inst.name}_{tb_hash}.json"
        corrupt.write_text("{not json", encoding="utf-8")

        # Must swallow the JSONDecodeError — the supervisor must survive.
        assert writer.emit_crash_event(inst, exit_code=1, gave_up=False) \
            is None

    def test_missing_patterns_file_raises_at_construction(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            FleetErrorEventWriter(
                queue_dir=tmp_path / "queue",
                stderr_dir=tmp_path / "stderr",
                manifest_path="m.json",
                patterns_path=tmp_path / "missing_patterns.json",
            )


# =============================================================================
# 5. FLEETRUNNER INTEGRATION (sink wiring + emission from poll_once)
# =============================================================================

class RecordingLauncher:
    """Fake popen that records kwargs and hands each spawned proc a real
    pipe-like stderr (BytesIO) so the tee pump thread runs for real."""

    def __init__(self):
        self.calls = []       # (cmd, kwargs)
        self.procs = []
        self.next_stderr = b""  # payload the NEXT spawned child emits

    def popen(self, cmd, **kwargs):
        import io
        from unittest.mock import MagicMock
        self.calls.append((tuple(str(c) for c in cmd), kwargs))
        proc = MagicMock(name=f"proc{len(self.procs)}")
        proc.poll.return_value = None
        proc.pid = 20000 + len(self.procs)
        proc.stderr = io.BytesIO(self.next_stderr)
        self.next_stderr = b""
        self.procs.append(proc)
        return proc

    def sleep(self, seconds):
        pass


def launched_runner(tmp_path, writer, max_restarts=5, first_stderr=b"",
                    echo_child_stderr=True, stderr_echo_stream=None):
    cfg = tmp_path / "strat_a.json"
    cfg.write_text(json.dumps(
        {"strategy_name": "strat_a", "live_config": {"client_id": 1400}}
    ))
    manifest = tmp_path / "fleet_manifest.json"
    manifest.write_text(json.dumps({
        "instances": [{"config": str(cfg), "enabled": True, "extra_args": []}],
        "stagger_seconds": 0, "data_port": 4002, "exec_port": 4002,
    }))
    launcher = RecordingLauncher()
    launcher.next_stderr = first_stderr
    runner = FleetRunner(
        manifest_path=str(manifest), popen=launcher.popen,
        sleep=launcher.sleep, max_restarts=max_restarts, error_writer=writer,
        echo_child_stderr=echo_child_stderr,
        stderr_echo_stream=stderr_echo_stream,
    )
    runner.load_manifest()
    runner.validate()
    runner.launch_all()
    return runner, launcher


def join_pump(runner, timeout=2.0):
    pump = runner.instances[0].stderr_pump
    if pump is not None:
        pump.join(timeout=timeout)


class TestFleetRunnerIntegration:

    def test_spawn_tees_stderr_via_pipe_and_pump_thread(self, tmp_path):
        import subprocess
        writer = make_writer(tmp_path)
        runner, launcher = launched_runner(
            tmp_path, writer, first_stderr=b"child stderr line\n",
        )

        _, kwargs = launcher.calls[0]
        assert kwargs.get("stderr") is subprocess.PIPE, \
            "_spawn must capture child stderr through a drained PIPE"
        join_pump(runner)
        sink = writer.stderr_path_for("strat_a")
        assert "child stderr line" in sink.read_text(encoding="utf-8"), \
            "pump must copy the child's stderr into the sink file"

    def test_stderr_echoed_to_console_by_default(self, tmp_path):
        import io
        writer = make_writer(tmp_path)
        console = io.BytesIO()
        runner, launcher = launched_runner(
            tmp_path, writer, first_stderr=b"visible to operator\n",
            stderr_echo_stream=console,
        )

        join_pump(runner)
        assert b"visible to operator" in console.getvalue(), \
            "default mode must TEE child stderr to the console"
        sink = writer.stderr_path_for("strat_a")
        assert "visible to operator" in sink.read_text(encoding="utf-8"), \
            "echo must not steal the line from the sink"

    def test_silent_mode_skips_console_echo_but_keeps_sink(self, tmp_path):
        import io
        writer = make_writer(tmp_path)
        console = io.BytesIO()
        runner, launcher = launched_runner(
            tmp_path, writer, first_stderr=b"quiet line\n",
            echo_child_stderr=False, stderr_echo_stream=console,
        )

        join_pump(runner)
        assert console.getvalue() == b"", "--silent must not echo"
        sink = writer.stderr_path_for("strat_a")
        assert "quiet line" in sink.read_text(encoding="utf-8")

    def test_no_error_writer_keeps_legacy_popen_call(self, tmp_path):
        runner, launcher = launched_runner(tmp_path, writer=None)
        _, kwargs = launcher.calls[0]
        assert "stderr" not in kwargs, (
            "error_writer=None must keep the pre-queue Popen call untouched"
        )

    def test_crash_emits_pending_event_and_restarts(self, tmp_path):
        writer = make_writer(tmp_path)
        runner, launcher = launched_runner(
            tmp_path, writer, first_stderr=CODE_BUG_TB.encode("utf-8"),
        )

        launcher.procs[0].poll.return_value = 1  # child died
        live = runner.poll_once()

        assert live == 1, "child must still be restarted after emission"
        files = pending_files(writer)
        assert len(files) == 1
        event = json.loads(files[0].read_text(encoding="utf-8"))
        assert event["model_name"] == "strat_a"
        assert event["gave_up"] is False
        assert "ZeroDivisionError" in event["traceback"], \
            "poll_once must reap the pump so the traceback tail is flushed"

    def test_restart_cap_exhaustion_emits_gave_up_event(self, tmp_path):
        writer = make_writer(tmp_path)
        runner, launcher = launched_runner(
            tmp_path, writer, max_restarts=1,
            first_stderr=CODE_BUG_TB.encode("utf-8"),
        )

        for _ in range(3):
            for proc in launcher.procs:
                proc.poll.return_value = 1
            runner.poll_once()

        assert runner.instances[0].gave_up is True
        files = pending_files(writer)
        assert len(files) == 1, "crash loop must dedup to one event"
        event = json.loads(files[0].read_text(encoding="utf-8"))
        assert event["gave_up"] is True
        assert event["occurrences"] >= 2

    def test_writer_failure_does_not_kill_supervision(self, tmp_path):
        writer = make_writer(tmp_path)
        runner, launcher = launched_runner(tmp_path, writer)
        # Simulate a broken queue (directory ripped out from under it).
        import shutil
        shutil.rmtree(writer.queue_dir)

        launcher.procs[0].poll.return_value = 1
        live = runner.poll_once()  # must not raise

        assert live == 1, "supervision must continue despite writer failure"
