"""
Tests for time_utils — IBKR duration string conversion.
"""

from datetime import timedelta

import pytest

from src.live_execution.utils.time_utils import (
    split_duration_into_chunks,
    timedelta_to_ib_duration,
)


class TestTimedeltaToIbDuration:
    """Tests for timedelta_to_ib_duration()."""

    def test_zero_gap_returns_one_day(self):
        """A zero-length gap should still request at least 1 day."""
        assert timedelta_to_ib_duration(timedelta(0)) == "1 D"

    def test_sub_day_gap_returns_one_day(self):
        """A 2-hour gap is less than a day — should request 1 D."""
        assert timedelta_to_ib_duration(timedelta(hours=2)) == "1 D"

    def test_one_day_gap(self):
        """1-day gap should give 1 + safety margin = 3 D."""
        result = timedelta_to_ib_duration(timedelta(days=1))
        assert result == "3 D"

    def test_three_day_gap(self):
        """3-day gap should give 3 + 2 = 5 D."""
        result = timedelta_to_ib_duration(timedelta(days=3))
        assert result == "5 D"

    def test_weekend_gap(self):
        """A ~2.5-day gap (weekend) should give 4 D (2+2)."""
        result = timedelta_to_ib_duration(timedelta(days=2, hours=12))
        assert result == "4 D"

    def test_thirty_day_gap(self):
        """30-day gap should give 32 D."""
        result = timedelta_to_ib_duration(timedelta(days=30))
        assert result == "32 D"

    def test_very_large_gap_capped_at_365(self):
        """Gaps > 365 days should be capped at 365 D."""
        result = timedelta_to_ib_duration(timedelta(days=500))
        assert result == "365 D"

    def test_negative_timedelta_raises(self):
        """Negative timedelta should raise ValueError."""
        with pytest.raises(ValueError, match="negative"):
            timedelta_to_ib_duration(timedelta(days=-1))

    def test_small_seconds_gap(self):
        """A gap of 300 seconds (5 min) should return 1 D."""
        result = timedelta_to_ib_duration(timedelta(seconds=300))
        assert result == "1 D"


class TestSplitDurationIntoChunks:
    """Tests for split_duration_into_chunks()."""

    def test_small_gap_single_chunk(self):
        """A 5-day gap should produce a single chunk."""
        chunks = split_duration_into_chunks(timedelta(days=5))
        assert len(chunks) == 1
        assert chunks[0] == "7 D"  # 5 + 2 safety

    def test_thirty_day_gap_two_chunks(self):
        """A 32-day gap should be split into two chunks."""
        chunks = split_duration_into_chunks(
            timedelta(days=32), max_chunk_days=30
        )
        # 32 + 2 = 34 total → [30, 4]
        assert len(chunks) == 2
        assert chunks[0] == "30 D"
        assert chunks[1] == "4 D"

    def test_exact_max_single_chunk(self):
        """A gap exactly at max_chunk_days should produce one chunk."""
        chunks = split_duration_into_chunks(
            timedelta(days=28), max_chunk_days=30
        )
        # 28 + 2 = 30 → single chunk
        assert len(chunks) == 1
        assert chunks[0] == "30 D"

    def test_zero_gap(self):
        """Zero gap should return a single minimal chunk."""
        chunks = split_duration_into_chunks(timedelta(0))
        assert len(chunks) == 1
        assert chunks[0] == "2 D"  # 0 days + 2 safety = 2

    def test_negative_raises(self):
        """Negative timedelta should raise ValueError."""
        with pytest.raises(ValueError, match="negative"):
            split_duration_into_chunks(timedelta(days=-1))

    def test_large_gap_capped(self):
        """A 400-day gap should be capped at 365 and split."""
        chunks = split_duration_into_chunks(
            timedelta(days=400), max_chunk_days=30
        )
        total_days = sum(int(c.split()[0]) for c in chunks)
        assert total_days == 365
