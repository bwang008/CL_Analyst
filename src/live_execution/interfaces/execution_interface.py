from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any

@dataclass
class StandardExecutionEvent:
    order_id: str
    symbol: str
    status: str
    filled_qty: int
    remaining_qty: int
    avg_price: float
    raw_event: Optional[Any] = None

class ExecutionClient(ABC):
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
    def register_order_status_callback(self, callback: Callable[[StandardExecutionEvent], None]) -> None:
        pass

    @abstractmethod
    def get_position(self, symbol: str) -> int:
        pass

    @abstractmethod
    def get_account_summary(self, symbol: str) -> dict:
        pass

    @abstractmethod
    def place_bracket_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        **kwargs
    ) -> list:
        pass

    @abstractmethod
    def place_child_orders(
        self,
        symbol: str,
        parent_order_id: int,
        action: str,
        quantity: int,
        tp_price: float,
        sl_price: float,
    ) -> list:
        pass

    @abstractmethod
    def cancel_open_orders(self, symbol: str) -> int:
        pass

    @abstractmethod
    def close_position(self, symbol: str, exit_mode: str, current_price: float) -> Any:
        pass
