"""
TDD AUTHORIZATION (fast-track, inline — Reviewer skipped per workflow
Step 2.3: LOW severity, not a recent regression)
Target Implementation File: src/live_execution/session_calendar.py
                            (_globex_session_open_anchor — most recent
                             Sun-Thu 17:00 CT open, tz-naive UTC; GLOBEX
                             dispatch branch returns it instead of None)
Target Class/Function: session_calendar._globex_session_open_anchor,
                       session_calendar.session_open_anchor (GLOBEX branch),
                       LiveTrader._check_stale_bars (consumer, unchanged —
                       integration coverage only)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: cl-watchdog-reopen-grace_07052026_0001
Phase: RED — the GLOBEX anchor is None on current HEAD (Q1 pin, retired by
operator authorization 2026-07-07 after the reopen false-positive fired on
schedule two consecutive days: 3-4 stale-bars-watchdog queue events + ~12
Error-366 lines + a needless fleet-wide reconnect at every 17:00 CT reopen).

Conventions: mirrors tests/test_session_watchdog_rollover.py (calendar
vectors via wall-clock helpers, DST covered by paired Jan/Jul instants) and
tests/test_reconnect_recovery_fixes.py (frozen lt_module.datetime, LiveTrader
object.__new__ stubs). Market-status byte-identity is NOT touched by the fix
and stays pinned by the existing sweep tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytz

from src.core.instrument_master import get_instrument
from src.live_execution import live_trader as lt_module
from src.live_execution.session_calendar import (
    market_status,
    session_open_anchor,
)

_CT = pytz.timezone("America/Chicago")
_CL = get_instrument("CL")
_MCL = get_instrument("MCL")
_NG = get_instrument("NG")
_GC = get_instrument("GC")
_SI = get_instrument("SI")


def _utc_from_ct(y, m, d, hh, mm):
    """Wall-clock CT -> tz-naive UTC (mirrors the rollover test helpers)."""
    wall = _CT.localize(datetime(y, m, d, hh, mm))
    return wall.astimezone(pytz.utc).replace(tzinfo=None)


# 2026-07-13 is a Monday; 2026-01-12 is a Monday (CST winter instant).


# ===========================================================================
# Anchor correctness
# ===========================================================================

class TestGlobexAnchor:

    def test_reopen_anchor_is_same_day_open(self):
        """Minutes after the Mon 17:00 CT reopen, the anchor IS that open —
        the exact instant the old None behavior turned into a false fire."""
        t = _utc_from_ct(2026, 7, 13, 17, 3)
        assert session_open_anchor(_CL, t) == _utc_from_ct(2026, 7, 13, 17, 0)

    def test_late_evening_still_same_day_open(self):
        t = _utc_from_ct(2026, 7, 13, 23, 0)
        assert session_open_anchor(_CL, t) == _utc_from_ct(2026, 7, 13, 17, 0)

    def test_mid_session_anchors_to_previous_day_open(self):
        """Tue 10:00 CT belongs to the session opened Mon 17:00 CT."""
        t = _utc_from_ct(2026, 7, 14, 10, 0)
        assert session_open_anchor(_CL, t) == _utc_from_ct(2026, 7, 13, 17, 0)

    def test_friday_session_anchors_to_thursday_open(self):
        """No Friday 17:00 CT open exists — Friday trades on Thu's open."""
        t = _utc_from_ct(2026, 7, 17, 10, 0)
        assert session_open_anchor(_CL, t) == _utc_from_ct(2026, 7, 16, 17, 0)

    def test_sunday_reopen_anchor(self):
        t = _utc_from_ct(2026, 7, 12, 17, 5)
        assert session_open_anchor(_CL, t) == _utc_from_ct(2026, 7, 12, 17, 0)

    def test_saturday_anchor_is_previous_thursday(self):
        """Closed all Saturday (watchdog is market-gated anyway) — the most
        recent open is still Thursday's; the anchor stays well-defined."""
        t = _utc_from_ct(2026, 7, 18, 12, 0)
        assert session_open_anchor(_CL, t) == _utc_from_ct(2026, 7, 16, 17, 0)

    def test_winter_dst_and_tz_naive_utc_contract(self):
        """CST (UTC-6): Mon 2026-01-12 17:00 CT == 23:00 UTC; summer
        (CDT, UTC-5): 17:00 CT == 22:00 UTC. Returned values are tz-naive."""
        t_win = _utc_from_ct(2026, 1, 12, 17, 3)
        a_win = session_open_anchor(_CL, t_win)
        assert a_win == datetime(2026, 1, 12, 23, 0)
        assert a_win.tzinfo is None
        t_sum = _utc_from_ct(2026, 7, 13, 17, 3)
        a_sum = session_open_anchor(_CL, t_sum)
        assert a_sum == datetime(2026, 7, 13, 22, 0)
        assert a_sum.tzinfo is None

    def test_all_globex_instruments_share_the_anchor(self):
        t = _utc_from_ct(2026, 7, 13, 17, 3)
        expected = _utc_from_ct(2026, 7, 13, 17, 0)
        for inst in (_CL, _MCL, _NG, _GC, _SI):
            assert session_open_anchor(inst, t) == expected, inst.symbol

    def test_reopen_status_still_open(self):
        """FENCE: market status is untouched by the fix (byte-identity for
        the OPEN gate stays with the existing sweep pins)."""
        assert market_status(_CL, _utc_from_ct(2026, 7, 13, 17, 3)) == "OPEN"


# ===========================================================================
# Watchdog integration — the grace in _check_stale_bars' own arithmetic
# ===========================================================================

def _frozen_clock(instant_utc_naive):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return instant_utc_naive.replace(tzinfo=tz)
            return instant_utc_naive
    return _Frozen


def _make_watchdog_trader(last_bar_utc_naive):
    lt = object.__new__(lt_module.LiveTrader)
    lt._get_market_status = lambda now: "OPEN"
    lt._enable_5m_stream = True
    lt._last_bar_time_5m = last_bar_utc_naive
    lt._instrument_context = SimpleNamespace(
        brain_symbol="CL", brain_instrument=_CL,
    )
    lt._subscriptions_lost = False
    lt._fruitless_reconnect_count = 0
    lt._send_watchdog_telegram = MagicMock()
    lt._emit_health_event = MagicMock()
    lt.data_client = MagicMock()
    lt.exec_client = MagicMock()
    return lt


class TestWatchdogReopenGrace:

    def test_watchdog_graced_at_reopen(self, monkeypatch):
        """Last bar is pre-halt (70 min old) but the session reopened 5 min
        ago — the anchored stale clock reads 5 min < threshold: NO fire.
        This is the 2026-07-06/07 daily false-positive, dead."""
        reopen = _utc_from_ct(2026, 7, 13, 17, 0)
        now = reopen + timedelta(minutes=5)
        lt = _make_watchdog_trader(last_bar_utc_naive=now - timedelta(minutes=70))
        monkeypatch.setattr(lt_module, "datetime", _frozen_clock(now))
        assert lt._check_stale_bars() is False
        lt._send_watchdog_telegram.assert_not_called()
        lt._emit_health_event.assert_not_called()

    def test_watchdog_still_fires_when_stale_past_reopen(self, monkeypatch):
        """The grace must not mask REAL post-reopen staleness: 35 min past
        the anchor with no bars still fires."""
        reopen = _utc_from_ct(2026, 7, 13, 17, 0)
        now = reopen + timedelta(minutes=35)
        lt = _make_watchdog_trader(last_bar_utc_naive=now - timedelta(minutes=100))
        monkeypatch.setattr(lt_module, "datetime", _frozen_clock(now))
        assert lt._check_stale_bars() is True
        lt._send_watchdog_telegram.assert_called_once()
        lt._emit_health_event.assert_called_once()

    def test_fresh_bars_after_reopen_never_fire(self, monkeypatch):
        reopen = _utc_from_ct(2026, 7, 13, 17, 0)
        now = reopen + timedelta(minutes=40)
        lt = _make_watchdog_trader(last_bar_utc_naive=now - timedelta(minutes=10))
        monkeypatch.setattr(lt_module, "datetime", _frozen_clock(now))
        assert lt._check_stale_bars() is False
        lt._send_watchdog_telegram.assert_not_called()
