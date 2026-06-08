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
            - blocked_entry_hours_est: list of ints representing blocked FILL
              hours in EST/EDT (e.g. [9] blocks fills at 9:00 AM pit open).
              The backtest passes bar_start + 1h; the live trader passes
              wall-clock time directly.
            - blocked_entry_hours_by_day: dict mapping day name to list of ints
              (e.g. {"Wednesday": [12]} blocks noon fills on EIA day)
            - block_long_weekends: bool
            - long_weekend_block_scope: list of str (default: BEFORE_LONG_WEEKEND, AFTER_LONG_WEEKEND)
            - override_global_filters: bool (default: False)
        """
        self.config = config
        self.blocked_entry_hours_est = config.get("blocked_entry_hours_est", [])
        self.blocked_entry_hours_by_day = config.get("blocked_entry_hours_by_day", {})
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

        # Configure logging to write to reports/backtest_engine_log and suppress console output
        # ONLY if running inside a backtest context (and NOT pytest or live trader)
        import sys
        from pathlib import Path

        is_backtest = False
        if sys.argv and sys.argv[0]:
            main_script = Path(sys.argv[0]).name
            # Check if executing via backtest_engine, sweep_ensembles, or execution_param_sweeper
            # But do NOT redirect logging if we are running in pytest
            if ("backtest_engine" in main_script or 
                "sweep" in main_script or 
                "sweeper" in main_script) and "pytest" not in main_script:
                is_backtest = True

        if is_backtest:
            # Direct logs to reports/backtest_engine_log and prevent propagation
            project_root = Path(__file__).resolve().parent.parent.parent
            log_dir = project_root / "reports"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "backtest_engine_log"

            # Setup dedicated logger file handler
            log.propagate = False
            
            # Remove and close any existing file handlers to prevent duplicate logs/leaks
            for h in list(log.handlers):
                try:
                    h.close()
                except Exception:
                    pass
                log.removeHandler(h)

            file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            file_formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", 
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            file_handler.setFormatter(file_formatter)
            log.addHandler(file_handler)
            log.setLevel(logging.WARNING)

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
            The timestamp to check.  In the backtest, this is the bar-start
            time shifted forward by one bar duration (= fill time).  In the
            live trader, this is wall-clock time (= fill time).

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

        # Day-specific hour blocking
        day_name = ts_est.strftime("%A")
        blocked_hours_for_day = self.blocked_entry_hours_by_day.get(day_name, [])
        if hour in blocked_hours_for_day:
            reason = f"BLOCKED: {hour:02d}:00 bar on {day_name} (blocked_entry_hours_by_day)"
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
        """Emit a warning only when the block reason changes (edge-triggered).

        When transitioning from allowed→blocked or between different block
        reasons, logs a clear 'GUARD ACTIVATED' message. Subsequent bars
        with the same reason are silent to avoid log spam.
        """
        if reason != self._last_block_reason:
            log.warning("[GUARD ACTIVATED] %s", reason)
            self._last_block_reason = reason

    def _clear_block(self) -> None:
        """Reset the block tracker when entry becomes allowed again.

        When transitioning from blocked→allowed, logs a clear
        'GUARD DEACTIVATED' message so the user knows entries are
        flowing again.
        """
        if self._last_block_reason is not None:
            log.info("[GUARD DEACTIVATED] new entries allowed")
            self._last_block_reason = None
