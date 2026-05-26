"""
Global Execution Guard for CL crude oil futures trading system.
Blocks new trade entries during structurally toxic times and dates.
"""

from __future__ import annotations

import logging
from datetime import timedelta
import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    USFederalHolidayCalendar,
)

log = logging.getLogger(__name__)


def get_friendly_holiday_name(name: str) -> str:
    """Map standard calendar holiday names to clean display names."""
    name_lower = name.lower()
    if "martin luther" in name_lower or "mlk" in name_lower:
        return "MLK Day"
    if "washington" in name_lower or "president" in name_lower:
        return "Presidents Day"
    if "memorial" in name_lower:
        return "Memorial Day"
    if "juneteenth" in name_lower:
        return "Juneteenth"
    if "july 4" in name_lower or "independence" in name_lower:
        return "Independence Day"
    if "labor" in name_lower:
        return "Labor Day"
    if "columbus" in name_lower:
        return "Columbus Day"
    if "veteran" in name_lower:
        return "Veterans Day"
    if "thanksgiving" in name_lower:
        return "Thanksgiving"
    if "christmas" in name_lower:
        return "Christmas"
    if "good friday" in name_lower:
        return "Good Friday"
    if "new year" in name_lower:
        return "New Year's Day"
    return name


class CMEMarketHolidayCalendar(AbstractHolidayCalendar):
    """Custom calendar that includes standard US Federal Holidays and Good Friday."""
    rules = USFederalHolidayCalendar.rules + [
        GoodFriday,
    ]


class ExecutionGuard:
    """Guard class responsible for blocking new entries during blocked periods."""

    def __init__(self, config: dict):
        """
        Accepts the merged config dict containing risk configuration parameters.

        Parameters
        ----------
        config : dict
            Configuration dictionary containing:
            - blocked_entry_hours_est: list of ints (e.g. [8, 11])
            - block_long_weekends: bool
            - long_weekend_block_scope: list of str (default: BEFORE_LONG_WEEKEND, AFTER_LONG_WEEKEND)
            - override_global_filters: bool (default: False)
        """
        self.config = config
        self.blocked_entry_hours_est = config.get("blocked_entry_hours_est", [])
        self.block_long_weekends = config.get("block_long_weekends", False)
        self.long_weekend_block_scope = config.get(
            "long_weekend_block_scope",
            ["BEFORE_LONG_WEEKEND", "AFTER_LONG_WEEKEND"],
        )
        self.override_global_filters = config.get("override_global_filters", False)

        # Cache of pre-computed toxic dates
        # Mapping: date -> (scope_reason, holiday_date, friendly_name)
        self._toxic_dates = {}
        # Keep track of actual holidays to never block them
        self._holiday_dates = set()
        # Track years pre-computed
        self._precomputed_years = set()

        # Edge-triggered logging: only log when the block reason changes
        self._last_block_reason: str | None = None

    def _precompute_year(self, year: int) -> None:
        """Pre-compute CME holidays and toxic adjacent days for a specific year."""
        if year in self._precomputed_years:
            return

        start_date = pd.Timestamp(f"{year}-01-01")
        end_date = pd.Timestamp(f"{year}-12-31")

        cal = CMEMarketHolidayCalendar()
        holidays_series = cal.holidays(start=start_date, end=end_date, return_name=True)

        for hol_ts, name in holidays_series.items():
            hol_date = hol_ts.date()
            self._holiday_dates.add(hol_date)
            weekday = hol_date.weekday()
            friendly_name = get_friendly_holiday_name(name)

            # Long Weekend adjacent toxic patterns:
            # BEFORE_LONG_WEEKEND: Friday before a Monday holiday, or Thursday before a Friday holiday.
            # AFTER_LONG_WEEKEND: Tuesday after a Monday holiday, or Monday after a Friday holiday.
            if weekday == 0:  # Monday holiday
                friday_before = hol_date - timedelta(days=3)
                self._toxic_dates[friday_before] = (
                    "BEFORE_LONG_WEEKEND",
                    hol_date,
                    friendly_name,
                )

                tuesday_after = hol_date + timedelta(days=1)
                self._toxic_dates[tuesday_after] = (
                    "AFTER_LONG_WEEKEND",
                    hol_date,
                    friendly_name,
                )
            elif weekday == 4:  # Friday holiday
                thursday_before = hol_date - timedelta(days=1)
                self._toxic_dates[thursday_before] = (
                    "BEFORE_LONG_WEEKEND",
                    hol_date,
                    friendly_name,
                )

                monday_after = hol_date + timedelta(days=3)
                self._toxic_dates[monday_after] = (
                    "AFTER_LONG_WEEKEND",
                    hol_date,
                    friendly_name,
                )

        self._precomputed_years.add(year)

    def is_entry_allowed(self, timestamp: pd.Timestamp) -> bool:
        """
        Determine if new trade entries are allowed at the given timestamp.

        Parameters
        ----------
        timestamp : pd.Timestamp
            The timestamp of the current bar (represents start of the hour).

        Returns
        -------
        bool
            True if entry is allowed, False if blocked.
        """
        if self.override_global_filters:
            return True

        # Standardize timezone to America/New_York (EST/EDT)
        if timestamp.tzinfo is not None:
            ts_est = timestamp.tz_convert("America/New_York")
        else:
            # Naive timestamp is assumed to be in EST/EDT as per CL hourly system design
            ts_est = timestamp

        # 1. Time Block Logic (check bar hour)
        hour = ts_est.hour
        if hour in self.blocked_entry_hours_est:
            reason = f"BLOCKED: {hour:02d}:00 bar in blocked_entry_hours_est"
            self._log_block(reason)
            return False

        # 2. Holiday / Long Weekend Block Logic
        if self.block_long_weekends:
            year = ts_est.year
            # Ensure adjacent years are pre-computed in case the date is close to year boundaries
            for y in (year - 1, year, year + 1):
                self._precompute_year(y)

            d = ts_est.date()
            if d in self._holiday_dates:
                # Do NOT block the actual holiday shortened session day itself (profitable)
                self._clear_block()
                return True

            if d in self._toxic_dates:
                scope, hol_date, hol_name = self._toxic_dates[d]
                if scope in self.long_weekend_block_scope:
                    reason = (
                        f"BLOCKED: {scope} adjacent to "
                        f"{hol_date.strftime('%Y-%m-%d')} {hol_name}"
                    )
                    self._log_block(reason)
                    return False

        self._clear_block()
        return True

    # ------------------------------------------------------------------
    # Edge-triggered logging helpers
    # ------------------------------------------------------------------

    def _log_block(self, reason: str) -> None:
        """Emit a warning only when the block reason changes (edge-triggered)."""
        if reason != self._last_block_reason:
            log.warning(reason)
            self._last_block_reason = reason

    def _clear_block(self) -> None:
        """Reset the block tracker when entry becomes allowed again."""
        self._last_block_reason = None
