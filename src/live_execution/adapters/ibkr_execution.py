from typing import Callable, Any, Optional
from src.core.instrument_master import (
    contract_matches,
    get_instrument,
    registry_symbol_for_contract,
)
from src.live_execution.interfaces.execution_interface import (
    ExecutionClient,
    StandardCommissionEvent,
    StandardExecutionEvent,
)
from src.live_execution.ibkr_client import IBKRConnectionManager
import logging

log = logging.getLogger("IBKRExecAdapter")

# IBKR wire sentinel for "no value" on a double field (e.g. realizedPNL on
# an opening fill) — must map to None, never be stored as 1.8e308.
_IBKR_UNSET_DOUBLE = 1.7976931348623157e+308

class IBKRExecutionClient(ExecutionClient):
    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 10, fallback_ports: list[int] = None):
        if fallback_ports is None:
            fallback_ports = [7497]
        self.manager = IBKRConnectionManager(host=host, port=port, client_id=client_id, readonly=False, fallback_ports=fallback_ports)
        self._order_callbacks = []
        self._commission_callbacks = []
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
        # Commission reports: exec session only — the data client's IB
        # session receives duplicate commissionReport events (see the
        # wrapper-logging note above), so bridging here exactly once.
        self.manager.ib.commissionReportEvent += self._on_commission_report

    def connect(self) -> None:
        self.manager.connect()

    def disconnect(self) -> None:
        self.manager.disconnect()

    def is_connected(self) -> bool:
        return self.manager.ib.isConnected()

    def register_order_status_callback(self, callback: Callable[[StandardExecutionEvent], None]) -> None:
        self._order_callbacks.append(callback)

    def register_commission_callback(
        self, callback: Callable[[StandardCommissionEvent], None]
    ) -> None:
        self._commission_callbacks.append(callback)

    def _on_commission_report(self, trade: Any, fill: Any, report: Any):
        """Bridge ib_insync commissionReportEvent -> StandardCommissionEvent.

        A consumer exception must never propagate into ib_insync's event
        loop; each callback is isolated.
        """
        realized = getattr(report, "realizedPNL", None)
        if realized is not None and abs(realized) >= _IBKR_UNSET_DOUBLE:
            realized = None
        event = StandardCommissionEvent(
            order_id=str(trade.order.orderId),
            exec_id=str(fill.execution.execId),
            symbol=registry_symbol_for_contract(
                trade.contract.symbol,
                getattr(trade.contract, "tradingClass", None),
            ),
            commission=float(getattr(report, "commission", 0.0) or 0.0),
            realized_pnl=realized,
            currency=str(getattr(report, "currency", "") or ""),
            raw_event=report,
        )
        for cb in self._commission_callbacks:
            try:
                cb(event)
            except Exception:
                log.warning(
                    "Commission callback failed (execId=%s)",
                    event.exec_id, exc_info=True,
                )

    def _on_order_status(self, trade: Any):
        event = StandardExecutionEvent(
            order_id=str(trade.order.orderId),
            symbol=registry_symbol_for_contract(
                trade.contract.symbol,
                getattr(trade.contract, "tradingClass", None),
            ),
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

    def get_cached_position(self, symbol: str) -> int:
        """Net position from ib_insync's LOCAL portfolio/position cache.

        A-2 (hourly housekeeping): this read must be safe under
        _ledger_lock, so it never calls ensure_connected/connect or any
        synchronous broker request — ib.portfolio()/ib.positions() are
        in-memory caches maintained by the account-update subscription.
        An unheld symbol reads as flat (0).
        """
        items = self.manager.ib.portfolio()
        if not items:
            items = self.manager.ib.positions()
        total = 0.0
        for item in items:
            contract = getattr(item, "contract", None)
            if not contract_matches(
                symbol, getattr(contract, "symbol", None),
                getattr(contract, "tradingClass", None),
            ):
                continue
            total += float(getattr(item, "position", 0) or 0)
        return int(total)

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
        # ib_search_symbol/tradingClass handle products that share an IB symbol
        # (micro silver SIL → symbol "SI", tradingClass "SIL").
        inst = get_instrument(symbol)
        contract = Future(
            symbol=inst.ib_search_symbol, localSymbol=local_sym,
            exchange=inst.exchange, tradingClass=inst.ib_trading_class or "",
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

    def cancel_orders_by_ids(self, order_ids: list) -> int:
        """Cancel exactly the requested order ids, irrespective of contract.

        OOB-recovery primitive: matches openTrades() on orderId only —
        NO symbol filter, so a protective order resting on an OLD contract
        symbol (post-reconfiguration, e.g. MGC->GC 2026-07-06) is still
        cancelled. Per-instance client ids mean openTrades() exposes only
        this instance's own orders, so cross-child interference is
        structurally impossible. str/int-robust: ledger ids are ints,
        ib_insync order ids are ints, callers may pass either.
        """
        wanted = {str(oid) for oid in order_ids}
        cancelled = 0
        for trade in self.manager.ib.openTrades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            if str(getattr(order, "orderId", None)) not in wanted:
                continue
            self.manager.ib.cancelOrder(trade.order)
            cancelled += 1
            log.info(
                "CANCEL BY ID: orderId=%s (contract=%s)",
                order.orderId,
                getattr(getattr(trade, "contract", None), "symbol", "?"),
            )
        return cancelled

    def get_executions(self, symbol: Optional[str] = None) -> list:
        """Return this session's fills as flat record dicts.

        Wraps ib_insync ``ib.fills()`` — current-day executions are
        locally cached by connect's reqExecutions, so this is a sync-safe
        read at startup recovery (same pattern as get_open_trades).
        symbol=None returns every fill: OOB recovery matches by order id
        and the fill may live on an old contract symbol.
        """
        records = []
        for fill in self.manager.ib.fills():
            execution = getattr(fill, "execution", None)
            if execution is None:
                continue
            contract = getattr(fill, "contract", None)
            contract_symbol = getattr(contract, "symbol", None)
            if symbol is not None and contract_symbol != symbol:
                continue
            report = getattr(fill, "commissionReport", None)
            # ib_insync pre-populates Fill.commissionReport with an empty
            # placeholder (execId == "") until the broker report arrives —
            # that placeholder means "no report", never a $0 commission.
            if report is not None and getattr(report, "execId", None) == "":
                report = None
            records.append({
                "order_id": str(getattr(execution, "orderId", None)),
                "perm_id": getattr(execution, "permId", None),
                "exec_id": getattr(execution, "execId", None),
                "price": float(getattr(execution, "price", 0.0) or 0.0),
                "qty": float(getattr(execution, "shares", 0) or 0),
                "side": getattr(execution, "side", None),
                "time": (
                    getattr(execution, "time", None)
                    or getattr(fill, "time", None)
                ),
                "symbol": contract_symbol,
                "commission_report": report,
            })
        return records

    def close_position(self, symbol: str, exit_mode: str, current_price: float) -> Any:
        return self.manager.close_cl_position(symbol=symbol, exit_mode=exit_mode, current_price=current_price)

    def register_error_callback(self, callback: Any) -> None:
        self.manager.ib.errorEvent += callback

    def get_open_trades(self, symbol: Optional[str]) -> list:
        """Query IBKR for all open/pending trades for a given symbol.

        Returns a list of StandardExecutionEvent objects built from
        ib_insync's openTrades(), enabling position recovery to verify
        TP/SL orders without relying on subscription callbacks.

        A-10: ``symbol=None`` returns EVERY session order irrespective
        of contract symbol — an orphaned protective order can rest on an
        OLD contract symbol after an instrument reconfiguration (the
        2026-07-06 MGC->GC trade_8 class) and a symbol filter would
        silently hide it from the housekeeping sweep.
        """
        if not self.is_connected():
            return []
        events = []
        for trade in self.manager.ib.openTrades():
            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            if contract is None or order is None:
                continue
            if symbol is not None and not contract_matches(
                symbol, getattr(contract, "symbol", None),
                getattr(contract, "tradingClass", None),
            ):
                continue
            events.append(StandardExecutionEvent(
                order_id=str(order.orderId),
                symbol=registry_symbol_for_contract(
                    contract.symbol, getattr(contract, "tradingClass", None),
                ),
                status=trade.orderStatus.status if trade.orderStatus else "Unknown",
                filled_qty=int(trade.orderStatus.filled) if trade.orderStatus else 0,
                remaining_qty=int(trade.orderStatus.remaining) if trade.orderStatus else 0,
                avg_price=float(trade.orderStatus.avgFillPrice) if trade.orderStatus else 0.0,
                raw_event=trade,
            ))
        return events
