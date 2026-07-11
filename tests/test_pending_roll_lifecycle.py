"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._attempt_pending_roll_resolution (NEW),
                       LiveTrader._check_contract_rollover (pending_roll
                       persist-at-detection + immediate resolution attempt),
                       LiveTrader._event_loop (retry wiring)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: jit-roll-ratio-empty_07102026_1453 (Stage 2 — pending_roll
lifecycle; blueprint.md "Stage 2" item 2 + auditor_rca_and_fix_proposal.md)

Every test carries an EXPECTED-RED (new behavior, must fail on current
HEAD) or EXPECTED-GREEN (characterization pin) tag in its docstring.

=============================================================================
LIVE-TRADER API CONTRACT the Coder MUST implement (companion to the
DataManager contract in tests/test_roll_seam_capture.py)
=============================================================================

H. LiveTrader._attempt_pending_roll_resolution() -> None   (no arguments)
   - Reads the pending record via self.data_manager_1h.get_pending_roll();
     record schema: {"from": str, "to": str, "detected_at": NAIVE-local
     ISO str} (see DataManager contract D).
   - No pending record -> NO resolve_roll_seam call, no clear, no alert.
   - Pending present -> calls
         data_manager_1h.resolve_roll_seam(
             from_contract=<rec "from">, to_contract=<rec "to">,
             detected_at=<rec "detected_at">)
     with KEYWORD arguments (detected_at may be passed through as the ISO
     str or a parsed pd.Timestamp — tests accept either).
   - Outcome "RESOLVED" -> data_manager_1h.clear_pending_roll(); when
     self.data_manager_5m is not None, resolve_roll_seam is ALSO attempted
     on the 5m manager (shared metadata file, same execution symbol).
   - Outcome "RETRY" or "ESCALATE" -> clear_pending_roll NOT called; the
     pending record stays for the next attempt. Quiet (no telegram, no
     CRITICAL log) while within the escalation deadline.
   - RETRY GATING (retry-on-new-1h-bar): consecutive calls while
     data_manager_1h.last_timestamp is UNCHANGED must NOT re-invoke
     resolve_roll_seam; a new 1h bar (last_timestamp advanced) re-enables
     exactly one new attempt. Internal gate state MUST be lazily
     initialized (getattr default) — object.__new__ stubs are the
     sanctioned harness and set no private attrs.
   - DEADLINE ESCALATION: when the pending record's detected_at is older
     than the escalation deadline (~3 days; pinned: 4 days MUST escalate,
     2 hours MUST NOT), the method must log.critical on the "LiveTrader"
     logger AND self._telegram.send(...) with an operator remedy; the
     pending record is NOT cleared (an operator must act). detected_at is
     a NAIVE ISO string — the deadline math must not mix tz-aware
     datetimes into the comparison.
   - The method must never raise past its own body into the poll loop.

I. LiveTrader._check_contract_rollover(): after the step-4
   data_manager front_month_id updates, the handler must
       1) persist the roll: data_manager_1h.set_pending_roll(
              <old local symbol>, <new local symbol>)   [POSITIONAL]
       2) immediately attempt resolution (via
          _attempt_pending_roll_resolution or an equivalent path that
          reaches data_manager_1h.resolve_roll_seam), clearing the pending
          record on a RESOLVED outcome and leaving it on RETRY.
   Existing behavior preserved (EXPECTED-GREEN pin): the handler still
   sets data_manager_1h.front_month_id to the new local symbol.

J. Retry wiring: LiveTrader._event_loop's poll body must invoke
   _attempt_pending_roll_resolution OUTSIDE the
   poll_count % _HEARTBEAT_CYCLES block (poll_count resets on every
   reconnect, so a heartbeat-gated retry stalls indefinitely under
   connection flapping — the same rationale as the A-7 housekeeping
   placement). Cadence self-gating comes from the new-1h-bar gate in H,
   not from poll counting.

LIVE-FLEET SAFETY: every I/O boundary is a MagicMock. No test touches a
broker, the telemetry DB, real metadata files, or the error queue.
LiveTrader instances are built with object.__new__ carrying ONLY the seams
each method reads (convention: tests/test_oob_entry_state_recovery.py,
tests/test_hourly_order_housekeeping.py). pandas 1.5.3 compatible.
=============================================================================
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

import src.live_execution.live_trader as lt_module

TICKET_ID = "jit-roll-ratio-empty_07102026_1453"

T1H_A = pd.Timestamp("2026-07-10 13:00:00")
T1H_B = pd.Timestamp("2026-07-10 14:00:00")


def _iso_ago(**kw):
    """NAIVE local ISO stamp `timedelta(**kw)` in the past (DataManager
    contract D: detected_at uses datetime.now().isoformat())."""
    return (datetime.now() - timedelta(**kw)).isoformat()


def _pending(detected_at=None):
    return {
        "from": "GCQ6",
        "to": "GCZ6",
        "detected_at": detected_at if detected_at is not None
        else _iso_ago(hours=2),
    }


def _dm_mock(pending=None, outcome="RESOLVED", last_ts=T1H_A):
    """MagicMock DataManager exposing the Stage-2 seams the trader uses."""
    dm = MagicMock(name="data_manager_1h")
    dm.get_pending_roll.return_value = pending
    dm.resolve_roll_seam.return_value = outcome
    dm.last_timestamp = last_ts
    dm.execution_symbol = "GC"
    return dm


def _attempt_stub(dm1h, dm5m=None):
    """Lightest viable harness for _attempt_pending_roll_resolution: only
    the seams the method may read. NO private gate attrs are pre-set —
    the implementation must lazily initialize its own state (contract H)."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt.data_manager_1h = dm1h
    lt.data_manager_5m = dm5m
    lt._telegram = MagicMock()
    return lt


def _rollover_stub(dm1h, dm5m=None):
    """Flat-position rollover harness (convention:
    tests/test_oob_entry_state_recovery.py::_rollover_stub)."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._last_rollover_check_date = None
    lt._rollover_in_progress = False
    lt.data_client = MagicMock()
    lt.data_client.get_front_month_contract.return_value = ("GCZ6", "202612")
    lt._front_month_local_symbol = "GCQ6"
    lt._front_month_str = "202608"
    lt._front_month_last_close = 68.0
    lt._front_month_bars = None
    lt._subscribe_front_month = MagicMock()
    lt.data_manager_5m = dm5m
    lt.data_manager_1h = dm1h
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = 0
    lt.exec_client.cancel_open_orders.return_value = 0
    lt._telegram = MagicMock()
    strategy = MagicMock()
    lt.strategy = strategy
    lt._strategy = strategy
    lt.telemetry = MagicMock()
    lt._pending_entry_order_id = None
    lt._pending_entry_bar_time = None
    lt._open_orders = {}
    lt._active_trade_id = None
    return lt


# ===========================================================================
# I. _check_contract_rollover — pending_roll persist-at-detection
# ===========================================================================

class TestCheckContractRolloverPendingLifecycle:

    def test_rollover_persists_pending_then_resolves_and_clears(self):
        """EXPECTED-RED (AssertionError: set_pending_roll never called —
        today's handler updates front_month_id in memory and persists
        NOTHING, which is exactly how the NG/CL seams were lost). On
        detection the handler must persist the pending roll FIRST
        (crash-safe), then attempt resolution; a RESOLVED outcome clears
        the pending record."""
        dm1h = _dm_mock(pending=_pending(), outcome="RESOLVED")
        lt = _rollover_stub(dm1h)

        lt._check_contract_rollover()

        dm1h.set_pending_roll.assert_called_once_with("GCQ6", "GCZ6")
        assert dm1h.resolve_roll_seam.called, (
            "the handler must attempt seam resolution immediately after "
            "persisting the pending roll"
        )
        assert dm1h.clear_pending_roll.called, (
            "a RESOLVED outcome must clear the pending record"
        )

    def test_rollover_retry_outcome_leaves_pending_in_place(self):
        """EXPECTED-RED (AssertionError: set_pending_roll never called).
        A RETRY outcome (IBKR CONTFUT basis not flipped yet) must leave
        the persisted pending record for the poll-loop retries — clear
        must NOT be called."""
        dm1h = _dm_mock(pending=_pending(), outcome="RETRY")
        lt = _rollover_stub(dm1h)

        lt._check_contract_rollover()

        dm1h.set_pending_roll.assert_called_once_with("GCQ6", "GCZ6")
        assert not dm1h.clear_pending_roll.called, (
            "RETRY must keep the pending record — clearing it silently "
            "loses the seam forever"
        )

    def test_rollover_still_updates_dm_front_month_id(self):
        """EXPECTED-GREEN (characterization pin of existing behavior the
        new lifecycle builds on): the handler sets
        data_manager_1h.front_month_id to the new local symbol."""
        dm1h = _dm_mock(pending=None)
        lt = _rollover_stub(dm1h)

        lt._check_contract_rollover()

        assert lt.data_manager_1h.front_month_id == "GCZ6"


# ===========================================================================
# H. _attempt_pending_roll_resolution — the retry/escalation seam
# ===========================================================================

class TestAttemptPendingRollResolution:

    def test_no_pending_makes_no_resolve_attempt(self):
        """EXPECTED-RED (AttributeError: _attempt_pending_roll_resolution).
        With no pending record the method is a no-op: no resolve call, no
        clear, no telegram."""
        dm1h = _dm_mock(pending=None)
        lt = _attempt_stub(dm1h)

        lt._attempt_pending_roll_resolution()

        assert not dm1h.resolve_roll_seam.called
        assert not dm1h.clear_pending_roll.called
        assert not lt._telegram.send.called

    def test_resolved_clears_pending_and_passes_contract_kwargs(self):
        """EXPECTED-RED (AttributeError: _attempt_pending_roll_resolution).
        Pending present -> resolve_roll_seam called with KEYWORD arguments
        taken from the record; RESOLVED -> clear_pending_roll called."""
        rec = _pending(detected_at="2026-07-10T02:00:00")
        dm1h = _dm_mock(pending=rec, outcome="RESOLVED")
        lt = _attempt_stub(dm1h)

        lt._attempt_pending_roll_resolution()

        assert dm1h.resolve_roll_seam.call_count == 1
        kwargs = dm1h.resolve_roll_seam.call_args.kwargs
        assert kwargs["from_contract"] == "GCQ6"
        assert kwargs["to_contract"] == "GCZ6"
        # detected_at may pass through as the ISO str or a parsed Timestamp
        assert pd.Timestamp(kwargs["detected_at"]) == pd.Timestamp(
            "2026-07-10T02:00:00"
        )
        dm1h.clear_pending_roll.assert_called_once_with()

    def test_retry_within_deadline_keeps_pending_and_stays_quiet(
        self, caplog
    ):
        """EXPECTED-RED (AttributeError: _attempt_pending_roll_resolution).
        RETRY on a 2-hour-old pending: no clear, no CRITICAL log, no
        telegram (alert spam is reserved for the deadline)."""
        dm1h = _dm_mock(pending=_pending(detected_at=_iso_ago(hours=2)),
                        outcome="RETRY")
        lt = _attempt_stub(dm1h)

        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            lt._attempt_pending_roll_resolution()

        assert dm1h.resolve_roll_seam.called
        assert not dm1h.clear_pending_roll.called
        criticals = [r for r in caplog.records
                     if r.levelno >= logging.CRITICAL]
        assert not criticals, "no CRITICAL before the escalation deadline"
        assert not lt._telegram.send.called

    def test_escalate_outcome_keeps_pending(self):
        """EXPECTED-RED (AttributeError: _attempt_pending_roll_resolution).
        An ESCALATE outcome (nothing to anchor on) must NOT clear the
        pending record — the seam is still unrecorded and an operator or a
        later scan must handle it."""
        dm1h = _dm_mock(pending=_pending(detected_at=_iso_ago(hours=2)),
                        outcome="ESCALATE")
        lt = _attempt_stub(dm1h)

        lt._attempt_pending_roll_resolution()

        assert not dm1h.clear_pending_roll.called

    def test_retry_gated_on_new_1h_bar(self):
        """EXPECTED-RED (AttributeError: _attempt_pending_roll_resolution).
        Retry-on-new-1h-bar: consecutive attempts with an UNCHANGED
        data_manager_1h.last_timestamp must not re-invoke
        resolve_roll_seam (the poll loop runs every ~5s); advancing
        last_timestamp re-enables exactly one new attempt."""
        dm1h = _dm_mock(pending=_pending(detected_at=_iso_ago(hours=2)),
                        outcome="RETRY", last_ts=T1H_A)
        lt = _attempt_stub(dm1h)

        lt._attempt_pending_roll_resolution()
        lt._attempt_pending_roll_resolution()  # same 1h bar — must be gated

        assert dm1h.resolve_roll_seam.call_count == 1, (
            "an unchanged 1h bar must not trigger a second resolve attempt"
        )

        dm1h.last_timestamp = T1H_B  # a new 1h bar arrived
        lt._attempt_pending_roll_resolution()

        assert dm1h.resolve_roll_seam.call_count == 2, (
            "a NEW 1h bar must re-enable a resolve attempt"
        )

    def test_deadline_escalation_logs_critical_and_telegrams(self, caplog):
        """EXPECTED-RED (AttributeError: _attempt_pending_roll_resolution).
        A pending roll unresolved past the ~3-day deadline (4 days old
        here) must escalate LOUDLY: log.critical on the 'LiveTrader'
        logger + telegram with an operator remedy; the pending record is
        NOT cleared. Never a silent skip."""
        dm1h = _dm_mock(pending=_pending(detected_at=_iso_ago(days=4)),
                        outcome="RETRY")
        lt = _attempt_stub(dm1h)

        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            lt._attempt_pending_roll_resolution()

        criticals = [r for r in caplog.records
                     if r.levelno >= logging.CRITICAL]
        assert criticals, (
            "a 4-day-old unresolved pending roll must log CRITICAL"
        )
        assert any("roll" in r.getMessage().lower() for r in criticals), (
            "the CRITICAL message must name the roll so it is actionable"
        )
        assert lt._telegram.send.called, (
            "deadline escalation must attempt a Telegram alert"
        )
        assert not dm1h.clear_pending_roll.called, (
            "escalation must NOT clear the pending record — the seam is "
            "still unrecorded"
        )

    def test_5m_manager_also_attempted_when_present(self):
        """EXPECTED-RED (AttributeError: _attempt_pending_roll_resolution).
        When a 5m manager exists (shared metadata file, same execution
        symbol) its seam must be resolved too — the 5m cache otherwise
        keeps a silent basis break."""
        dm1h = _dm_mock(pending=_pending(detected_at=_iso_ago(hours=2)),
                        outcome="RESOLVED")
        dm5m = _dm_mock(pending=None, outcome="RESOLVED")
        lt = _attempt_stub(dm1h, dm5m=dm5m)

        lt._attempt_pending_roll_resolution()

        assert dm5m.resolve_roll_seam.called, (
            "the 5m manager's seam must also be captured when present"
        )


# ===========================================================================
# J. _event_loop wiring — retry driven from the poll body
# ===========================================================================

class TestEventLoopWiring:

    def test_event_loop_invokes_pending_roll_attempt_outside_heartbeat_gate(
        self,
    ):
        """EXPECTED-RED (AssertionError: call_count == 0 — the loop never
        retries today). With only 3 poll iterations (poll_count never
        reaches _HEARTBEAT_CYCLES=60 — the reconnect-reset regime, since
        poll_count zeroes on every reconnect), the loop must still invoke
        _attempt_pending_roll_resolution while _log_heartbeat provably
        never ran. Cadence self-gating lives in the new-1h-bar gate
        (contract H), NOT in poll counting. Harness convention:
        tests/test_hourly_order_housekeeping.py."""
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
        lt._log_heartbeat = MagicMock()
        lt._run_hourly_housekeeping = MagicMock()
        lt._attempt_pending_roll_resolution = MagicMock()

        polls = {"n": 0}

        def _sleep(_interval):
            polls["n"] += 1
            if polls["n"] >= 3:
                lt._running = False

        lt.data_client.sleep = _sleep

        lt._event_loop()

        assert lt._log_heartbeat.call_count == 0, (
            "harness sanity: 3 iterations must never reach the "
            "poll_count %% _HEARTBEAT_CYCLES block"
        )
        assert lt._attempt_pending_roll_resolution.call_count >= 1, (
            "the event loop never attempted pending-roll resolution "
            "outside the heartbeat gate — a heartbeat-gated (or absent) "
            "retry stalls under reconnect flapping and the seam is lost"
        )
