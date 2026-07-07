"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/session_calendar.py (NEW leaf module:
                            market_status + session_open_anchor, GLOBEX body moved
                            VERBATIM from live_trader._get_market_status, GRAINS
                            calendar, dispatch on registry session_hours_ct,
                            unknown shape raises),
                            src/live_execution/live_trader.py
                            (_get_market_status delegates to the calendar;
                            _check_stale_bars gains the grains reopen grace anchor
                            with GLOBEX arithmetic bit-identical — Q1 false-positive
                            PINNED AS-IS; DataManager construction feeds registry
                            bars_per_day_5m/1h + execution_symbol; 4320-raise message
                            cache-name-derived, CL text byte-identical),
                            src/live_execution/ibkr_client.py
                            (_select_front_month + _first_notice_proxy pure helpers;
                            _EXPIRY_BUFFER_DAYS deleted; C3 GC/MGC Memorial-Day
                            FND guard — outcome-pinned, remedy left to the Coder),
                            src/live_execution/data_manager.py
                            (derive_seed_lookback_days + REQUIRED_1H_BARS;
                            roll_ratio_tolerance from registry; keyword-only
                            execution_symbol; roll-metadata per-execution-symbol
                            namespace + C1 namespaced roll_history "from" +
                            C2 ownership-filtered Step-0 restore),
                            src/core/instrument_master.py
                            (REQUIRED roll_ratio_tolerance on all 15 entries)
Target Class/Function: market_status, session_open_anchor,
                       LiveTrader._get_market_status / _check_stale_bars /
                       _warm_start / __init__,
                       IBKRConnectionManager._select_front_month /
                       _first_notice_proxy,
                       DataManager.__init__ / initialize / _detect_rollover /
                       _save_roll_metadata / _seed_from_csv /
                       derive_seed_lookback_days,
                       Instrument.roll_ratio_tolerance
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t5-hours-watchdog-rollover_07042026_2305
Phase: RED — src/live_execution/session_calendar.py does NOT exist yet; this
file fails at collection (ghost import per TDD protocol) until the Coder
implements audit.md section 4 under blueprint rulings Q1/Q4/Q5 and
impact_review conditions C1-C5.

Governing documents (this file transcribes their pins, it does not invent):
  - blueprint.md — Q1: the CL reopen/holiday watchdog false-positive is PINNED
    AS-IS (tests below assert TODAY'S behavior: reopen window reads OPEN and
    the watchdog fires — do NOT "fix" these tests); Q2: non-CL tolerance 0.001,
    CL/MCL 0.01; C3 outcome-pinned; C5: these pins are the ONLY regression
    fence for this logic (the parity harness bypasses it).
  - audit.md §1-1a — the frozen legacy CL market-status reference below is a
    VERBATIM transcription of live_trader._get_market_status at HEAD 7a861bb.
  - impact_review.md — pinned instants, seed-formula numbers (280/292/406),
    C1/C2 defect scenarios, C3 2027 Memorial-Day case (true FND 2027-05-28 vs
    naive proxy 2027-05-31).

All I/O boundaries are mocked or tmp_path-local: no IBKR gateway, no real data
files, no Telegram (MagicMock), cache backups redirected off the repo via a
monkeypatched _CACHE_BACKUP_DIR. Clocks are frozen by injecting utc_now into
the pure calendar functions and by patching live_trader.datetime with a frozen
subclass for the watchdog (no freezegun in this repo — conventions follow
tests/test_reconnection.py stubs and tests/test_symbol_data_paths.py seams).

NOTE (ambiguity resolved per audit §4a, which governs the grains design):
grains reopen is 19:00 CT — Tue 19:30 CT is OPEN (overnight session), the
evening halt is [13:20, 19:00). The closest CLOSED instants are pinned at
18:30/18:59 CT.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import pytz

from src.core.instrument_master import (
    INSTRUMENT_REGISTRY,
    get_instrument,
)
from src.live_execution import data_manager as dm_module
from src.live_execution import ibkr_client as ibkr_module
from src.live_execution import live_trader as lt_module
from src.live_execution.live_trader import LiveTrader

# ---------------------------------------------------------------------------
# Ghost imports (RED) — the Coder builds these per audit.md §4a/§4c/§4e.
# Do NOT fix ImportError here; the whole file fails at collection until then.
# ---------------------------------------------------------------------------
from src.live_execution.session_calendar import (  # noqa: E402
    market_status,
    session_open_anchor,
)
from src.live_execution.ibkr_client import (  # noqa: E402
    IBKRConnectionManager,
    _first_notice_proxy,
    _select_front_month,
)
from src.live_execution.data_manager import (  # noqa: E402
    REQUIRED_1H_BARS,
    DataManager,
    derive_seed_lookback_days,
)


# ===========================================================================
# Frozen legacy reference — VERBATIM transcription of
# LiveTrader._get_market_status at HEAD 7a861bb (live_trader.py:3928-3959).
# This is THE byte-identity fence (C5). Never edit to match new code;
# the new calendar must match THIS.
# ===========================================================================

_WEEKEND_STR = "CLOSED (weekend — opens Sun 6pm ET)"
_HALT_STR = "CLOSED (daily halt 5-6pm ET)"


def _legacy_cl_market_status(utc_now: datetime) -> str:
    """Frozen copy of the HEAD-7a861bb CL market-status body (do not edit)."""
    et = pytz.timezone("America/New_York")
    et_now = utc_now.replace(tzinfo=pytz.utc).astimezone(et)
    weekday = et_now.weekday()  # 0=Mon … 6=Sun
    hour = et_now.hour

    # Saturday: always closed
    if weekday == 5:
        return "CLOSED (weekend — opens Sun 6pm ET)"
    # Sunday before 18:00 ET: closed
    if weekday == 6 and hour < 18:
        return "CLOSED (weekend — opens Sun 6pm ET)"
    # Friday after 17:00 ET: closed
    if weekday == 4 and hour >= 17:
        return "CLOSED (weekend — opens Sun 6pm ET)"
    # Mon-Thu 17:00-18:00 ET: daily maintenance halt
    if 0 <= weekday <= 3 and hour == 17:
        return "CLOSED (daily halt 5-6pm ET)"
    return "OPEN"


# ===========================================================================
# Time helpers (frozen wall-clock -> tz-naive UTC, matching the production
# convention of naive-UTC datetimes everywhere)
# ===========================================================================

_ET = pytz.timezone("America/New_York")
_CT = pytz.timezone("America/Chicago")


def _utc_from_et(y, m, d, hh, mm):
    return _ET.localize(datetime(y, m, d, hh, mm)).astimezone(pytz.utc).replace(
        tzinfo=None
    )


def _utc_from_ct(y, m, d, hh, mm):
    return _CT.localize(datetime(y, m, d, hh, mm)).astimezone(pytz.utc).replace(
        tzinfo=None
    )


def _frozen_lt_clock(naive_utc: datetime):
    """Patch live_trader.datetime so datetime.now(timezone.utc) is frozen.

    Subclasses datetime so arithmetic/isinstance in production code keeps
    working (repo has no freezegun; this is the module-name seam the code
    actually reads: `from datetime import datetime, timezone` at module top).
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            if tz is not None:
                return naive_utc.replace(tzinfo=timezone.utc).astimezone(tz)
            return naive_utc

    return patch.object(lt_module, "datetime", _Frozen)


# Registry instruments used throughout
_CL = get_instrument("CL")
_MCL = get_instrument("MCL")
_ES = get_instrument("ES")
_GC = get_instrument("GC")
_ZC = get_instrument("ZC")
_ZS = get_instrument("ZS")

# Impact-review V1 pinned instants (verified executable against HEAD body):
# (ET wall-clock tuple, exact expected string). Jan = EST, Jul = EDT.
_CL_PINNED_CASES = [
    ((2026, 1, 10, 12, 0), _WEEKEND_STR),   # Sat
    ((2026, 1, 11, 17, 30), _WEEKEND_STR),  # Sun 17:30 ET pre-open (ticket case)
    ((2026, 1, 11, 18, 0), "OPEN"),         # Sun 18:00 ET open
    ((2026, 1, 12, 17, 30), _HALT_STR),     # Mon 17:30 ET daily halt
    ((2026, 1, 12, 18, 3), "OPEN"),         # Mon 18:03 ET — Q1 false-positive window
    ((2026, 1, 16, 16, 59), "OPEN"),        # Fri 16:59 ET
    ((2026, 1, 16, 17, 0), _WEEKEND_STR),   # Fri 17:00 ET close
    ((2026, 7, 11, 12, 0), _WEEKEND_STR),
    ((2026, 7, 12, 17, 30), _WEEKEND_STR),
    ((2026, 7, 12, 18, 0), "OPEN"),
    ((2026, 7, 13, 17, 30), _HALT_STR),
    ((2026, 7, 13, 18, 3), "OPEN"),
    ((2026, 7, 17, 16, 59), "OPEN"),
    ((2026, 7, 17, 17, 0), _WEEKEND_STR),
]


# ===========================================================================
# 1. TestSessionCalendarGlobex — CL byte-identity pins (C5: the ONLY
#    regression fence; the parity harness bypasses this layer)
# ===========================================================================

class TestSessionCalendarGlobex:
    def test_cl_pinned_instants_byte_identical(self):
        """Exact-string pins at the impact-review instants (byte equality)."""
        for wall, expected in _CL_PINNED_CASES:
            t = _utc_from_et(*wall)
            got = market_status(_CL, t)
            assert got == expected, f"CL @ ET{wall}: {got!r} != {expected!r}"
            # And equality with the frozen legacy body (same thing, two pins)
            assert got == _legacy_cl_market_status(t)

    def test_cl_minute_by_minute_dst_year_equivalence_sweep(self):
        """Minute-by-minute equivalence vs the frozen HEAD body across a
        DST-straddling grid: a full plain week in Jan (EST) and Jul (EDT),
        plus 3-day windows over BOTH 2026 US DST transitions
        (2026-03-08 spring-forward, 2026-11-01 fall-back). Every weekday,
        weekend boundary, halt hour and both transitions are covered."""
        windows = [
            (datetime(2026, 1, 12), datetime(2026, 1, 19)),   # plain EST week
            (datetime(2026, 3, 7), datetime(2026, 3, 10)),    # spring-forward
            (datetime(2026, 7, 13), datetime(2026, 7, 20)),   # plain EDT week
            (datetime(2026, 10, 31), datetime(2026, 11, 3)),  # fall-back
        ]
        mismatches = []
        for start, end in windows:
            t = start
            while t < end:
                want = _legacy_cl_market_status(t)
                got = market_status(_CL, t)
                if got != want:
                    mismatches.append((t, want, got))
                    if len(mismatches) >= 5:
                        break
                t += timedelta(minutes=1)
            if len(mismatches) >= 5:
                break
        assert not mismatches, (
            "GLOBEX calendar diverges from the frozen legacy CL body "
            f"(first mismatches, utc/(want,got)): {mismatches}"
        )

    def test_globex_family_dispatches_to_same_calendar(self):
        """CL/MCL/GC/SI all carry _GLOBEX_SESSION -> identical strings
        at every pinned instant (registry-tuple dispatch, audit test 3).

        T7 EVOLUTION (t7-es-ops-runway_07052026_0214): ES/NQ dropped from
        the GLOBEX family — they (and their micros) move to the EQUITY
        session tuple (("17:00","15:15"),("15:30","16:00")), the sanctioned
        evolution the C4 block in session_calendar.py:29-37 pre-declared.
        The negative assertion below pins the departure."""
        for sym in ("CL", "MCL", "GC", "SI"):
            inst = get_instrument(sym)
            for wall, expected in _CL_PINNED_CASES:
                t = _utc_from_et(*wall)
                assert market_status(inst, t) == expected, (
                    f"{sym} @ ET{wall} must dispatch to the GLOBEX calendar"
                )
        # T7: ES/NQ no longer carry the GLOBEX tuple (equity dispatch)
        for sym in ("ES", "NQ"):
            got = get_instrument(sym).session_hours_ct
            assert got != (("17:00", "16:00"),), (
                f"{sym} must have LEFT the GLOBEX family (T7 equity "
                f"session tuple), still carries {got!r}"
            )

    def test_es_maintenance_break_modeled_closed(self):
        """The 16:00-17:00 CT maintenance break — ES Mon 17:30 ET
        (= 16:30 CT) reads CLOSED.

        T7 EVOLUTION (t7-es-ops-runway_07052026_0214): re-pinned to the
        EQUITY calendar semantics. The T5 docstring marked the real ES
        15:15-15:30 CT halt "C4 documentation-only — deliberately NOT
        modeled" (written to be evolved); ES now dispatches its own equity
        calendar, so the maintenance instant returns the equity
        'maintenance' string (shape-pinned), NOT the CL byte-frozen
        _HALT_STR. Only "OPEN" is byte-load-bearing (watchdog gate)."""
        for wall in ((2026, 7, 13, 17, 30), (2026, 1, 12, 17, 30)):
            status = market_status(_ES, _utc_from_et(*wall))
            assert status != "OPEN", f"ES @ ET{wall}: got 'OPEN'"
            assert status.startswith("CLOSED"), (
                f"ES @ ET{wall}: got {status!r}"
            )
            assert "maintenance" in status, (
                f"ES @ ET{wall}: equity maintenance marker missing "
                f"from {status!r}"
            )

    def test_session_open_anchor_globex_reopen_grace(self):
        """PIN UPDATED by cl-watchdog-reopen-grace_07052026_0001 (operator-
        authorized 2026-07-07): the Q1 anchor-is-None pin was the documented
        placeholder for exactly this ticket — GLOBEX now anchors at the most
        recent Sun-Thu 17:00 CT open (18:00 ET), like grains (M4) and equity
        (T7). Exhaustive anchor vectors live in
        tests/test_globex_reopen_grace.py.

        T7 EVOLUTION (t7-es-ops-runway_07052026_0214): _ES stays on the
        EQUITY calendar (exact vectors in
        tests/test_hourly_only_equity_session.py)."""
        cases = [
            # (instant, expected anchor) — ET wall clocks; open = 18:00 ET
            (_utc_from_et(2026, 7, 13, 12, 0),   # Mon open hours
             _utc_from_et(2026, 7, 12, 18, 0)),  # -> Sunday's open
            (_utc_from_et(2026, 7, 13, 17, 30),  # Mon daily halt
             _utc_from_et(2026, 7, 12, 18, 0)),  # -> still Sunday's open
            (_utc_from_et(2026, 7, 13, 18, 3),   # Mon reopen window (ex-Q1)
             _utc_from_et(2026, 7, 13, 18, 0)),  # -> Monday's own open
            (_utc_from_et(2026, 7, 12, 18, 5),   # Sunday open
             _utc_from_et(2026, 7, 12, 18, 0)),
            (_utc_from_et(2026, 7, 11, 12, 0),   # Saturday (closed)
             _utc_from_et(2026, 7, 9, 18, 0)),   # -> Thursday's open
        ]
        for inst in (_CL, _MCL, _GC):
            for t, expected in cases:
                assert session_open_anchor(inst, t) == expected, (
                    f"{inst.symbol} @ {t}: GLOBEX anchor must be the most "
                    f"recent Sun-Thu 17:00 CT open ({expected})"
                )
        # T7: ES stays on the equity calendar — anchor is NOT None
        for t, _ in cases:
            assert session_open_anchor(_ES, t) is not None, (
                f"ES @ {t}: equity session_open_anchor must return the most "
                "recent 15:30/17:00 CT open"
            )

    def test_cl_reopen_surface_open_with_grace_anchor(self):
        """Ex-Q1 PIN (updated by cl-watchdog-reopen-grace_07052026_0001): at
        Mon 18:03 ET the status is OPEN and the anchor is Monday's own
        18:00 ET open — the watchdog stale clock restarts at the reopen
        instead of counting the halt (the 2026-07-06/07 daily false
        positive, retired)."""
        t = _utc_from_et(2026, 7, 13, 18, 3)
        assert market_status(_CL, t) == "OPEN"
        assert session_open_anchor(_CL, t) == _utc_from_et(2026, 7, 13, 18, 0)
        t_sun = _utc_from_et(2026, 7, 12, 18, 5)
        assert market_status(_CL, t_sun) == "OPEN"
        assert session_open_anchor(_CL, t_sun) == _utc_from_et(2026, 7, 12, 18, 0)


# ===========================================================================
# 2. TestSessionCalendarGrains — ZC/ZS (registry _GRAINS_SESSION:
#    overnight 19:00-07:45 CT + day 08:30-13:20 CT, evaluated in
#    America/Chicago; audit §4a). OPEN is pinned byte-exact ("OPEN" is the
#    watchdog gate); CLOSED strings are shape-pinned (startswith CLOSED +
#    weekend/halt marker) — only CL strings are byte-frozen.
# ===========================================================================

def _assert_closed_halt(status: str, ctx: str):
    assert status != "OPEN", ctx
    assert status.startswith("CLOSED"), ctx
    assert "halt" in status, ctx


def _assert_closed_weekend(status: str, ctx: str):
    assert status != "OPEN", ctx
    assert status.startswith("CLOSED"), ctx
    assert "weekend" in status, ctx


# Same wall-clock instants in a CST week (Jan) and a CDT week (Jul):
# Tue 2026-01-06 / 2026-07-07, Wed 01-07 / 07-08, Fri 01-09 / 07-10,
# Sat 01-10 / 07-11, Sun 01-11 / 07-12 (weekday-verified).
_GRAIN_WEEKS = [
    {"mon": (2026, 1, 5), "tue": (2026, 1, 6), "wed": (2026, 1, 7),
     "fri": (2026, 1, 9), "sat": (2026, 1, 10), "sun": (2026, 1, 11)},
    {"mon": (2026, 7, 6), "tue": (2026, 7, 7), "wed": (2026, 7, 8),
     "fri": (2026, 7, 10), "sat": (2026, 7, 11), "sun": (2026, 7, 12)},
]


class TestSessionCalendarGrains:
    @pytest.mark.parametrize("week", _GRAIN_WEEKS)
    def test_zc_afternoon_halt_closed(self, week):
        """Ticket case: ZC Tuesday 15:00 CT -> CLOSED (daily halt)."""
        y, m, d = week["tue"]
        _assert_closed_halt(
            market_status(_ZC, _utc_from_ct(y, m, d, 15, 0)),
            f"ZC Tue 15:00 CT ({y}-{m:02d}-{d:02d}) must be a CLOSED halt",
        )

    @pytest.mark.parametrize("week", _GRAIN_WEEKS)
    def test_zc_morning_halt_closed(self, week):
        y, m, d = week["tue"]
        for hh, mm in ((7, 45), (8, 0), (8, 29)):
            _assert_closed_halt(
                market_status(_ZC, _utc_from_ct(y, m, d, hh, mm)),
                f"ZC Tue {hh:02d}:{mm:02d} CT is inside the 07:45-08:30 halt",
            )

    @pytest.mark.parametrize("week", _GRAIN_WEEKS)
    def test_zc_open_instants_exact(self, week):
        """OPEN pins (byte-exact — the watchdog gate compares == 'OPEN').

        NOTE: Tue 19:30 CT is OPEN — grains reopen at 19:00 CT (audit §4a:
        the evening halt is [13:20, 19:00); 'else OPEN incl. Mon-Thu >=19:00').
        """
        ty, tm, td = week["tue"]
        wy, wm, wd = week["wed"]
        fy, fm, fd = week["fri"]
        sy, sm, sd = week["sun"]
        open_cases = [
            (ty, tm, td, 10, 0),   # ticket case: Tue 10:00 CT day session
            (ty, tm, td, 8, 30),   # reopen boundary
            (ty, tm, td, 13, 19),  # last minute of day session
            (ty, tm, td, 19, 30),  # overnight session (19:00 reopen)
            (ty, tm, td, 20, 0),
            (wy, wm, wd, 3, 0),    # overnight tail past midnight
            (fy, fm, fd, 7, 0),    # Friday overnight tail
            (sy, sm, sd, 19, 0),   # Sunday 19:00 CT open
        ]
        for y, m, d, hh, mm in open_cases:
            got = market_status(_ZC, _utc_from_ct(y, m, d, hh, mm))
            assert got == "OPEN", (
                f"ZC {y}-{m:02d}-{d:02d} {hh:02d}:{mm:02d} CT must be exactly "
                f"'OPEN', got {got!r}"
            )

    @pytest.mark.parametrize("week", _GRAIN_WEEKS)
    def test_zc_evening_halt_boundaries(self, week):
        y, m, d = week["tue"]
        for hh, mm in ((13, 20), (18, 30), (18, 59)):
            _assert_closed_halt(
                market_status(_ZC, _utc_from_ct(y, m, d, hh, mm)),
                f"ZC Tue {hh:02d}:{mm:02d} CT is inside the 13:20-19:00 halt",
            )

    @pytest.mark.parametrize("week", _GRAIN_WEEKS)
    def test_zc_weekend_closed(self, week):
        sy, sm, sd = week["sat"]
        uy, um, ud = week["sun"]
        fy, fm, fd = week["fri"]
        _assert_closed_weekend(
            market_status(_ZC, _utc_from_ct(sy, sm, sd, 12, 0)), "Sat 12:00 CT"
        )
        _assert_closed_weekend(
            market_status(_ZC, _utc_from_ct(uy, um, ud, 18, 30)),
            "Sun 18:30 CT (pre-open)",
        )
        _assert_closed_weekend(
            market_status(_ZC, _utc_from_ct(fy, fm, fd, 13, 20)),
            "Fri 13:20 CT (weekend close — no Friday evening session)",
        )
        _assert_closed_weekend(
            market_status(_ZC, _utc_from_ct(fy, fm, fd, 19, 30)),
            "Fri 19:30 CT (weekend)",
        )

    def test_zs_dispatches_grains_calendar(self):
        _assert_closed_halt(
            market_status(_ZS, _utc_from_ct(2026, 7, 7, 15, 0)),
            "ZS shares _GRAINS_SESSION -> Tue 15:00 CT CLOSED halt",
        )
        assert market_status(_ZS, _utc_from_ct(2026, 7, 7, 10, 0)) == "OPEN"

    def test_session_open_anchor_grain_vectors(self):
        """Anchor = most recent session-open instant, tz-naive UTC (audit
        test 5). Expected values computed from CT wall clock (CDT: UTC-5)."""
        cases = [
            # (query instant, expected anchor)
            (_utc_from_ct(2026, 7, 7, 19, 2), _utc_from_ct(2026, 7, 7, 19, 0)),
            (_utc_from_ct(2026, 7, 7, 9, 0), _utc_from_ct(2026, 7, 7, 8, 30)),
            (_utc_from_ct(2026, 7, 7, 8, 35), _utc_from_ct(2026, 7, 7, 8, 30)),
            (_utc_from_ct(2026, 7, 6, 3, 0), _utc_from_ct(2026, 7, 5, 19, 0)),
            (_utc_from_ct(2026, 7, 8, 3, 0), _utc_from_ct(2026, 7, 7, 19, 0)),
        ]
        for t, expected in cases:
            anchor = session_open_anchor(_ZC, t)
            assert anchor is not None, f"ZC @ {t}: anchor must not be None"
            assert anchor == expected, (
                f"ZC @ {t}: anchor {anchor!r} != expected {expected!r} "
                "(tz-naive UTC of the most recent 08:30/19:00 CT open)"
            )
            # tz-naive contract
            assert getattr(anchor, "tzinfo", None) is None

    # -- watchdog reopen grace (ticket case: hours-old last bar right at
    #    the 08:30 CT reopen must NOT flag stale) --------------------------

    def test_zc_reopen_grace_watchdog_no_stale_flag(self):
        """Tue 08:35 CT, last completed 5m bar Mon 13:15 CT (~19h old):
        the reopen anchor (08:30 CT) restarts the stale clock -> False,
        no Telegram, no disconnect."""
        trader = _watchdog_stub("ZC")
        trader._last_bar_time_5m = pd.Timestamp("2026-07-06 18:15:00")  # Mon 13:15 CT
        with _frozen_lt_clock(_utc_from_ct(2026, 7, 7, 8, 35)):
            result = trader._check_stale_bars()
        assert result is False
        trader._telegram.send.assert_not_called()
        trader.data_client.disconnect.assert_not_called()
        trader.exec_client.disconnect.assert_not_called()
        assert trader._subscriptions_lost is False

    def test_zc_evening_reopen_grace(self):
        """Tue 19:02 CT, last bar 13:15 CT (halt-old) -> grace -> False."""
        trader = _watchdog_stub("ZC")
        trader._last_bar_time_5m = pd.Timestamp("2026-07-07 18:15:00")  # Tue 13:15 CT
        with _frozen_lt_clock(_utc_from_ct(2026, 7, 7, 19, 2)):
            result = trader._check_stale_bars()
        assert result is False
        trader._telegram.send.assert_not_called()

    def test_zc_grace_expires_after_threshold(self):
        """Tue 09:05 CT (open 35 min, still no bars) -> the watchdog must
        still catch a genuinely dead feed -> True + disconnect.
        (Query instant moved 08:50 -> 09:05 CT: 35 min past the 08:30
        anchor clears the 30-min threshold of the 2026-07-06 directive,
        ticket watchdog-telegram-throttle_07062026_0007.)"""
        trader = _watchdog_stub("ZC")
        trader._last_bar_time_5m = pd.Timestamp("2026-07-06 18:15:00")
        with _frozen_lt_clock(_utc_from_ct(2026, 7, 7, 9, 5)):
            result = trader._check_stale_bars()
        assert result is True
        trader.data_client.disconnect.assert_called_once()
        trader.exec_client.disconnect.assert_called_once()
        assert trader._subscriptions_lost is True

    def test_unknown_session_shape_raises(self):
        """No silent default: an unrecognized session_hours_ct tuple must
        raise ValueError naming the symbol (audit test 6)."""
        fake = dataclasses.replace(_ZC, session_hours_ct=(("09:00", "10:00"),))
        with pytest.raises(ValueError) as excinfo:
            market_status(fake, _utc_from_ct(2026, 7, 7, 10, 0))
        assert "ZC" in str(excinfo.value)


# ===========================================================================
# Watchdog stub (mirrors tests/test_reconnection.py::_make_trader_stub)
# ===========================================================================

def _watchdog_stub(execution_symbol: str):
    trader = object.__new__(lt_module.LiveTrader)
    trader.data_client = MagicMock()
    trader.exec_client = MagicMock()
    trader._telegram = MagicMock()
    trader._subscriptions_lost = False
    # T4 structural-derivation seam: _brain_instrument falls back through
    # _execution_symbol for object.__new__ stubs (live_trader.py:2153-2174).
    trader._execution_symbol = execution_symbol
    trader._last_bar_time_5m = None
    return trader


# ===========================================================================
# 3. TestFrontMonthSelection — pure _select_front_month helper on mocked
#    ContractDetails (contractMonth lives on the DETAIL, per ib_insync 0.9.86)
# ===========================================================================

class _DetailStub:
    """Mock ContractDetails: contract carries localSymbol + LTD; the detail
    carries contractMonth. forbid_month_read turns any contractMonth access
    into an AssertionError (the CL all-12 short-circuit sentinel)."""

    def __init__(self, local_symbol: str, ltd: str, contract_month: str = "",
                 forbid_month_read: bool = False):
        self.contract = SimpleNamespace(
            localSymbol=local_symbol,
            lastTradeDateOrContractMonth=ltd,
            conId=42,
        )
        self._contract_month = contract_month
        self._forbid = forbid_month_read

    @property
    def contractMonth(self):  # noqa: N802 — ib_insync field casing
        if self._forbid:
            raise AssertionError(
                "contractMonth must NOT be read for an all-12-active-months "
                "instrument (CL byte-identity short-circuit)"
            )
        return self._contract_month


def _legacy_cl_selection(details, now_utc, buffer_days=6):
    """Frozen HEAD-7a861bb selection logic (ibkr_client.py:693-713):
    sort by LTD string, keep LTD >= (now+buffer) as YYYYMMDD string,
    pick first; fallback to the nearest-expiry detail."""
    ds = sorted(details, key=lambda d: d.contract.lastTradeDateOrContractMonth)
    cutoff_str = (now_utc + timedelta(days=buffer_days)).strftime("%Y%m%d")
    tradable = [
        d for d in ds
        if d.contract.lastTradeDateOrContractMonth >= cutoff_str
    ]
    return tradable[0] if tradable else ds[0]


class TestFrontMonthSelection:
    def test_cl_all_twelve_short_circuit_never_reads_contract_month(self):
        """CL (active_months == all 12): the helper must not touch
        contractMonth at all — every detail raises on access."""
        now = datetime(2026, 7, 4, 12, 0)
        details = [
            _DetailStub("CLU6", "20260820", forbid_month_read=True),
            _DetailStub("CLQ6", "20260721", forbid_month_read=True),
            _DetailStub("CLV6", "20260922", forbid_month_read=True),
        ]
        picked = _select_front_month(details, _CL, now)
        assert picked.contract.localSymbol == "CLQ6"
        assert picked is _legacy_cl_selection(details, now)

    def test_cl_selection_matches_legacy_buffer6_including_boundary(self):
        """Byte-identity with the legacy buffer-6 string compare, including
        the LTD == cutoff boundary (string >= keeps it eligible)."""
        details = [
            _DetailStub("CLQ6", "20260721", forbid_month_read=True),
            _DetailStub("CLU6", "20260820", forbid_month_read=True),
        ]
        # 2026-07-15 + 6d = 20260721 == CLQ6 LTD -> still eligible
        boundary_now = datetime(2026, 7, 15, 12, 0)
        picked = _select_front_month(details, _CL, boundary_now)
        assert picked.contract.localSymbol == "CLQ6"
        assert picked is _legacy_cl_selection(details, boundary_now)
        # One day later the cutoff passes CLQ6 -> CLU6
        next_day = datetime(2026, 7, 16, 12, 0)
        picked2 = _select_front_month(details, _CL, next_day)
        assert picked2.contract.localSymbol == "CLU6"
        assert picked2 is _legacy_cl_selection(details, next_day)

    def test_cl_all_near_expiry_falls_back_to_nearest(self):
        """All contracts inside the buffer -> legacy fallback: nearest
        expiry (sorted details[0]), no raise."""
        now = datetime(2026, 9, 20, 12, 0)
        details = [
            _DetailStub("CLU6", "20260820", forbid_month_read=True),
            _DetailStub("CLQ6", "20260721", forbid_month_read=True),
        ]
        picked = _select_front_month(details, _CL, now)
        assert picked.contract.localSymbol == "CLQ6"
        assert picked is _legacy_cl_selection(details, now)

    def test_gc_serial_months_filtered_next_active_picked(self):
        """Ticket case B8a: GC serial months (F/H not in 'GJMQVZ') must
        never be selected even when nearest-expiry."""
        now = datetime(2026, 12, 15, 12, 0)
        f7 = _DetailStub("GCF7", "20270127", contract_month="202701")  # serial
        g7 = _DetailStub("GCG7", "20270224", contract_month="202702")  # active
        h7 = _DetailStub("GCH7", "20270329", contract_month="202703")  # serial
        j7 = _DetailStub("GCJ7", "20270427", contract_month="202704")  # active
        picked = _select_front_month([f7, g7, h7, j7], _GC, now)
        assert picked.contract.localSymbol == "GCG7"

    def test_gc_fnd_buffer_skips_to_next_active_not_serial(self):
        """Near FND(G7): proxy(202702) = 2027-01-29 (last weekday of Jan).
        now+3d past it -> G7 out; serial H7 stays filtered -> J7."""
        now = datetime(2027, 1, 27, 12, 0)  # +3d = 2027-01-30 > 2027-01-29
        f7 = _DetailStub("GCF7", "20270127", contract_month="202701")
        g7 = _DetailStub("GCG7", "20270224", contract_month="202702")
        h7 = _DetailStub("GCH7", "20270329", contract_month="202703")
        j7 = _DetailStub("GCJ7", "20270427", contract_month="202704")
        picked = _select_front_month([f7, g7, h7, j7], _GC, now)
        assert picked.contract.localSymbol == "GCJ7"

    def test_gc_memorial_day_2027_june_ineligible_on_true_fnd(self):
        """C3 OUTCOME PIN (remedy-agnostic): true FND of GCM7 is Fri
        2027-05-28; the naive last-weekday proxy lands Mon 2027-05-31
        (Memorial Day), 3 days late — consuming the whole 3-day buffer.
        Whichever remedy the Coder picks (holiday-aware proxy OR bumped
        GC/MGC roll_buffer_days), GCM7 must NOT be eligible on 2027-05-27
        or on the true FND 2027-05-28."""
        m7 = _DetailStub("GCM7", "20270628", contract_month="202706")
        q7 = _DetailStub("GCQ7", "20270827", contract_month="202708")
        for day in (27, 28):
            picked = _select_front_month(
                [m7, q7], _GC, datetime(2027, 5, day, 12, 0)
            )
            assert picked.contract.localSymbol == "GCQ7", (
                f"GCM7 still eligible on 2027-05-{day} — past/at the true "
                "FND 2027-05-28 (C3 Memorial-Day edge, IBKR force-"
                "liquidation exposure)"
            )

    def test_gc_memorial_day_2027_still_eligible_early_may(self):
        """Guard against over-rolling: well before FND (2027-05-20, both
        remedies leave >=3 clear days) GCM7 is still the front month."""
        m7 = _DetailStub("GCM7", "20270628", contract_month="202706")
        q7 = _DetailStub("GCQ7", "20270827", contract_month="202708")
        picked = _select_front_month([m7, q7], _GC, datetime(2027, 5, 20, 12, 0))
        assert picked.contract.localSymbol == "GCM7"

    def test_first_notice_proxy_vectors(self):
        """Audit test 13 vectors (remedy-agnostic: no US holiday involved):
        last weekday of the month preceding delivery."""
        assert _first_notice_proxy("202603") == date(2026, 2, 27)  # Fri
        assert _first_notice_proxy("202612") == date(2026, 11, 30)  # Mon

    def test_es_quarterly_hmuz_buffer8(self):
        """Ticket case: ES rolls on the registry 8-day buffer (LTD-referenced).
        ESU6 LTD 20260918: eligible on 2026-09-10 (cutoff == LTD), gone on
        2026-09-11."""
        u6 = _DetailStub("ESU6", "20260918", contract_month="202609")
        z6 = _DetailStub("ESZ6", "20261218", contract_month="202612")
        picked = _select_front_month([u6, z6], _ES, datetime(2026, 9, 10, 0, 0))
        assert picked.contract.localSymbol == "ESU6"
        picked = _select_front_month([u6, z6], _ES, datetime(2026, 9, 11, 0, 0))
        assert picked.contract.localSymbol == "ESZ6"

    def test_es_serial_decoy_filtered(self):
        """A non-quarterly decoy (V=Oct not in 'HMUZ') between U6 and Z6 must
        be filtered — quarterlies only."""
        u6 = _DetailStub("ESU6", "20260918", contract_month="202609")
        v6 = _DetailStub("ESV6", "20261016", contract_month="202610")
        z6 = _DetailStub("ESZ6", "20261218", contract_month="202612")
        picked = _select_front_month(
            [u6, v6, z6], _ES, datetime(2026, 9, 11, 0, 0)
        )
        assert picked.contract.localSymbol == "ESZ6"

    def test_no_active_month_contract_raises(self):
        """All details in inactive months -> loud raise (a silent serial-month
        pick is exactly bug B8a)."""
        f7 = _DetailStub("GCF7", "20270127", contract_month="202701")
        h7 = _DetailStub("GCH7", "20270329", contract_month="202703")
        with pytest.raises(RuntimeError) as excinfo:
            _select_front_month([f7, h7], _GC, datetime(2026, 12, 15, 12, 0))
        assert "GC" in str(excinfo.value)

    def test_missing_contract_month_on_restricted_raises(self):
        """A blank contractMonth on a restricted instrument cannot be
        silently passed through (audit test 15)."""
        g7 = _DetailStub("GCG7", "20270224", contract_month="")
        with pytest.raises(ValueError):
            _select_front_month([g7], _GC, datetime(2026, 12, 15, 12, 0))

    def test_expiry_buffer_days_gone(self):
        """_EXPIRY_BUFFER_DAYS is deleted — the registry roll_buffer_days is
        the single source of truth (replaces the T2 pin at
        tests/test_build_future_contract.py:390, updated by the Coder)."""
        assert not hasattr(IBKRConnectionManager, "_EXPIRY_BUFFER_DAYS")
        assert not hasattr(ibkr_module, "_EXPIRY_BUFFER_DAYS")


# ===========================================================================
# 4. TestRollToleranceRegistry — registry field + DataManager consumption
#    (noise-floor semantics: BELOW tolerance = adjustment skipped;
#    detection is the front-month string change, unaffected)
# ===========================================================================

def _write_seed_csv(path: Path, n: int = 50, close: float = 100.0,
                    start: str = "2026-01-05 09:00") -> None:
    rows = []
    base = pd.Timestamp(start)
    for i in range(n):
        dt = base + pd.Timedelta(minutes=5 * i)
        rows.append(
            f"{dt.strftime('%d/%m/%Y')};{dt.strftime('%H:%M')};"
            f"{close:.3f};{close:.3f};{close:.3f};{close:.3f};100"
        )
    path.write_text("\n".join(rows), encoding="utf-8")


def _write_cache_parquet(path: Path, n: int = 100, close: float = 100.0,
                         start: str = "2026-05-31 00:00",
                         freq: str = "5min") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq=freq)
    df = pd.DataFrame({
        "DateTime": idx,
        "Open": close, "High": close, "Low": close, "Close": close,
        "Volume": 100.0,
    })
    df.to_parquet(path, engine="pyarrow")
    return df


def _run_roll_scenario(tmp_path: Path, monkeypatch, symbol: str, scale: float):
    """Full initialize() roll: previous front month {sym}H6 -> {sym}M6,
    IBKR overlap uniformly scaled by `scale` (exact ratio). All I/O in
    tmp_path; cache backups redirected off the repo."""
    proc = tmp_path / f"{symbol}_{str(scale).replace('.', '_')}"
    proc.mkdir(parents=True)

    seed = proc / "seed.csv"
    _write_seed_csv(seed)
    cache = proc / "cache.parquet"
    cache_df = _write_cache_parquet(cache)
    meta_path = proc / "roll.json"
    meta_path.write_text(
        json.dumps({"last_front_month": f"{symbol}H6"}), encoding="utf-8"
    )

    overlap = cache_df.tail(60).copy()
    for col in ("Open", "High", "Low", "Close"):
        overlap[col] = overlap[col] * scale
    overlap = overlap.set_index(pd.DatetimeIndex(overlap["DateTime"]), drop=False)
    overlap.index.name = "DateTime"

    feed = MagicMock()
    feed.fetch_historical_bars_by_duration = MagicMock(
        side_effect=lambda **kw: overlap.copy()
    )
    monkeypatch.setattr(dm_module, "_CACHE_BACKUP_DIR", proc / "backups")

    dm = DataManager(
        symbol=symbol,
        seed_path=str(seed),
        cache_path=str(cache),
        master_ledger_path=str(proc / "ledger.parquet"),
        roll_metadata_path=str(meta_path),
        data_client=feed,
        front_month_id=f"{symbol}M6",
    )
    dm.initialize()

    meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
    ledger = pd.read_parquet(proc / "ledger.parquet")
    if "DateTime" in ledger.columns:
        ledger = ledger.sort_values("DateTime")
    else:
        ledger = ledger.sort_index()
    return dm, meta_after, ledger


class TestRollToleranceRegistry:
    def test_registry_all_entries_have_roll_ratio_tolerance(self):
        """New REQUIRED field on every one of the 15 entries (no dataclass
        default — house no-silent-null rule)."""
        assert len(INSTRUMENT_REGISTRY) == 15
        for sym, inst in INSTRUMENT_REGISTRY.items():
            assert hasattr(inst, "roll_ratio_tolerance"), (
                f"{sym}: missing required roll_ratio_tolerance"
            )
            assert 0 < inst.roll_ratio_tolerance < 0.02, (
                f"{sym}: roll_ratio_tolerance {inst.roll_ratio_tolerance!r} "
                "out of sane band (0, 0.02)"
            )

    def test_registry_tolerance_values_pinned(self):
        """Q2 ruling: CL/MCL keep today's 0.01 constant (pin); every other
        symbol gets the ACKed 0.001 (10 bps noise floor)."""
        for sym, inst in INSTRUMENT_REGISTRY.items():
            expected = 0.01 if sym in ("CL", "MCL") else 0.001
            assert inst.roll_ratio_tolerance == expected, (
                f"{sym}: roll_ratio_tolerance {inst.roll_ratio_tolerance!r} "
                f"!= {expected}"
            )

    def test_datamanager_tolerance_from_registry(self, tmp_path):
        def _dm(symbol):
            return DataManager(
                symbol=symbol,
                seed_path=str(tmp_path / "seed.csv"),
                cache_path=str(tmp_path / "cache.parquet"),
                master_ledger_path=str(tmp_path / "ledger.parquet"),
                roll_metadata_path=str(tmp_path / "roll.json"),
                data_client=None,
            )
        assert _dm("CL").roll_ratio_tolerance == 0.01
        assert _dm("ES").roll_ratio_tolerance == 0.001

    @pytest.mark.parametrize(
        "symbol,scale,applied",
        [
            ("ES", 1.004, True),    # ticket case: 40 bps ES gap IS applied
            ("CL", 1.004, False),   # pin: today's CL behavior — swallowed
            ("CL", 1.02, True),     # pin: today's CL behavior — applied
        ],
    )
    def test_roll_gap_application_by_tolerance(
        self, tmp_path, monkeypatch, symbol, scale, applied
    ):
        """End-to-end initialize(): detection fires on the front-month string
        change in every case; the ADJUSTMENT (roll ratio recorded, roll_history
        appended, ledger scaled) happens only when |ratio-1| exceeds the
        instrument's registry tolerance."""
        dm, meta_after, ledger = _run_roll_scenario(
            tmp_path, monkeypatch, symbol, scale
        )
        first_close = float(ledger["Close"].iloc[0])
        history = meta_after.get("roll_history", [])
        # Detection always updates the stored front month (swallow semantics)
        assert meta_after["last_front_month"] == f"{symbol}M6"
        if applied:
            assert dm._roll_ratios, (
                f"{symbol} ratio {scale} must be APPLIED (above tolerance)"
            )
            assert dm._roll_ratios[-1] == pytest.approx(scale, rel=1e-6)
            assert len(history) == 1
            assert history[0]["ratio"] == pytest.approx(scale, rel=1e-4)
            assert history[0]["from"] == f"{symbol}H6"
            assert history[0]["to"] == f"{symbol}M6"
            # Ledger history scaled (seed rows predate the overlap window)
            assert first_close == pytest.approx(100.0 * scale, rel=1e-6)
        else:
            assert dm._roll_ratios == [], (
                f"{symbol} ratio {scale} must be SKIPPED (within tolerance — "
                "today's behavior, pinned)"
            )
            assert history == []
            assert first_close == pytest.approx(100.0, rel=1e-9)


# ===========================================================================
# 5. TestRollMetadataNamespace — per-execution-symbol namespace (T2-C2
#    deferral), C1 roll_history "from", C2 Step-0 ownership filter,
#    legacy-file migration. CL file semantics stay byte-identical
#    (legacy last_front_month still written).
# ===========================================================================

def _ns_dm(tmp_path: Path, front_month_id: str, execution_symbol=None,
           meta_name: str = "roll.json", cache_name: str = "cache.parquet"):
    kwargs = dict(
        symbol="CL",
        seed_path=str(tmp_path / "seed.csv"),
        cache_path=str(tmp_path / cache_name),
        master_ledger_path=str(tmp_path / "ledger.parquet"),
        roll_metadata_path=str(tmp_path / meta_name),
        data_client=None,
        front_month_id=front_month_id,
    )
    if execution_symbol is not None:
        kwargs["execution_symbol"] = execution_symbol
    return DataManager(**kwargs)


def _read_meta(tmp_path: Path, meta_name: str = "roll.json") -> dict:
    return json.loads((tmp_path / meta_name).read_text(encoding="utf-8"))


class TestRollMetadataNamespace:
    def test_execution_symbol_defaults_to_symbol_and_explicit_wins(self, tmp_path):
        """Structural derivation (T2 _brain_symbol precedent): outright
        exec==brain by default; live_trader passes micros explicitly."""
        assert _ns_dm(tmp_path, "CLQ6").execution_symbol == "CL"
        assert _ns_dm(
            tmp_path, "MCLQ6", execution_symbol="MCL"
        ).execution_symbol == "MCL"

    def test_cl_only_save_keeps_legacy_semantics_adds_namespace(self, tmp_path):
        """Byte-semantics pin: a CL-only save writes every legacy key with
        today's exact values; the file only GAINS the namespaced map."""
        dm = _ns_dm(tmp_path, "CLQ6")
        dm._save_roll_metadata()
        meta = _read_meta(tmp_path)
        assert meta["last_front_month"] == "CLQ6"       # legacy key unchanged
        assert "updated_at" in meta
        assert meta["roll_history"] == []
        assert meta["cumulative_ratio"] == 1.0
        assert meta["last_front_month_by_symbol"] == {"CL": "CLQ6"}

    def test_two_symbol_shared_file_no_ping_pong(self, tmp_path):
        """CL + MCL sharing one brain file: each reads its OWN namespaced
        entry — the CLQ6<->MCLQ6 restart ping-pong is dead."""
        dm_cl = _ns_dm(tmp_path, "CLQ6")
        dm_mcl = _ns_dm(tmp_path, "MCLQ6", execution_symbol="MCL")
        dm_cl._save_roll_metadata()
        dm_mcl._save_roll_metadata()   # legacy key now "MCLQ6" — the old trap

        assert dm_cl._detect_rollover() is False, (
            "CL must read its own namespaced entry (CLQ6), not the legacy "
            "key last written by MCL"
        )
        assert dm_mcl._detect_rollover() is False
        # A REAL CL roll is still detected through the namespace
        dm_cl.front_month_id = "CLU6"
        assert dm_cl._detect_rollover() is True

    def test_c1_roll_history_from_uses_own_namespace(self, tmp_path):
        """C1 (MUST): with interleaved two-symbol writes, a CL roll's
        roll_history 'from' must come from the CL namespaced entry — not
        from the legacy key (which MCL wrote last)."""
        dm_cl = _ns_dm(tmp_path, "CLQ6")
        dm_mcl = _ns_dm(tmp_path, "MCLQ6", execution_symbol="MCL")
        dm_cl._save_roll_metadata()
        dm_mcl._save_roll_metadata()   # legacy last_front_month == "MCLQ6"

        dm_cl.front_month_id = "CLU6"
        dm_cl._roll_detected = True
        dm_cl._roll_ratios.append(1.02)   # above CL tolerance -> appended
        dm_cl._roll_timestamps.append(pd.Timestamp("2026-07-04 12:00:00"))
        dm_cl._save_roll_metadata()

        meta = _read_meta(tmp_path)
        assert len(meta["roll_history"]) == 1
        entry = meta["roll_history"][0]
        assert entry["from"] == "CLQ6", (
            "C1 violated: roll_history 'from' read the legacy key "
            f"(cross-symbol contamination): {entry!r}"
        )
        assert entry["to"] == "CLU6"
        # Namespace merged, not replaced: MCL's entry survives the CL write
        assert meta["last_front_month_by_symbol"]["MCL"] == "MCLQ6"
        assert meta["last_front_month_by_symbol"]["CL"] == "CLU6"

    # -- C2: Step-0 restore ownership filter -------------------------------

    _CL_ENTRY = {
        "from": "CLM6", "to": "CLQ6", "ratio": 1.02,
        "timestamp": "2026-05-01T00:00:00",
        "timestamp_cutoff": "2026-05-01T00:00:00",
    }
    _MCL_ENTRY = {
        "from": "MCLM6", "to": "MCLQ6", "ratio": 1.03,
        "timestamp": "2026-05-02T00:00:00",
        "timestamp_cutoff": "2026-05-02T00:00:00",
    }

    def _init_with_history(self, tmp_path, monkeypatch, execution_symbol,
                           front_month_id, history, tag):
        proc = tmp_path / tag
        proc.mkdir()
        _write_cache_parquet(proc / "cache.parquet", n=20, start="2026-06-01")
        (proc / "roll.json").write_text(
            json.dumps({
                "last_front_month": "MCLQ6",
                "last_front_month_by_symbol": {"CL": "CLQ6", "MCL": "MCLQ6"},
                "roll_history": history,
                "cumulative_ratio": 1.0,
            }),
            encoding="utf-8",
        )
        backups = proc / "backups"
        backups.mkdir()
        (backups / "sentinel.parquet").write_text("x", encoding="utf-8")
        monkeypatch.setattr(dm_module, "_CACHE_BACKUP_DIR", backups)
        dm = _ns_dm(proc, front_month_id, execution_symbol=execution_symbol)
        dm.initialize()
        return dm

    def test_c2_step0_restore_cl_owns_only_cl_entries(self, tmp_path, monkeypatch):
        """C2 (MUST): a CL-execution instance must restore ONLY CL-owned
        roll_history entries — never MCL's — so a shared parent+micro file
        cannot double-apply the same economic roll."""
        dm = self._init_with_history(
            tmp_path, monkeypatch, execution_symbol=None,
            front_month_id="CLQ6",
            history=[self._CL_ENTRY, self._MCL_ENTRY], tag="cl",
        )
        assert dm._roll_ratios == [pytest.approx(1.02)], (
            f"CL restore must own only the CL entry, got {dm._roll_ratios}"
        )

    def test_c2_step0_restore_mcl_owns_only_mcl_entries(self, tmp_path, monkeypatch):
        dm = self._init_with_history(
            tmp_path, monkeypatch, execution_symbol="MCL",
            front_month_id="MCLQ6",
            history=[self._CL_ENTRY, self._MCL_ENTRY], tag="mcl",
        )
        assert dm._roll_ratios == [pytest.approx(1.03)], (
            f"MCL restore must own only the MCL entry, got {dm._roll_ratios}"
        )

    def test_c2_cl_only_file_restores_all_entries(self, tmp_path, monkeypatch):
        """Pin (C2 condition): CL-only files pass the ownership filter
        UNCHANGED — every historical CL entry still restores, in order."""
        second = dict(self._CL_ENTRY)
        second.update({"from": "CLQ6", "to": "CLU6", "ratio": 1.5,
                       "timestamp_cutoff": "2026-06-01T00:00:00"})
        dm = self._init_with_history(
            tmp_path, monkeypatch, execution_symbol=None,
            front_month_id="CLU6",
            history=[self._CL_ENTRY, second], tag="cl_only",
        )
        assert dm._roll_ratios == [pytest.approx(1.02), pytest.approx(1.5)]

    def test_legacy_file_migration_loads_without_raising(self, tmp_path):
        """A pre-T5 file (no namespace key) must load quietly:
        - CL reads the legacy key via the startswith ownership check —
          comparison identical to today (no roll for same contract, roll
          for a different CL contract);
        - MCL against a CL-written legacy file -> first-run (False), no
          phantom roll (today's backup-spam trigger)."""
        (tmp_path / "roll.json").write_text(
            json.dumps({"last_front_month": "CLQ6",
                        "updated_at": "2026-07-01T00:00:00"}),
            encoding="utf-8",
        )
        dm_cl = _ns_dm(tmp_path, "CLQ6")
        assert dm_cl._detect_rollover() is False

        dm_mcl = _ns_dm(tmp_path, "MCLQ6", execution_symbol="MCL")
        assert dm_mcl._detect_rollover() is False, (
            "MCL first run against a CL legacy file must be first-run "
            "(ownership check), not a phantom rollover"
        )

        # Real CL roll still detected off the legacy key
        (tmp_path / "roll.json").write_text(
            json.dumps({"last_front_month": "CLH6"}), encoding="utf-8",
        )
        assert dm_cl._detect_rollover() is True


# ===========================================================================
# 6. TestSeedLookbackMath — formula ceil(ceil(4320/bph)*7/5)+28
#    (CL 280 EXACT pin; ES 292; ZC 406) + trim-window consumption +
#    4320-raise message pins
# ===========================================================================

def _write_hourly_parquet_seed(path: Path, span_days: int,
                               end: str = "2026-06-01 00:00") -> pd.DatetimeIndex:
    idx = pd.date_range(end=pd.Timestamp(end), periods=span_days * 24 + 1,
                        freq="1H")
    pd.DataFrame({
        "DateTime": idx,
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
        "Volume": 10.0,
    }).to_parquet(path, engine="pyarrow")
    return idx


def _seed_dm(tmp_path: Path, symbol: str, seed_path: Path) -> "DataManager":
    return DataManager(
        symbol=symbol,
        seed_path=str(seed_path),
        cache_path=str(tmp_path / f"cache_{symbol}.parquet"),
        master_ledger_path=str(tmp_path / f"ledger_{symbol}.parquet"),
        roll_metadata_path=str(tmp_path / f"roll_{symbol}.json"),
        data_client=None,
        bar_size="1 hour",
    )


def _warm_start_stub(tmp_path: Path, cache_name: str, n_1h_bars: int):
    """LiveTrader.__new__ stub reaching _warm_start's post-load validation."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._telegram = MagicMock()
    trader._bar_size = "1h"

    idx5 = pd.date_range("2026-06-01", periods=10, freq="5min")
    df5 = pd.DataFrame({"Close": 100.0}, index=idx5)
    trader.data_manager_5m = MagicMock()
    trader.data_manager_5m.initialize.return_value = df5

    idx1 = pd.date_range("2026-06-01", periods=n_1h_bars, freq="1H")
    df1 = pd.DataFrame({"Close": 100.0}, index=idx1)
    dm1 = MagicMock()
    dm1.initialize.return_value = df1
    dm1.cache_path = tmp_path / cache_name
    trader.data_manager_1h = dm1
    return trader


class TestSeedLookbackMath:
    def test_derive_seed_lookback_days_cl_280_exact(self):
        """HARD PIN: the formula must reproduce CL's legacy constant EXACTLY
        (bars_per_day_1h=24 -> 180 trading days -> 252 -> +28 = 280)."""
        assert derive_seed_lookback_days(24) == 280

    def test_derive_seed_lookback_days_es_292(self):
        assert derive_seed_lookback_days(23) == 292

    def test_derive_seed_lookback_days_zc_406(self):
        assert derive_seed_lookback_days(16) == 406

    def test_derive_seed_lookback_days_rejects_nonpositive(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                derive_seed_lookback_days(bad)

    def test_required_1h_bars_constant_4320(self):
        assert REQUIRED_1H_BARS == 4320

    def test_instance_seed_lookback_from_registry(self, tmp_path):
        seed = tmp_path / "seed.parquet"
        _write_hourly_parquet_seed(seed, span_days=10)
        assert _seed_dm(tmp_path, "CL", seed).seed_lookback_days == 280
        assert _seed_dm(tmp_path, "ES", seed).seed_lookback_days == 292
        assert _seed_dm(tmp_path, "ZC", seed).seed_lookback_days == 406

    def test_cl_seed_trim_pinned_280_days(self, tmp_path):
        """CL trims a 450-day seed to exactly the last 280 days (inclusive
        cutoff) — byte-identical to the legacy _SEED_LOOKBACK_DAYS window."""
        seed = tmp_path / "seed_cl.parquet"
        idx = _write_hourly_parquet_seed(seed, span_days=450)
        out = _seed_dm(tmp_path, "CL", seed)._seed_from_csv()
        assert out.index.max() == idx.max()
        assert out.index.min() == idx.max() - timedelta(days=280)
        assert len(out) == 280 * 24 + 1

    def test_zc_seed_trim_406_days(self, tmp_path):
        """ZC trims a 450-day seed to the formula-derived 406-day window."""
        seed = tmp_path / "seed_zc.parquet"
        idx = _write_hourly_parquet_seed(seed, span_days=450)
        out = _seed_dm(tmp_path, "ZC", seed)._seed_from_csv()
        assert out.index.min() == idx.max() - timedelta(days=406)
        assert len(out) == 406 * 24 + 1

    def test_zc_350_day_seed_untrimmed_no_raise(self, tmp_path):
        """A ~350-day ZC 1h seed sits INSIDE the 406-day window: nothing is
        trimmed and construction completes without raising (the legacy
        280-day trim would have discarded ~70 days — M5's crash precursor).
        The retained bars satisfy the 4320 floor for this synthetic density."""
        seed = tmp_path / "seed_zc350.parquet"
        idx = _write_hourly_parquet_seed(seed, span_days=350)
        out = _seed_dm(tmp_path, "ZC", seed)._seed_from_csv()
        assert out.index.min() == idx.min(), (
            "406-day window must not trim a 350-day seed (legacy 280-day "
            "trim did)"
        )
        assert len(out) == len(idx)
        assert len(out) >= REQUIRED_1H_BARS

    def test_4320_raise_message_cl_byte_identical(self, tmp_path):
        """HARD PIN: the CL cache-validation RuntimeError text is byte-
        identical to HEAD 7a861bb (live_trader.py:2007-2011) — the cache-
        name-derived form must reproduce it exactly for CL."""
        trader = _warm_start_stub(tmp_path, "warm_start_cache_1h.parquet",
                                  n_1h_bars=100)
        with pytest.raises(RuntimeError) as excinfo:
            trader._warm_start()
        expected = (
            "1H cache has only 100 bars — "
            "need 4320 for 1h MACRO_6M feature warmup. "
            "Delete warm_start_cache_1h.parquet to trigger reseed."
        )
        assert str(excinfo.value) == expected

    def test_4320_raise_message_zc_names_zc_cache(self, tmp_path):
        """ZC's message must name ZC's REAL cache file (the HEAD text
        hardcodes the CL filename — wrong, actionable-message defect)."""
        trader = _warm_start_stub(tmp_path, "warm_start_cache_ZC_1h.parquet",
                                  n_1h_bars=100)
        with pytest.raises(RuntimeError) as excinfo:
            trader._warm_start()
        msg = str(excinfo.value)
        assert msg.startswith("1H cache has only 100 bars — need 4320 for 1h")
        assert "Delete warm_start_cache_ZC_1h.parquet to trigger reseed." in msg
        # The HEAD defect: the CL filename must be gone from ZC's message
        # ("warm_start_cache_ZC_1h" does not contain "warm_start_cache_1h").
        assert "Delete warm_start_cache_1h.parquet" not in msg


# ===========================================================================
# 7. TestLiveTraderSessionWiring — construction seams (mirrors
#    tests/test_symbol_data_paths.py::TestLiveTraderDataManagerPaths) +
#    frozen-clock watchdog integration
# ===========================================================================

class _DummyStrategy:
    """Minimal strategy stub mirroring tests/test_symbol_data_paths.py."""

    def __init__(self, config: dict):
        self.feature_names = ["MACD"]
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config


def _cfg(symbol: str) -> dict:
    return {
        "nickname": f"{symbol}_style",
        "execution_symbol": symbol,
        "bar_size": "1h",
    }


def _build_trader_with_mock_dm(cfg: dict, tmp_path: Path):
    with patch("src.live_execution.live_trader.DataManager") as MockDM, patch(
        "pathlib.Path.exists", return_value=True
    ):
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=_DummyStrategy(config=cfg),
            db_path=str(tmp_path / "telemetry.db"),
            dry_run=True,
        )
    trader._telegram = MagicMock()
    return trader, MockDM


class TestLiveTraderSessionWiring:
    def test_cl_market_status_byte_match_via_calendar(self, tmp_path):
        """A CL trader's _get_market_status must return the calendar's GLOBEX
        strings — byte-equal to the frozen legacy body at every pinned
        instant (heartbeat market=%s line unchanged)."""
        trader, _ = _build_trader_with_mock_dm(_cfg("CL"), tmp_path)
        for wall, expected in _CL_PINNED_CASES:
            t = _utc_from_et(*wall)
            got = trader._get_market_status(t)
            assert got == expected
            assert got == _legacy_cl_market_status(t)
            assert got == market_status(_CL, t)  # delegation, not a fork

    def test_cl_dm_bars_per_day_pins_288_24(self, tmp_path):
        """CL pins: registry-driven construction feeds the SAME legacy
        literals (bars_per_day_5m=288, bars_per_day_1h=24)."""
        _, MockDM = _build_trader_with_mock_dm(_cfg("CL"), tmp_path)
        assert MockDM.call_count == 2
        assert MockDM.call_args_list[0].kwargs.get("bars_per_day") == 288
        assert MockDM.call_args_list[1].kwargs.get("bars_per_day") == 24

    def test_es_dm_bars_per_day_from_registry_276_23(self, tmp_path):
        """ES must get ES's registry values (276/23), not CL's literals."""
        _, MockDM = _build_trader_with_mock_dm(_cfg("ES"), tmp_path)
        assert MockDM.call_args_list[0].kwargs.get("bars_per_day") == 276
        assert MockDM.call_args_list[1].kwargs.get("bars_per_day") == 23

    def test_zc_dm_bars_per_day_from_registry_200_16(self, tmp_path):
        _, MockDM = _build_trader_with_mock_dm(_cfg("ZC"), tmp_path)
        assert MockDM.call_args_list[0].kwargs.get("bars_per_day") == 200
        assert MockDM.call_args_list[1].kwargs.get("bars_per_day") == 16

    def test_dm_execution_symbol_wiring(self, tmp_path):
        """The roll-metadata namespace only works if live_trader passes the
        EXECUTION symbol: CL->'CL'; MCL-> brain symbol 'CL' + execution
        'MCL'; ZC->'ZC'. Both managers of one process share it."""
        _, cl_dm = _build_trader_with_mock_dm(_cfg("CL"), tmp_path)
        for call in cl_dm.call_args_list:
            assert call.kwargs.get("symbol") == "CL"
            assert call.kwargs.get("execution_symbol") == "CL"

        _, mcl_dm = _build_trader_with_mock_dm(_cfg("MCL"), tmp_path)
        for call in mcl_dm.call_args_list:
            assert call.kwargs.get("symbol") == "CL"          # brain-keyed
            assert call.kwargs.get("execution_symbol") == "MCL"

        _, zc_dm = _build_trader_with_mock_dm(_cfg("ZC"), tmp_path)
        for call in zc_dm.call_args_list:
            assert call.kwargs.get("symbol") == "ZC"
            assert call.kwargs.get("execution_symbol") == "ZC"

    def test_stale_threshold_pin_30(self):
        """The 30-minute threshold constant stays pinned (audit §4b:
        bar-size-driven, not instrument-driven).

        EVOLVED 15 -> 30 per the explicit 2026-07-06 USER DIRECTIVE
        (ticket watchdog-telegram-throttle_07062026_0007 — the
        pin-invalidating event; Impact-Reviewer-approved lock evolution).

        T7 CLARIFICATION (t7-es-ops-runway_07052026_0214, impact_review
        C7 — docstring-scope only, assertion unchanged): the threshold
        applies to 5m-ENABLED instances (enable_5m_stream true — every
        CL config; the flag defaults true), watching _last_bar_time_5m.
        Hourly-only instances (enable_5m_stream false) watch
        _last_bar_time_1h against the separate
        _STALE_BAR_THRESHOLD_MINUTES_1H = 135, pinned in
        tests/test_hourly_only_equity_session.py."""
        assert lt_module._STALE_BAR_THRESHOLD_MINUTES == 30

    def test_zc_halt_hours_watchdog_false_despite_stale_clock(self):
        """Ticket case M4: ZC Tue 15:00 CT (daily halt), last bar 13:15 CT
        (105 min stale) -> the session-aware gate returns False. No
        Telegram, no disconnect — the grain reconnect storm is dead."""
        trader = _watchdog_stub("ZC")
        trader._last_bar_time_5m = pd.Timestamp("2026-07-07 18:15:00")  # 13:15 CT
        with _frozen_lt_clock(_utc_from_ct(2026, 7, 7, 15, 0)):
            result = trader._check_stale_bars()
        assert result is False
        trader._telegram.send.assert_not_called()
        trader.data_client.disconnect.assert_not_called()
        trader.exec_client.disconnect.assert_not_called()
        assert trader._subscriptions_lost is False

    def test_cl_monday_reopen_graced_false(self):
        """Ex-Q1 PIN, updated by cl-watchdog-reopen-grace_07052026_0001
        (operator-authorized 2026-07-07 after two consecutive days of the
        scheduled false fire): Mon 18:03 ET reopen, last bar 16:55 ET ->
        status OPEN, anchor = Monday's own 18:00 ET open, 3 min stale ->
        False. No disconnect, no spurious reconnect."""
        trader = _watchdog_stub("CL")
        trader._last_bar_time_5m = pd.Timestamp("2026-07-13 20:55:00")  # 16:55 ET
        with _frozen_lt_clock(_utc_from_et(2026, 7, 13, 18, 3)):
            result = trader._check_stale_bars()
        assert result is False, (
            "reopen grace violated: the stale clock must restart at the "
            "17:00 CT open (cl-watchdog-reopen-grace_07052026_0001)"
        )
        trader.data_client.disconnect.assert_not_called()
        trader.exec_client.disconnect.assert_not_called()
        assert trader._subscriptions_lost is False

    def test_cl_sunday_reopen_graced_false(self):
        """Ex-Q1 PIN (same ticket): Sunday 18:05 ET open, last bar Friday
        16:55 ET -> anchored at Sun 18:00 ET, 5 min stale -> False."""
        trader = _watchdog_stub("CL")
        trader._last_bar_time_5m = pd.Timestamp("2026-07-10 20:55:00")  # Fri 16:55 ET
        with _frozen_lt_clock(_utc_from_et(2026, 7, 12, 18, 5)):
            result = trader._check_stale_bars()
        assert result is False
        trader._telegram.send.assert_not_called()

    def test_cl_open_hours_31min_stale_true(self):
        """CL arithmetic pin: Mon 12:00 ET, 31 min since last bar -> True
        (vector 16 -> 31 per the 2026-07-06 directive's 30-min threshold,
        ticket watchdog-telegram-throttle_07062026_0007)."""
        now = _utc_from_et(2026, 7, 13, 12, 0)
        trader = _watchdog_stub("CL")
        trader._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=31))
        with _frozen_lt_clock(now):
            assert trader._check_stale_bars() is True

    def test_cl_open_hours_29min_stale_false(self):
        """NEW boundary companion (watchdog-telegram-throttle_07062026_0007):
        29 min < the 30-min threshold -> False, no Telegram."""
        now = _utc_from_et(2026, 7, 13, 12, 0)
        trader = _watchdog_stub("CL")
        trader._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=29))
        with _frozen_lt_clock(now):
            assert trader._check_stale_bars() is False
        trader._telegram.send.assert_not_called()

    def test_cl_open_hours_10min_stale_false(self):
        now = _utc_from_et(2026, 7, 13, 12, 0)
        trader = _watchdog_stub("CL")
        trader._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=10))
        with _frozen_lt_clock(now):
            assert trader._check_stale_bars() is False
        trader._telegram.send.assert_not_called()

    def test_no_bars_yet_returns_false(self):
        """Warm start in progress (_last_bar_time_5m None) -> False
        (unchanged legacy guard)."""
        trader = _watchdog_stub("CL")
        trader._last_bar_time_5m = None
        with _frozen_lt_clock(_utc_from_et(2026, 7, 13, 12, 0)):
            assert trader._check_stale_bars() is False
