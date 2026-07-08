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


@dataclass
class StandardCommissionEvent:
    """Broker commission report for one fill.

    realized_pnl is None on opening fills (brokers report realized PnL
    only when a position is reduced/closed).
    """
    order_id: str
    exec_id: str
    symbol: str
    commission: float
    realized_pnl: Optional[float]
    currency: str
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

    def register_commission_callback(
        self, callback: Callable[[StandardCommissionEvent], None]
    ) -> None:
        """Register a consumer for broker commission reports.

        Default implementation is a no-op: non-broker adapters (simulation)
        have no commission stream; the live IBKR adapter overrides this.
        """
        pass

    @abstractmethod
    def get_position(self, symbol: str) -> int:
        pass

    def get_cached_position(self, symbol: str) -> int:
        """Net position for a symbol from the adapter's LOCAL cache.

        Deadlock-safe position primitive (A-2, hourly housekeeping):
        ``get_position`` routes through ``ensure_connected``, and a
        blocking reconnect under ``_ledger_lock`` pumps the event loop
        into a re-entrant bar-callback deadlock. Implementations MUST
        read a locally maintained cache and MUST NOT connect,
        reconnect, or issue synchronous broker requests.

        No silent default: an adapter without a cache read must say so
        loudly — a fabricated flat (0) would make the housekeeping
        sweep cancel protective orders on a live position.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_cached_position"
        )

    def get_position_settled(self, symbol: str) -> int:
        """Net position from an AUTHORITATIVE, freshly-settled broker snapshot.

        Unlike ``get_position`` (which for the live adapter reads ib_insync's
        in-memory position cache that is empty until the async account-update
        stream arrives after a (re)connect), this MUST force a fresh request
        and wait for it to settle before reading — so a stale/empty cache
        right after reconnect can never masquerade as flat.

        Contract: RAISE on timeout/error (do NOT return a fabricated flat).
        Callers use a raised/None result as a FAIL-CLOSED trigger — they must
        retain protective orders rather than treat an unconfirmed read as
        flat. Safe to call only on the main thread with the event loop idle
        (startup recovery / the 5-minute poll), like the rollover check.

        No silent default: an adapter without a settled read must say so
        loudly — a fabricated flat (0) would let the recovery/time-barrier
        paths cancel protective orders on a live position (the exact
        reconnect-false-flat OOB bug this primitive exists to prevent).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_position_settled"
        )

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

    def cancel_orders_by_ids(self, order_ids: list) -> int:
        """Cancel exactly the given order ids, IRRESPECTIVE of contract symbol.

        Targeted OOB-recovery primitive (oob-entry-state-recovery): the
        symbol-scoped ``cancel_open_orders`` silently missed protective
        orders resting on an OLD contract symbol after an instance
        reconfiguration (2026-07-06 MGC->GC orphaned-GTC incident).
        Returns how many of the requested ids were found open and
        cancelled; unknown ids are simply not counted.

        No silent default here: an adapter that cannot cancel by id must
        say so loudly — a fabricated success count would recreate the
        silent-miss bug this primitive exists to fix.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement cancel_orders_by_ids"
        )

    def get_executions(self, symbol: Optional[str] = None) -> list:
        """Return broker execution (fill) records as flat dicts.

        Record contract: each dict carries at least the keys
        ``order_id``, ``perm_id``, ``exec_id``, ``price``, ``qty``,
        ``side``, ``time``, ``symbol`` and ``commission_report`` (the
        broker commissionReport object/mapping when present, else None).
        ``symbol=None`` returns ALL fills — OOB recovery matches by order
        id, and the fill may rest on an OLD contract symbol.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_executions"
        )

    @abstractmethod
    def close_position(self, symbol: str, exit_mode: str, current_price: float) -> Any:
        pass

    @abstractmethod
    def register_error_callback(self, callback: Any) -> None:
        pass

    def get_open_trades(self, symbol: Optional[str]) -> list:
        """Return open/pending trades as a StandardExecutionEvent list.

        Used during startup recovery to verify TP/SL orders exist on
        the broker *before* subscription callbacks have populated the
        in-memory order cache.

        A-10: the parameter stays REQUIRED — ``symbol=None`` is an
        EXPLICIT "all symbols on this session", never an accidental
        default. The None form exists for old-contract orphans after an
        instrument reconfiguration (2026-07-06 MGC->GC trade_8 class),
        where the resting order lives on a symbol the instance no
        longer trades.

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
