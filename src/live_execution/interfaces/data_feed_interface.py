from abc import ABC, abstractmethod
import pandas as pd
from typing import Any, Optional

class DataFeedClient(ABC):
    @abstractmethod
    def connect(self) -> None:
        pass

    @abstractmethod
    def disconnect(self) -> None:
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        pass

    @abstractmethod
    def fetch_historical_bars(
        self,
        days_back: int = 5,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False
    ) -> pd.DataFrame:
        pass

    @abstractmethod
    def fetch_historical_bars_by_duration(
        self,
        duration_str: str,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False
    ) -> pd.DataFrame:
        pass

    @abstractmethod
    async def fetch_historical_bars_by_duration_async(
        self,
        duration_str: str,
        continuous: bool = True,
        contract_month: Optional[str] = None,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False
    ) -> pd.DataFrame:
        pass

    @abstractmethod
    def subscribe_live_bars(
        self,
        symbol: str,
        continuous: bool = False,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S"
    ) -> Any:
        pass

    @abstractmethod
    async def subscribe_live_bars_async(
        self,
        symbol: str,
        continuous: bool = False,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        duration_str: str = "60 S"
    ) -> Any:
        pass

    @abstractmethod
    def cancel_subscription(self, bars: Any) -> None:
        pass

    @abstractmethod
    def get_front_month_contract(self, symbol: str) -> tuple:
        # T2 (C6): the silent '= "CL"' default is dropped from the abstract
        # signature. NOTE: SimulatedDataFeed keeps its own "CL" default —
        # Python does not enforce abstract signature parity and the parity
        # harness depends on the sim staying byte-untouched.
        pass
        
    @abstractmethod
    def get_bid_ask(self, contract: Any, timeout: float = 2.0) -> tuple:
        pass
    
    @abstractmethod
    def qualify_contract(self, contract: Any) -> Any:
        pass

    @abstractmethod
    def register_error_callback(self, callback: Any) -> None:
        pass

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        pass
