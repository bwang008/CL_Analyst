"""
Tests for account-wide margin lines in the 1-Hour Telegram heartbeat.

Target: LiveTrader._build_heartbeat_payload()
Ticket: heartbeat-margin-report_07062026_2348

The heartbeat already renders `Total Liq`. This feature adds three account-wide
margin lines immediately after it — Init Margin, Maint Margin, and Free Cushion
(Excess Liquidity) — all pulled from the same cached accountValues() feed
get_account_summary() already reads (no new network call, no thread risk).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock


def _full_acct_summary(**overrides):
    acct = {
        "account": "DU1899929",
        "net_liquidation": 1483258.15,
        "available_funds": 1466758.15,
        "init_margin_req": 18150.0,
        "maint_margin_req": 16500.0,
        "excess_liquidity": 1466758.15,
        "cl_position": 0,
        "cl_unrealized_pnl": 0.0,
        "cl_realized_pnl": 0.0,
        "cl_market_value": 0.0,
        "cl_avg_cost": 0.0,
        "cl_market_price": 0.0,
    }
    acct.update(overrides)
    return acct


def _make_trader(acct_summary, connected=True):
    from src.live_execution import live_trader as lt

    t = object.__new__(lt.LiveTrader)
    t._bot_start_time = datetime.now(timezone.utc)
    t._execution_symbol = "SI"
    t._last_inference_bar_time = None
    t._last_5m_bar_log = ""
    t._last_1h_bar_log = ""
    t._last_virtual_ledger_log = ""
    t._last_inference_log = ""
    t._last_inference_time_sec = 0.0

    data_client = MagicMock()
    data_client.is_connected.return_value = connected
    exec_client = MagicMock()
    exec_client.is_connected.return_value = connected
    exec_client.get_account_summary.return_value = acct_summary
    t.data_client = data_client
    t.exec_client = exec_client
    return t


def test_heartbeat_shows_account_margin_lines():
    """The three margin lines render right after Total Liq, in $#,##0.00."""
    trader = _make_trader(_full_acct_summary())

    payload = trader._build_heartbeat_payload()

    assert "Total Liq: `$1,483,258.15`" in payload
    assert "Init Margin (acct): `$18,150.00`" in payload
    assert "Maint Margin (acct): `$16,500.00`" in payload
    assert "Free Cushion (Excess Liq): `$1,466,758.15`" in payload


def test_margin_lines_positioned_after_total_liq():
    """Ordering: Total Liq, then Init, then Maint, then Free Cushion."""
    trader = _make_trader(_full_acct_summary())

    payload = trader._build_heartbeat_payload()

    i_liq = payload.index("Total Liq:")
    i_init = payload.index("Init Margin (acct):")
    i_maint = payload.index("Maint Margin (acct):")
    i_cushion = payload.index("Free Cushion (Excess Liq):")
    assert i_liq < i_init < i_maint < i_cushion


def test_margin_lines_render_zero_when_disconnected():
    """Disconnected path skips the IBKR query — margin lines still render $0.00."""
    trader = _make_trader({}, connected=False)

    payload = trader._build_heartbeat_payload()

    assert "Init Margin (acct): `$0.00`" in payload
    assert "Maint Margin (acct): `$0.00`" in payload
    assert "Free Cushion (Excess Liq): `$0.00`" in payload
