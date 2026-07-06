"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
                            (module constant _WATCHDOG_TG_COOLDOWN_SECONDS = 3600;
                            _STALE_BAR_THRESHOLD_MINUTES 15 -> 30 per the
                            2026-07-06 user directive;
                            NEW LiveTrader._send_watchdog_telegram(msg) helper —
                            per-instance hourly Telegram throttle for the
                            watchdog family: attempt-consumes-budget, strict-<
                            suppression against the cooldown, INFO suppression
                            log INCLUDING the full suppressed message text,
                            "+N watchdog-family alerts suppressed" consolidation
                            suffix on the next send, never raises (send and each
                            persistence I/O in separate try/except);
                            __init__ derives the per-client_id JSON sidecar
                            _watchdog_tg_state_path =
                            Path(db_path).with_name(f"watchdog_tg_cid{{cid}}.json")
                            immediately after the telemetry identity block when
                            client_id is not None; client_id None -> state path
                            None -> in-memory-only, ZERO disk I/O)
Target Class/Function: live_trader._WATCHDOG_TG_COOLDOWN_SECONDS,
                       live_trader._STALE_BAR_THRESHOLD_MINUTES,
                       LiveTrader._send_watchdog_telegram,
                       LiveTrader._check_stale_bars,
                       LiveTrader.__init__ (_watchdog_tg_state_path)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: watchdog-telegram-throttle_07062026_0007
Phase: RED — at HEAD b947ee6:
  - _WATCHDOG_TG_COOLDOWN_SECONDS does not exist (scenario 1 + every helper
    call raises AttributeError);
  - _STALE_BAR_THRESHOLD_MINUTES is 15, not 30 (scenario 1 pin fails);
  - LiveTrader has no _send_watchdog_telegram and no _watchdog_tg_state_path
    (scenarios 2, 5-12 fail with AttributeError);
  - _check_stale_bars sends Telegram unthrottled on every firing
    (scenarios 3-4 fail on send call counts).

Governing document: blueprint.md (watchdog-telegram-throttle_07062026_0007) §6
scenarios 1-12, transcribed here verbatim — this file does not invent pins.
Log lines are NEVER throttled (directive #3): the suppression path must emit an
INFO record carrying the full suppressed message text.

Conventions (mirroring tests/test_reconnect_recovery_fixes.py):
  - LiveTrader stubs via object.__new__ with only the seams each method reads;
    full LiveTrader construction (mocked DataManager + Path.exists) only where
    the __init__ sidecar derivation itself is the pin (scenarios 6, 11, 12).
  - Clock frozen by patching lt_module.datetime with a datetime subclass
    reading a mutable holder, so tests can advance time across the cooldown
    boundary (no freezegun in this repo).
  - All I/O is tmp_path-local; Telegram is a MagicMock; no gateway, no network.
  - pandas 1.5.3 compatible (no pandas 2.x APIs).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.live_execution import live_trader as lt_module


# ===========================================================================
# Frozen, advanceable clock — Tue 2026-07-07 16:00 UTC == 12:00 ET (CL OPEN,
# same instant as tests/test_reconnect_recovery_fixes.py so the
# _check_stale_bars integration scenarios fire during market hours).
# ===========================================================================

_UTC_T0 = datetime(2026, 7, 7, 16, 0, 0)  # tz-naive UTC "now"


class _Clock:
    """Mutable frozen-clock holder — advance() moves the instant forward."""

    def __init__(self, start: datetime = _UTC_T0):
        self.now_utc = start

    def advance(self, minutes: float) -> None:
        self.now_utc = self.now_utc + timedelta(minutes=minutes)


def _frozen_lt_clock(clock: _Clock):
    """Patch lt_module.datetime so datetime.now(timezone.utc) follows clock."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            if tz is not None:
                return clock.now_utc.replace(tzinfo=timezone.utc).astimezone(tz)
            return clock.now_utc - timedelta(hours=7)  # fabricated local wall

        @classmethod
        def utcnow(cls):  # noqa: D102
            return clock.now_utc

    return patch.object(lt_module, "datetime", _Frozen)


# ===========================================================================
# Stub builders
# ===========================================================================

_UNSET = object()


def _throttle_stub(state_path=_UNSET):
    """Minimal stub with only the seams _send_watchdog_telegram reads.

    state_path semantics:
      - _UNSET: the attribute is NOT set at all (scenario 9 — the helper must
        tolerate object.__new__ stubs via the established getattr seam);
      - None: explicit in-memory-only mode (client_id None path);
      - Path: sidecar persistence enabled.
    """
    trader = object.__new__(lt_module.LiveTrader)
    trader._telegram = MagicMock()
    if state_path is not _UNSET:
        trader._watchdog_tg_state_path = state_path
    return trader


def _watchdog_stub():
    """_check_stale_bars stub (mirrors tests/test_reconnect_recovery_fixes.py).

    No fruitless-counter attribute and no _watchdog_tg_state_path attribute
    are set ON PURPOSE — both must be stub-safe getattr seams."""
    trader = object.__new__(lt_module.LiveTrader)
    trader.data_client = MagicMock()
    trader.exec_client = MagicMock()
    trader._telegram = MagicMock()
    trader._subscriptions_lost = False
    trader._execution_symbol = "CL"
    trader._last_bar_time_5m = pd.Timestamp(_UTC_T0) - pd.Timedelta(minutes=60)
    return trader


class _DummyStrategy:
    """Minimal strategy stub (mirrors tests/test_session_watchdog_rollover.py)."""

    def __init__(self, config: dict):
        self.feature_names = ["MACD"]
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config


def _build_trader(tmp_path: Path, client_id=None):
    """Full LiveTrader construction with mocked DataManager so the REAL
    __init__ sidecar derivation runs (scenarios 6, 11, 12). db_path mirrors
    the shared-fleet layout: ONE <data_root>/fleet_telemetry.db."""
    cfg = {"nickname": "CL_wtg", "execution_symbol": "CL", "bar_size": "1h"}
    with patch("src.live_execution.live_trader.DataManager"), patch(
        "pathlib.Path.exists", return_value=True
    ):
        trader = lt_module.LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=_DummyStrategy(config=cfg),
            db_path=str(tmp_path / "fleet_telemetry.db"),
            dry_run=True,
            client_id=client_id,
        )
    trader._telegram = MagicMock()
    return trader


def _sent_texts(trader) -> list:
    """Message texts passed to _telegram.send (positional or keyword)."""
    out = []
    for c in trader._telegram.send.call_args_list:
        out.append(str(c.args[0]) if c.args else str(c.kwargs.get("message", "")))
    return out


# ===========================================================================
# Scenario 1 — constant pins
# ===========================================================================

class TestConstantPins:
    def test_watchdog_tg_cooldown_constant_3600(self):
        """Scenario 1: the throttle window is a patchable module constant —
        exactly one hour."""
        assert lt_module._WATCHDOG_TG_COOLDOWN_SECONDS == 3600, (
            "_WATCHDOG_TG_COOLDOWN_SECONDS must be a module-level constant "
            "== 3600 (user directive #2: watchdog-family Telegram at most "
            "once per hour per instance)"
        )

    def test_stale_threshold_constant_30(self):
        """Scenario 1: user directive #1 (2026-07-06) — the 5m stale-bar
        threshold moves 15 -> 30 minutes."""
        assert lt_module._STALE_BAR_THRESHOLD_MINUTES == 30, (
            "_STALE_BAR_THRESHOLD_MINUTES must be 30 per the 2026-07-06 "
            "user directive (ticket watchdog-telegram-throttle_07062026_0007)"
        )


# ===========================================================================
# Scenarios 2, 5, 9, 10 — helper semantics, in-memory (no sidecar)
# ===========================================================================

class TestHelperInMemoryThrottle:
    def test_first_fire_fresh_instance_sends_exactly_once(self):
        """Scenario 2: first fire on a fresh instance (state path None) sends
        exactly once, message text passed through."""
        clock = _Clock()
        trader = _throttle_stub(state_path=None)
        with _frozen_lt_clock(clock):
            trader._send_watchdog_telegram("*STALE BAR WATCHDOG* - first fire")
        assert trader._telegram.send.call_count == 1
        assert "*STALE BAR WATCHDOG* - first fire" in _sent_texts(trader)[0]

    def test_send_after_cooldown_carries_suppressed_suffix_and_resets(self):
        """Scenario 5: a fire > 1 hour after the last send goes out with the
        '+N watchdog-family alerts suppressed' suffix; the counter resets so
        the following post-cooldown send has NO suffix."""
        clock = _Clock()
        trader = _throttle_stub(state_path=None)
        with _frozen_lt_clock(clock):
            trader._send_watchdog_telegram("msg1")          # T0: sends
            clock.advance(10)
            trader._send_watchdog_telegram("msg2")          # suppressed
            clock.advance(10)
            trader._send_watchdog_telegram("msg3")          # suppressed
            clock.advance(41)                               # T0 + 61 min
            trader._send_watchdog_telegram("msg4")          # sends + suffix
            clock.advance(61)                               # T0 + 122 min
            trader._send_watchdog_telegram("msg5")          # sends, no suffix

        texts = _sent_texts(trader)
        assert trader._telegram.send.call_count == 3, (
            f"expected sends for msg1/msg4/msg5 only, got: {texts!r}"
        )
        assert "msg1" in texts[0]
        assert "suppressed" not in texts[0], (
            "first send has nothing suppressed behind it — no suffix"
        )
        assert "msg4" in texts[1]
        assert "+2 watchdog-family alerts suppressed" in texts[1], (
            "the post-cooldown send must consolidate the 2 suppressed alerts"
        )
        assert "msg5" in texts[2]
        assert "suppressed" not in texts[2], (
            "the counter must reset to zero after the consolidated send"
        )

    def test_missing_state_path_attr_object_new_stub_in_memory_only(self):
        """Scenario 9: object.__new__ stub WITHOUT _watchdog_tg_state_path —
        the helper must run in-memory-only via the established getattr seam
        pattern, throttle correctly, and never crash."""
        clock = _Clock()
        trader = _throttle_stub()  # attribute deliberately never set
        with _frozen_lt_clock(clock):
            trader._send_watchdog_telegram("*RECONNECT* - stub fire 1")
            clock.advance(10)
            trader._send_watchdog_telegram("*RECONNECT* - stub fire 2")
        assert trader._telegram.send.call_count == 1

    def test_telegram_send_raising_consumes_budget_and_never_raises(self):
        """Scenario 10: telegram.send raising inside the helper — the helper
        returns normally and the ATTEMPT still consumes the hourly budget
        (a Telegram outage must not become per-fire retry spam)."""
        clock = _Clock()
        trader = _throttle_stub(state_path=None)
        trader._telegram.send.side_effect = Exception("telegram down")
        with _frozen_lt_clock(clock):
            trader._send_watchdog_telegram("msg-fail")  # must NOT raise
        assert trader._telegram.send.call_count == 1

        trader._telegram.send.side_effect = None
        with _frozen_lt_clock(clock):
            clock.advance(10)
            trader._send_watchdog_telegram("msg-inside-window")  # suppressed
            assert trader._telegram.send.call_count == 1, (
                "the FAILED attempt must have consumed the budget — a fire "
                "10 min later stays suppressed"
            )
            clock.advance(51)  # T0 + 61 min
            trader._send_watchdog_telegram("msg-after-window")   # sends
        assert trader._telegram.send.call_count == 2


# ===========================================================================
# Scenarios 3, 4 — integration through the real _check_stale_bars
# ===========================================================================

class TestWatchdogIntegrationThrottle:
    def test_second_fire_within_hour_suppressed_but_watchdog_unweakened(
        self, caplog
    ):
        """Scenario 3: second watchdog firing within the hour — NO second
        Telegram send, but an INFO suppression record carries the full
        suppressed message text, and _check_stale_bars STILL returns True
        and STILL disconnects (recovery machinery is not weakened)."""
        clock = _Clock()
        trader = _watchdog_stub()
        with _frozen_lt_clock(clock), caplog.at_level(logging.INFO):
            assert trader._check_stale_bars() is True   # fire #1: sends
            clock.advance(5)
            assert trader._check_stale_bars() is True   # fire #2: suppressed

        assert trader._telegram.send.call_count == 1, (
            "the second firing within the cooldown must NOT send"
        )
        assert trader.data_client.disconnect.call_count == 2
        assert trader.exec_client.disconnect.call_count == 2
        suppression = [
            r for r in caplog.records
            if r.levelno == logging.INFO
            and "TELEGRAM SUPPRESSED" in r.getMessage()
        ]
        assert suppression, (
            "suppression must be logged at INFO — log lines are never "
            "throttled (user directive #3)"
        )
        assert any(
            "STALE BAR WATCHDOG" in r.getMessage() for r in suppression
        ), "the INFO suppression record must include the full message text"

    def test_escalation_within_cooldown_exits_with_critical_log(self, caplog):
        """Scenario 4: the 3rd consecutive fruitless firing inside the
        cooldown — SystemExit still raised (non-zero), CRITICAL log still
        emitted, escalation Telegram suppressed + INFO-logged with the
        message text."""
        clock = _Clock()
        trader = _watchdog_stub()
        with _frozen_lt_clock(clock), caplog.at_level(logging.INFO):
            assert trader._check_stale_bars() is True   # fire #1: sends
            clock.advance(5)
            assert trader._check_stale_bars() is True   # fire #2: suppressed
            clock.advance(5)
            with pytest.raises(SystemExit) as excinfo:
                trader._check_stale_bars()              # fire #3: escalates

        assert excinfo.value.code not in (None, 0)
        assert trader._telegram.send.call_count == 1, (
            "the escalation Telegram within the cooldown must be suppressed "
            "— only fire #1 sends"
        )
        assert any(
            r.levelno >= logging.CRITICAL for r in caplog.records
        ), "escalation must still log at CRITICAL (logs are never throttled)"
        assert any(
            r.levelno == logging.INFO
            and "TELEGRAM SUPPRESSED" in r.getMessage()
            and "WATCHDOG ESCALATION" in r.getMessage()
            for r in caplog.records
        ), "the suppressed escalation text must be fully visible at INFO"


# ===========================================================================
# Scenarios 6, 7, 8, 11, 12 — cid-keyed sidecar persistence
# ===========================================================================

class TestSidecarPersistence:
    def test_round_trip_across_simulated_fleet_runner_restart(self, tmp_path):
        """Scenario 6: T0 send writes watchdog_tg_cid{N}.json beside the
        shared db; a NEW instance (simulated fleet_runner restart, same
        cid/db_path) hydrates it — suppresses at T0+30 min, sends at
        T0+61 min."""
        clock = _Clock()
        sidecar = tmp_path / "watchdog_tg_cid1010.json"

        first = _build_trader(tmp_path, client_id=1010)
        assert first._watchdog_tg_state_path == sidecar, (
            "sidecar must be cid-keyed and derived BESIDE the shared "
            "fleet_telemetry.db (Reviewer R1: never from the db stem alone)"
        )
        with _frozen_lt_clock(clock):
            first._send_watchdog_telegram("*STALE BAR WATCHDOG* - t0")
        assert first._telegram.send.call_count == 1
        assert sidecar.exists(), "T0 send must persist the sidecar"
        payload = json.loads(sidecar.read_text())
        assert "last_send_utc" in payload
        assert "suppressed_count" in payload

        # Simulated fleet_runner restart: same cid, same db_path.
        clock.advance(30)
        second = _build_trader(tmp_path, client_id=1010)
        with _frozen_lt_clock(clock):
            second._send_watchdog_telegram("*RECONNECT* - t0+30m")
        second._telegram.send.assert_not_called()

        clock.advance(31)  # T0 + 61 min
        with _frozen_lt_clock(clock):
            second._send_watchdog_telegram("*RECONNECT* - t0+61m")
        assert second._telegram.send.call_count == 1, (
            "the restarted instance must send once the hydrated window "
            "expires (T0+61 min)"
        )

    def test_corrupt_state_file_treated_as_no_state(self, tmp_path):
        """Scenario 7: corrupt sidecar — treated as no-state: the fire
        sends, nothing raises, and the post-send persist rewrites valid
        JSON."""
        clock = _Clock()
        sidecar = tmp_path / "watchdog_tg_cid7.json"
        sidecar.write_text("{not json!! ###")
        trader = _throttle_stub(state_path=sidecar)
        with _frozen_lt_clock(clock):
            trader._send_watchdog_telegram("*STALE BAR WATCHDOG* - corrupt")
        assert trader._telegram.send.call_count == 1
        payload = json.loads(sidecar.read_text())  # rewritten valid
        assert "last_send_utc" in payload
        assert "suppressed_count" in payload

    def test_unwritable_state_path_degrades_to_in_memory(self, tmp_path):
        """Scenario 8: unwritable state path (missing parent directory) —
        the send still succeeds, nothing raises, and the throttle keeps
        working in-memory."""
        clock = _Clock()
        bad_path = tmp_path / "no_such_dir" / "nested" / "watchdog_tg_cid8.json"
        trader = _throttle_stub(state_path=bad_path)
        with _frozen_lt_clock(clock):
            trader._send_watchdog_telegram("*RECONNECTED* - unwritable")
            assert trader._telegram.send.call_count == 1
            assert not bad_path.exists()
            clock.advance(10)
            trader._send_watchdog_telegram("*RECONNECTED* - again")
        assert trader._telegram.send.call_count == 1, (
            "persistence failure must degrade to in-memory-only throttling, "
            "not disable the throttle"
        )

    def test_per_cid_isolation_two_instances_one_data_root(self, tmp_path):
        """Scenario 11: two instances sharing one data root with distinct
        cids get DISTINCT sidecars; A's T0 send must NOT suppress B's first
        send at T0+1 min."""
        clock = _Clock()
        a = _build_trader(tmp_path, client_id=1010)
        b = _build_trader(tmp_path, client_id=1012)
        assert a._watchdog_tg_state_path != b._watchdog_tg_state_path
        assert a._watchdog_tg_state_path.name == "watchdog_tg_cid1010.json"
        assert b._watchdog_tg_state_path.name == "watchdog_tg_cid1012.json"

        with _frozen_lt_clock(clock):
            a._send_watchdog_telegram("*STALE BAR WATCHDOG* - A t0")
            clock.advance(1)
            b._send_watchdog_telegram("*STALE BAR WATCHDOG* - B t0+1m")
        assert a._telegram.send.call_count == 1
        assert b._telegram.send.call_count == 1, (
            "per-instance throttle: A's budget must never suppress B"
        )

    def test_client_id_none_in_memory_only_no_file_anywhere(self, tmp_path):
        """Scenario 12: client_id None (livetest/tests) — state path None,
        the throttle works purely in-memory, NO sidecar file is created
        anywhere under the data root, and nothing raises."""
        clock = _Clock()
        trader = _build_trader(tmp_path, client_id=None)
        assert trader._watchdog_tg_state_path is None
        with _frozen_lt_clock(clock):
            trader._send_watchdog_telegram("*STALE BAR WATCHDOG* - none-cid")
            clock.advance(10)
            trader._send_watchdog_telegram("*STALE BAR WATCHDOG* - again")
        assert trader._telegram.send.call_count == 1
        assert list(tmp_path.rglob("watchdog_tg*")) == [], (
            "client_id None must mean ZERO watchdog-throttle disk I/O"
        )
        assert list(tmp_path.rglob("*.json")) == []
