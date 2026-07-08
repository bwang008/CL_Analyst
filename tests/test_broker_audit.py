"""Broker-truth naked-leg cross-check (pure `analyze`, no broker I/O).

Ticket unprotected-leg-verification_07082026_0315 (detection half). The audit
matches a resting STOP to a position on the EXACT contract (symbol + expiry).
"""
from src.live_execution.broker_audit import analyze


def _pos(symbol, expiry, position):
    return {"symbol": symbol, "expiry": expiry, "position": position}


def _order(symbol, expiry, order_id, order_type, status):
    return {"symbol": symbol, "expiry": expiry, "order_id": order_id,
            "order_type": order_type, "status": status}


def test_protected_position_no_finding():
    positions = [_pos("NG", "20260729", 1)]
    orders = [_order("NG", "20260729", 35, "STP", "PreSubmitted")]
    findings, checked = analyze(positions, orders)
    assert findings == []
    assert checked == [(positions[0], [35])]


def test_naked_position_flagged():
    positions = [_pos("MES", "20260918", 1)]
    orders = [_order("MES", "20260918", 30, "LMT", "Submitted")]  # only a TP, no stop
    findings, _ = analyze(positions, orders)
    assert len(findings) == 1
    assert findings[0]["symbol"] == "MES"
    assert "NAKED" in findings[0]["detail"]


def test_stop_on_wrong_contract_does_not_protect():
    # A stop left on the OLD full-size contract must NOT count for the micro.
    positions = [_pos("ES", "20260918", 1)]
    orders = [_order("ES", "20251219", 24, "STP", "PreSubmitted")]  # different expiry
    findings, _ = analyze(positions, orders)
    assert len(findings) == 1


def test_cancelled_stop_does_not_protect():
    positions = [_pos("MGC", "20260827", 1)]
    orders = [_order("MGC", "20260827", 36, "STP", "Cancelled")]  # not resting
    findings, _ = analyze(positions, orders)
    assert len(findings) == 1


def test_flat_position_ignored():
    positions = [_pos("CL", "20260820", 0)]
    orders = []
    findings, checked = analyze(positions, orders)
    assert findings == []
    assert checked == []


def test_stp_lmt_counts_as_protection():
    positions = [_pos("NG", "20260729", -1)]
    orders = [_order("NG", "20260729", 41, "STP LMT", "Submitted")]
    findings, _ = analyze(positions, orders)
    assert findings == []


def test_mixed_fleet_only_naked_flagged():
    positions = [
        _pos("NG", "20260729", 1),
        _pos("MES", "20260918", 1),
        _pos("MGC", "20260827", 1),
    ]
    orders = [
        _order("NG", "20260729", 35, "STP", "PreSubmitted"),
        _order("MES", "20260918", 30, "LMT", "Submitted"),   # MES has only a TP → naked
        _order("MGC", "20260827", 36, "STP", "PreSubmitted"),
    ]
    findings, checked = analyze(positions, orders)
    assert len(checked) == 3
    assert [f["symbol"] for f in findings] == ["MES"]
