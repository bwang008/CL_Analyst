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
    def modify_order(self, order_id, event=None) -> Any:
        """Synchronously transmit a modification of a resting order to the venue.

        The caller has already written the new price into
        ``event.raw_event.order`` (e.g. ``auxPrice`` for a STP order) —
        the new price travels on the event by side effect, not as an
        argument.

        Implementations MUST synchronously transmit the modification and
        MUST raise when it cannot be transmitted or validated: malformed
        event (missing event / raw_event / order / contract / price),
        order-id mismatch, or disconnected venue. Venue-side rejection of
        a transmitted modify is reported asynchronously via the error
        callback, not by this method. An unknown/not-found order id MAY
        warn and no-op (matches live IBKR async semantics: re-placing an
        already-filled/cancelled order is rejected via the async
        errorEvent, not synchronously).
        """

    @abstractmethod
    def cancel_open_orders(self, symbol: str) -> int:
        pass

    @abstractmethod
    def close_position(self, symbol: str, exit_mode: str, current_price: float) -> Any:
        pass

    @abstractmethod
    def register_error_callback(self, callback: Any) -> None:
        pass

    def get_open_trades(self, symbol: str) -> list:
        """Return open/pending trades for a symbol as StandardExecutionEvent list.

        Used during startup recovery to verify TP/SL orders exist on
        the broker *before* subscription callbacks have populated the
        in-memory order cache.

        Default implementation returns an empty list for non-IBKR adapters.
        """
        return []

    def resolve_contract(self, symbol: str) -> None:
        """Pre-resolve and cache the qualified contract for a symbol.

        Must be called during startup (outside the asyncio event loop)
        so that order placement methods can use the cached contract
        without making async IBKR API calls that would cause
        'This event loop is already running' errors.

        Default implementation is a no-op for non-IBKR adapters.
        """
        pass
