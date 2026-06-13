from typing import Callable, Any, Optional
from src.live_execution.interfaces.execution_interface import ExecutionClient, StandardExecutionEvent
from src.live_execution.ibkr_client import IBKRConnectionManager, build_cl_contract

class IBKRExecutionClient(ExecutionClient):
    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 10, fallback_ports: list[int] = None):
        if fallback_ports is None:
            fallback_ports = [7497]
        self.manager = IBKRConnectionManager(host=host, port=port, client_id=client_id, readonly=False, fallback_ports=fallback_ports)
        self._order_callbacks = []
        
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

    def place_bracket_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        **kwargs
    ) -> list:
        # Resolve the contract
        from ib_insync import Future
        local_sym, _ = self.manager.get_front_month_contract(symbol=symbol)
        contract = Future(symbol=symbol, localSymbol=local_sym, exchange="NYMEX")
        contract = self.manager.qualify_contract(contract)
        
        return self.manager.place_bracket_order(
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
        from ib_insync import Future
        local_sym, _ = self.manager.get_front_month_contract(symbol=symbol)
        contract = Future(symbol=symbol, localSymbol=local_sym, exchange="NYMEX")
        contract = self.manager.qualify_contract(contract)

        return self.manager.place_child_orders(
            contract=contract,
            parent_order_id=parent_order_id,
            action=action,
            quantity=quantity,
            tp_price=tp_price,
            sl_price=sl_price,
        )

    def cancel_open_orders(self, symbol: str) -> int:
        return self.manager.cancel_open_cl_orders(symbol=symbol)

    def close_position(self, symbol: str, exit_mode: str, current_price: float) -> Any:
        return self.manager.close_cl_position(symbol=symbol, exit_mode=exit_mode, current_price=current_price)
