import pandas as pd
from typing import Any, Optional

from src.core.instrument_master import get_instrument
from src.live_execution.interfaces.data_feed_interface import DataFeedClient
from src.live_execution.ibkr_client import IBKRConnectionManager
from src.live_execution.instrument_context import InstrumentContext

# T4: index-contract routing map — the single source of truth for which
# (exchange, currency) serves each macro index the live path may fetch.
# Keys are IB index symbols, which by registry invariant
# (live_vol_index == volatility_index.replace("CLS", "")) double as the
# FRED column labels / live-override keys. DX is dormant in the live path
# (DXY is FRED-only) but kept for the legacy subscribe branches.
_INDEX_CONTRACT_SPECS: dict[str, tuple[str, str]] = {
    "VIX": ("CBOE", "USD"),
    "OVX": ("CBOE", "USD"),
    "GVZ": ("CBOE", "USD"),
    "DX": ("NYBOT", "USD"),
}


class IBKRDataFeedClient(DataFeedClient):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 1,
        fallback_ports: list[int] = None,
        *,
        instrument_context: InstrumentContext,
    ):
        # T2 (D1): the instrument context is REQUIRED — no silent CL default.
        # Historical fetches are brain-stream continuous fetches, so the
        # symbol is bound once at construction (symbol=ctx.brain_symbol).
        if fallback_ports is None:
            fallback_ports = [7496]
        self._instrument_context = instrument_context
        self.manager = IBKRConnectionManager(host=host, port=port, client_id=client_id, readonly=True, fallback_ports=fallback_ports)

    def connect(self) -> None:
        self.manager.connect()

    def disconnect(self) -> None:
        self.manager.disconnect()

    def is_connected(self) -> bool:
        return self.manager.ib.isConnected()

    def fetch_historical_bars(
        self,
        days_back: int = 5,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False
    ) -> pd.DataFrame:
        return self.manager.fetch_historical_bars(
            symbol=self._instrument_context.brain_symbol,
            days_back=days_back,
            continuous=continuous,
            contract_month=contract_month,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth
        )

    def fetch_historical_bars_by_duration(
        self,
        duration_str: str,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False
    ) -> pd.DataFrame:
        return self.manager.fetch_historical_bars_by_duration(
            symbol=self._instrument_context.brain_symbol,
            duration_str=duration_str,
            continuous=continuous,
            contract_month=contract_month,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth
        )

    async def fetch_historical_bars_by_duration_async(
        self,
        duration_str: str,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False
    ) -> pd.DataFrame:
        return await self.manager.fetch_historical_bars_by_duration_async(
            symbol=self._instrument_context.brain_symbol,
            duration_str=duration_str,
            continuous=continuous,
            contract_month=contract_month,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth
        )

    def subscribe_live_bars(
        self,
        symbol: str,
        continuous: bool = False,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S"
    ) -> Any:
        from ib_insync import Future, Index
        if symbol in _INDEX_CONTRACT_SPECS:
            contract = Index(symbol, *_INDEX_CONTRACT_SPECS[symbol])
            if symbol != "DX":
                what_to_show = "TRADES"  # CBOE vol indices use TRADES or MIDPOINT
        elif continuous:
            # T2: build the REQUESTED symbol's continuous contract — the
            # legacy not-MCL->CL fallback is dead; unknown symbols raise.
            from src.live_execution.ibkr_client import build_future_contract
            contract = build_future_contract(symbol, continuous=True)
        else:
            local_sym, _ = self.manager.get_front_month_contract(symbol=symbol)
            contract = Future(
                symbol=symbol,
                localSymbol=local_sym,
                exchange=get_instrument(symbol).exchange,
            )

        # We don't qualify indices like DX on NYBOT as easily, but qualify_contract works for most.
        try:
            contract = self.manager.qualify_contract(contract)
        except ValueError as e:
            if symbol in _INDEX_CONTRACT_SPECS:
                pass  # Sometimes index qualification fails but reqHistoricalData still works
            else:
                raise e

        return self.manager.subscribe_live_bars(
            contract=contract,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth,
            duration_str=duration_str
        )

    async def subscribe_live_bars_async(
        self,
        symbol: str,
        continuous: bool = False,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S"
    ) -> Any:
        from ib_insync import Future, Index
        if symbol in _INDEX_CONTRACT_SPECS:
            contract = Index(symbol, *_INDEX_CONTRACT_SPECS[symbol])
            if symbol != "DX":
                what_to_show = "TRADES"
        elif continuous:
            # T2: build the REQUESTED symbol's continuous contract — the
            # legacy not-MCL->CL fallback is dead; unknown symbols raise.
            from src.live_execution.ibkr_client import build_future_contract
            contract = build_future_contract(symbol, continuous=True)
        else:
            local_sym, _ = await self.manager.get_front_month_contract_async(symbol=symbol)
            contract = Future(
                symbol=symbol,
                localSymbol=local_sym,
                exchange=get_instrument(symbol).exchange,
            )

        try:
            contract = await self.manager.qualify_contract_async(contract)
        except Exception as e:
            if symbol in _INDEX_CONTRACT_SPECS:
                pass
            else:
                raise e

        return await self.manager.subscribe_live_bars_async(
            contract=contract,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth,
            duration_str=duration_str
        )

    async def fetch_daily_close_async(self, symbol: str) -> float:
        from ib_insync import Index
        spec = _INDEX_CONTRACT_SPECS.get(symbol)
        if spec is None:
            raise ValueError(
                f"fetch_daily_close_async only supports index symbols "
                f"{sorted(_INDEX_CONTRACT_SPECS)}. Got: {symbol}"
            )
        contract = Index(symbol, *spec)

        try:
            contract = await self.manager.qualify_contract_async(contract)
        except Exception:
            pass  # Index qualification might fail but data fetch often still works

        return await self.manager.fetch_daily_close_async(contract)

    def fetch_daily_close(self, symbol: str) -> float:
        return self.manager.ib.run(self.fetch_daily_close_async(symbol))

    def cancel_subscription(self, bars: Any) -> None:
        self.manager.cancel_subscription(bars)

    def get_front_month_contract(self, symbol: str) -> tuple[str, str]:
        # T2 (C6): the silent '= "CL"' default is dropped — all callers
        # pass the symbol explicitly.
        return self.manager.get_front_month_contract(symbol=symbol)
        
    def get_bid_ask(self, contract: Any, timeout: float = 2.0) -> tuple:
        return self.manager.get_bid_ask(contract=contract, timeout=timeout)

    def qualify_contract(self, contract: Any) -> Any:
        return self.manager.qualify_contract(contract)

    def register_error_callback(self, callback: Any) -> None:
        self.manager.ib.errorEvent += callback

    def sleep(self, seconds: float) -> None:
        self.manager.ib.sleep(seconds)
