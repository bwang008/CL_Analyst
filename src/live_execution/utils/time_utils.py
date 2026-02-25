"""
Time utilities for IBKR API integration.

Converts Python timedeltas to IBKR-compatible duration strings,
respecting API formatting requirements and pacing limits.

IBKR duration string format:
    S  = seconds    (max: 1800 for 5-min bars)
    D  = days       (max: 365 for 5-min bars)
    W  = weeks
    M  = months
    Y  = years

For 5-minute bars the practical maximum per single request is ~30 days.
"""

from __future__ import annotations

from datetime import timedelta
from typing import List

# Maximum days IBKR will accept in a single request for 5-min bars.
_MAX_REQUEST_DAYS = 30

# Safety margin (extra days) to ensure we cover any bars at the boundary.
_SAFETY_MARGIN_DAYS = 2


def timedelta_to_ib_duration(delta: timedelta) -> str:
    """
    Convert a timedelta to an IBKR-compatible duration string.

    Args:
        delta: Time gap to cover. Must be non-negative.

    Returns:
        A duration string like '5 D', '2 W', etc.

    Raises:
        ValueError: If delta is negative.

    Examples:
        >>> timedelta_to_ib_duration(timedelta(hours=2))
        '1 D'
        >>> timedelta_to_ib_duration(timedelta(days=3))
        '5 D'
        >>> timedelta_to_ib_duration(timedelta(days=0, seconds=0))
        '1 D'
    """
    if delta < timedelta(0):
        raise ValueError(
            f"Cannot convert negative timedelta to IBKR duration: {delta}"
        )

    total_seconds = delta.total_seconds()

    # For very small gaps (< 1 day), request at least 1 day
    if total_seconds < 86400:  # 1 day in seconds
        return "1 D"

    days_needed = delta.days + _SAFETY_MARGIN_DAYS

    # Clamp to 365 days maximum (IBKR won't accept more for 5-min bars)
    days_needed = min(days_needed, 365)

    # Ensure at least 1 day
    days_needed = max(days_needed, 1)

    return f"{days_needed} D"


def split_duration_into_chunks(
    delta: timedelta,
    max_chunk_days: int = _MAX_REQUEST_DAYS,
) -> List[str]:
    """
    Split a large timedelta into multiple IBKR-safe duration strings.

    When the gap is larger than what IBKR can handle in a single request
    (30 days for 5-min bars), this returns a list of smaller chunks.

    Args:
        delta: Total time gap to cover.
        max_chunk_days: Maximum days per request chunk.

    Returns:
        List of IBKR duration strings, ordered from oldest to newest.

    Raises:
        ValueError: If delta is negative.
    """
    if delta < timedelta(0):
        raise ValueError(
            f"Cannot split negative timedelta: {delta}"
        )

    total_days = delta.days + _SAFETY_MARGIN_DAYS
    total_days = max(total_days, 1)

    # Cap total to 1 year
    total_days = min(total_days, 365)

    chunks: List[str] = []
    remaining = total_days

    while remaining > 0:
        chunk_size = min(remaining, max_chunk_days)
        chunks.append(f"{chunk_size} D")
        remaining -= chunk_size

    return chunks
