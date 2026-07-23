"""
TDD-TESTER AUTHORIZATION
Target Implementation Files:
    src/live_execution/ibkr_client.py
    src/live_execution/live_trader.py
    src/live_execution/adapters/simulated_execution.py
    src/live_execution/broker_audit.py
Target Class/Function:
    IBKRConnectionManager.place_child_orders  (native OCA group stamping)
    LiveTrader.__init__                       (TIERED multi-lot startup gate)
    SimulatedExecution.place_child_orders + _RestingOrder.oca_group (sim parity)
    broker_audit.analyze + broker_audit.format_checked_line (read-only report)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: oco-leg-race-audit_07212026_1935 -- Stage 1 ONLY.
Stages 2/3/4 (reversal branch, sign check, time-barrier ordering) are OUT of
scope for this file and deliberately untested here.

Phase: RED -- against current source these fail on missing features:
  - ib_insync Order defaults ocaGroup='' / ocaType=0 and place_child_orders
    never sets them -> every OCA-stamping/format/uniqueness assert fails;
  - LiveTrader has no TIERED multi-lot startup gate -> pytest.raises gets
    no RuntimeError;
  - _RestingOrder has no oca_group field and the sim's mock child trades
    expose no order.ocaGroup/ocaType -> AttributeError;
  - broker_audit has no format_checked_line and analyze() drops oca keys ->
    ImportError / shape assert failure.
Intentionally GREEN today (permanent regression pins, must stay green):
  - Error-201 red sentinel (children never carry parentId);
  - byte-identical child-order behavior (LMT/STP types, outsideRth, GTC,
    triggerMethod=1, transmit, rung lots/prices, zero-lot rung skip);
  - TIERED 1-lot and SINGLE multi-lot configs construct successfully;
  - broker_audit.analyze backward compat when order dicts lack oca keys.

Blueprint pins covered (Stage 1, sections 2.1-2.2, 2.5-2.6, 2.8, 3.1, 5.1,
5.5c, 5.8):
  A. place_child_orders stamps ONE shared, per-call-unique, non-empty
     ocaGroup + ocaType=2 on the SL and every TP (single-TP and tiered-list
     variants); tag begins "OCA-", embeds localSymbol (or symbol) and the
     manager's client_id, and is NEVER derived from parent_order_id (the
     heal path calls with a hardcoded 0 -- live_trader.py:2590 -- so an
     id-derived tag would collide across every healed bracket fleet-wide);
     parentId stays unset (Error-201 sentinel, 4a50a4f constraint); a
     trailing-stop auxPrice mutation on the SAME SL Order object leaves its
     ocaGroup/ocaType untouched (a moving stop must not fall out of its
     pairing).
  B. LiveTrader startup gate: exit_mode TIERED (case-insensitive) with an
     effective _max_position_size > 1 (explicit max_position_size key OR
     tier lots) refuses to start with a RuntimeError naming "TIERED" and
     this ticket id; TIERED 1-lot and SINGLE multi-lot construct fine.
  C. Sim parity (observability only -- NO new matcher semantics): one call
     stamps one shared non-empty oca group tag on every _RestingOrder it
     creates (new optional field oca_group, default None) and the returned
     mock trades expose matching order.ocaGroup / order.ocaType == 2;
     consecutive calls get different tags.
  D. broker_audit read-only reporting: analyze() keeps working when order
     dicts lack oca keys (bare-id checked entries, existing tests stay
     green); an order dict carrying oca_group contributes an
     (order_id, oca_group) tuple instead; NEW pure helper
     format_checked_line(position, resting_stops) renders
     "id=<id> oca=<group>" / "id=<id> oca=no-oca" fragments.

All I/O is mocked: no gateway, no network, no real IB connection, no real
data files. IBKRConnectionManager is constructed under a patched IB class
and its .ib replaced with a MagicMock (mirrors tests/test_tick_order_pricing
_make_manager); LiveTrader full-init uses the mocked-DataManager +
Path.exists seam (mirrors tests/test_symbol_data_paths.py); the simulated
adapter and broker_audit.analyze are pure in-memory objects.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Importing ib_insync is fine (plain Order dataclasses); CONNECTING is not.
from ib_insync import LimitOrder, StopOrder

from src.live_execution.adapters.simulated_execution import (
    SimulatedExecution,
    _OrderType,
    _RestingOrder,
)
from src.live_execution.broker_audit import analyze
from src.live_execution.ibkr_client import IBKRConnectionManager
from src.live_execution.live_trader import LiveTrader

TICKET_ID = "oco-leg-race-audit_07212026_1935"


# ===========================================================================
# Helpers -- IBKRConnectionManager without a connection
# (mirrors tests/test_tick_order_pricing.py::_make_manager)
# ===========================================================================

def _make_manager(client_id: int = 99) -> IBKRConnectionManager:
    """Manager whose IB object is a MagicMock; placeOrder echoes the order
    back as SimpleNamespace(order=order) so assertions run on the REAL
    ib_insync Order objects place_child_orders builds. ensure_connected is
    replaced with a no-op -- no connection is ever attempted."""
    with patch("src.live_execution.ibkr_client.IB"):
        mgr = IBKRConnectionManager(
            host="127.0.0.1", port=4002, client_id=client_id
        )
    mgr.ib = MagicMock()
    mgr.ib.placeOrder.side_effect = (
        lambda contract, order: SimpleNamespace(order=order)
    )
    mgr.ensure_connected = lambda: None  # no-op: connecting is forbidden
    return mgr


def _contract() -> SimpleNamespace:
    return SimpleNamespace(symbol="CL", localSymbol="CLQ6")


def _place_single(mgr, *, parent_order_id=0, quantity=1,
                  tp=66.00, sl=63.50):
    return mgr.place_child_orders(
        contract=_contract(),
        parent_order_id=parent_order_id,
        action="SELL",
        quantity=quantity,
        tp_price=tp,
        sl_price=sl,
    )


def _place_tiered(mgr, tiers, *, parent_order_id=0, quantity=1, sl=63.50):
    return mgr.place_child_orders(
        contract=_contract(),
        parent_order_id=parent_order_id,
        action="SELL",
        quantity=quantity,
        tp_price=tiers,
        sl_price=sl,
    )


def _tags(trades) -> set:
    return {t.order.ocaGroup for t in trades}


def _one_tag(trades) -> str:
    """All children of ONE placement must share ONE non-empty str tag."""
    tags = _tags(trades)
    assert len(tags) == 1, f"children carry mixed ocaGroup values: {tags!r}"
    (tag,) = tags
    assert isinstance(tag, str), f"ocaGroup must be str, got {type(tag)}"
    assert tag != "", "ocaGroup must be non-empty on every child order"
    return tag


# ===========================================================================
# A1 -- shared non-empty ocaGroup + ocaType=2 on every child, both variants
# ===========================================================================

class TestOcaGroupStamping:
    def test_single_tp_variant_children_share_nonempty_group(self):
        """Scalar tp_price: [TP, SL] both carry the SAME non-empty ocaGroup
        and ocaType == 2 (blueprint 2.2: proportionate-reduce WITH block)."""
        trades = _place_single(_make_manager())
        assert len(trades) == 2
        _one_tag(trades)
        for t in trades:
            assert t.order.ocaType == 2, (
                f"ocaType must be 2 on every child, got {t.order.ocaType!r}"
            )

    def test_tiered_single_rung_variant_children_share_nonempty_group(self):
        """The LIVE shape today: 1-lot TIERED configs produce a one-element
        [(lots, price)] list -> [TP, SL] share one non-empty group."""
        trades = _place_tiered(_make_manager(), [(1, 66.00)])
        assert len(trades) == 2
        _one_tag(trades)
        for t in trades:
            assert t.order.ocaType == 2

    def test_tiered_multi_rung_variant_all_children_one_group(self):
        """Multi-rung list: every TP rung AND the SL share the ONE group."""
        trades = _place_tiered(
            _make_manager(), [(1, 66.00), (2, 67.50)], quantity=3
        )
        assert len(trades) == 3
        _one_tag(trades)
        for t in trades:
            assert t.order.ocaType == 2


# ===========================================================================
# A2 -- tag format: "OCA-" prefix, localSymbol (or symbol), client_id
# ===========================================================================

class TestOcaGroupFormat:
    def test_group_prefix_symbol_and_client_id(self):
        """Tag begins with 'OCA-', embeds the contract's localSymbol (or
        symbol -- 'CL' is a substring of 'CLQ6' so the assert admits both)
        and the manager's client_id."""
        mgr = _make_manager(client_id=4711)
        tag = _one_tag(_place_single(mgr))
        assert tag.startswith("OCA-"), f"tag must start with 'OCA-': {tag!r}"
        assert ("CLQ6" in tag) or ("CL" in tag), (
            f"tag must embed localSymbol/symbol: {tag!r}"
        )
        assert "4711" in tag, f"tag must embed the client_id: {tag!r}"

    def test_group_embeds_each_managers_own_client_id(self):
        """Two managers with different client ids (their plain Order objects
        have colliding default orderIds -- IBKR ids are per-clientId
        sequences) must each stamp their OWN client id, and the tags must
        differ."""
        tag_a = _one_tag(_place_single(_make_manager(client_id=626)))
        tag_b = _one_tag(_place_single(_make_manager(client_id=1010)))
        assert "626" in tag_a, f"manager A tag must embed 626: {tag_a!r}"
        assert "1010" in tag_b, f"manager B tag must embed 1010: {tag_b!r}"
        assert tag_a != tag_b


# ===========================================================================
# A2 (uniqueness) -- per-call unique; NEVER derived from parent_order_id
# ===========================================================================

class TestOcaGroupUniqueness:
    def test_two_heal_replacements_with_parent_zero_get_different_groups(self):
        """THE landmine (blueprint 2.1): _verify_and_heal_protective_legs
        calls place_child_orders(..., parent_order_id=0, ...) with a literal
        hardcoded 0 on EVERY heal re-placement (live_trader.py:2590). Two
        consecutive calls with the SAME parent_order_id=0 MUST produce
        DIFFERENT ocaGroup values, else a fill on one healed bracket could
        cancel an UNRELATED instrument's SL."""
        mgr = _make_manager()
        tag_first = _one_tag(_place_single(mgr, parent_order_id=0))
        tag_second = _one_tag(_place_single(mgr, parent_order_id=0))
        assert tag_first != tag_second, (
            "two heal re-placements (parent_order_id=0 twice) produced the "
            f"IDENTICAL ocaGroup {tag_first!r} -- the tag must carry a "
            "per-call unique component, not a parent_order_id derivation"
        )

    def test_group_not_derived_from_parent_order_id(self):
        """Per-call uniqueness regardless of parent_order_id: tags from
        pid=0, pid=0 again, and pid=12345 are pairwise distinct (no equality
        in either direction; the derivation cannot collide on the shared 0).
        parent_order_id may be LOGGED next to the tag but never participates
        in its derivation."""
        mgr = _make_manager()
        tag_zero_a = _one_tag(_place_single(mgr, parent_order_id=0))
        tag_zero_b = _one_tag(_place_single(mgr, parent_order_id=0))
        tag_nonzero = _one_tag(_place_single(mgr, parent_order_id=12345))
        assert len({tag_zero_a, tag_zero_b, tag_nonzero}) == 3, (
            "ocaGroup values must be unique PER CALL: "
            f"{tag_zero_a!r}, {tag_zero_b!r}, {tag_nonzero!r}"
        )


# ===========================================================================
# A3 -- RED SENTINEL (Error-201 guard): parentId stays unset FOREVER
# ===========================================================================

class TestError201RedSentinel:
    """Permanent pin (GREEN today, must stay green forever): commit 4a50a4f
    removed parentId because children placed milliseconds after a fast entry
    fill referenced a parent already purged from IBKR's working-order table
    -> Error 201 -> rejected children -> naked position. An ocaGroup is a
    free-standing sibling tag; NO field may reference the terminal parent."""

    def test_no_child_carries_parent_id_single_variant(self):
        trades = _place_single(_make_manager(), parent_order_id=777)
        for t in trades:
            assert not getattr(t.order, "parentId", 0), (
                f"child order carries parentId={t.order.parentId!r} -- "
                "Error-201 regression (4a50a4f constraint violated)"
            )

    def test_no_child_carries_parent_id_tiered_variant(self):
        trades = _place_tiered(
            _make_manager(), [(1, 66.00), (2, 67.50)],
            parent_order_id=777, quantity=3,
        )
        for t in trades:
            assert not getattr(t.order, "parentId", 0), (
                f"child order carries parentId={t.order.parentId!r} -- "
                "Error-201 regression (4a50a4f constraint violated)"
            )


# ===========================================================================
# A4 -- existing child-order behavior byte-identical (GREEN today, stays)
# ===========================================================================

class TestChildOrderContractPreserved:
    def test_single_tp_types_prices_and_flags(self):
        """TP is a LimitOrder, SL is a StopOrder (SL LAST in the returned
        list); outsideRth True, tif GTC, transmit True on both; SL keeps
        triggerMethod == 1 (overnight-stop rationale, ibkr_client:1484-1489);
        quantities and prices pass through unchanged."""
        trades = _place_single(
            _make_manager(), quantity=2, tp=66.00, sl=63.50
        )
        assert len(trades) == 2
        tp_order = trades[0].order
        sl_order = trades[-1].order

        assert isinstance(tp_order, LimitOrder)
        assert tp_order.orderType == "LMT"
        assert tp_order.lmtPrice == 66.00
        assert tp_order.totalQuantity == 2
        assert tp_order.action == "SELL"

        assert isinstance(sl_order, StopOrder)
        assert sl_order.orderType == "STP"
        assert sl_order.auxPrice == 63.50
        assert sl_order.totalQuantity == 2
        assert sl_order.action == "SELL"
        assert sl_order.triggerMethod == 1

        for o in (tp_order, sl_order):
            assert o.outsideRth is True
            assert o.tif == "GTC"
            assert o.transmit is True

    def test_tiered_rung_lots_prices_and_zero_lot_skip(self):
        """Tiered rung lots/prices pass through as given; a zero-lot rung is
        skipped (current behavior, preserved byte-identical); the SL rests
        at the FULL quantity and is the LAST returned trade."""
        trades = _place_tiered(
            _make_manager(),
            [(1, 66.00), (0, 68.00), (2, 67.50)],
            quantity=3, sl=63.50,
        )
        # 2 TP rungs (zero-lot rung skipped) + 1 SL
        assert len(trades) == 3

        tp_orders = [t.order for t in trades[:-1]]
        sl_order = trades[-1].order

        assert [(o.totalQuantity, o.lmtPrice) for o in tp_orders] == [
            (1, 66.00), (2, 67.50),
        ]
        for o in tp_orders:
            assert isinstance(o, LimitOrder)
            assert o.orderType == "LMT"
            assert o.outsideRth is True
            assert o.tif == "GTC"
            assert o.transmit is True

        assert isinstance(sl_order, StopOrder)
        assert sl_order.orderType == "STP"
        assert sl_order.totalQuantity == 3
        assert sl_order.auxPrice == 63.50
        assert sl_order.triggerMethod == 1
        assert sl_order.outsideRth is True
        assert sl_order.tif == "GTC"
        assert sl_order.transmit is True


# ===========================================================================
# A5 -- trailing-modify OCA-membership pin
# ===========================================================================

class TestTrailingModifyOcaMembership:
    def test_aux_price_mutation_preserves_group_fields(self):
        """_check_trailing_stop mutates auxPrice on the SAME Order object and
        re-transmits it via modify_order. After the mutation the object's
        ocaGroup/ocaType must be UNCHANGED (and still valid) -- a moving
        stop must never fall out of its OCA pairing."""
        trades = _place_single(_make_manager())
        sl_order = trades[-1].order
        assert isinstance(sl_order, StopOrder)

        tag_before = sl_order.ocaGroup
        type_before = sl_order.ocaType
        assert isinstance(tag_before, str) and tag_before != "", (
            "SL must carry a non-empty ocaGroup before the trailing modify"
        )
        assert type_before == 2

        sl_order.auxPrice = 64.10  # the trailing ratchet mutation

        assert sl_order.ocaGroup == tag_before, (
            "trailing auxPrice mutation must not change ocaGroup"
        )
        assert sl_order.ocaType == type_before == 2, (
            "trailing auxPrice mutation must not change ocaType"
        )


# ===========================================================================
# B -- LiveTrader startup gate: TIERED + multi-lot refuses to start
# (full-init seam mirrors tests/test_symbol_data_paths.py /
#  tests/test_tick_order_pricing.py::_build_full_trader; precedent = the
#  former multi-rung trailing_ladder guard, removed when live ladder
#  support shipped — ticket live-trailing-ladder-phase3_07232026_0035)
# ===========================================================================

class DummyStrategy:
    """Minimal strategy stub mirroring tests/test_instrument_context.py."""

    def __init__(self, feature_names=None, config=None):
        self.feature_names = (
            feature_names if feature_names is not None else ["MACD"]
        )
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config if config is not None else {}


def _gate_cfg(**extra) -> dict:
    cfg = {
        "nickname": "CL_oca_gate",
        "execution_symbol": "CL",
        "bar_size": "1h",
    }
    cfg.update(extra)
    return cfg


def _build_full_trader(cfg: dict, tmp_path) -> LiveTrader:
    """Full LiveTrader init with mocked DataManager + existence checks
    (the tests/test_symbol_data_paths.py seam); simulated-side MagicMock
    clients, dry_run=True, tmp_path db."""
    with patch("src.live_execution.live_trader.DataManager"), patch(
        "pathlib.Path.exists", return_value=True
    ):
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(config=cfg),
            db_path=str(tmp_path / "telemetry.db"),
            dry_run=True,
        )
    return trader


class TestTieredMultiLotStartupGate:
    def test_tiered_with_explicit_multilot_key_refuses_to_start(self, tmp_path):
        """exit_mode TIERED + max_position_size 2 -> RuntimeError at
        construction whose message names 'TIERED' and this ticket id
        (blueprint 2.5: converts a silent future arming of D3 into a loud
        refusal until Stage 3 lands)."""
        cfg = _gate_cfg(exit_mode="TIERED", max_position_size=2)
        with pytest.raises(RuntimeError) as excinfo:
            _build_full_trader(cfg, tmp_path)
        message = str(excinfo.value)
        assert "TIERED" in message
        assert TICKET_ID in message

    def test_tiered_multilot_via_tier_lots_refuses_to_start(self, tmp_path):
        """No explicit max_position_size key: the effective
        _max_position_size (live_trader.py:421-435) comes from the tier
        lots -> a 2-lot tier must trip the same gate."""
        cfg = _gate_cfg(
            exit_mode="TIERED",
            long={"tiers": [{"min_prob": 0.5, "lots": 2}]},
        )
        with pytest.raises(RuntimeError) as excinfo:
            _build_full_trader(cfg, tmp_path)
        message = str(excinfo.value)
        assert "TIERED" in message
        assert TICKET_ID in message

    def test_tiered_gate_is_case_insensitive(self, tmp_path):
        """exit_mode 'tiered' (lowercase) + multi-lot must ALSO refuse."""
        cfg = _gate_cfg(exit_mode="tiered", max_position_size=3)
        with pytest.raises(RuntimeError) as excinfo:
            _build_full_trader(cfg, tmp_path)
        message = str(excinfo.value)
        assert "tiered" in message.lower()
        assert TICKET_ID in message

    def test_tiered_single_lot_constructs(self, tmp_path):
        """The entire live fleet today: TIERED at 1 lot must construct
        (gate blocks nothing currently deployed)."""
        cfg = _gate_cfg(
            exit_mode="TIERED",
            max_position_size=1,
            long={"tiers": [{"min_prob": 0.5, "lots": 1}]},
        )
        trader = _build_full_trader(cfg, tmp_path)
        assert trader._max_position_size == 1

    def test_single_exit_mode_multilot_constructs(self, tmp_path):
        """The gate is TIERED-specific: SINGLE + multi-lot constructs."""
        cfg = _gate_cfg(exit_mode="SINGLE", max_position_size=2)
        trader = _build_full_trader(cfg, tmp_path)
        assert trader._max_position_size == 2


# ===========================================================================
# C -- simulated adapter parity (observability ONLY -- no matcher semantics)
# ===========================================================================

class TestSimulatedAdapterOcaParity:
    def test_resting_order_oca_group_field_defaults_none(self):
        """New OPTIONAL field oca_group on _RestingOrder, default None --
        existing construction sites need no change."""
        rec = _RestingOrder(
            order_id=1,
            parent_order_id=0,
            symbol="CL",
            action="SELL",
            quantity=1,
            price=100.0,
            order_type=_OrderType.LIMIT,
            position_side=1,
        )
        assert rec.oca_group is None

    def test_single_call_stamps_one_group_on_records_and_trades(self):
        """ONE call: every _RestingOrder it creates shares ONE non-empty
        oca group tag, and the returned mock trade namespaces expose a
        matching order.ocaGroup and order.ocaType == 2."""
        sim = SimulatedExecution()
        trades = sim.place_child_orders(
            symbol="CL",
            parent_order_id=1,
            action="SELL",
            quantity=1,
            tp_price=102.0,
            sl_price=98.0,
        )
        assert len(trades) == 2

        record_groups = {
            r.oca_group for r in sim._resting_orders.values()
        }
        assert len(record_groups) == 1, (
            f"resting records carry mixed oca groups: {record_groups!r}"
        )
        (group,) = record_groups
        assert isinstance(group, str) and group != ""

        for t in trades:
            assert t.order.ocaGroup == group
            assert t.order.ocaType == 2

    def test_tiered_call_stamps_one_group_on_all_children(self):
        """Tiered variant: all TP rung records + the SL record share the
        one group of that call."""
        sim = SimulatedExecution()
        trades = sim.place_child_orders(
            symbol="CL",
            parent_order_id=1,
            action="SELL",
            quantity=3,
            tp_price=[(1, 102.0), (2, 103.0)],
            sl_price=98.0,
        )
        assert len(trades) == 3
        record_groups = {
            r.oca_group for r in sim._resting_orders.values()
        }
        assert len(record_groups) == 1
        (group,) = record_groups
        assert isinstance(group, str) and group != ""
        assert {t.order.ocaGroup for t in trades} == {group}
        assert {t.order.ocaType for t in trades} == {2}

    def test_two_calls_produce_different_group_tags(self):
        """Consecutive calls = different brackets = different tags."""
        sim = SimulatedExecution()
        first = sim.place_child_orders(
            symbol="CL", parent_order_id=1, action="SELL",
            quantity=1, tp_price=102.0, sl_price=98.0,
        )
        second = sim.place_child_orders(
            symbol="CL", parent_order_id=2, action="SELL",
            quantity=1, tp_price=104.0, sl_price=99.0,
        )
        tag_first = {t.order.ocaGroup for t in first}
        tag_second = {t.order.ocaGroup for t in second}
        assert len(tag_first) == 1 and len(tag_second) == 1
        assert tag_first != tag_second, (
            "two consecutive sim placements must carry DIFFERENT group tags"
        )


# ===========================================================================
# D -- broker_audit read-only OCA reporting (pure functions, no I/O)
# ===========================================================================

class TestBrokerAuditOcaReporting:
    """CONTRACT for the Coder (blueprint 2.8 -- read-only reporting only):

    1. analyze(positions, orders): order dicts gain OPTIONAL
       ``oca_group`` / ``oca_type`` keys (main() sources them from
       t.order.ocaGroup / t.order.ocaType).
       - BACKWARD COMPAT: an order dict WITHOUT an ``oca_group`` key
         contributes a bare ``order_id`` to the checked resting-stop list,
         exactly as today -- tests/test_broker_audit.py stays green
         UNCHANGED.
       - An order dict WITH an ``oca_group`` key contributes the tuple
         ``(order_id, oca_group)`` instead, making the group available to
         reporting.
    2. NEW pure helper ``format_checked_line(position, resting_stops)``
       -> str: renders the BROKER_OK report fragment for one checked
       position. It MUST contain the position's symbol and expiry, and per
       resting-stop entry the fragment ``id=<id> oca=<group>`` when a
       non-empty group is known, or exactly ``id=<id> oca=no-oca``
       otherwise (bare-int entry OR falsy group). Pure function: no IB,
       no I/O, no printing."""

    @staticmethod
    def _pos(symbol, expiry, position):
        return {"symbol": symbol, "expiry": expiry, "position": position}

    @staticmethod
    def _order(symbol, expiry, order_id, order_type, status, **oca):
        d = {"symbol": symbol, "expiry": expiry, "order_id": order_id,
             "order_type": order_type, "status": status}
        d.update(oca)
        return d

    def test_analyze_without_oca_keys_backward_compat(self):
        """GREEN pin: order dicts lacking oca keys keep today's exact
        checked shape (bare ids) and naked detection semantics."""
        positions = [self._pos("NG", "20260729", 1),
                     self._pos("MES", "20260918", 1)]
        orders = [
            self._order("NG", "20260729", 35, "STP", "PreSubmitted"),
            self._order("MES", "20260918", 30, "LMT", "Submitted"),  # TP only
        ]
        findings, checked = analyze(positions, orders)
        assert checked == [
            (positions[0], [35]),
            (positions[1], []),
        ]
        assert [f["symbol"] for f in findings] == ["MES"]

    def test_analyze_exposes_oca_group_when_key_present(self):
        """An order dict carrying oca_group/oca_type contributes an
        (order_id, oca_group) tuple to the checked resting-stop list, and
        protection semantics are unchanged (still protected, no finding)."""
        positions = [self._pos("CL", "20260820", 1)]
        orders = [
            self._order(
                "CL", "20260820", 41, "STP", "PreSubmitted",
                oca_group="OCA-CLQ6-1010-abc123", oca_type=2,
            ),
        ]
        findings, checked = analyze(positions, orders)
        assert findings == []
        assert len(checked) == 1
        pos, resting = checked[0]
        assert pos is positions[0]
        assert resting == [(41, "OCA-CLQ6-1010-abc123")], (
            "checked resting-stop entries must expose the oca_group when "
            f"the input order dict carries one; got {resting!r}"
        )

    def test_analyze_oca_keys_do_not_change_naked_detection(self):
        """GREEN-adjacent semantics pin: oca keys are reporting-only -- a
        naked position (TP-only book, even with an oca_group on the TP)
        is still flagged NAKED."""
        positions = [self._pos("MGC", "20260827", 1)]
        orders = [
            self._order(
                "MGC", "20260827", 30, "LMT", "Submitted",
                oca_group="OCA-MGCQ6-3001-deadbe", oca_type=2,
            ),
        ]
        findings, checked = analyze(positions, orders)
        assert len(findings) == 1
        assert "NAKED" in findings[0]["detail"]
        assert checked == [(positions[0], [])]

    def test_format_checked_line_renders_group_and_no_oca(self):
        """format_checked_line renders 'id=<id> oca=<group>' for a stop with
        a known non-empty group and exactly 'id=<id> oca=no-oca' for a stop
        without one (bare-int entry or falsy group)."""
        # Ghost import (TDD protocol): the Coder adds this pure helper to
        # src/live_execution/broker_audit.py.
        from src.live_execution.broker_audit import format_checked_line

        position = self._pos("CL", "20260820", 1)
        resting_stops = [
            (41, "OCA-CLQ6-1010-abc123"),  # group known
            (42, None),                     # tuple entry, no group
            43,                             # legacy bare-id entry
        ]
        line = format_checked_line(position, resting_stops)
        assert isinstance(line, str)
        assert "CL" in line
        assert "20260820" in line
        assert "id=41 oca=OCA-CLQ6-1010-abc123" in line
        assert "id=42 oca=no-oca" in line
        assert "id=43 oca=no-oca" in line

    def test_format_checked_line_empty_string_group_is_no_oca(self):
        """IB returns '' for an order with no group: falsy group renders as
        'no-oca', never 'oca=' with an empty value."""
        from src.live_execution.broker_audit import format_checked_line

        line = format_checked_line(
            self._pos("NG", "20260729", -1), [(35, "")]
        )
        assert "id=35 oca=no-oca" in line
        assert "oca= " not in line
