"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/interfaces/execution_interface.py
                            (ExecutionClient.modify_order becomes
                            @abstractmethod with SYNC-TRANSMIT failure
                            semantics per impact_review C-2),
                            src/live_execution/adapters/ibkr_execution.py
                            (NEW IBKRExecutionClient.modify_order: validate
                            event/raw_event/order/contract/order-id/connection
                            then ib.placeOrder(trade.contract, trade.order) —
                            sync, callback-safe, NO qualification calls),
                            src/live_execution/adapters/simulated_execution.py
                            (C-1: raise ValueError on the malformed-event
                            class; order-not-found stays a no-op but upgraded
                            to WARNING),
                            src/live_execution/live_trader.py
                            (_check_trailing_stop ~:1120-1161 — delete the
                            hasattr guard; TRANSMIT-THEN-COMMIT: transmit
                            inside a targeted try/except; on failure restore
                            raw_order.auxPrice, ERROR log WITHOUT the success
                            substring, commit NOTHING → natural retry next
                            bar; on success commit everything as today)
Target Class/Function: ExecutionClient.modify_order,
                       IBKRExecutionClient.modify_order,
                       SimulatedExecution.modify_order,
                       LiveTrader._check_trailing_stop
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: live-trailing-modify-order-dead_07042026_2012
Phase: RED — at HEAD 5db573a:
  - IBKRExecutionClient has NO modify_order → A1-A5 fail (AttributeError /
    DID NOT RAISE the required validation error).
  - ExecutionClient does NOT declare modify_order → I1 fails (a dummy
    subclass missing it instantiates fine today); I2 fails (AttributeError
    on interface/IBKR lookup).
  - live_trader.py:1125 hasattr-guards the call and COMMITS state without
    transmitting → C1/C3/C4 fail on the lie-surface assertions
    (raw_order.auxPrice never restored; with a method-less exec client the
    guard silently skips and commits everything).
  - SimulatedExecution.modify_order silently no-ops malformed events and
    logs order-not-found at DEBUG → the C-1 contract tests fail
    (DID NOT RAISE / no WARNING record).

Coverage (blueprint test list — A1-A5, I1-I2, C1-C5, C-3 pin, C-1 sim):
  - TestIBKRModifyOrderAdapter: A1 placeOrder called exactly once with
    (trade.contract, trade.order) and the Trade returned; A2 raises
    ValueError on event=None / missing raw_event / missing .order /
    missing .contract (parametrized, nothing transmitted); A3 raises on
    order-id mismatch; A4 raises when disconnected; A5 event-loop pin —
    NO qualifyContracts / reqContractDetails / reqHistoricalData /
    qualify_contract / get_front_month_contract during modify_order.
  - TestModifyOrderInterfaceContract: I1 abstractmethod enforcement (a
    subclass implementing every abstract EXCEPT modify_order raises
    TypeError at instantiation; a full subclass instantiates); I2
    inspect.signature parity (order_id, event=None) across interface /
    sim / IBKR.
  - TestTrailingStopTransmitCallSite (S6 __new__ seam per
    tests/test_tick_order_pricing.py): C1 guard removal — a method-less
    exec client surfaces as the loud failure path (nothing committed,
    ERROR logged) and a working client is called exactly once with
    (evt.order_id, evt) BEFORE any commit; C2 success commits all state
    exactly as today; C3 no-lie-on-failure (auxPrice restored,
    _trailing_activated False, _tracked_sl_price unchanged, no
    update_position_sl, no TRAILING_ACTIVATED snapshot, ERROR log without
    the success substring); C4 retry-on-next-bar (fail bar N, succeed
    bar N+1 — trigger not latched); C5 sim-adapter integration (real
    SimulatedExecution resting SL price mutated).
  - TestModifiedSLOrderLogPin (C-3, complements the Strict-Lock
    tests/test_trailing_stop_log_format.py): the exact substring
    "modified SL order" appears EXACTLY ONCE on success and ZERO times
    on failure.
  - TestSimulatedModifyOrderContract (C-1): malformed event raises
    ValueError with the resting order untouched; unknown order-id is a
    WARNING-level no-op; well-formed modify updates the resting price
    (str order-id coercion pinned).

Out of scope (blueprint scope guards): trailing binding/scheduling
(5m-harness ticket), escalation-on-repeated-failure (spun-off ticket),
T4-T8, the --disable-trailing parity path (sits above the changed region).

All I/O boundaries are mocked (patched IBKRConnectionManager, MagicMock
telemetry/exec clients); no IBKR gateway, no real data files.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.live_execution.adapters.ibkr_execution import IBKRExecutionClient
from src.live_execution.adapters.simulated_execution import SimulatedExecution
from src.live_execution.interfaces.execution_interface import (
    ExecutionClient,
    StandardExecutionEvent,
)
from src.live_execution.live_trader import LiveTrader

# ---------------------------------------------------------------------------
# Shared constants — the canonical trailing scenario (long CL)
# ---------------------------------------------------------------------------

_ORIG_SL = 68.50   # the SL price resting at the broker before trailing
_NEW_SL = 70.50    # entry 70.00 + 0.5 (offset) * 1.0 (ATR), on the 0.01 grid
_SUCCESS_SUBSTRING = "modified SL order"   # C-3 Strict-Lock pinned substring


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _std_event(order_id="888", raw_event="__default__", aux_price=_ORIG_SL):
    """StandardExecutionEvent whose raw_event mimics an ib_insync Trade."""
    if raw_event == "__default__":
        raw_event = SimpleNamespace(
            order=SimpleNamespace(orderId=int(order_id), auxPrice=aux_price),
            contract=SimpleNamespace(symbol="CL"),
        )
    return StandardExecutionEvent(
        order_id=str(order_id),
        symbol="CL",
        status="Submitted",
        filled_qty=0,
        remaining_qty=1,
        avg_price=0.0,
        raw_event=raw_event,
    )


def _sim_event(order_id, aux_price):
    """Well-formed event for SimulatedExecution.modify_order."""
    return StandardExecutionEvent(
        order_id=str(order_id),
        symbol="CL",
        status="Submitted",
        filled_qty=0,
        remaining_qty=1,
        avg_price=0.0,
        raw_event=SimpleNamespace(
            order=SimpleNamespace(orderId=order_id, auxPrice=aux_price),
        ),
    )


# ---------------------------------------------------------------------------
# A-series helpers — IBKRExecutionClient with a fully mocked manager
# (pattern: tests/test_ibkr_adapters.py::_make_client_and_manager)
# ---------------------------------------------------------------------------

def _make_ibkr_client(connected: bool = True):
    with patch(
        "src.live_execution.adapters.ibkr_execution.IBKRConnectionManager"
    ) as MockMgr:
        mock_mgr = MockMgr.return_value
        mock_mgr.ib = MagicMock()
        mock_mgr.ib.isConnected.return_value = connected
        client = IBKRExecutionClient()
    return client, mock_mgr


# ===========================================================================
# TestIBKRModifyOrderAdapter — A1-A5 (audit 5.4)
# ===========================================================================

class TestIBKRModifyOrderAdapter:
    def test_a1_places_same_contract_and_order_exactly_once(self):
        """A1: modify_order = re-placeOrder of the SAME Trade.contract and
        Trade.order (the ib_insync modify flow), exactly once, returning
        placeOrder's Trade."""
        client, mgr = _make_ibkr_client()
        modified_trade = MagicMock(name="modified_trade")
        mgr.ib.placeOrder.return_value = modified_trade
        event = _std_event(order_id="888")

        result = client.modify_order("888", event)

        mgr.ib.placeOrder.assert_called_once_with(
            event.raw_event.contract, event.raw_event.order
        )
        assert result is modified_trade

    @pytest.mark.parametrize(
        "label,event_factory",
        [
            ("event_none", lambda: None),
            ("raw_event_none", lambda: _std_event(raw_event=None)),
            (
                "raw_event_lacks_order_and_contract",
                lambda: _std_event(raw_event=SimpleNamespace()),
            ),
            (
                "order_none",
                lambda: _std_event(
                    raw_event=SimpleNamespace(
                        order=None, contract=SimpleNamespace(symbol="CL")
                    )
                ),
            ),
            (
                "contract_none",
                lambda: _std_event(
                    raw_event=SimpleNamespace(
                        order=SimpleNamespace(orderId=888, auxPrice=_NEW_SL),
                        contract=None,
                    )
                ),
            ),
            (
                "contract_attr_missing",
                lambda: _std_event(
                    raw_event=SimpleNamespace(
                        order=SimpleNamespace(orderId=888, auxPrice=_NEW_SL),
                    )
                ),
            ),
        ],
    )
    def test_a2_malformed_event_raises_nothing_transmitted(
        self, label, event_factory
    ):
        """A2: no silent no-op — every malformed-event shape raises
        ValueError and transmits NOTHING."""
        client, mgr = _make_ibkr_client()
        with pytest.raises(ValueError):
            client.modify_order("888", event_factory())
        mgr.ib.placeOrder.assert_not_called()

    def test_a3_order_id_mismatch_raises_nothing_transmitted(self):
        """A3: order_id argument must match raw_event.order.orderId."""
        client, mgr = _make_ibkr_client()
        event = _std_event(order_id="999")  # raw order carries orderId=999
        with pytest.raises(ValueError):
            client.modify_order("888", event)
        mgr.ib.placeOrder.assert_not_called()

    def test_a4_disconnected_raises_nothing_transmitted(self):
        """A4: a disconnected session must raise, never quietly drop the
        modification."""
        client, mgr = _make_ibkr_client(connected=False)
        event = _std_event(order_id="888")
        with pytest.raises((ConnectionError, RuntimeError)):
            client.modify_order("888", event)
        mgr.ib.placeOrder.assert_not_called()

    def test_a5_no_qualification_family_calls_event_loop_pin(self):
        """A5: modify_order fires inside bar-update callbacks — it must
        NEVER touch the run_until_complete family (qualifyContracts /
        reqContractDetails / reqHistoricalData) nor re-resolve contracts."""
        client, mgr = _make_ibkr_client()
        event = _std_event(order_id="888")

        client.modify_order("888", event)

        mgr.ib.placeOrder.assert_called_once()
        mgr.ib.qualifyContracts.assert_not_called()
        mgr.ib.qualifyContractsAsync.assert_not_called()
        mgr.ib.reqContractDetails.assert_not_called()
        mgr.ib.reqHistoricalData.assert_not_called()
        mgr.qualify_contract.assert_not_called()
        mgr.get_front_month_contract.assert_not_called()


# ===========================================================================
# TestModifyOrderInterfaceContract — I1-I2
# ===========================================================================

def _partial_execution_client(exclude=("modify_order",)):
    """Subclass ExecutionClient implementing every CURRENT abstract method
    except the excluded ones (dynamic — survives future interface growth)."""
    abstracts = getattr(ExecutionClient, "__abstractmethods__", frozenset())
    namespace = {
        name: (lambda self, *args, **kwargs: None)
        for name in abstracts
        if name not in exclude
    }
    return type("PartialExecutionClient", (ExecutionClient,), namespace)


class TestModifyOrderInterfaceContract:
    def test_i1_modify_order_is_declared_abstract(self):
        """I1 pin: modify_order is part of the ABC contract — the absent
        declaration IS the root cause of this ticket."""
        abstracts = getattr(ExecutionClient, "__abstractmethods__", frozenset())
        assert "modify_order" in abstracts, (
            "ExecutionClient must declare modify_order as @abstractmethod "
            "(no-silent-defaults: a future adapter missing it must fail at "
            "instantiation, not at trailing-trigger time)"
        )

    def test_i1_subclass_without_modify_order_cannot_instantiate(self):
        """I1: implementing everything EXCEPT modify_order → TypeError at
        construction (the loudest possible failure point)."""
        Partial = _partial_execution_client(exclude=("modify_order",))
        with pytest.raises(TypeError, match="modify_order"):
            Partial()

    def test_i1_full_subclass_instantiates(self):
        """I1 guard: implementing ALL abstracts (modify_order included)
        must still instantiate — no over-abstraction."""
        Full = _partial_execution_client(exclude=())
        Full()  # must not raise

    def test_i2_signature_parity_interface_sim_ibkr(self):
        """I2: (self, order_id, event=None) on the interface and BOTH
        adapters — signature drift is how this bug hid for 3 weeks."""
        signatures = {
            "interface": inspect.signature(ExecutionClient.modify_order),
            "simulated": inspect.signature(SimulatedExecution.modify_order),
            "ibkr": inspect.signature(IBKRExecutionClient.modify_order),
        }
        for impl, sig in signatures.items():
            params = list(sig.parameters.values())
            names = [p.name for p in params]
            assert names[:3] == ["self", "order_id", "event"], (
                f"{impl}.modify_order parameters are {names}, expected "
                f"['self', 'order_id', 'event', ...]"
            )
            assert params[2].default is None, (
                f"{impl}.modify_order 'event' parameter must default to None"
            )


# ===========================================================================
# LiveTrader S6 seam (mirrors tests/test_tick_order_pricing.py
# _make_trailing_trader — long CL, trigger satisfied on the seeded bar)
# ===========================================================================

def _make_trailing_trader(exec_client=None):
    """__new__ seam driving the REAL _check_trailing_stop to the transmit
    block. Long CL: entry 70.00, ATR 1.0, mult 1.0 → bar high 71.50
    triggers; offset 0.5 → new SL 70.50. Returns (trader, raw_order, evt)."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._trailing_activated = False
    trader._entry_price = 70.0
    trader._atr_at_entry = 1.0
    trader._trade_trailing_atr_mult = None
    trader._trailing_atr_mult = 1.0
    trader._trailing_sl_atr_offset_long = 0.5
    trader._trailing_sl_atr_offset_short = 0.5
    trader._position_side = 1
    trader._highest_high = 70.0
    trader._lowest_low = 70.0
    trader._position_bars_held = 3
    trader._active_trade_id = "trade_modify_test"
    trader._tracked_sl_price = _ORIG_SL
    trader.telemetry = MagicMock()
    trader._utc_iso_now = MagicMock(return_value="2026-07-04T20:12:00Z")
    trader.rolling_df_5m = pd.DataFrame(
        [{"Open": 71.0, "High": 71.5, "Low": 69.8, "Close": 71.2}]
    )
    trader._sl_order_id = 888
    raw_order = SimpleNamespace(orderId=888, auxPrice=_ORIG_SL)
    evt = _std_event(order_id="888", raw_event=SimpleNamespace(
        order=raw_order, contract=SimpleNamespace(symbol="CL"),
    ))
    trader._open_orders = {"888": evt}
    trader.exec_client = exec_client if exec_client is not None else MagicMock()
    return trader, raw_order, evt


def _trailing_activated_snapshots(telemetry):
    return [
        c for c in telemetry.log_decision_state.call_args_list
        if c.kwargs.get("event_type") == "TRAILING_ACTIVATED"
    ]


def _success_records(caplog):
    return [
        r for r in caplog.records if _SUCCESS_SUBSTRING in r.getMessage()
    ]


# ===========================================================================
# TestTrailingStopTransmitCallSite — C1-C5
# ===========================================================================

class TestTrailingStopTransmitCallSite:
    def test_c1_methodless_exec_client_fails_loud_commits_nothing(
        self, caplog
    ):
        """C1 (guard removal): an exec client WITHOUT modify_order must
        surface as the loud failure path — AttributeError at call time,
        caught, NOTHING committed, ERROR logged. Today the hasattr guard
        silently skips the transmit and commits the full lie surface."""
        trader, raw_order, evt = _make_trailing_trader(
            exec_client=SimpleNamespace()  # deliberately lacks modify_order
        )
        with caplog.at_level(logging.INFO):
            trader._check_trailing_stop()  # must not propagate

        assert raw_order.auxPrice == _ORIG_SL, (
            "cached Trade auxPrice must not stay poisoned when nothing "
            "was transmitted"
        )
        assert trader._trailing_activated is False
        assert trader._tracked_sl_price == _ORIG_SL
        trader.telemetry.update_position_sl.assert_not_called()
        assert _trailing_activated_snapshots(trader.telemetry) == []
        assert _success_records(caplog) == [], (
            "the success log must never fire without a broker transmit"
        )
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "a failed/impossible transmit must be logged at ERROR"
        )

    def test_c1_transmit_called_exactly_once_before_any_commit(self):
        """C1 (+ordering pin): modify_order called exactly once with
        (evt.order_id, evt), and at transmit time NOTHING is committed yet
        (transmit-then-commit)."""
        trader, raw_order, evt = _make_trailing_trader()
        state_at_transmit = {}

        def _spy(order_id, event):
            state_at_transmit["latched"] = trader._trailing_activated
            state_at_transmit["ledger_written"] = (
                trader.telemetry.update_position_sl.called
            )
            state_at_transmit["snapshots"] = len(
                _trailing_activated_snapshots(trader.telemetry)
            )

        trader.exec_client.modify_order.side_effect = _spy
        trader._check_trailing_stop()

        trader.exec_client.modify_order.assert_called_once_with(
            evt.order_id, evt
        )
        assert state_at_transmit["latched"] is False
        assert state_at_transmit["ledger_written"] is False
        assert state_at_transmit["snapshots"] == 0

    def test_c2_success_path_commits_all_state(self, caplog):
        """C2: a successful transmit commits everything exactly as today —
        latch, tracked price, mutated auxPrice, ledger SL update,
        TRAILING_ACTIVATED snapshot, success log."""
        trader, raw_order, evt = _make_trailing_trader()
        with caplog.at_level(logging.INFO):
            trader._check_trailing_stop()

        trader.exec_client.modify_order.assert_called_once_with(
            evt.order_id, evt
        )
        assert trader._trailing_activated is True
        assert trader._tracked_sl_price == _NEW_SL
        assert raw_order.auxPrice == _NEW_SL

        trader.telemetry.update_position_sl.assert_called_once()
        call = trader.telemetry.update_position_sl.call_args
        passed = list(call.args) + list(call.kwargs.values())
        assert "trade_modify_test" in passed
        assert call.kwargs.get("new_sl_price") == _NEW_SL
        assert str(call.kwargs.get("sl_order_id")) == "888"

        snaps = _trailing_activated_snapshots(trader.telemetry)
        assert len(snaps) == 1
        assert snaps[0].kwargs.get("sl_price") == _NEW_SL
        assert snaps[0].kwargs.get("trailing_activated") is True

        assert len(_success_records(caplog)) == 1

    def test_c3_transmit_failure_restores_aux_price_commits_nothing(
        self, caplog
    ):
        """C3 (no-lie-on-failure): modify_order raises → the FULL lie
        surface stays clean: auxPrice restored to the original, no latch,
        tracked price unchanged, no ledger write, no snapshot, ERROR log
        without the success substring."""
        trader, raw_order, evt = _make_trailing_trader()
        trader.exec_client.modify_order.side_effect = RuntimeError(
            "gateway down"
        )
        with caplog.at_level(logging.INFO):
            trader._check_trailing_stop()  # must not propagate

        trader.exec_client.modify_order.assert_called_once_with(
            evt.order_id, evt
        )
        assert raw_order.auxPrice == _ORIG_SL, (
            "raw_order.auxPrice must be restored to the pre-mutation value "
            "on transmit failure — otherwise the [PNL] line and recovery "
            "read a phantom SL forever"
        )
        assert trader._trailing_activated is False, (
            "the latch must not be set on failure — it kills the retry"
        )
        assert trader._tracked_sl_price == _ORIG_SL
        trader.telemetry.update_position_sl.assert_not_called()
        assert _trailing_activated_snapshots(trader.telemetry) == []
        assert _success_records(caplog) == []
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_c4_failed_transmit_retries_next_bar_and_succeeds(self, caplog):
        """C4: fail on bar N, succeed on bar N+1. Because nothing is
        committed on failure (trigger not latched, extremes persist), the
        next bar re-triggers and the second transmit commits."""
        trader, raw_order, evt = _make_trailing_trader()
        trader.exec_client.modify_order.side_effect = [
            RuntimeError("transient outage"),
            None,
        ]
        with caplog.at_level(logging.INFO):
            trader._check_trailing_stop()  # bar N — transmit fails

            # Interim lie-surface check: NOTHING committed after bar N
            assert trader._trailing_activated is False
            assert raw_order.auxPrice == _ORIG_SL
            assert trader._tracked_sl_price == _ORIG_SL
            trader.telemetry.update_position_sl.assert_not_called()

            trader._check_trailing_stop()  # bar N+1 — retry succeeds

        assert trader.exec_client.modify_order.call_count == 2, (
            "a failed transmit must NOT latch the trigger — the next bar "
            "must call modify_order again"
        )
        assert trader._trailing_activated is True
        assert raw_order.auxPrice == _NEW_SL
        assert trader._tracked_sl_price == _NEW_SL
        trader.telemetry.update_position_sl.assert_called_once()
        assert len(_success_records(caplog)) == 1

    def test_c5_sim_adapter_integration_resting_order_mutated(self):
        """C5: through the REAL SimulatedExecution, a trailing trigger
        updates the resting SL order's price (livetest behavior preserved
        end-to-end)."""
        sim = SimulatedExecution()
        sim._position = 1
        sim._position_side = 1
        children = sim.place_child_orders(
            symbol="CL",
            parent_order_id=500,
            action="SELL",
            quantity=1,
            tp_price=75.00,
            sl_price=_ORIG_SL,
        )
        sl_oid = children[-1].order.orderId
        sl_evt = next(
            e for e in sim.get_open_trades("CL")
            if str(e.order_id) == str(sl_oid)
        )

        trader, _, _ = _make_trailing_trader(exec_client=sim)
        trader._sl_order_id = sl_oid
        trader._open_orders = {str(sl_oid): sl_evt}

        trader._check_trailing_stop()

        assert sim._resting_orders[sl_oid].price == _NEW_SL
        assert trader._trailing_activated is True


# ===========================================================================
# TestModifiedSLOrderLogPin — C-3 (complements the Strict-Lock
# tests/test_trailing_stop_log_format.py — do NOT reword the success log)
# ===========================================================================

class TestModifiedSLOrderLogPin:
    def test_success_substring_exactly_once_on_success(self, caplog):
        """C-3(a)+(b): "modified SL order" appears EXACTLY ONCE, and only
        after a successful transmit."""
        trader, _, _ = _make_trailing_trader()
        with caplog.at_level(logging.INFO):
            trader._check_trailing_stop()
        assert len(_success_records(caplog)) == 1

    def test_success_substring_zero_times_on_failure(self, caplog):
        """C-3(c): the failure-path message must NOT contain the pinned
        success substring — logs may never claim a modification that was
        not transmitted."""
        trader, _, _ = _make_trailing_trader()
        trader.exec_client.modify_order.side_effect = RuntimeError(
            "gateway down"
        )
        with caplog.at_level(logging.INFO):
            trader._check_trailing_stop()
        assert len(_success_records(caplog)) == 0
        error_records = [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert error_records, "transmit failure must be logged at ERROR"
        assert all(
            _SUCCESS_SUBSTRING not in r.getMessage() for r in error_records
        )


# ===========================================================================
# TestSimulatedModifyOrderContract — C-1 (sim adapter failure semantics)
# ===========================================================================

def _sim_with_resting_sl():
    """Real sim engine holding one resting SL at _ORIG_SL. Returns
    (sim, sl_order_id)."""
    sim = SimulatedExecution()
    sim._position = 1
    sim._position_side = 1
    children = sim.place_child_orders(
        symbol="CL",
        parent_order_id=500,
        action="SELL",
        quantity=1,
        tp_price=75.00,
        sl_price=_ORIG_SL,
    )
    return sim, children[-1].order.orderId


class TestSimulatedModifyOrderContract:
    @pytest.mark.parametrize(
        "label,event_factory",
        [
            ("event_none", lambda oid: None),
            ("raw_event_none", lambda oid: _std_event(
                order_id=str(oid), raw_event=None)),
            ("raw_event_lacks_order", lambda oid: _std_event(
                order_id=str(oid), raw_event=SimpleNamespace())),
            ("order_none", lambda oid: _std_event(
                order_id=str(oid),
                raw_event=SimpleNamespace(order=None))),
            ("aux_price_attr_missing", lambda oid: _std_event(
                order_id=str(oid),
                raw_event=SimpleNamespace(
                    order=SimpleNamespace(orderId=oid)))),
            ("aux_price_none", lambda oid: _std_event(
                order_id=str(oid),
                raw_event=SimpleNamespace(
                    order=SimpleNamespace(orderId=oid, auxPrice=None)))),
        ],
    )
    def test_c1_malformed_event_raises_resting_order_untouched(
        self, label, event_factory
    ):
        """C-1: the sync-detectable malformed-event class must raise
        ValueError in the sim EXACTLY like the IBKR adapter — silent
        no-ops here are what masked this bug in every parity run."""
        sim, sl_oid = _sim_with_resting_sl()
        with pytest.raises(ValueError):
            sim.modify_order(sl_oid, event_factory(sl_oid))
        assert sim._resting_orders[sl_oid].price == _ORIG_SL

    def test_c1_unknown_order_id_warns_and_noops(self, caplog):
        """C-1: order-not-found stays a no-op (mirrors live venue-async
        rejection) but must be logged at WARNING, not DEBUG."""
        sim, sl_oid = _sim_with_resting_sl()
        unknown_oid = 424242
        assert unknown_oid not in sim._resting_orders
        event = _sim_event(unknown_oid, _NEW_SL)

        with caplog.at_level(logging.DEBUG):
            result = sim.modify_order(unknown_oid, event)  # must not raise

        assert result is None
        warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and str(unknown_oid) in r.getMessage()
        ]
        assert len(warnings) == 1, (
            "unknown order-id must produce exactly one WARNING record "
            f"naming the order id, got {len(warnings)}"
        )
        assert sim._resting_orders[sl_oid].price == _ORIG_SL

    def test_well_formed_modify_updates_resting_price(self):
        """Regression pin (livetest good path): a well-formed event moves
        the resting SL; string order-id coercion preserved."""
        sim, sl_oid = _sim_with_resting_sl()
        sim.modify_order(str(sl_oid), _sim_event(sl_oid, _NEW_SL))
        assert sim._resting_orders[sl_oid].price == _NEW_SL
