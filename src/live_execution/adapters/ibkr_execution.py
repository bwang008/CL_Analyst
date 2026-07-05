from typing import Callable, Any, Optional
from src.core.instrument_master import get_instrument
from src.live_execution.interfaces.execution_interface import ExecutionClient, StandardExecutionEvent
from src.live_execution.ibkr_client import IBKRConnectionManager
import logging

log = logging.getLogger("IBKRExecAdapter")

class IBKRExecutionClient(ExecutionClient):
    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 10, fallback_ports: list[int] = None):
        if fallback_ports is None:
            fallback_ports = [7497]
        self.manager = IBKRConnectionManager(host=host, port=port, client_id=client_id, readonly=False, fallback_ports=fallback_ports)
        self._order_callbacks = []
        # Contract cache: symbol -> qualified Contract.
        # Populated by resolve_contract() at startup (outside the event loop).
        # Used by place_bracket_order / place_child_orders to avoid async
        # IBKR API calls (reqContractDetails, qualifyContracts) that would
        # crash with "This event loop is already running" when invoked from
        # an ib_insync bar-update callback.
        self._cached_contracts: dict[str, Any] = {}
        
        # Suppress ib_insync internal wrapper logging on the execution client.
        # Both data_client and exec_client have separate IB connections that each
        # receive updatePortfolio/position/commissionReport events from IBKR.
        # Without this, every portfolio update is logged twice (identical lines).
        # The data_client's connection already provides these logs.
        _wrapper_logger = logging.getLogger("ib_insync.wrapper")
        _wrapper_logger.setLevel(logging.WARNING)

        # Attach event handlers to bridge events
        self.manager.ib.orderStatusEvent += self._on_order_status

    def connect(self) -> None:
        self.manager.connect()

    def disconnect(self) -> None:
        self.manager.disconnect()

    def is_connected(self) -> bool:
        return self.manager.ib.isConnected()

    def register_order_status_callback(self, callback: Callable[[StandardExecutionEvent], None]) -> None:
        self._order_callbacks.append(callback)

    def _on_order_status(self, trade: Any):
        event = StandardExecutionEvent(
            order_id=str(trade.order.orderId),
            symbol=trade.contract.symbol,
            status=trade.orderStatus.status,
            filled_qty=int(trade.orderStatus.filled),
            remaining_qty=int(trade.orderStatus.remaining),
            avg_price=float(trade.orderStatus.avgFillPrice),
            raw_event=trade
        )
        for cb in self._order_callbacks:
            cb(event)

    def get_position(self, symbol: str) -> int:
        return self.manager.get_cl_position(symbol=symbol)

    def get_account_summary(self, symbol: str) -> dict:
        return self.manager.get_account_summary(symbol=symbol)

    def resolve_contract(self, symbol: str) -> None:
        """Pre-resolve and cache the qualified contract for a symbol.

        Called during startup (outside the asyncio event loop) so that
        order placement methods can use the cached contract without
        making async IBKR calls that crash inside event loop callbacks.
        """
        from ib_insync import Future
        local_sym, _ = self.manager.get_front_month_contract(symbol=symbol)
        # T2: exchange from the instrument registry (was hardcoded NYMEX —
        # behavior-identical for CL/MCL, enables non-NYMEX symbols).
        contract = Future(
            symbol=symbol, localSymbol=local_sym,
            exchange=get_instrument(symbol).exchange,
        )
        contract = self.manager.qualify_contract(contract)
        self._cached_contracts[symbol] = contract
        log.info(
            "Cached qualified contract for %s: %s (conId=%d)",
            symbol, contract.localSymbol, contract.conId,
        )

    def _get_contract(self, symbol: str) -> Any:
        """Return the cached qualified contract for a symbol.

        Raises RuntimeError if the contract was not pre-resolved via
        resolve_contract() — this is intentional to catch call-ordering
        bugs early rather than silently re-entering the event loop.
        """
        if symbol not in self._cached_contracts:
            raise RuntimeError(
                f"No cached contract for '{symbol}'. "
                f"Call resolve_contract('{symbol}') during startup "
                f"before the event loop starts."
            )
        return self._cached_contracts[symbol]

    def place_bracket_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        **kwargs
    ) -> list:
        contract = self._get_contract(symbol)

        # Two-phase order placement support:
        # Phase 1 (entry-only): live_trader calls without tp_price/sl_price,
        #   children are placed separately via place_child_orders after fill.
        # Full bracket: tp_price and sl_price are provided for a complete
        #   bracket order (parent + TP + SL).
        if "tp_price" in kwargs and "sl_price" in kwargs:
            return self.manager.place_bracket_order(
                contract=contract,
                action=action,
                quantity=quantity,
                **kwargs
            )
        else:
            # Entry-only: route to place_entry_order (returns single Trade)
            return self.manager.place_entry_order(
                contract=contract,
                action=action,
                quantity=quantity,
                **kwargs
            )

    def place_child_orders(
        self,
        symbol: str,
        parent_order_id: int,
        action: str,
        quantity: int,
        tp_price: float,
        sl_price: float,
    ) -> list:
        contract = self._get_contract(symbol)

        return self.manager.place_child_orders(
            contract=contract,
            parent_order_id=parent_order_id,
            action=action,
            quantity=quantity,
            tp_price=tp_price,
            sl_price=sl_price,
        )

    def modify_order(self, order_id, event=None) -> Any:
        """Transmit a modification of a resting order to IBKR.

        Modify = re-placeOrder the SAME ib_insync Order object (same
        orderId, unchanged permId) on the same client session — the
        documented ib_insync modify flow, identical to the pre-refactor
        trailing transmit. The qualified contract rides in on the Trade
        (``event.raw_event``), so this method makes NO qualification /
        reqContractDetails / reqHistoricalData calls and is safe to
        invoke from ib_insync bar-update callbacks (placeOrder is a
        plain synchronous message send).

        Raises on every sync-detectable failure per the ExecutionClient
        contract: ValueError (malformed event, order-id mismatch),
        ConnectionError (session not connected). Venue-side rejection
        arrives asynchronously via the error callback.
        """
        if event is None or getattr(event, "raw_event", None) is None:
            raise ValueError(
                f"modify_order({order_id}): event.raw_event "
                f"(ib_insync Trade) is required"
            )
        trade = event.raw_event
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        if order is None or contract is None:
            raise ValueError(
                f"modify_order({order_id}): raw_event lacks .order/.contract"
            )
        if str(order.orderId) != str(order_id):
            raise ValueError(
                f"modify_order: order_id mismatch — called with {order_id} "
                f"but raw_event.order.orderId is {order.orderId}"
            )
        if not self.is_connected():
            raise ConnectionError(
                f"modify_order({order_id}): IBKR session not connected — "
                f"cannot transmit SL modification"
            )
        log.info(
            "MODIFY ORDER: re-placing orderId=%s auxPrice=%s",
            order.orderId, getattr(order, "auxPrice", None),
        )
        return self.manager.ib.placeOrder(contract, order)

    def cancel_open_orders(self, symbol: str) -> int:
        return self.manager.cancel_open_cl_orders(symbol=symbol)

    def close_position(self, symbol: str, exit_mode: str, current_price: float) -> Any:
        return self.manager.close_cl_position(symbol=symbol, exit_mode=exit_mode, current_price=current_price)

    def register_error_callback(self, callback: Any) -> None:
        self.manager.ib.errorEvent += callback

    def get_open_trades(self, symbol: str) -> list:
        """Query IBKR for all open/pending trades for a given symbol.

        Returns a list of StandardExecutionEvent objects built from
        ib_insync's openTrades(), enabling position recovery to verify
        TP/SL orders without relying on subscription callbacks.
        """
        if not self.is_connected():
            return []
        events = []
        for trade in self.manager.ib.openTrades():
            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            if contract is None or order is None:
                continue
            if getattr(contract, "symbol", None) != symbol:
                continue
            events.append(StandardExecutionEvent(
                order_id=str(order.orderId),
                symbol=contract.symbol,
                status=trade.orderStatus.status if trade.orderStatus else "Unknown",
                filled_qty=int(trade.orderStatus.filled) if trade.orderStatus else 0,
                remaining_qty=int(trade.orderStatus.remaining) if trade.orderStatus else 0,
                avg_price=float(trade.orderStatus.avgFillPrice) if trade.orderStatus else 0.0,
                raw_event=trade,
            ))
        return events
