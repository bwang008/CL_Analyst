"""Ticket recovery-barsheld-wallclock_07092026_1239.

Restart-recovery converted WALL-CLOCK elapsed time into bar counts
(int(delta_minutes / bar_dur)), but market gaps (weekend ~49h, daily 1h halt)
contain zero bars:

  1. Position recovery over-estimated ``_position_bars_held`` — a Friday
     position recovered after a weekend restart computed ~52 phantom "bars"
     vs a true ~3, instantly exceeding fleet ``max_hold_bars`` (18-24) and
     firing a spurious TIME_BARRIER close at Sunday open.
  2. ``_seed_restart_cooldown`` over-estimated ``bars_elapsed`` — restored
     cooldowns aged out too fast across gaps.

Fix under test: ``LiveTrader._bars_since(ts)`` counts ACTUAL brain-stream bars
strictly after ``ts`` from the rolling frame matching ``self._bar_size``
(gap-immune), and both recovery sites use it. Reviewer conditions C1-C5
(impact_review.md) are pinned below.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from src.live_execution.live_trader import LiveTrader
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy

REPO_ROOT = Path(__file__).resolve().parents[1]


def _hourly_index_with_weekend_gap():
    """Fri 10:00-16:00 (7 bars), weekend gap, Sun 18:00 - Mon 12:00 (19 bars)."""
    fri = pd.date_range("2026-07-03 10:00", "2026-07-03 16:00", freq="h")
    mon = pd.date_range("2026-07-05 18:00", "2026-07-06 12:00", freq="h")
    return fri.append(mon)


def _trader(*, bar_size="1h", df_1h=None, df_5m=None) -> LiveTrader:
    lt = object.__new__(LiveTrader)
    lt._bar_size = bar_size
    lt.rolling_df_1h = df_1h
    lt.rolling_df_5m = df_5m
    return lt


def _df(idx) -> pd.DataFrame:
    return pd.DataFrame({"Close": [1.0] * len(idx)}, index=idx)


# ---------------------------------------------------------------------------
# _bars_since — gap-immune bar counting
# ---------------------------------------------------------------------------


class TestBarsSince:
    def test_weekend_gap_counts_bars_not_wallclock(self):
        """THE bug scenario: entry Friday 14:00, recovered Monday 12:00.
        Wall-clock math said int(70h/1h) = 70 phantom bars; the true count of
        received bars after entry is 2 (Fri 15:00, 16:00) + 19 (Sun/Mon) = 21."""
        idx = _hourly_index_with_weekend_gap()
        lt = _trader(df_1h=_df(idx))
        entry = pd.Timestamp("2026-07-03 14:00:00")

        bars = lt._bars_since(entry)

        wallclock = int((idx[-1] - entry).total_seconds() / 3600)
        assert bars == 21
        assert wallclock >= 70 and bars < wallclock  # gap-immunity

    def test_off_by_none_vs_steady_state_counter(self):
        """Strictly-greater: the entry bar itself reads 0, +1 per later bar —
        identical to the live counter (bars_held=0 on the entry bar)."""
        idx = pd.date_range("2026-07-06 10:00", periods=5, freq="h")
        lt = _trader(df_1h=_df(idx))
        assert lt._bars_since(idx[-1]) == 0   # entry bar = latest bar
        assert lt._bars_since(idx[0]) == 4    # 4 bars after entry

    def test_ts_before_seeded_window_is_lower_bound(self):
        """Entry older than the seed window → count = whole frame (lower
        bound): errs toward HOLDING, never toward a spurious close."""
        idx = pd.date_range("2026-07-06 10:00", periods=5, freq="h")
        lt = _trader(df_1h=_df(idx))
        assert lt._bars_since(pd.Timestamp("2020-01-01")) == 5

    def test_5m_bar_size_uses_5m_frame(self):
        idx5 = pd.date_range("2026-07-06 10:00", periods=13, freq="5min")
        lt = _trader(bar_size="5m", df_5m=_df(idx5), df_1h=None)
        assert lt._bars_since(idx5[0]) == 12

    def test_unsupported_bar_size_returns_none(self):
        """C1: 2h/4h brains are RESAMPLED from 1h rows — raw counting would
        over-count 2-4x. Must refuse (None), not guess."""
        idx = pd.date_range("2026-07-06 10:00", periods=10, freq="h")
        for size in ("2h", "4h", "7m"):
            lt = _trader(bar_size=size, df_1h=_df(idx))
            assert lt._bars_since(idx[0]) is None, size

    def test_missing_or_empty_frame_returns_none(self):
        assert _trader(df_1h=None)._bars_since(pd.Timestamp("2026-07-06")) is None
        empty = _df(pd.DatetimeIndex([]))
        assert _trader(df_1h=empty)._bars_since(pd.Timestamp("2026-07-06")) is None

    def test_malformed_ts_returns_none_never_raises(self):
        """C2: recovery must never crash startup."""
        idx = pd.date_range("2026-07-06 10:00", periods=3, freq="h")
        lt = _trader(df_1h=_df(idx))
        assert lt._bars_since("not-a-timestamp") is None
        assert lt._bars_since(None) is None


# ---------------------------------------------------------------------------
# Site 2 functional: cooldown seeding across a weekend restart
# ---------------------------------------------------------------------------


def _strategy() -> ConfigurableStrategy:
    s = object.__new__(ConfigurableStrategy)
    s.config = {}
    s._last_exit_bars_ago_long = 9999
    s._last_exit_bars_ago_short = 9999
    s._last_exit_reason_long = ""
    s._last_exit_reason_short = ""
    s._exec_strategy = MagicMock()
    return s


class TestWeekendRestartCooldown:
    def test_cooldown_not_over_aged_across_weekend(self):
        """SL exit on Friday's last bar, restart Sunday 20:00 (2 bars later).
        Wall-clock math computed ~52 bars elapsed → cooldown fully aged-out;
        honest count is 2 bars → sl_cooldown_bars=7 must still block."""
        idx = _hourly_index_with_weekend_gap()
        # restart moment: only bars up to Sun 19:00 have been received
        seeded = idx[idx <= pd.Timestamp("2026-07-05 19:00:00")]
        s = _strategy()
        lt = object.__new__(LiveTrader)
        lt._strategy = s
        lt._bar_size = "1h"
        lt._position_bars_held = 0
        lt.rolling_df_1h = _df(seeded)
        lt.rolling_df_5m = None

        close_time = "2026-07-03 16:00:00"  # Friday's last bar
        lt._seed_restart_cooldown(1, "SL_HIT", close_time=close_time)

        # bars after Fri 16:00 in the received frame: Sun 18:00, 19:00 → 2
        assert s._last_exit_bars_ago_long == 1  # bars_elapsed(2) - 1
        assert s._last_exit_reason_long == "SL_HIT"


# ---------------------------------------------------------------------------
# Site 1 structural pin: position recovery uses _bars_since, not wall-clock
# ---------------------------------------------------------------------------


class TestRecoverySiteWiring:
    def test_position_recovery_counts_bars_not_wallclock(self):
        """The 'Restore entry bar time' recovery block must call _bars_since()
        and must no longer derive bars_held from a delta_minutes division
        (source-structure pin, same pattern as test_exit_bar_semantics F(2))."""
        src = (REPO_ROOT / "src" / "live_execution" / "live_trader.py").read_text(
            encoding="utf-8", errors="replace"
        )
        i = src.index("Restore entry bar time")
        block = src[i:i + 2000]
        assert "_bars_since(" in block, (
            "position recovery no longer calls _bars_since() — bars_held "
            "estimation regressed"
        )
        assert "delta_minutes" not in block, (
            "wall-clock delta_minutes division is back in the recovery block "
            "— weekend gaps would count as phantom bars again"
        )

    def test_seed_restart_cooldown_counts_bars_not_wallclock(self):
        src = (REPO_ROOT / "src" / "live_execution" / "live_trader.py").read_text(
            encoding="utf-8", errors="replace"
        )
        i = src.index("def _seed_restart_cooldown")
        block = src[i:src.index("def _reconstruct_cooldown_from_ledger")]
        assert "_bars_since(" in block
        assert "delta_min" not in block
