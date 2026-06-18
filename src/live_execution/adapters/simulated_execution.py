"""Simulated Execution Adapter — Bar-Level Matching Engine.

Implements the ExecutionClient interface with an in-memory matching engine
that evaluates resting TP/SL orders against each bar's High and Low.

Slippage, commission, and gap-fill logic are identical to BacktestEngine
to ensure parity between the two backtesting pipelines.

Fill model:
    - Entry fills: at current_price + slippage (adverse 1-tick)
    - TP/SL fills: bar H/L evaluation with gap-fill at Open if bar
      gaps past target, plus slippage
    - Same-bar conflict (both TP and SL trigger): SL wins (pessimistic)
    - Commission: $2.50/side/contract (round-trip $5.00)
    - Slippage: $0.01/side (1 CL tick = $10/side at 1000x multiplier)

Usage:
    Called by livetest_engine.py driver — not run standalone.

Author: CL Analyst
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from src.live_execution.interfaces.execution_interface import (
    ExecutionClient,
    StandardExecutionEvent,
)

log = logging.getLogger("SimExec")


# ---------------------------------------------------------------------------
# Constants (mirroring BacktestEngine defaults)
# ---------------------------------------------------------------------------

_DEFAULT_SLIPPAGE_PER_SIDE: float = 0.01     # $0.01 price units (1 CL tick)
_DEFAULT_COMMISSION_PER_SIDE: float = 2.50   # $2.50 per side per contract
_DEFAULT_CONTRACT_MULTIPLIER: float = 1000.0 # CL futures: $1000 per 1.0 move


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

class _OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class _OrderType(Enum):
    LIMIT = "LMT"       # TP resting order (profit target)
    STOP = "STP"        # SL resting order (stop loss)
    MARKET = "MKT"      # Market order (entry or time-barrier exit)


@dataclass
class _RestingOrder:
    """A resting TP or SL order waiting to be evaluated against bars."""
    order_id: int
    parent_order_id: int
    symbol: str
    action: str            # "BUY" or "SELL" (the exit action)
    quantity: int
    price: float           # trigger price (limit for TP, stop for SL)
    order_type: _OrderType # LIMIT (TP) or STOP (SL)
    position_side: int     # +1 = long position, -1 = short position


@dataclass
class _SimTrade:
    """Record of a completed simulated trade for the export ledger."""
    entry_time: pd.Timestamp
    exit_time: Optional[pd.Timestamp] = None
    signal_side: str = ""        # "LONG" or "SHORT"
    entry_price: float = 0.0
    entry_fill: float = 0.0
    exit_price: float = 0.0
    exit_fill: float = 0.0
    exit_reason: str = ""        # "TP_HIT", "SL_HIT", "TIME_BARRIER"
    initial_tp_price: float = 0.0
    initial_sl_price: float = 0.0
    atr_at_entry: float = 0.0
    duration_bars: int = 0
    lots: int = 1
    gross_pnl_dollars: float = 0.0
    commission_dollars: float = 0.0
    net_pnl_dollars: float = 0.0


# ---------------------------------------------------------------------------
# Simulated Execution Client
# ---------------------------------------------------------------------------

class SimulatedExecution(ExecutionClient):
    """In-memory matching engine implementing ExecutionClient.

    Evaluates resting TP/SL orders against each bar's OHLC, applying
    the same gap-fill and slippage logic as BacktestEngine.

    The driver calls ``on_bar_feed(bar)`` BEFORE firing the LiveTrader's
    bar callback.  This ensures that TP/SL fills from the previous bar's
    bracket are reflected in ``get_position()`` by the time the trader
    checks its position on the new bar.
    """

    def __init__(
        self,
        *,
        slippage_per_side: float = _DEFAULT_SLIPPAGE_PER_SIDE,
        commission_per_side: float = _DEFAULT_COMMISSION_PER_SIDE,
        contract_multiplier: float = _DEFAULT_CONTRACT_MULTIPLIER,
    ) -> None:
        self._slippage = slippage_per_side
        self._commission = commission_per_side
        self._multiplier = contract_multiplier

        # State
        self._position: int = 0                 # net contracts (+1, -1, 0)
        self._position_side: int = 0            # +1 long, -1 short, 0 flat
        self._next_order_id: int = 1000
        self._resting_orders: dict[int, _RestingOrder] = {}
        self._order_callbacks: list[Callable] = []
        self._current_bar_close: float = 0.0    # updated by on_bar_feed()
        self._current_bar_open: float = 0.0
        self._current_bar_high: float = 0.0
        self._current_bar_low: float = 0.0

        # Trade ledger
        self._active_entry: Optional[dict] = None  # entry context for open trade
        self._completed_trades: list[_SimTrade] = []
        self._bars_since_entry: int = 0

        # Deferred callbacks (fired after bar processing)
        self._deferred_callbacks: list[StandardExecutionEvent] = []

    # ── ExecutionClient interface ────────────────────────────────────

    def connect(self) -> None:
        log.info("SimulatedExecution: connected (in-memory)")

    def disconnect(self) -> None:
        log.info("SimulatedExecution: disconnected")

    def is_connected(self) -> bool:
        return True

    def register_order_status_callback(
        self, callback: Callable[[StandardExecutionEvent], None]
    ) -> None:
        self._order_callbacks.append(callback)

    def register_error_callback(self, callback: Any) -> None:
        pass  # No errors in simulation

    def get_position(self, symbol: str) -> int:
        return self._position

    def get_account_summary(self, symbol: str) -> dict:
        """Return mock account summary matching IBKRExecutionClient format."""
        unrealized = 0.0
        if self._active_entry and self._position != 0:
            entry_fill = self._active_entry["entry_fill"]
            side = self._position_side
            price_pnl = side * (self._current_bar_close - entry_fill)
            unrealized = price_pnl * self._multiplier * abs(self._position)

        realized = sum(t.net_pnl_dollars for t in self._completed_trades)
        return {
            "cl_position": self._position,
            "cl_unrealized_pnl": unrealized,
            "cl_realized_pnl": realized,
        }

    def resolve_contract(self, symbol: str) -> None:
        pass  # No contract resolution needed

    def get_open_trades(self, symbol: str) -> list:
        """Return StandardExecutionEvent list for resting orders."""
        events = []
        for oid, order in self._resting_orders.items():
            events.append(StandardExecutionEvent(
                order_id=str(oid),
                symbol=order.symbol,
                status="Submitted",
                filled_qty=0,
                remaining_qty=order.quantity,
                avg_price=0.0,
                raw_event=SimpleNamespace(
                    order=SimpleNamespace(
                        orderId=oid,
                        parentId=order.parent_order_id,
                        action=order.action,
                        totalQuantity=order.quantity,
                        orderType=order.order_type.value,
                        lmtPrice=order.price if order.order_type == _OrderType.LIMIT else 0.0,
                        auxPrice=order.price if order.order_type == _OrderType.STOP else 0.0,
                    )
                ),
            ))
        return events

    def place_bracket_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        **kwargs,
    ) -> Any:
        """Simulate an entry fill at the current bar's close + slippage.

        Phase 1 only: places the ENTRY order and returns a mock Trade
        object.  The LiveTrader will call ``place_child_orders()`` from
        its fill callback to register TP/SL.

        Returns a SimpleNamespace mimicking ib_insync Trade with
        ``.order.orderId`` for the LiveTrader to read.
        """
        order_id = self._alloc_order_id()
        limit_price = kwargs.get("limit_price", self._current_bar_close)

        # Apply slippage (adverse for the trader)
        fill_price = self._apply_slippage(limit_price, action)

        # Update position
        if action.upper() == "BUY":
            self._position = quantity
            self._position_side = 1
        else:
            self._position = -quantity
            self._position_side = -1

        # Store entry context
        self._active_entry = {
            "order_id": order_id,
            "symbol": symbol,
            "action": action.upper(),
            "quantity": quantity,
            "entry_price": limit_price,
            "entry_fill": fill_price,
            "entry_bar_time": self._current_bar_time,
        }
        self._bars_since_entry = 0

        log.info(
            "SIM ENTRY: %s %d %s @ %.2f (fill=%.2f, orderId=%d)",
            action, quantity, symbol, limit_price, fill_price, order_id,
        )

        # Build the mock Trade object that LiveTrader expects
        mock_trade = SimpleNamespace(
            order=SimpleNamespace(
                orderId=order_id,
                parentId=0,
                action=action.upper(),
                totalQuantity=quantity,
                orderType=kwargs.get("entry_mode", "MKT"),
                algoStrategy=None,
                permId=order_id,
                account="SIM",
                tif="GTC",
                lmtPrice=limit_price,
                auxPrice=0.0,
            ),
            orderStatus=SimpleNamespace(
                status="Filled",
                filled=quantity,
                remaining=0,
                avgFillPrice=fill_price,
            ),
            contract=SimpleNamespace(symbol=symbol),
        )

        # Queue fill callback (fired by driver after _on_new_bar returns)
        fill_event = StandardExecutionEvent(
            order_id=str(order_id),
            symbol=symbol,
            status="Filled",
            filled_qty=quantity,
            remaining_qty=0,
            avg_price=fill_price,
            raw_event=mock_trade,
        )
        self._deferred_callbacks.append(fill_event)

        return mock_trade

    def place_child_orders(
        self,
        symbol: str,
        parent_order_id: int,
        action: str,
        quantity: int,
        tp_price: float | list,
        sl_price: float,
    ) -> list:
        """Register TP and SL as resting orders for bar-level evaluation.

        tp_price may be a scalar (single TP) or a list of
        ``(lots, price)`` tuples for tiered TPs.

        Returns a list of mock Trade objects (TP trades + SL trade).
        """
        position_side = self._position_side
        child_trades = []

        # ── TP orders ─────────────────────────────────────────────
        if isinstance(tp_price, list):
            # Tiered TPs: [(lots, price), ...]
            for lots, price in tp_price:
                tp_oid = self._alloc_order_id()
                self._resting_orders[tp_oid] = _RestingOrder(
                    order_id=tp_oid,
                    parent_order_id=parent_order_id,
                    symbol=symbol,
                    action=action.upper(),
                    quantity=lots,
                    price=price,
                    order_type=_OrderType.LIMIT,
                    position_side=position_side,
                )
                child_trades.append(SimpleNamespace(
                    order=SimpleNamespace(orderId=tp_oid, parentId=parent_order_id),
                ))
                log.debug(
                    "SIM RESTING TP: orderId=%d price=%.2f lots=%d",
                    tp_oid, price, lots,
                )
        else:
            # Single TP
            tp_oid = self._alloc_order_id()
            self._resting_orders[tp_oid] = _RestingOrder(
                order_id=tp_oid,
                parent_order_id=parent_order_id,
                symbol=symbol,
                action=action.upper(),
                quantity=quantity,
                price=tp_price,
                order_type=_OrderType.LIMIT,
                position_side=position_side,
            )
            child_trades.append(SimpleNamespace(
                order=SimpleNamespace(orderId=tp_oid, parentId=parent_order_id),
            ))
            log.debug(
                "SIM RESTING TP: orderId=%d price=%.2f qty=%d",
                tp_oid, tp_price, quantity,
            )

        # ── SL order ──────────────────────────────────────────────
        sl_oid = self._alloc_order_id()
        self._resting_orders[sl_oid] = _RestingOrder(
            order_id=sl_oid,
            parent_order_id=parent_order_id,
            symbol=symbol,
            action=action.upper(),
            quantity=quantity,
            price=sl_price,
            order_type=_OrderType.STOP,
            position_side=position_side,
        )
        child_trades.append(SimpleNamespace(
            order=SimpleNamespace(orderId=sl_oid, parentId=parent_order_id),
        ))
        log.debug(
            "SIM RESTING SL: orderId=%d price=%.2f qty=%d",
            sl_oid, sl_price, quantity,
        )

        return child_trades

    def modify_order(self, order_id, event=None) -> None:
        """Modify a resting order's price.

        Called by LiveTrader._check_trailing_stop() to update the SL
        price after trailing stop activation.  The new price is already
        written into the event's raw_event.order.auxPrice by the caller.
        We just need to update our internal _resting_orders dict.
        """
        oid = int(order_id) if not isinstance(order_id, int) else order_id
        resting = self._resting_orders.get(oid)
        if resting is None:
            log.debug("modify_order: orderId=%s not found in resting orders", order_id)
            return
        # Read the new price from the event if available
        if event is not None:
            raw = getattr(event, "raw_event", None)
            raw_order = getattr(raw, "order", None) if raw else None
            new_price = getattr(raw_order, "auxPrice", None) if raw_order else None
            if new_price is not None:
                old_price = resting.price
                resting.price = new_price
                log.debug(
                    "SIM MODIFY ORDER: orderId=%d price=%.2f → %.2f",
                    oid, old_price, new_price,
                )

    def cancel_open_orders(self, symbol: str) -> int:
        """Cancel all resting orders for the given symbol."""
        to_remove = [
            oid for oid, o in self._resting_orders.items()
            if o.symbol == symbol
        ]
        for oid in to_remove:
            del self._resting_orders[oid]
        if to_remove:
            log.info(
                "SIM CANCEL: removed %d resting orders for %s",
                len(to_remove), symbol,
            )
        return len(to_remove)

    def close_position(
        self,
        symbol: str,
        exit_mode: str,
        current_price: float,
    ) -> Any:
        """Close the current position at current_price + slippage.

        Used by LiveTrader for time-barrier exits.
        """
        if self._position == 0:
            return None

        # Determine exit action
        exit_action = "SELL" if self._position > 0 else "BUY"
        qty = abs(self._position)

        # Apply slippage
        fill_price = self._apply_slippage(current_price, exit_action)

        # Record trade
        self._record_exit(
            exit_price=current_price,
            exit_fill=fill_price,
            exit_reason="TIME_BARRIER",
            exit_bar_time=self._current_bar_time,
        )

        # Cancel any remaining resting orders
        self.cancel_open_orders(symbol)

        # Reset position
        self._position = 0
        self._position_side = 0

        log.info(
            "SIM CLOSE: %s %d %s @ %.2f (fill=%.2f) reason=TIME_BARRIER",
            exit_action, qty, symbol, current_price, fill_price,
        )

        return SimpleNamespace(
            order=SimpleNamespace(orderId=self._alloc_order_id()),
        )

    # ── Matching Engine (called by driver) ───────────────────────────

    def on_bar_feed(
        self,
        bar_time: pd.Timestamp,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
    ) -> None:
        """Evaluate all resting orders against this bar's OHLC.

        Called by the driver BEFORE LiveTrader's bar callback fires.
        This ensures TP/SL fills from resting brackets are reflected
        in ``get_position()`` before the trader checks.

        Matching priority (pessimistic, same as BacktestEngine):
          1. If BOTH TP and SL trigger on the same bar → SL wins
          2. SL-only → fill at gap_fill_price
          3. TP-only → fill at gap_fill_price
        """
        # Update current bar state
        self._current_bar_time = bar_time
        self._current_bar_open = open_
        self._current_bar_high = high
        self._current_bar_low = low
        self._current_bar_close = close

        # Track bars held for the active position
        if self._active_entry is not None:
            self._bars_since_entry += 1

        if not self._resting_orders:
            return

        # Classify each resting order as TP or SL
        tp_orders: list[_RestingOrder] = []
        sl_orders: list[_RestingOrder] = []
        for order in self._resting_orders.values():
            if order.order_type == _OrderType.LIMIT:
                tp_orders.append(order)
            elif order.order_type == _OrderType.STOP:
                sl_orders.append(order)

        # Evaluate triggers
        tp_triggered = [o for o in tp_orders if self._is_tp_triggered(o, high, low)]
        sl_triggered = [o for o in sl_orders if self._is_sl_triggered(o, high, low)]

        if tp_triggered and sl_triggered:
            # ── Same-bar conflict: SL wins (pessimistic) ──────────
            self._fill_sl_orders(sl_triggered, open_, bar_time)
            # Cancel remaining TP orders (OCA behavior)
            for o in tp_orders:
                self._resting_orders.pop(o.order_id, None)
        elif sl_triggered:
            self._fill_sl_orders(sl_triggered, open_, bar_time)
            # Cancel TPs (OCA)
            for o in tp_orders:
                self._resting_orders.pop(o.order_id, None)
        elif tp_triggered:
            self._fill_tp_orders(tp_triggered, open_, bar_time)
            # Cancel SLs (OCA)
            for o in sl_orders:
                self._resting_orders.pop(o.order_id, None)

    def flush_deferred_callbacks(self) -> None:
        """Fire any deferred fill callbacks queued during this bar.

        Called by the driver AFTER LiveTrader's ``_on_new_bar`` returns,
        so that entry fills don't recursively trigger ``place_child_orders``
        while ``_on_new_bar`` is still executing under ``_ledger_lock``.
        """
        while self._deferred_callbacks:
            event = self._deferred_callbacks.pop(0)
            for cb in self._order_callbacks:
                cb(event)

    # ── Trade ledger export ──────────────────────────────────────────

    def export_ledger(self) -> pd.DataFrame:
        """Export completed trades as a DataFrame for reconciliation.

        Schema matches BacktestResult.to_dataframe() so that
        trade_reconciler.py can diff them directly.
        """
        if not self._completed_trades:
            return pd.DataFrame()
        rows = []
        for t in self._completed_trades:
            rows.append({
                "entry_time": t.entry_time,
                "signal_side": t.signal_side,
                "entry_price": t.entry_price,
                "entry_fill": t.entry_fill,
                "initial_tp_price": t.initial_tp_price,
                "initial_sl_price": t.initial_sl_price,
                "exit_time": t.exit_time,
                "exit_price": t.exit_price,
                "exit_fill": t.exit_fill,
                "exit_reason": t.exit_reason,
                "atr_at_entry": t.atr_at_entry,
                "duration_bars": t.duration_bars,
                "lots": t.lots,
                "gross_pnl_dollars": t.gross_pnl_dollars,
                "commission_dollars": t.commission_dollars,
                "net_pnl_dollars": t.net_pnl_dollars,
            })
        return pd.DataFrame(rows)

    @property
    def completed_trades(self) -> list[_SimTrade]:
        return self._completed_trades

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl_dollars for t in self._completed_trades)

    @property
    def trade_count(self) -> int:
        return len(self._completed_trades)

    @property
    def win_rate(self) -> float:
        if not self._completed_trades:
            return 0.0
        wins = sum(1 for t in self._completed_trades if t.net_pnl_dollars > 0)
        return wins / len(self._completed_trades)

    # ── Internal helpers ─────────────────────────────────────────────

    def _alloc_order_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    def _apply_slippage(self, price: float, action: str) -> float:
        """Adverse 1-tick slippage (identical to BacktestEngine)."""
        if action.upper() == "BUY":
            return price + self._slippage
        return price - self._slippage

    def _gap_fill_price(
        self,
        bar_open: float,
        target_price: float,
        position_side: int,
        is_tp: bool,
    ) -> float:
        """Determine fill price accounting for gaps.

        Identical to BacktestEngine._gap_fill_price():
        If bar opens past the target, fill at bar_open (not target).
        """
        if position_side == 1:  # Long position
            if is_tp:
                # TP: price goes UP past target
                return bar_open if bar_open >= target_price else target_price
            else:
                # SL: price goes DOWN past target
                return bar_open if bar_open <= target_price else target_price
        else:  # Short position
            if is_tp:
                # TP: price goes DOWN past target
                return bar_open if bar_open <= target_price else target_price
            else:
                # SL: price goes UP past target
                return bar_open if bar_open >= target_price else target_price

    def _is_tp_triggered(
        self, order: _RestingOrder, bar_high: float, bar_low: float
    ) -> bool:
        """Check if a TP limit order is triggered by this bar."""
        if order.position_side == 1:
            # Long TP: sell limit — triggered when bar_high >= price
            return bar_high >= order.price
        else:
            # Short TP: buy limit — triggered when bar_low <= price
            return bar_low <= order.price

    def _is_sl_triggered(
        self, order: _RestingOrder, bar_high: float, bar_low: float
    ) -> bool:
        """Check if a SL stop order is triggered by this bar."""
        if order.position_side == 1:
            # Long SL: sell stop — triggered when bar_low <= price
            return bar_low <= order.price
        else:
            # Short SL: buy stop — triggered when bar_high >= price
            return bar_high >= order.price

    def _fill_tp_orders(
        self,
        orders: list[_RestingOrder],
        bar_open: float,
        bar_time: pd.Timestamp,
    ) -> None:
        """Fill triggered TP orders with gap-fill logic."""
        for order in orders:
            raw_fill = self._gap_fill_price(
                bar_open, order.price, order.position_side, is_tp=True,
            )
            fill_price = self._apply_slippage(raw_fill, order.action)

            log.info(
                "SIM TP FILL: orderId=%d @ %.2f (gap_fill=%.2f, target=%.2f)",
                order.order_id, fill_price, raw_fill, order.price,
            )

            # Record trade exit
            self._record_exit(
                exit_price=raw_fill,
                exit_fill=fill_price,
                exit_reason="TP_HIT",
                exit_bar_time=bar_time,
            )

            # Fire callback to LiveTrader
            event = StandardExecutionEvent(
                order_id=str(order.order_id),
                symbol=order.symbol,
                status="Filled",
                filled_qty=order.quantity,
                remaining_qty=0,
                avg_price=fill_price,
            )
            for cb in self._order_callbacks:
                cb(event)

            # Remove from resting
            self._resting_orders.pop(order.order_id, None)

        # Reset position
        self._position = 0
        self._position_side = 0

    def _fill_sl_orders(
        self,
        orders: list[_RestingOrder],
        bar_open: float,
        bar_time: pd.Timestamp,
    ) -> None:
        """Fill triggered SL orders with gap-fill logic."""
        for order in orders:
            raw_fill = self._gap_fill_price(
                bar_open, order.price, order.position_side, is_tp=False,
            )
            fill_price = self._apply_slippage(raw_fill, order.action)

            log.info(
                "SIM SL FILL: orderId=%d @ %.2f (gap_fill=%.2f, target=%.2f)",
                order.order_id, fill_price, raw_fill, order.price,
            )

            # Record trade exit
            self._record_exit(
                exit_price=raw_fill,
                exit_fill=fill_price,
                exit_reason="SL_HIT",
                exit_bar_time=bar_time,
            )

            # Fire callback to LiveTrader
            event = StandardExecutionEvent(
                order_id=str(order.order_id),
                symbol=order.symbol,
                status="Filled",
                filled_qty=order.quantity,
                remaining_qty=0,
                avg_price=fill_price,
            )
            for cb in self._order_callbacks:
                cb(event)

            # Remove from resting
            self._resting_orders.pop(order.order_id, None)

        # Reset position
        self._position = 0
        self._position_side = 0

    def _record_exit(
        self,
        exit_price: float,
        exit_fill: float,
        exit_reason: str,
        exit_bar_time: pd.Timestamp,
    ) -> None:
        """Record a completed trade into the ledger."""
        if self._active_entry is None:
            return

        entry = self._active_entry
        side = self._position_side
        qty = abs(self._position) or entry["quantity"]
        entry_fill = entry["entry_fill"]

        gross_pnl_price = side * (exit_fill - entry_fill)
        gross_pnl_dollars = gross_pnl_price * self._multiplier * qty
        commission = 2 * self._commission * qty
        net_pnl = gross_pnl_dollars - commission

        # Gather TP/SL prices from resting orders (before they're cancelled)
        tp_price = 0.0
        sl_price = 0.0
        for o in self._resting_orders.values():
            if o.order_type == _OrderType.LIMIT and tp_price == 0.0:
                tp_price = o.price
            elif o.order_type == _OrderType.STOP and sl_price == 0.0:
                sl_price = o.price

        trade = _SimTrade(
            entry_time=entry.get("entry_bar_time", pd.NaT),
            exit_time=exit_bar_time,
            signal_side="LONG" if side == 1 else "SHORT",
            entry_price=entry["entry_price"],
            entry_fill=entry_fill,
            exit_price=exit_price,
            exit_fill=exit_fill,
            exit_reason=exit_reason,
            initial_tp_price=tp_price,
            initial_sl_price=sl_price,
            duration_bars=self._bars_since_entry,
            lots=qty,
            gross_pnl_dollars=gross_pnl_dollars,
            commission_dollars=commission,
            net_pnl_dollars=net_pnl,
        )
        self._completed_trades.append(trade)
        self._active_entry = None

        log.info(
            "SIM TRADE CLOSED: %s entry=%.2f exit=%.2f pnl=$%.2f reason=%s bars=%d",
            trade.signal_side, entry_fill, exit_fill,
            net_pnl, exit_reason, trade.duration_bars,
        )

    def update_sl_price(self, sl_order_id: int, new_sl_price: float) -> None:
        """Update a resting SL order's price (for trailing stop)."""
        if sl_order_id in self._resting_orders:
            self._resting_orders[sl_order_id].price = new_sl_price
            log.debug(
                "SIM SL UPDATE: orderId=%d new_price=%.2f",
                sl_order_id, new_sl_price,
            )
