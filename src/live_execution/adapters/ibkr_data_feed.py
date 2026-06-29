import pandas as pd
from typing import Any, Optional

from src.live_execution.interfaces.data_feed_interface import DataFeedClient
from src.live_execution.ibkr_client import IBKRConnectionManager

class IBKRDataFeedClient(DataFeedClient):
    def __init__(self, host: str = "127.0.0.1", port: int = 4001, client_id: int = 1, fallback_ports: list[int] = None):
        if fallback_ports is None:
            fallback_ports = [7496]
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
        if symbol in ("VIX", "OVX"):
            contract = Index(symbol, "CBOE", "USD")
            what_to_show = "TRADES"  # Indices generally use TRADES or MIDPOINT
        elif symbol == "DX":
            contract = Index("DX", "NYBOT", "USD")
        elif continuous:
            from src.live_execution.ibkr_client import build_cl_contract, build_mcl_contract
            if symbol == "MCL":
                contract = build_mcl_contract(continuous=True)
            else:
                contract = build_cl_contract(continuous=True)
        else:
            local_sym, _ = self.manager.get_front_month_contract(symbol=symbol)
            contract = Future(symbol=symbol, localSymbol=local_sym, exchange="NYMEX")
        
        # We don't qualify indices like DX on NYBOT as easily, but qualify_contract works for most.
        try:
            contract = self.manager.qualify_contract(contract)
        except ValueError as e:
            if symbol in ("VIX", "OVX", "DX"):
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
        if symbol in ("VIX", "OVX"):
            contract = Index(symbol, "CBOE", "USD")
            what_to_show = "TRADES"
        elif symbol == "DX":
            contract = Index("DX", "NYBOT", "USD")
        elif continuous:
            from src.live_execution.ibkr_client import build_cl_contract, build_mcl_contract
            if symbol == "MCL":
                contract = build_mcl_contract(continuous=True)
            else:
                contract = build_cl_contract(continuous=True)
        else:
            local_sym, _ = await self.manager.get_front_month_contract_async(symbol=symbol)
            contract = Future(symbol=symbol, localSymbol=local_sym, exchange="NYMEX")
        
        try:
            contract = await self.manager.qualify_contract_async(contract)
        except Exception as e:
            if symbol in ("VIX", "OVX", "DX"):
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
        if symbol in ("VIX", "OVX"):
            contract = Index(symbol, "CBOE", "USD")
        else:
            raise ValueError(f"fetch_daily_close_async only supports index symbols. Got: {symbol}")
        
        try:
            contract = await self.manager.qualify_contract_async(contract)
        except Exception as e:
            pass  # Index qualification might fail but data fetch often still works
            
        return await self.manager.fetch_daily_close_async(contract)

    def fetch_daily_close(self, symbol: str) -> float:
        return self.manager.ib.run(self.fetch_daily_close_async(symbol))

    def cancel_subscription(self, bars: Any) -> None:
        self.manager.cancel_subscription(bars)

    def get_front_month_contract(self, symbol: str = "CL") -> tuple[str, str]:
        return self.manager.get_front_month_contract(symbol=symbol)
        
    def get_bid_ask(self, contract: Any, timeout: float = 2.0) -> tuple:
        return self.manager.get_bid_ask(contract=contract, timeout=timeout)

    def qualify_contract(self, contract: Any) -> Any:
        return self.manager.qualify_contract(contract)

    def register_error_callback(self, callback: Any) -> None:
        self.manager.ib.errorEvent += callback

    def sleep(self, seconds: float) -> None:
        self.manager.ib.sleep(seconds)
