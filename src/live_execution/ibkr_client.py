from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import pandas as pd
from ib_insync import (
    IB, ContFuture, Contract, Future, LimitOrder, MarketOrder,
    Order, StopOrder, TagValue, Trade, util,
)

# Pure stdlib leaf — no import cycle (instrument_master imports nothing
# from live_execution).
from src.core.instrument_master import get_instrument, round_to_tick

log = logging.getLogger(__name__)

_PACING_ERROR_CODES = {162}
_DEFAULT_SOURCE_TZ = "America/New_York"
_DEFAULT_TARGET_TZ = "UTC"

# Port defaults: IB Gateway paper = 4002, TWS paper = 7497
_PORT_GATEWAY = 4002
_PORT_TWS = 7497


def build_future_contract(
    symbol: str,
    *,
    continuous: bool = True,
    contract_month: Optional[str] = None,
    exchange: Optional[str] = None,
    currency: str = "USD",
) -> Contract:
    """
    Build a futures contract for any INSTRUMENT_REGISTRY symbol (T2, D6).

    Args:
        symbol: Futures symbol. Must exist in INSTRUMENT_REGISTRY —
            unknown symbols raise ValueError (no silent CL fallback).
        continuous: If True, use IB's continuous futures contract.
        contract_month: Specific front-month (YYYYMM), required if continuous=False.
        exchange: Futures exchange. None resolves the exchange from the
            instrument registry.
        currency: Contract currency.
    """
    # Fail-fast symbol validation (raises ValueError on unknown symbol)
    # even when an explicit exchange is supplied.
    instrument = get_instrument(symbol)
    if exchange is None:
        exchange = instrument.exchange

    if continuous:
        # C3: includeExpired=True on the ContFuture branch ONLY (legacy parity).
        return ContFuture(symbol=symbol, exchange=exchange, currency=currency, includeExpired=True)

    if not contract_month:
        raise ValueError("contract_month is required when continuous=False (format: YYYYMM).")

    return Future(
        symbol=symbol,
        lastTradeDateOrContractMonth=contract_month,
        exchange=exchange,
        currency=currency,
    )


def build_cl_contract(
    *,
    continuous: bool = True,
    contract_month: Optional[str] = None,
    exchange: str = "NYMEX",
    currency: str = "USD",
) -> Contract:
    """
    Build a CL futures contract for IBKR.

    Thin wrapper over build_future_contract (T2, D5) — kept for CL-bound
    scripts (e.g. scripts/download_ibkr_history.py). Field-identical output.

    Args:
        continuous: If True, use IB's continuous futures contract.
        contract_month: Specific front-month (YYYYMM), required if continuous=False.
        exchange: Futures exchange (NYMEX for CL).
        currency: Contract currency.
    """
    return build_future_contract(
        "CL",
        continuous=continuous,
        contract_month=contract_month,
        exchange=exchange,
        currency=currency,
    )


def build_mcl_contract(
    *,
    continuous: bool = True,
    contract_month: Optional[str] = None,
    exchange: str = "NYMEX",
    currency: str = "USD",
) -> Contract:
    """Build a Micro WTI Crude Oil (MCL) futures contract for IBKR.

    Thin wrapper over build_future_contract (T2, D5). MCL is 1/10th the
    size of CL ($100/point vs $1,000/point). Used for the 'Hands'
    execution stream when the strategy wants to trade smaller size while
    reading CL signals ('Brain').

    Args:
        continuous: If True, use IB's continuous futures contract.
        contract_month: Specific front-month (YYYYMM), required if continuous=False.
        exchange: Futures exchange (NYMEX for MCL).
        currency: Contract currency.
    """
    return build_future_contract(
        "MCL",
        continuous=continuous,
        contract_month=contract_month,
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

    # Error 326 = "client id already in use"
    _CLIENT_ID_IN_USE_CODE = 326
    _MAX_CLIENT_ID = 31  # IB allows client IDs 0-31

    def connect(self) -> None:
        """Connect to IBKR, trying the primary port first then fallbacks.

        If the connection fails because the client ID is already in use
        (IBKR Error 326), the method automatically increments the client ID
        and retries.  IB Gateway supports up to 32 concurrent connections
        (client IDs 0-31).  For any other connection error (gateway down,
        network refused), it fails fast after trying all ports once.
        """
        if self.ib.isConnected():
            return

        ports_to_try = [self.port] + [
            p for p in self.fallback_ports if p != self.port
        ]
        last_exc: Optional[Exception] = None

        # Try up to 32 client IDs starting from the initial value in case of conflicts
        start_id = self.client_id
        cid = start_id
        max_cid = start_id + 31

        while cid <= max_cid:
            self._last_error = None
            got_client_id_error = False

            for port in ports_to_try:
                try:
                    log.info(
                        "Attempting IBKR connection on %s:%d (clientId=%d) ...",
                        self.host, port, cid,
                    )
                    self.ib.connect(
                        host=self.host,
                        port=port,
                        clientId=cid,
                        readonly=self.readonly,
                        timeout=self.connect_timeout,
                    )
                    self.client_id = cid  # remember successful client ID
                    self.port = port      # remember successful port
                    log.info(
                        "Connected to IBKR on port %d with clientId %d",
                        port, cid,
                    )
                    return
                except Exception as exc:
                    last_exc = exc
                    # Check if the error callback received Error 326
                    if (
                        self._last_error
                        and self._last_error[0] == self._CLIENT_ID_IN_USE_CODE
                    ):
                        got_client_id_error = True
                        log.warning(
                            "clientId %d already in use on port %d — "
                            "will try next ID",
                            cid, port,
                        )
                    else:
                        log.warning(
                            "Connection failed on port %d: %s", port, exc,
                        )
                    # Ensure clean disconnected state before retrying
                    try:
                        self.ib.disconnect()
                    except Exception:
                        pass

                    # If client ID conflict, skip remaining ports for this ID
                    if got_client_id_error:
                        break

            # Only try the next client ID if Error 326 was the reason;
            # otherwise all ports genuinely failed (gateway down, etc.)
            if got_client_id_error:
                cid += 1
            else:
                break

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

    async def qualify_contract_async(self, contract: Contract) -> Contract:
        if not self.ib.isConnected():
            raise ConnectionError("Not connected to IBKR. Cannot qualify contract asynchronously.")
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError("Failed to qualify CL contract with IBKR.")
        return qualified[0]

    def fetch_historical_bars(
        self,
        *,
        symbol: str,
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
        Request historical bars for a symbol from IBKR and return as DataFrame.

        T2: ``symbol`` is REQUIRED (no silent CL default); the contract is
        built for the requested symbol with the exchange from the registry.

        Output columns: DateTime, Open, High, Low, Close, Volume
        """
        self.ensure_connected()
        contract = build_future_contract(
            symbol,
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
                    if _is_pacing_error(RuntimeError(self._last_error[1])):
                        raise RuntimeError(f"IBKR pacing violation: {self._last_error[1]}")
                    else:
                        log.warning(
                            "IBKR error %d (non-pacing): %s — retrying normally",
                            self._last_error[0], self._last_error[1],
                        )
                        raise RuntimeError(f"IBKR historical data error: {self._last_error[1]}")
                if throttle_seconds:
                    time.sleep(throttle_seconds)
                return bars
            except Exception as exc:
                is_pacing = _is_pacing_error(exc) or (
                    self._last_error
                    and self._last_error[0] in _PACING_ERROR_CODES
                    and _is_pacing_error(RuntimeError(self._last_error[1]))
                )
                if attempt >= max_retries:
                    raise
                sleep_for = backoff_seconds * attempt
                if not is_pacing:
                    sleep_for = min(sleep_for, 5.0)
                time.sleep(sleep_for)
        return []

    async def _request_historical_data_async(
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
        import asyncio
        attempt = 0
        while attempt < max_retries:
            attempt += 1
            self._last_error = None
            try:
                bars = await self.ib.reqHistoricalDataAsync(
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
                    if _is_pacing_error(RuntimeError(self._last_error[1])):
                        raise RuntimeError(f"IBKR pacing violation: {self._last_error[1]}")
                    else:
                        log.warning(
                            "IBKR error %d (non-pacing): %s — retrying normally",
                            self._last_error[0], self._last_error[1],
                        )
                        raise RuntimeError(f"IBKR historical data error: {self._last_error[1]}")
                if throttle_seconds:
                    await asyncio.sleep(throttle_seconds)
                return bars
            except Exception as exc:
                is_pacing = _is_pacing_error(exc) or (
                    self._last_error
                    and self._last_error[0] in _PACING_ERROR_CODES
                    and _is_pacing_error(RuntimeError(self._last_error[1]))
                )
                if attempt >= max_retries:
                    raise
                sleep_for = backoff_seconds * attempt
                if not is_pacing:
                    sleep_for = min(sleep_for, 5.0)
                await asyncio.sleep(sleep_for)
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

    def cancel_open_cl_orders(self, symbol: str = "CL") -> int:
        """Cancel all open orders for CL to support time-barrier exits."""
        self.ensure_connected()
        cancelled = 0
        for trade in self.ib.openTrades():
            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            if contract is None or order is None:
                continue
            if getattr(contract, "symbol", None) != symbol:
                continue
            try:
                self.ib.cancelOrder(order)
                cancelled += 1
            except Exception as exc:
                log.warning("Failed to cancel CL order %s: %s", order, exc)
        return cancelled

    def close_cl_position_market(self, symbol: str = "CL") -> Optional[Trade]:
        """Close any open CL position using a market order."""
        self.ensure_connected()
        for pos in self.ib.positions():
            if pos.contract.symbol != symbol:
                continue
            if int(pos.position) == 0:
                continue

            # IBKR positions() returns contracts without exchange — inject
            # the registry exchange for the position's OWN contract symbol
            # (MCL≠CL-safe; unknown symbols raise, no order transmitted).
            inst = get_instrument(pos.contract.symbol)
            pos.contract.exchange = inst.exchange

            action = "SELL" if pos.position > 0 else "BUY"
            qty = abs(int(pos.position))
            order = MarketOrder(action, qty)
            order.tif = "GTC"
            order.outsideRth = True
            return self.ib.placeOrder(pos.contract, order)
        return None

    def close_cl_position(
        self,
        symbol: str = "CL",
        *,
        exit_mode: str = "market",
        current_price: float | None = None,
    ) -> Optional[Trade]:
        """Close any open CL position using the specified order mode.

        Args:
            symbol: Contract symbol (default "CL").
            exit_mode: Order type for the exit — one of:
                - "market" (default) — plain market order.
                - "marketable_limit" — limit order priced 2 ticks through
                  ``current_price`` for bounded slippage.
                - "adaptive" — IBKR Adaptive Algo limit order.
            current_price: Current bar close price.  Required for
                ``marketable_limit`` mode; if None the method falls back
                to a plain market order.

        Returns:
            Trade object from IBKR, or None if no position to close.
        """
        self.ensure_connected()
        for pos in self.ib.positions():
            if pos.contract.symbol != symbol:
                continue
            if int(pos.position) == 0:
                continue

            # IBKR positions() returns contracts without exchange — inject
            # the registry exchange for the position's OWN contract symbol
            # (MCL≠CL-safe; unknown symbols raise, no order transmitted).
            inst = get_instrument(pos.contract.symbol)
            pos.contract.exchange = inst.exchange

            action = "SELL" if pos.position > 0 else "BUY"
            qty = abs(int(pos.position))

            if exit_mode == "marketable_limit" and current_price is not None:
                buf = 2 * inst.tick_size  # 2 instrument ticks (CL: $0.02)
                if action == "BUY":
                    lmt_price = round_to_tick(current_price + buf, inst.tick_size)
                else:
                    lmt_price = round_to_tick(current_price - buf, inst.tick_size)
                order = LimitOrder(action, qty, lmt_price)
                log.info(
                    "Exit mode: MARKETABLE_LIMIT %s "
                    "(price=%.2f, limit=%.2f, buffer=%.2f)",
                    action, current_price, lmt_price, buf,
                )

            elif exit_mode == "adaptive" and current_price is not None:
                # R1 (T3): snap the adaptive limit to the instrument grid —
                # identity for on-grid inputs (all real bar closes); an
                # off-grid input today would draw Error 110 on an EXIT
                # (stuck position), after: a valid order.
                order = LimitOrder(
                    action, qty, round_to_tick(current_price, inst.tick_size)
                )
                order.algoStrategy = "Adaptive"
                order.algoParams = [
                    TagValue("adaptivePriority", "Urgent"),
                ]
                log.info(
                    "Exit mode: ADAPTIVE %s (limit=%.2f, priority=Urgent)",
                    action, current_price,
                )

            else:
                # Fallback: plain market order
                order = MarketOrder(action, qty)
                if exit_mode != "market" and current_price is None:
                    log.warning(
                        "Exit mode '%s' requires current_price — "
                        "falling back to market order.",
                        exit_mode,
                    )
                else:
                    log.info("Exit mode: MARKET %s", action)

            order.tif = "GTC"
            order.outsideRth = True
            return self.ib.placeOrder(pos.contract, order)
        return None

    def get_account_summary(self, symbol: str = "CL") -> dict:
        """
        Query IBKR for CL-only account summary.

        Returns a dict with:
            account: str — account ID
            net_liquidation: float
            available_funds: float
            cl_position: int — net CL contracts
            cl_unrealized_pnl: float — CL unrealized PnL
            cl_realized_pnl: float — CL realized PnL
            cl_market_value: float — CL market value
            cl_avg_cost: float — CL average cost per contract
        """
        self.ensure_connected()

        # Account values
        summary: dict = {
            "account": "",
            "net_liquidation": 0.0,
            "available_funds": 0.0,
            "cl_position": 0,
            "cl_unrealized_pnl": 0.0,
            "cl_realized_pnl": 0.0,
            "cl_market_value": 0.0,
            "cl_avg_cost": 0.0,
        }

        # Account summary tags
        # Use accountValues() which is a cached property, avoiding cross-thread asyncio issues
        # that occur with the blocking accountSummary() network request.
        acct_values = self.ib.accountValues()
        for av in acct_values:
            if av.tag == "NetLiquidation" and av.currency == "USD":
                summary["net_liquidation"] = float(av.value)
                summary["account"] = av.account
            elif av.tag == "AvailableFunds" and av.currency == "USD":
                summary["available_funds"] = float(av.value)

        # CL-only portfolio items
        portfolio = self.ib.portfolio()
        for item in portfolio:
            if item.contract.symbol == symbol:
                summary["cl_position"] = int(item.position)
                summary["cl_unrealized_pnl"] = float(item.unrealizedPNL)
                summary["cl_realized_pnl"] = float(item.realizedPNL)
                summary["cl_market_value"] = float(item.marketValue)
                summary["cl_avg_cost"] = float(item.averageCost)

        return summary

    # IBKR blocks trading on physically-delivered futures near expiry.
    # CL contracts within this buffer (days) are skipped in favour of
    # the next month to avoid Error 201 rejections.
    _EXPIRY_BUFFER_DAYS = 6

    def get_front_month_contract(
        self, symbol: str = "CL",
    ) -> tuple[str, str]:
        """Resolve the current front-month futures contract.

        Supports any INSTRUMENT_REGISTRY symbol (exchange resolved from
        the registry — T2).

        Uses reqContractDetails to find the nearest-expiry contract
        that is still tradable.  Contracts expiring within
        ``_EXPIRY_BUFFER_DAYS`` are skipped to avoid IBKR's
        near-expiration physical delivery restrictions.

        Args:
            symbol: Futures symbol — "CL" (default), "MCL", "ES", ...

        Returns:
            tuple: (qualified Contract, contract_month string e.g. '202504')
        """
        from datetime import datetime, timedelta

        self.ensure_connected()
        # Use a generic Future to search for available contracts
        search = Future(
            symbol=symbol, exchange=get_instrument(symbol).exchange, currency="USD"
        )
        details = self.ib.reqContractDetails(search)

        if not details:
            raise RuntimeError(
                f"Could not retrieve {symbol} contract details from IBKR."
            )

        # Sort by expiry and pick the nearest one that isn't too close
        details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)

        cutoff = datetime.utcnow() + timedelta(days=self._EXPIRY_BUFFER_DAYS)
        cutoff_str = cutoff.strftime("%Y%m%d")

        # Filter out near-expiry contracts
        tradable = [
            d for d in details
            if d.contract.lastTradeDateOrContractMonth >= cutoff_str
        ]

        if tradable:
            front = tradable[0]
        else:
            # Fallback: all contracts are near-expiry (shouldn't happen)
            log.warning(
                "All %s contracts expire within %d days — "
                "using nearest available",
                symbol, self._EXPIRY_BUFFER_DAYS,
            )
            front = details[0]

        contract = front.contract
        month_str = contract.lastTradeDateOrContractMonth[:6]  # YYYYMM

        log.info(
            "Front-month %s contract: %s (conId=%d, month=%s, "
            "expiry=%s, buffer=%dd)",
            symbol, contract.localSymbol, contract.conId, month_str,
            contract.lastTradeDateOrContractMonth,
            self._EXPIRY_BUFFER_DAYS,
        )
        return contract.localSymbol, month_str

    async def get_front_month_contract_async(
        self, symbol: str = "CL",
    ) -> tuple[str, str]:
        """Async version of get_front_month_contract."""
        from datetime import datetime, timedelta

        self.ensure_connected()
        search = Future(
            symbol=symbol, exchange=get_instrument(symbol).exchange, currency="USD"
        )
        details = await self.ib.reqContractDetailsAsync(search)

        if not details:
            raise RuntimeError(
                f"Could not retrieve {symbol} contract details from IBKR."
            )

        details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)

        cutoff = datetime.utcnow() + timedelta(days=self._EXPIRY_BUFFER_DAYS)
        cutoff_str = cutoff.strftime("%Y%m%d")

        tradable = [
            d for d in details
            if d.contract.lastTradeDateOrContractMonth >= cutoff_str
        ]

        if tradable:
            front = tradable[0]
        else:
            log.warning(
                "All %s contracts expire within %d days — "
                "using nearest available",
                symbol, self._EXPIRY_BUFFER_DAYS,
            )
            front = details[0]

        contract = front.contract
        month_str = contract.lastTradeDateOrContractMonth[:6]

        log.info(
            "Front-month %s contract: %s (conId=%d, month=%s, "
            "expiry=%s, buffer=%dd)",
            symbol, contract.localSymbol, contract.conId, month_str,
            contract.lastTradeDateOrContractMonth,
            self._EXPIRY_BUFFER_DAYS,
        )
        return contract.localSymbol, month_str

    def fetch_historical_bars_by_duration(
        self,
        *,
        duration_str: str,
        symbol: str,
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
        Fetch historical bars using a raw duration string.

        Unlike fetch_historical_bars (which takes days_back), this accepts
        the IBKR duration string directly (e.g. '5 D', '2 W').
        Used by DataManager for precise backfill requests.

        T2: ``symbol`` is REQUIRED (no silent CL default).
        """
        self.ensure_connected()
        contract = build_future_contract(
            symbol,
            continuous=continuous,
            contract_month=contract_month,
        )
        contract = self.qualify_contract(contract)

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

    async def fetch_historical_bars_by_duration_async(
        self,
        *,
        duration_str: str,
        symbol: str,
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
        Fetch historical bars asynchronously using a raw duration string.

        T2: ``symbol`` is REQUIRED (no silent CL default).
        """
        if not self.ib.isConnected():
            raise ConnectionError("Not connected to IBKR. Cannot fetch historical bars asynchronously.")
        contract = build_future_contract(
            symbol,
            continuous=continuous,
            contract_month=contract_month,
        )
        contract = await self.qualify_contract_async(contract)

        bars = await self._request_historical_data_async(
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

    async def fetch_daily_close_async(self, contract: Contract) -> float:
        """Fetch the previous daily close for a given contract asynchronously.
        
        This fetches 2 days of daily bars and extracts the most recent completed day's close.
        """
        self.ensure_connected()
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="2 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            keepUpToDate=False,
        )
        if not bars:
            raise ValueError(f"No historical daily data returned for {contract.symbol}")
        
        import pandas as pd
        today = pd.Timestamp.now("America/New_York").date()
        last_bar_date = bars[-1].date
        if hasattr(last_bar_date, 'date'):
            last_bar_date = last_bar_date.date()
            
        if last_bar_date == today and len(bars) > 1:
            return float(bars[-2].close)
        return float(bars[-1].close)

    def fetch_daily_close(self, contract: Contract) -> float:
        """Fetch the previous daily close for a given contract synchronously."""
        return self.ib.run(self.fetch_daily_close_async(contract))

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

    async def subscribe_live_bars_async(
        self,
        contract: Contract,
        *,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S",
    ):
        """Async version of subscribe_live_bars for use inside the event loop.

        Identical to subscribe_live_bars but uses reqHistoricalDataAsync
        so it can be awaited from an async context (e.g., reconnection
        callbacks) without crashing with 'event loop already running'.
        """
        self.ensure_connected()
        bars = await self.ib.reqHistoricalDataAsync(
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
    # Real-time quote snapshot
    # ------------------------------------------------------------------

    def get_bid_ask(
        self,
        contract: Contract,
        *,
        timeout: float = 2.0,
    ) -> tuple[float | None, float | None]:
        """Fetch real-time best bid/ask for a contract.

        Uses reqTickers() for a live snapshot.  Returns (bid, ask)
        where either may be None if the quote is unavailable or stale.

        Args:
            contract: Qualified IBKR contract.
            timeout: Seconds to wait for a valid quote.

        Returns:
            Tuple of (best_bid, best_ask).  Values may be None.
        """
        self.ensure_connected()
        tickers = self.ib.reqTickers(contract)
        if not tickers:
            log.warning("get_bid_ask: no ticker data returned")
            return None, None

        ticker = tickers[0]
        # ib_insync populates bid/ask after a short delay; poll briefly
        elapsed = 0.0
        poll_step = 0.1
        while elapsed < timeout:
            bid = getattr(ticker, "bid", None)
            ask = getattr(ticker, "ask", None)
            # IBKR returns -1.0 for unavailable quotes
            bid_valid = bid is not None and bid > 0
            ask_valid = ask is not None and ask > 0
            if bid_valid and ask_valid:
                return float(bid), float(ask)
            self.ib.sleep(poll_step)
            elapsed += poll_step

        # Return whatever we have (may be None)
        bid = getattr(ticker, "bid", None)
        ask = getattr(ticker, "ask", None)
        bid = float(bid) if bid is not None and bid > 0 else None
        ask = float(ask) if ask is not None and ask > 0 else None
        if bid is None or ask is None:
            log.warning(
                "get_bid_ask: incomplete quote after %.1fs (bid=%s, ask=%s)",
                timeout, bid, ask,
            )
        return bid, ask

    # ------------------------------------------------------------------
    # Bracket order execution
    # ------------------------------------------------------------------

    # Valid entry modes for the parent leg of a bracket order
    _VALID_ENTRY_MODES = {"market", "adaptive", "marketable_limit"}

    def place_bracket_order(
        self,
        contract: Contract,
        action: str,
        quantity: int,
        limit_price: float,
        tp_price: float,
        sl_price: float,
        *,
        entry_mode: str = "adaptive",
        adaptive_priority: str = "Normal",
        use_market: bool | None = None,
    ) -> list[Trade]:
        """Create and transmit a bracket order (parent + TP + SL children).

        Args:
            contract: Qualified IBKR contract.
            action: 'BUY' or 'SELL'.
            quantity: Number of contracts.
            limit_price: Limit price for parent (used by LMT and as fallback).
            tp_price: Take-profit price (limit order).
            sl_price: Stop-loss price (stop order).
            entry_mode: Parent order type — one of:
                - "adaptive"  (default) — IBKR Adaptive Algo, seeks spread
                  improvement server-side.  No extra data subscriptions.
                - "marketable_limit" — Limit order priced 2 ticks through
                  the NBBO (asks for BUY, bids for SELL) to guarantee an
                  instant fill while capping max slippage.
                - "market" — Plain market order (legacy behavior).
            adaptive_priority: Urgency for Adaptive Algo — "Normal",
                "Urgent", or "Patient".  Ignored unless entry_mode is
                "adaptive".
            use_market: **Deprecated** — backward compatibility shim.
                If True and entry_mode is not explicitly provided,
                equivalent to entry_mode="market".

        Returns:
            list[Trade]: [parent_trade, tp_trade, sl_trade].
        """
        self.ensure_connected()

        # ── Backward-compat: honour old use_market flag ──────────────
        if use_market is True and entry_mode == "adaptive":
            # Caller used the old API without setting entry_mode;
            # preserve legacy behavior.
            entry_mode = "market"
            log.debug(
                "place_bracket_order: use_market=True mapped to "
                "entry_mode='market' (deprecated — use entry_mode instead)"
            )

        if entry_mode not in self._VALID_ENTRY_MODES:
            raise ValueError(
                f"Invalid entry_mode '{entry_mode}'. "
                f"Must be one of: {sorted(self._VALID_ENTRY_MODES)}"
            )

        bracket = self.ib.bracketOrder(
            action=action,
            quantity=quantity,
            limitPrice=limit_price,
            takeProfitPrice=tp_price,
            stopLossPrice=sl_price,
        )

        parent = bracket[0]

        # ── Configure parent order based on entry_mode ───────────────
        if entry_mode == "adaptive":
            # IBKR Adaptive Algo: server-side algo that seeks price
            # improvement inside the bid/ask spread.
            parent.orderType = "LMT"
            parent.lmtPrice = limit_price
            parent.algoStrategy = "Adaptive"
            parent.algoParams = [
                TagValue("adaptivePriority", adaptive_priority),
            ]
            log.info(
                "Entry mode: ADAPTIVE (priority=%s, limit=%.2f)",
                adaptive_priority, limit_price,
            )

        elif entry_mode == "marketable_limit":
            # Marketable Limit: price 2 ticks through the current price
            # for instant fill with bounded slippage.
            # NOTE: We use limit_price (bar close) instead of fetching
            # live NBBO via reqTickers(), because reqTickers() is an
            # async call that fails inside ib_insync callbacks with
            # "RuntimeError: This event loop is already running".
            # T3: tick resolved from the registry via the contract symbol
            # (raises for unknown symbols BEFORE anything is transmitted).
            tick = get_instrument(contract.symbol).tick_size
            buf = 2 * tick  # 2 instrument ticks (CL: $0.02, unchanged)

            if action.upper() == "BUY":
                ml_price = round_to_tick(limit_price + buf, tick)
                parent.orderType = "LMT"
                parent.lmtPrice = ml_price
                log.info(
                    "Entry mode: MARKETABLE_LIMIT BUY "
                    "(price=%.2f, limit=%.2f, buffer=%.2f)",
                    limit_price, ml_price, buf,
                )
            else:  # SELL
                ml_price = round_to_tick(limit_price - buf, tick)
                parent.orderType = "LMT"
                parent.lmtPrice = ml_price
                log.info(
                    "Entry mode: MARKETABLE_LIMIT SELL "
                    "(price=%.2f, limit=%.2f, buffer=%.2f)",
                    limit_price, ml_price, buf,
                )

        else:  # entry_mode == "market"
            parent.orderType = "MKT"
            parent.lmtPrice = 0
            log.info("Entry mode: MARKET")

        # Configure all orders: GTC + outside-RTH to avoid DAY-order
        # expiry and ensure triggers work during overnight sessions.
        for order in bracket:
            order.outsideRth = True
            order.tif = "GTC"

        # Stop-loss (bracket[2]): use native exchange trigger via
        # triggerMethod=1 (double bid/ask) instead of IB-simulated stop.
        # Simulated stops (PreSubmitted + whyHeld='trigger') can fail to
        # fire during low-liquidity overnight sessions.
        sl_order = bracket[2]
        sl_order.triggerMethod = 1

        trades = []
        for order in bracket:
            trade = self.ib.placeOrder(contract, order)
            trades.append(trade)

        return trades

    def place_entry_order(
        self,
        contract: Contract,
        action: str,
        quantity: int,
        limit_price: float,
        *,
        entry_mode: str = "adaptive",
        adaptive_priority: str = "Normal",
    ) -> Trade:
        """Submit only the parent entry order (no TP/SL children).

        Phase 1 of two-phase order placement.  TP/SL children are placed
        separately via ``place_child_orders`` after the entry fills, so
        their prices can be computed from the actual fill price.

        Args:
            contract: Qualified IBKR contract.
            action: 'BUY' or 'SELL'.
            quantity: Number of contracts.
            limit_price: Limit price for the entry (bar close).
            entry_mode: "adaptive", "marketable_limit", or "market".
            adaptive_priority: Urgency for Adaptive Algo.

        Returns:
            Trade object for the submitted entry order.
        """
        self.ensure_connected()

        if entry_mode not in self._VALID_ENTRY_MODES:
            raise ValueError(
                f"Invalid entry_mode '{entry_mode}'. "
                f"Must be one of: {sorted(self._VALID_ENTRY_MODES)}"
            )

        # Build parent entry order
        if entry_mode == "adaptive":
            order = LimitOrder(action, quantity, limit_price)
            order.algoStrategy = "Adaptive"
            order.algoParams = [
                TagValue("adaptivePriority", adaptive_priority),
            ]
            log.info(
                "Entry mode: ADAPTIVE (priority=%s, limit=%.2f)",
                adaptive_priority, limit_price,
            )

        elif entry_mode == "marketable_limit":
            # T3: tick resolved from the registry via the contract symbol
            # (raises for unknown symbols BEFORE anything is transmitted).
            tick = get_instrument(contract.symbol).tick_size
            buf = 2 * tick  # 2 instrument ticks (CL: $0.02, unchanged)
            if action.upper() == "BUY":
                ml_price = round_to_tick(limit_price + buf, tick)
            else:
                ml_price = round_to_tick(limit_price - buf, tick)
            order = LimitOrder(action, quantity, ml_price)
            log.info(
                "Entry mode: MARKETABLE_LIMIT %s "
                "(price=%.2f, limit=%.2f, buffer=%.2f)",
                action, limit_price, ml_price, buf,
            )

        else:  # market
            order = MarketOrder(action, quantity)
            log.info("Entry mode: MARKET")

        order.outsideRth = True
        order.tif = "GTC"
        order.transmit = True

        trade = self.ib.placeOrder(contract, order)
        return trade

    def place_child_orders(
        self,
        contract: Contract,
        parent_order_id: int,
        action: str,
        quantity: int,
        tp_price: float | list[tuple[int, float]],
        sl_price: float,
    ) -> list[Trade]:
        """Submit TP and SL orders as independent standalone orders.

        Phase 2 of two-phase order placement.  Called from the fill
        callback after the entry order fills so prices are computed
        from the actual fill price.

        IMPORTANT: These orders are submitted WITHOUT parentId.  Using
        parentId after the entry fills causes IBKR Error 201 ("Parent
        order is being cancelled") because IBKR removes the parent from
        its working-order table the moment it fills — any children
        arriving milliseconds later reference a terminal order ID.
        Fast fills (e.g. 3-lot marketable limit split into 3 partial
        fills) reliably trigger this race condition.

        OCA behavior (cancel the other leg on fill) is handled in
        software by LiveTrader._on_order_status.

        Args:
            contract: Same contract as the parent entry order.
            parent_order_id: orderId of the filled parent entry
                (kept for logging/context; no longer set on orders).
            action: Exit action — opposite of entry ('BUY' if entry
                was SELL, 'SELL' if entry was BUY).
            quantity: Number of contracts (matches entry).
            tp_price: Take-profit limit price (computed from fill price).
            sl_price: Stop-loss trigger price (computed from fill price).

        Returns:
            list[Trade]: [tp_trade, sl_trade].
        """
        self.ensure_connected()

        trades = []

        # Take-profit order(s) (standalone LMT — no parentId)
        if isinstance(tp_price, list):
            for tq, target_price in tp_price:
                if tq <= 0:
                    continue
                tp_order = LimitOrder(action, tq, target_price)
                tp_order.outsideRth = True
                tp_order.tif = "GTC"
                tp_order.transmit = True
                trades.append(self.ib.placeOrder(contract, tp_order))
        else:
            tp_order = LimitOrder(action, quantity, tp_price)
            tp_order.outsideRth = True
            tp_order.tif = "GTC"
            tp_order.transmit = True
            trades.append(self.ib.placeOrder(contract, tp_order))

        # Stop-loss order (standalone STP — no parentId)
        sl_order = StopOrder(action, quantity, sl_price)
        sl_order.outsideRth = True
        sl_order.tif = "GTC"
        sl_order.triggerMethod = 1  # native exchange trigger (double bid/ask)
        sl_order.transmit = True

        trades.append(self.ib.placeOrder(contract, sl_order))

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

    Deliberately CL-bound (documented CL helper) — passes symbol="CL"
    explicitly to the symbol-required manager method (T2).
    """
    manager = IBKRConnectionManager(host=host, port=port, client_id=client_id)
    try:
        manager.connect()
        return manager.fetch_historical_bars(
            symbol="CL",
            days_back=days_back,
            continuous=continuous,
            contract_month=contract_month,
        )
    finally:
        manager.disconnect()
