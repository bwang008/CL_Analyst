from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

import pandas as pd
from ib_insync import IB, ContFuture, Contract, Future, util

_PACING_ERROR_CODES = {162}
_DEFAULT_SOURCE_TZ = "America/New_York"
_DEFAULT_TARGET_TZ = "UTC"


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
    port: int = 7497
    client_id: int = 1
    readonly: bool = True
    connect_timeout: int = 5

    def __post_init__(self) -> None:
        self.ib = IB()
        self._last_error: Optional[tuple[int, str]] = None
        self.ib.errorEvent += self._on_error

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract: Contract) -> None:
        self._last_error = (error_code, error_string)

    def connect(self) -> None:
        if self.ib.isConnected():
            return
        self.ib.connect(
            host=self.host,
            port=self.port,
            clientId=self.client_id,
            readonly=self.readonly,
            timeout=self.connect_timeout,
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
    port: int = 7497,
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
