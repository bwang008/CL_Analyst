"""Simulated Data Feed Adapter — Historical Bar Replay.

Implements the DataFeedClient interface by replaying historical bars
from a preloaded DataFrame.  Used by livetest_engine.py to inject
deterministic data into the LiveTrader.

Key behaviors:
  - ``connect()`` / ``disconnect()`` are no-ops
  - ``fetch_historical_bars()`` returns a warmup slice from the dataset
  - ``subscribe_live_bars()`` returns a mock BarDataList with an
    ``updateEvent`` that the driver fires bar-by-bar
  - ``sleep()`` is a no-op (simulation runs as fast as possible)

Author: CL Analyst
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pandas as pd

from src.live_execution.interfaces.data_feed_interface import DataFeedClient

log = logging.getLogger("SimDataFeed")


# ---------------------------------------------------------------------------
# Mock event system (mimics ib_insync Event += callback)
# ---------------------------------------------------------------------------

class _MockEvent:
    """Minimal event that supports += for callback registration."""

    def __init__(self) -> None:
        self._callbacks: list[Callable] = []

    def __iadd__(self, callback: Callable) -> "_MockEvent":
        self._callbacks.append(callback)
        return self

    def __isub__(self, callback: Callable) -> "_MockEvent":
        self._callbacks = [cb for cb in self._callbacks if cb is not callback]
        return self

    def fire(self, *args, **kwargs) -> None:
        for cb in self._callbacks:
            cb(*args, **kwargs)


class _MockBarDataList(list):
    """Mimics ib_insync BarDataList with an updateEvent."""

    def __init__(self) -> None:
        super().__init__()
        self.updateEvent = _MockEvent()


# ---------------------------------------------------------------------------
# Simulated Data Feed
# ---------------------------------------------------------------------------

class SimulatedDataFeed(DataFeedClient):
    """DataFeedClient implementation that replays historical bars.

    Args:
        warmup_df: DataFrame with warmup bars (returned by
            ``fetch_historical_bars``).  Must have OHLCV columns and a
            DatetimeIndex named "DateTime".
        replay_df: DataFrame with bars to replay as "live" data.
            Same schema as warmup_df.
    """

    def __init__(
        self,
        warmup_df: pd.DataFrame,
        replay_df: pd.DataFrame,
    ) -> None:
        self._warmup_df = warmup_df.copy()
        self._replay_df = replay_df.copy()
        self._connected = False

        # Mock bar subscriptions (one per stream)
        self._subscriptions: dict[str, _MockBarDataList] = {}

    # ── DataFeedClient interface ─────────────────────────────────────

    def connect(self) -> None:
        self._connected = True
        log.info(
            "SimulatedDataFeed: connected (warmup=%d bars, replay=%d bars)",
            len(self._warmup_df), len(self._replay_df),
        )

    def disconnect(self) -> None:
        self._connected = False
        log.info("SimulatedDataFeed: disconnected")

    def is_connected(self) -> bool:
        return self._connected

    def fetch_historical_bars(
        self,
        days_back: int = 5,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
    ) -> pd.DataFrame:
        """Return warmup bars as historical data.

        The DataManager calls this during initialization to backfill
        its rolling window.  We return the pre-sliced warmup DataFrame.
        """
        log.info(
            "SimulatedDataFeed: returning %d warmup bars for fetch_historical_bars(days_back=%d)",
            len(self._warmup_df), days_back,
        )
        return self._warmup_df.copy()

    def fetch_historical_bars_by_duration(
        self,
        duration_str: str,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
    ) -> pd.DataFrame:
        """Same as fetch_historical_bars — returns the warmup slice."""
        return self._warmup_df.copy()

    async def fetch_historical_bars_by_duration_async(
        self,
        duration_str: str,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
    ) -> pd.DataFrame:
        """Same as fetch_historical_bars_by_duration — returns the warmup slice asynchronously."""
        return self._warmup_df.copy()

    def subscribe_live_bars(
        self,
        symbol: str,
        continuous: bool = False,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S",
    ) -> Any:
        """Return a mock BarDataList subscription.

        The driver calls ``fire_bar()`` to push bars through the
        ``updateEvent`` callback, which LiveTrader has registered
        via ``bars.updateEvent += self._on_bar_update_5m``.
        """
        stream_key = f"{symbol}_{bar_size}_{continuous}"
        bars = _MockBarDataList()
        self._subscriptions[stream_key] = bars

        log.info(
            "SimulatedDataFeed: subscribed to %s (bar_size=%s, continuous=%s)",
            symbol, bar_size, continuous,
        )

        # Pre-populate with a dummy bar so LiveTrader sees len(bars) >= 2
        # on the first real bar push.  The dummy uses the last warmup bar.
        if len(self._warmup_df) > 0:
            last_warmup = self._warmup_df.iloc[-1]
            dummy_bar = _make_bar_object(
                self._warmup_df.index[-1],
                float(last_warmup["Open"]),
                float(last_warmup["High"]),
                float(last_warmup["Low"]),
                float(last_warmup["Close"]),
                float(last_warmup.get("Volume", 0)),
            )
            bars.append(dummy_bar)

        return bars

    async def subscribe_live_bars_async(
        self,
        symbol: str,
        continuous: bool = False,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S",
    ) -> Any:
        """Async variant — delegates to sync version."""
        return self.subscribe_live_bars(
            symbol, continuous, bar_size, what_to_show, use_rth, duration_str,
        )

    def cancel_subscription(self, bars: Any) -> None:
        log.debug("SimulatedDataFeed: subscription cancelled")

    def get_front_month_contract(self, symbol: str = "CL") -> tuple:
        """Return a mock front-month contract identifier."""
        return (f"{symbol}Z9", "202612")

    def get_bid_ask(self, contract: Any, timeout: float = 2.0) -> tuple:
        bid = self._current_close - 0.01 if hasattr(self, '_current_close') else 0.0
        ask = self._current_close + 0.01 if hasattr(self, '_current_close') else 0.0
        return (bid, ask)

    def qualify_contract(self, contract: Any) -> Any:
        return contract

    def register_error_callback(self, callback: Any) -> None:
        pass  # No errors in simulation

    def sleep(self, seconds: float) -> None:
        """No-op: simulation runs at max speed."""
        pass

    # ── Driver API ───────────────────────────────────────────────────

    def get_subscription(self, key: str = None) -> _MockBarDataList | None:
        """Get a subscription by key, or the first one if key is None."""
        if key and key in self._subscriptions:
            return self._subscriptions[key]
        if self._subscriptions:
            return next(iter(self._subscriptions.values()))
        return None

    @property
    def all_subscriptions(self) -> dict[str, _MockBarDataList]:
        return self._subscriptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bar_object(
    timestamp: pd.Timestamp,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> SimpleNamespace:
    """Create a mock bar object matching ib_insync RealTimeBar attributes.

    LiveTrader's ``_on_bar_update_5m`` reads:
      - bar.date   → pd.Timestamp
      - bar.open   → float (note: NOT open_)
      - bar.high   → float
      - bar.low    → float
      - bar.close  → float
      - bar.volume → float
    """
    return SimpleNamespace(
        date=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )
