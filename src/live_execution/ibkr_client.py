from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import pandas as pd
from ib_insync import (
    IB, ContFuture, Contract, Future, LimitOrder, MarketOrder,
    Order, StopOrder, Trade, util,
)

log = logging.getLogger(__name__)

_PACING_ERROR_CODES = {162}
_DEFAULT_SOURCE_TZ = "America/New_York"
_DEFAULT_TARGET_TZ = "UTC"

# Port defaults: IB Gateway paper = 4002, TWS paper = 7497
_PORT_GATEWAY = 4002
_PORT_TWS = 7497


def build_cl_contract(
    *,
    continuous: bool = True,
    contract_month: Optional[str] = None,
    exchange: str = "NYMEX",
    currency: str = "USD",
) -> Contract:
    """
    Build a CL futures contract for IBKR.

    Args:
        continuous: If True, use IB's continuous futures contract.
        contract_month: Specific front-month (YYYYMM), required if continuous=False.
        exchange: Futures exchange (NYMEX for CL).
        currency: Contract currency.
    """
    if continuous:
        return ContFuture(symbol="CL", exchange=exchange, currency=currency, includeExpired=True)

    if not contract_month:
        raise ValueError("contract_month is required when continuous=False (format: YYYYMM).")

    return Future(
        symbol="CL",
        lastTradeDateOrContractMonth=contract_month,
        exchange=exchange,
        currency=currency,
    )


def _is_pacing_error(error: Exception) -> bool:
    message = str(error).lower()
    return "pacing" in message or "rate limit" in message


def _normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "date": "DateTime",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    df = df.rename(columns=rename_map)
    required = ["DateTime", "Open", "High", "Low", "Close", "Volume"]
    missing = set(required) - set(df.columns)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"Missing required columns from IBKR bars: {missing_list}")
    return df[required]


def _standardize_timezone(
    series: pd.Series,
    *,
    source_tz: str = _DEFAULT_SOURCE_TZ,
    target_tz: str = _DEFAULT_TARGET_TZ,
    make_naive: bool = True,
) -> pd.Series:
    dt = pd.to_datetime(series, errors="coerce")
    if dt.dt.tz is None:
        dt = dt.dt.tz_localize(source_tz)
    dt = dt.dt.tz_convert(target_tz)
    if make_naive:
        dt = dt.dt.tz_localize(None)
    return dt


@dataclass
class IBKRConnectionManager:
    host: str = "127.0.0.1"
    port: int = _PORT_GATEWAY
    client_id: int = 1
    readonly: bool = True
    connect_timeout: int = 5
    fallback_ports: list[int] = field(default_factory=lambda: [_PORT_TWS])

    def __post_init__(self) -> None:
        self.ib = IB()
        self._last_error: Optional[tuple[int, str]] = None
        self.ib.errorEvent += self._on_error

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract: Contract) -> None:
        self._last_error = (error_code, error_string)

    def connect(self) -> None:
        """Connect to IBKR, trying the primary port first then fallbacks."""
        if self.ib.isConnected():
            return

        ports_to_try = [self.port] + [
            p for p in self.fallback_ports if p != self.port
        ]
        last_exc: Optional[Exception] = None

        for port in ports_to_try:
            try:
                log.info("Attempting IBKR connection on %s:%d ...", self.host, port)
                self.ib.connect(
                    host=self.host,
                    port=port,
                    clientId=self.client_id,
                    readonly=self.readonly,
                    timeout=self.connect_timeout,
                )
                self.port = port  # remember successful port
                log.info("Connected to IBKR on port %d", port)
                return
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "Connection failed on port %d: %s", port, exc,
                )
                # Ensure disconnected state before trying next port
                try:
                    self.ib.disconnect()
                except Exception:
                    pass

        raise ConnectionError(
            f"Could not connect to IBKR on any port {ports_to_try}: {last_exc}"
        )

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def ensure_connected(self) -> None:
        if not self.ib.isConnected():
            self.connect()

    def qualify_contract(self, contract: Contract) -> Contract:
        self.ensure_connected()
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError("Failed to qualify CL contract with IBKR.")
        return qualified[0]

    def fetch_historical_bars(
        self,
        *,
        days_back: int = 5,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        end_datetime: str = "",
        max_retries: int = 5,
        backoff_seconds: float = 2.0,
        throttle_seconds: float = 0.5,
        source_tz: str = _DEFAULT_SOURCE_TZ,
        target_tz: str = _DEFAULT_TARGET_TZ,
        make_naive: bool = True,
        set_index: bool = True,
    ) -> pd.DataFrame:
        """
        Request historical 5-minute CL bars from IBKR and return as DataFrame.

        Output columns: DateTime, Open, High, Low, Close, Volume
        """
        self.ensure_connected()
        contract = build_cl_contract(
            continuous=continuous,
            contract_month=contract_month,
        )
        contract = self.qualify_contract(contract)

        duration_str = f"{max(days_back, 1)} D"
        bars = self._request_historical_data(
            contract=contract,
            duration_str=duration_str,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth,
            end_datetime=end_datetime,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            throttle_seconds=throttle_seconds,
        )
        return ib_bars_to_dataframe(
            bars,
            source_tz=source_tz,
            target_tz=target_tz,
            make_naive=make_naive,
            set_index=set_index,
        )

    def _request_historical_data(
        self,
        *,
        contract: Contract,
        duration_str: str,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
        end_datetime: str,
        max_retries: int,
        backoff_seconds: float,
        throttle_seconds: float,
    ) -> list:
        attempt = 0
        while attempt < max_retries:
            attempt += 1
            self._last_error = None
            try:
                bars = self.ib.reqHistoricalData(
                    contract,
                    endDateTime=end_datetime,
                    durationStr=duration_str,
                    barSizeSetting=bar_size,
                    whatToShow=what_to_show,
                    useRTH=use_rth,
                    formatDate=1,
                    keepUpToDate=False,
                )
                if self._last_error and self._last_error[0] in _PACING_ERROR_CODES:
                    raise RuntimeError(f"IBKR pacing violation: {self._last_error[1]}")
                if throttle_seconds:
                    time.sleep(throttle_seconds)
                return bars
            except Exception as exc:
                is_pacing = _is_pacing_error(exc) or (
                    self._last_error and self._last_error[0] in _PACING_ERROR_CODES
                )
                if attempt >= max_retries:
                    raise
                sleep_for = backoff_seconds * attempt
                if not is_pacing:
                    sleep_for = min(sleep_for, 5.0)
                time.sleep(sleep_for)
        return []

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def get_cl_position(self, symbol: str = "CL") -> int:
        """
        Query IBKR portfolio for the current CL position.

        Returns:
            int: Net position size (0 = flat, positive = long, negative = short).
        """
        self.ensure_connected()
        positions = self.ib.positions()
        for pos in positions:
            if pos.contract.symbol == symbol:
                return int(pos.position)
        return 0

    # ------------------------------------------------------------------
    # Live bar subscription
    # ------------------------------------------------------------------

    def subscribe_live_bars(
        self,
        contract: Contract,
        *,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S",
    ):
        """
        Subscribe to live-updating historical bars (keepUpToDate=True).

        The returned bars object will be updated in-place by ib_insync
        whenever a new bar closes. Attach a callback via
        ``bars.updateEvent += my_handler`` to react to new bars.

        Args:
            contract: Qualified IBKR contract.
            bar_size: Bar size setting (default "5 mins").
            what_to_show: Data type (default "TRADES").
            use_rth: Regular trading hours only (default False).
            duration_str: Initial lookback duration (default "60 S").

        Returns:
            BarDataList with live updates enabled.
        """
        self.ensure_connected()
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration_str,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=True,
        )
        return bars

    def cancel_subscription(self, bars) -> None:
        """Cancel a live bar subscription."""
        if self.ib.isConnected():
            self.ib.cancelHistoricalData(bars)

    # ------------------------------------------------------------------
    # Bracket order execution
    # ------------------------------------------------------------------

    def place_bracket_order(
        self,
        contract: Contract,
        action: str,
        quantity: int,
        limit_price: float,
        tp_price: float,
        sl_price: float,
        *,
        use_market: bool = True,
    ) -> list[Trade]:
        """
        Create and transmit a bracket order (parent + TP + SL children).

        Args:
            contract: Qualified IBKR contract.
            action: 'BUY' or 'SELL'.
            quantity: Number of contracts.
            limit_price: Limit price for parent (ignored if use_market=True).
            tp_price: Take-profit price (limit order).
            sl_price: Stop-loss price (stop order).
            use_market: If True, parent is a Market order (default).

        Returns:
            list[Trade]: [parent_trade, tp_trade, sl_trade].
        """
        self.ensure_connected()
        bracket = self.ib.bracketOrder(
            action=action,
            quantity=quantity,
            limitPrice=limit_price,
            takeProfitPrice=tp_price,
            stopLossPrice=sl_price,
        )

        # If market order requested, convert parent to Market
        if use_market:
            parent = bracket[0]
            parent.orderType = "MKT"
            parent.lmtPrice = 0

        trades = []
        for order in bracket:
            trade = self.ib.placeOrder(contract, order)
            trades.append(trade)

        return trades


def ib_bars_to_dataframe(
    bars: Iterable,
    *,
    source_tz: str = _DEFAULT_SOURCE_TZ,
    target_tz: str = _DEFAULT_TARGET_TZ,
    make_naive: bool = True,
    set_index: bool = True,
) -> pd.DataFrame:
    """
    Convert ib_insync bars to a standardized OHLCV DataFrame.
    """
    if not bars:
        return pd.DataFrame(columns=["DateTime", "Open", "High", "Low", "Close", "Volume"])

    df = util.df(bars)
    df = _normalize_ohlcv_columns(df)
    df["DateTime"] = _standardize_timezone(
        df["DateTime"],
        source_tz=source_tz,
        target_tz=target_tz,
        make_naive=make_naive,
    )
    df = df.sort_values("DateTime").reset_index(drop=True)
    if set_index:
        df = df.set_index("DateTime", drop=False)
        df.index.name = "DateTime"
    return df


def fetch_historical_bars(
    days_back: int = 5,
    *,
    continuous: bool = True,
    contract_month: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = _PORT_GATEWAY,
    client_id: int = 1,
) -> pd.DataFrame:
    """
    Convenience function to fetch CL historical bars with default settings.
    """
    manager = IBKRConnectionManager(host=host, port=port, client_id=client_id)
    try:
        manager.connect()
        return manager.fetch_historical_bars(
            days_back=days_back,
            continuous=continuous,
            contract_month=contract_month,
        )
    finally:
        manager.disconnect()
