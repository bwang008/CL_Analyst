"""
Unit tests for ExecutionGuard class.
"""

from __future__ import annotations

import logging
import pandas as pd
import pytest

from src.live_execution.execution_guard import ExecutionGuard


@pytest.fixture
def default_config() -> dict:
    """Default risk configuration matching configs/global_risk_filters.json."""
    return {
        "blocked_entry_hours_est": [8, 11],
        "block_long_weekends": True,
        "long_weekend_block_scope": ["BEFORE_LONG_WEEKEND", "AFTER_LONG_WEEKEND"],
        "override_global_filters": False,
    }


def test_blocked_hours(default_config: dict, caplog: pytest.LogCaptureFixture) -> None:
    """Verify that blocked hours return False and log correct reason."""
    guard = ExecutionGuard(default_config)

    # Let's pick a regular Tuesday (not a holiday transition)
    # 2025-02-11 is a Tuesday
    ts_08 = pd.Timestamp("2025-02-11 08:00:00")
    ts_11 = pd.Timestamp("2025-02-11 11:00:00")

    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_08)
        assert "BLOCKED: 08:00 bar in blocked_entry_hours_est" in caplog.text

    caplog.clear()

    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_11)
        assert "BLOCKED: 11:00 bar in blocked_entry_hours_est" in caplog.text


def test_non_blocked_hours(default_config: dict) -> None:
    """Verify that non-blocked hours on a regular day return True."""
    guard = ExecutionGuard(default_config)

    # 2025-02-11 is a Tuesday
    for hour in [0, 5, 7, 9, 10, 12, 15, 23]:
        ts = pd.Timestamp(f"2025-02-11 {hour:02d}:00:00")
        assert guard.is_entry_allowed(ts), f"Hour {hour} should be allowed"


def test_long_weekend_transitions(default_config: dict, caplog: pytest.LogCaptureFixture) -> None:
    """
    Verify that toxic long-weekend transitions return False.
    Monday Holiday (e.g. MLK Day Monday 2025-01-20):
      - Friday before: 2025-01-17
      - Tuesday after: 2025-01-21
    Friday Holiday (e.g. Good Friday Friday 2025-04-18):
      - Thursday before: 2025-04-17
      - Monday after: 2025-04-21
    """
    guard = ExecutionGuard(default_config)

    # 1. Monday Holiday (MLK Day 2025-01-20)
    # Friday before (2025-01-17) at 10:00 AM (not blocked by hour filter)
    ts_friday_before = pd.Timestamp("2025-01-17 10:00:00")
    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_friday_before)
        assert "BLOCKED: BEFORE_LONG_WEEKEND adjacent to 2025-01-20 MLK Day" in caplog.text

    caplog.clear()

    # Tuesday after (2025-01-21) at 10:00 AM (not blocked by hour filter)
    ts_tuesday_after = pd.Timestamp("2025-01-21 10:00:00")
    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_tuesday_after)
        assert "BLOCKED: AFTER_LONG_WEEKEND adjacent to 2025-01-20 MLK Day" in caplog.text

    caplog.clear()

    # 2. Friday Holiday (Good Friday 2025-04-18)
    # Thursday before (2025-04-17) at 10:00 AM
    ts_thursday_before = pd.Timestamp("2025-04-17 10:00:00")
    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_thursday_before)
        assert "BLOCKED: BEFORE_LONG_WEEKEND adjacent to 2025-04-18 Good Friday" in caplog.text

    caplog.clear()

    # Monday after (2025-04-21) at 10:00 AM
    ts_monday_after = pd.Timestamp("2025-04-21 10:00:00")
    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_monday_after)
        assert "BLOCKED: AFTER_LONG_WEEKEND adjacent to 2025-04-18 Good Friday" in caplog.text


def test_regular_days_near_holidays(default_config: dict) -> None:
    """Verify that regular days near holidays that aren't toxic transitions return True."""
    guard = ExecutionGuard(default_config)

    # Monday Holiday: 2025-01-20 (MLK Day)
    # Wednesday before (2025-01-15) should be allowed
    assert guard.is_entry_allowed(pd.Timestamp("2025-01-15 10:00:00"))

    # Wednesday after (2025-01-22) should be allowed
    assert guard.is_entry_allowed(pd.Timestamp("2025-01-22 10:00:00"))

    # Friday Holiday: 2025-04-18 (Good Friday)
    # Tuesday before (2025-04-15) should be allowed
    assert guard.is_entry_allowed(pd.Timestamp("2025-04-15 10:00:00"))

    # Tuesday after (2025-04-22) should be allowed
    assert guard.is_entry_allowed(pd.Timestamp("2025-04-22 10:00:00"))


def test_holiday_itself_not_blocked(default_config: dict) -> None:
    """Verify that actual holidays shortened sessions themselves are NOT blocked by holiday logic."""
    guard = ExecutionGuard(default_config)

    # Monday Holiday: 2025-01-20 (MLK Day) at 10:00 AM
    assert guard.is_entry_allowed(pd.Timestamp("2025-01-20 10:00:00"))

    # Friday Holiday: 2025-04-18 (Good Friday) at 10:00 AM
    assert guard.is_entry_allowed(pd.Timestamp("2025-04-18 10:00:00"))


def test_override_global_filters(default_config: dict) -> None:
    """Verify that when override_global_filters is True, all filters are bypassed."""
    config = default_config.copy()
    config["override_global_filters"] = True
    guard = ExecutionGuard(config)

    # Blocked hours
    assert guard.is_entry_allowed(pd.Timestamp("2025-02-11 08:00:00"))
    assert guard.is_entry_allowed(pd.Timestamp("2025-02-11 11:00:00"))

    # Long weekend transitions
    assert guard.is_entry_allowed(pd.Timestamp("2025-01-17 10:00:00"))  # Friday before MLK Day
    assert guard.is_entry_allowed(pd.Timestamp("2025-01-21 10:00:00"))  # Tuesday after MLK Day
    assert guard.is_entry_allowed(pd.Timestamp("2025-04-17 10:00:00"))  # Thursday before Good Friday
    assert guard.is_entry_allowed(pd.Timestamp("2025-04-21 10:00:00"))  # Monday after Good Friday


def test_timezone_normalization(default_config: dict) -> None:
    """Verify that tz-aware and tz-naive timestamps are normalized properly to America/New_York."""
    guard = ExecutionGuard(default_config)

    # 08:00 AM EST is 13:00 UTC
    # Passing 13:00 UTC should be blocked because it normalizes to 08:00 EST
    ts_utc = pd.Timestamp("2025-02-11 13:00:00", tz="UTC")
    assert not guard.is_entry_allowed(ts_utc)

    # 08:00 AM EST is 08:00 EST (America/New_York)
    ts_est = pd.Timestamp("2025-02-11 08:00:00", tz="America/New_York")
    assert not guard.is_entry_allowed(ts_est)

    # 10:00 AM EST is 15:00 UTC. It should be allowed.
    ts_allow_utc = pd.Timestamp("2025-02-11 15:00:00", tz="UTC")
    assert guard.is_entry_allowed(ts_allow_utc)


def test_year_boundary_new_years(default_config: dict) -> None:
    """Verify that year-end / year-start holidays don't misfire across year boundaries.

    New Year's Day 2025 falls on a Wednesday (not Monday or Friday), so
    it does NOT create a long weekend pattern. No adjacent dates should
    be blocked by the long weekend filter.
    """
    guard = ExecutionGuard(default_config)

    # Dec 30, 2024 (Tuesday) — should NOT be blocked
    assert guard.is_entry_allowed(pd.Timestamp("2024-12-30 10:00:00"))
    # Dec 31, 2024 (Tuesday) — should NOT be blocked
    assert guard.is_entry_allowed(pd.Timestamp("2024-12-31 10:00:00"))
    # Jan 1, 2025 (Wednesday, actual holiday) — should NOT be blocked (holiday pass-through)
    assert guard.is_entry_allowed(pd.Timestamp("2025-01-01 10:00:00"))
    # Jan 2, 2025 (Thursday) — should NOT be blocked
    assert guard.is_entry_allowed(pd.Timestamp("2025-01-02 10:00:00"))


def test_year_boundary_new_years_monday_holiday(default_config: dict) -> None:
    """Verify long weekend logic when New Year's Day falls on a Monday.

    New Year's Day 2024 falls on Monday Jan 1, creating a long weekend.
    Friday Dec 29, 2023 should be BEFORE_LONG_WEEKEND.
    Tuesday Jan 2, 2024 should be AFTER_LONG_WEEKEND.
    This tests cross-year pre-computation.
    """
    guard = ExecutionGuard(default_config)

    # Friday Dec 29, 2023 — BEFORE_LONG_WEEKEND (Friday before Monday Jan 1 2024)
    assert not guard.is_entry_allowed(pd.Timestamp("2023-12-29 10:00:00"))
    # Monday Jan 1, 2024 (actual holiday) — pass-through
    assert guard.is_entry_allowed(pd.Timestamp("2024-01-01 10:00:00"))
    # Tuesday Jan 2, 2024 — AFTER_LONG_WEEKEND
    assert not guard.is_entry_allowed(pd.Timestamp("2024-01-02 10:00:00"))


def test_edge_triggered_logging(default_config: dict, caplog: pytest.LogCaptureFixture) -> None:
    """Verify that repeated blocked checks with the same reason only log once (edge-triggered)."""
    guard = ExecutionGuard(default_config)

    # Call is_entry_allowed 10 times on the same blocked hour
    ts_blocked = pd.Timestamp("2025-02-11 08:00:00")
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            assert not guard.is_entry_allowed(ts_blocked)

    # Should only have 1 log warning, not 10
    blocked_messages = [r for r in caplog.records if "BLOCKED" in r.message]
    assert len(blocked_messages) == 1, (
        f"Expected 1 log message but got {len(blocked_messages)}"
    )

    caplog.clear()

    # Now call with an allowed timestamp to reset state, then block again
    ts_allowed = pd.Timestamp("2025-02-11 10:00:00")
    guard.is_entry_allowed(ts_allowed)

    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_blocked)

    # Should log again since the state was reset
    blocked_messages = [r for r in caplog.records if "BLOCKED" in r.message]
    assert len(blocked_messages) == 1


def test_day_specific_blocked_hours(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that day-specific blocked hours block entries correctly only on those days,
    and have no effect on other days, and that empty configurations have no effect.
    """
    config = {
        "blocked_entry_hours_est": [8],
        "blocked_entry_hours_by_day": {"Wednesday": [11]},
        "block_long_weekends": False,
        "override_global_filters": False,
    }
    guard = ExecutionGuard(config)

    # Wednesday 2025-02-12
    ts_wed_11 = pd.Timestamp("2025-02-12 11:00:00")
    # Monday 2025-02-10
    ts_mon_11 = pd.Timestamp("2025-02-10 11:00:00")
    # Tuesday 2025-02-11
    ts_tue_11 = pd.Timestamp("2025-02-11 11:00:00")
    # Thursday 2025-02-13
    ts_thu_11 = pd.Timestamp("2025-02-13 11:00:00")
    # Friday 2025-02-14
    ts_fri_11 = pd.Timestamp("2025-02-14 11:00:00")

    # 1. Hour 11 is blocked on Wednesday
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert not guard.is_entry_allowed(ts_wed_11)
        assert "BLOCKED: 11:00 bar on Wednesday (blocked_entry_hours_by_day)" in caplog.text

    # 2. Hour 11 is NOT blocked on Monday, Tuesday, Thursday, Friday
    for ts in [ts_mon_11, ts_tue_11, ts_thu_11, ts_fri_11]:
        assert guard.is_entry_allowed(ts), f"Hour 11 should be allowed on {ts.strftime('%A')}"

    # 3. Hour 8 is blocked every day (retains global hourly config blocking)
    for day_ts in [ts_mon_11, ts_tue_11, ts_wed_11, ts_thu_11, ts_fri_11]:
        ts_8 = day_ts.replace(hour=8)
        assert not guard.is_entry_allowed(ts_8), f"Hour 8 should be blocked on {ts_8.strftime('%A')}"

    # 4. Empty blocked_entry_hours_by_day has no effect
    config_empty = {
        "blocked_entry_hours_est": [8],
        "blocked_entry_hours_by_day": {},
        "block_long_weekends": False,
        "override_global_filters": False,
    }
    guard_empty = ExecutionGuard(config_empty)
    assert guard_empty.is_entry_allowed(ts_wed_11)
    assert guard_empty.is_entry_allowed(ts_mon_11)

