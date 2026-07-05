"""
Session calendars for the live engine (T5, t5-hours-watchdog-rollover).

Two explicit calendars, dispatched on the registry ``session_hours_ct``
tuple (no generic segment engine — only two shapes exist in
INSTRUMENT_REGISTRY; an unknown shape RAISES, no silent default):

* GLOBEX (``(("17:00", "16:00"),)``) — the legacy CL body from
  ``LiveTrader._get_market_status`` at HEAD 7a861bb, moved VERBATIM
  (byte-identical status strings; a minute-by-minute DST-year sweep in
  ``tests/test_session_watchdog_rollover.py`` pins it against a frozen
  transcription). ``session_open_anchor`` is always ``None`` for GLOBEX,
  which keeps the CL stale-bar watchdog arithmetic bit-identical —
  including the pre-existing reopen false-positive (Q1: PINNED AS-IS;
  follow-up ticket ``cl-watchdog-reopen-grace_07052026_0001``).

* GRAINS (``(("19:00", "07:45"), ("08:30", "13:20"))``) — CBOT grains
  evaluated in America/Chicago: overnight 19:00-07:45 CT, day
  08:30-13:20 CT, morning halt [07:45, 08:30), afternoon/evening halt
  [13:20, 19:00), weekend from Fri 13:20 CT to Sun 19:00 CT.
  ``session_open_anchor`` returns the most recent session-open instant
  (08:30 CT, 19:00 CT, or Sun 19:00 CT) as a tz-naive UTC datetime so
  the watchdog's stale clock restarts at each reopen (kills the grain
  reconnect storm, M4).

Known accepted limits (parity with today's CL behavior):
  * No exchange-holiday awareness and no early closes — CME holidays
    read as regular weekdays, exactly as the legacy CL body did.
  * C4 (impact_review, Q4 amendment): ES/NQ have a REAL daily
    15:15-15:30 CT trading halt that the GLOBEX calendar does NOT model
    (it reports OPEN there). Once ES trades live, the stale-bar watchdog
    has a daily false-positive window of roughly 15:25-15:35 CT — the
    same defect shape as the Q1-pinned CL reopen false-positive.
    Modeling an equity-index session shape (registry tuple + calendar
    branch — the dispatch below already supports adding one) is a
    REQUIRED precondition for the ES launch (T7 checklist); it is
    deliberately NOT built here (ZC is the near-term launch driver).

Leaf module: stdlib + pytz + ``src.core.instrument_master`` only, so
``live_trader.py`` can import it top-level without cycles. All functions
take an injected ``utc_now`` (tz-naive UTC) for deterministic
frozen-clock testing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pytz

from src.core.instrument_master import (
    _GLOBEX_SESSION,
    _GRAINS_SESSION,
    Instrument,
)

_CT = pytz.timezone("America/Chicago")


def _unsupported_session_shape(instrument: Instrument) -> ValueError:
    return ValueError(
        f"Unsupported session_hours_ct {instrument.session_hours_ct!r} for "
        f"{instrument.symbol}: no calendar implementation. Supported: "
        "GLOBEX (('17:00', '16:00'),) and "
        "GRAINS (('19:00', '07:45'), ('08:30', '13:20'))."
    )


def _globex_market_status(utc_now: datetime) -> str:
    """GLOBEX (CL-legacy) market status.

    Body moved VERBATIM from ``LiveTrader._get_market_status`` at HEAD
    7a861bb (live_trader.py:3940-3959) — do NOT edit: the status strings
    are byte-frozen (heartbeat ``market=%s`` line + watchdog gate).

    CL futures trade Sunday 18:00 ET → Friday 17:00 ET with a
    daily maintenance halt 17:00-18:00 ET (Mon-Thu). The Mon-Thu
    17:00-18:00 ET branch IS the CME 16:00-17:00 CT maintenance break
    (verified for the whole GLOBEX family — impact_review V4).
    """
    import pytz
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


def _grains_market_status(utc_now: datetime) -> str:
    """CBOT grains (ZC/ZS) market status, evaluated in America/Chicago.

    Sessions (audit §4a): overnight Sun-Thu 19:00 → 07:45 CT and day
    Mon-Fri 08:30 → 13:20 CT. The evening halt is [13:20, 19:00) —
    Mon-Thu ≥ 19:00 CT is OPEN (overnight session); Friday closes for
    the weekend at 13:20 CT (no Friday evening session).

    Only "OPEN" is byte-pinned (the watchdog gate compares ``== "OPEN"``);
    the CLOSED strings are shape-pinned (startswith CLOSED + marker).
    """
    ct_now = utc_now.replace(tzinfo=pytz.utc).astimezone(_CT)
    weekday = ct_now.weekday()  # 0=Mon … 6=Sun
    hour = ct_now.hour
    minute = ct_now.minute

    # Saturday: always closed
    if weekday == 5:
        return "CLOSED (weekend — opens Sun 7pm CT)"
    # Sunday before 19:00 CT: closed
    if weekday == 6 and hour < 19:
        return "CLOSED (weekend — opens Sun 7pm CT)"
    # Friday from the 13:20 CT day-session close: closed for the weekend
    if weekday == 4 and (hour > 13 or (hour == 13 and minute >= 20)):
        return "CLOSED (weekend — opens Sun 7pm CT)"
    # Mon-Fri morning halt [07:45, 08:30) CT
    if weekday <= 4 and ((hour == 7 and minute >= 45) or (hour == 8 and minute < 30)):
        return "CLOSED (daily halt — reopens 8:30am CT)"
    # Mon-Thu afternoon/evening halt [13:20, 19:00) CT
    if weekday <= 3 and ((hour == 13 and minute >= 20) or 14 <= hour < 19):
        return "CLOSED (daily halt — reopens 7pm CT)"
    return "OPEN"


def _grains_session_open_anchor(utc_now: datetime) -> datetime:
    """Most recent grains session-open instant, as tz-naive UTC.

    Opens: Mon-Fri 08:30 CT (day session) and Sun-Thu 19:00 CT
    (overnight session). Walks back day by day (the loop bound is
    generous; the farthest open from any instant is < 3 days back).
    """
    ct_now = utc_now.replace(tzinfo=pytz.utc).astimezone(_CT)
    for days_back in range(0, 9):
        day = (ct_now - timedelta(days=days_back)).date()
        candidates = []
        if day.weekday() <= 4:                            # Mon-Fri 08:30 CT
            candidates.append((8, 30))
        if day.weekday() <= 3 or day.weekday() == 6:      # Sun-Thu 19:00 CT
            candidates.append((19, 0))
        for hh, mm in sorted(candidates, reverse=True):
            wall = _CT.localize(datetime(day.year, day.month, day.day, hh, mm))
            if wall <= ct_now:
                return wall.astimezone(pytz.utc).replace(tzinfo=None)
    raise RuntimeError(
        f"No grains session open found within 9 days before {utc_now!r} — "
        "calendar bug."
    )


def market_status(instrument: Instrument, utc_now: datetime) -> str:
    """Human-readable market status for an instrument at ``utc_now``.

    Dispatches on the registry ``session_hours_ct`` shape; an unknown
    shape raises ValueError (no silent default).

    Args:
        instrument: Registry Instrument (drives the calendar dispatch).
        utc_now: Current time in UTC (tz-naive) — injected for testing.

    Returns:
        "OPEN" (byte-exact — the watchdog gate) or a "CLOSED (...)" string.
    """
    shape = instrument.session_hours_ct
    if shape == _GLOBEX_SESSION:
        return _globex_market_status(utc_now)
    if shape == _GRAINS_SESSION:
        return _grains_market_status(utc_now)
    raise _unsupported_session_shape(instrument)


def session_open_anchor(
    instrument: Instrument, utc_now: datetime
) -> Optional[datetime]:
    """Most recent session-open instant for the watchdog reopen grace.

    GLOBEX → always ``None``: the stale-bar arithmetic stays bit-identical
    to the legacy CL behavior, INCLUDING the pre-existing reopen
    false-positive (Q1 — pinned as-is, fixed by the follow-up ticket).

    GRAINS → tz-naive UTC datetime of the most recent 08:30 / 19:00 CT
    open, so the stale clock restarts after each halt.
    """
    shape = instrument.session_hours_ct
    if shape == _GLOBEX_SESSION:
        return None
    if shape == _GRAINS_SESSION:
        return _grains_session_open_anchor(utc_now)
    raise _unsupported_session_shape(instrument)
