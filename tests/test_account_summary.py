"""
Tests for the startup account summary report.

Covers:
- IBKRConnectionManager.get_account_summary() (CL-only filtering)
- TelemetryDB.trade_summary() (aggregate stats)
- LiveTrader._print_account_summary() (formatted output)
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.live_execution.ibkr_client import IBKRConnectionManager
from src.live_execution.telemetry import TelemetryDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_account_value(tag, value, currency="USD", account="DU1899929"):
    return SimpleNamespace(tag=tag, value=str(value), currency=currency, account=account)


def _make_portfolio_item(symbol, position, unrealized, realized, market_value, avg_cost, market_price=0.0):
    # market_price: real ib_insync PortfolioItems always carry marketPrice;
    # surfaced as cl_market_price since telemetry-fill-commission_07062026_0640.
    contract = SimpleNamespace(symbol=symbol)
    return SimpleNamespace(
        contract=contract,
        position=position,
        unrealizedPNL=unrealized,
        realizedPNL=realized,
        marketValue=market_value,
        averageCost=avg_cost,
        marketPrice=market_price,
    )


# ---------------------------------------------------------------------------
# Tests: get_account_summary
# ---------------------------------------------------------------------------

class TestGetAccountSummary:
    """Tests for IBKRConnectionManager.get_account_summary()."""

    def _make_manager(self, acct_values, portfolio_items):
        """Create a manager with mocked IB connection."""
        mgr = object.__new__(IBKRConnectionManager)
        mgr.ib = MagicMock()
        mgr.ib.isConnected.return_value = True
        mgr.ib.accountValues.return_value = acct_values
        mgr.ib.portfolio.return_value = portfolio_items
        mgr._last_error = None
        return mgr

    def test_returns_cl_only_data(self):
        """Should only include CL portfolio data, ignoring stocks."""
        acct_values = [
            _make_account_value("NetLiquidation", 1000000.0),
            _make_account_value("AvailableFunds", 950000.0),
        ]
        portfolio = [
            _make_portfolio_item("XOM", 0, 0.0, 9166.81, 0.0, 0.0),
            _make_portfolio_item("MSFT", 0, 0.0, -11860.1, 0.0, 0.0),
            _make_portfolio_item("CL", 1, 500.0, 200.0, 65500.0, 65000.0),
        ]
        mgr = self._make_manager(acct_values, portfolio)

        result = mgr.get_account_summary()

        assert result["account"] == "DU1899929"
        assert result["net_liquidation"] == 1000000.0
        assert result["available_funds"] == 950000.0
        assert result["cl_position"] == 1
        assert result["cl_unrealized_pnl"] == 500.0
        assert result["cl_realized_pnl"] == 200.0
        assert result["cl_market_value"] == 65500.0
        assert result["cl_avg_cost"] == 65000.0

    def test_parses_account_margin_tags(self):
        """Account-wide margin tags ride the same cached accountValues() feed
        (ticket heartbeat-margin-report_07062026_2348)."""
        acct_values = [
            _make_account_value("NetLiquidation", 1483258.15),
            _make_account_value("AvailableFunds", 1466758.15),
            _make_account_value("InitMarginReq", 18150.0),
            _make_account_value("MaintMarginReq", 16500.0),
            _make_account_value("ExcessLiquidity", 1466758.15),
        ]
        mgr = self._make_manager(acct_values, [])

        result = mgr.get_account_summary()

        assert result["init_margin_req"] == 18150.0
        assert result["maint_margin_req"] == 16500.0
        assert result["excess_liquidity"] == 1466758.15

    def test_missing_margin_tags_default_zero(self):
        """A flat / just-connected account reports no margin tags — the new
        keys must default to 0.0 (broker value, not a config field) and must
        not raise."""
        acct_values = [_make_account_value("NetLiquidation", 500000.0)]
        mgr = self._make_manager(acct_values, [])

        result = mgr.get_account_summary()

        assert result["init_margin_req"] == 0.0
        assert result["maint_margin_req"] == 0.0
        assert result["excess_liquidity"] == 0.0

    def test_no_cl_position_returns_zeros(self):
        """When no CL in portfolio, CL fields should be zero."""
        acct_values = [
            _make_account_value("NetLiquidation", 500000.0),
        ]
        portfolio = [
            _make_portfolio_item("XOM", 100, 500.0, 200.0, 15000.0, 140.0),
        ]
        mgr = self._make_manager(acct_values, portfolio)

        result = mgr.get_account_summary()

        assert result["cl_position"] == 0
        assert result["cl_unrealized_pnl"] == 0.0
        assert result["cl_realized_pnl"] == 0.0

    def test_empty_account(self):
        """Empty account values and portfolio returns all defaults."""
        mgr = self._make_manager([], [])
        result = mgr.get_account_summary()

        assert result["account"] == ""
        assert result["net_liquidation"] == 0.0
        assert result["cl_position"] == 0


# ---------------------------------------------------------------------------
# Tests: trade_summary
# ---------------------------------------------------------------------------

class TestTradeSummary:
    """Tests for TelemetryDB.trade_summary()."""

    @pytest.fixture
    def db(self, tmp_path):
        """Create an in-memory telemetry DB."""
        db_path = str(tmp_path / "test_telemetry.db")
        telemetry = TelemetryDB(db_path)
        yield telemetry
        telemetry.close()

    def test_empty_db(self, db):
        """Empty DB should return zero counts and None dates."""
        result = db.trade_summary()

        assert result["total_signals"] == 0
        assert result["executed_trades"] == 0
        assert result["first_signal"] is None
        assert result["last_signal"] is None
        assert result["total_bars"] == 0

    def test_with_signals(self, db):
        """Should count signals and return correct date range."""
        db.log_signal("2026-02-20T10:00:00", "Hold", 42.0, "HOLD", current_price=65.0)
        db.log_signal("2026-02-21T10:00:00", "Buy", 55.0, "EXECUTE", current_price=65.5)
        db.log_signal("2026-02-22T10:00:00", "Hold", 40.0, "HOLD", current_price=64.0)

        result = db.trade_summary()

        assert result["total_signals"] == 3
        assert result["executed_trades"] == 1
        assert "2026-02-20" in result["first_signal"]
        assert "2026-02-22" in result["last_signal"]

    def test_bars_counted(self, db):
        """Should count market bars."""
        db.log_bar("2026-02-20T10:00:00", 65.0, 65.5, 64.8, 65.2, 100.0)
        db.log_bar("2026-02-20T10:05:00", 65.2, 65.6, 65.1, 65.4, 120.0)

        result = db.trade_summary()
        assert result["total_bars"] == 2


# ---------------------------------------------------------------------------
# Tests: _print_account_summary
# ---------------------------------------------------------------------------

class TestPrintAccountSummary:
    """Tests for LiveTrader._print_account_summary()."""

    def _make_trader_stub(self, acct_data, trade_data):
        """Create a trader stub with mocked manager and telemetry."""
        from src.live_execution import live_trader as lt_module

        trader = object.__new__(lt_module.LiveTrader)
        trader._execution_symbol = "CL"
        trader.exec_client = MagicMock()
        trader.exec_client.get_account_summary.return_value = acct_data
        trader.telemetry = MagicMock()
        trader.telemetry.trade_summary.return_value = trade_data
        return trader

    def test_prints_account_info(self, caplog):
        """Should log the formatted account summary."""
        acct = {
            "account": "DU1899929",
            "net_liquidation": 1000000.0,
            "available_funds": 950000.0,
            "cl_position": 0,
            "cl_unrealized_pnl": 0.0,
            "cl_realized_pnl": 0.0,
            "cl_market_value": 0.0,
            "cl_avg_cost": 0.0,
        }
        ts = {
            "total_signals": 42,
            "executed_trades": 5,
            "first_signal": "2026-02-20T10:00:00",
            "last_signal": "2026-02-25T15:00:00",
            "total_bars": 1000,
        }
        trader = self._make_trader_stub(acct, ts)

        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            trader._print_account_summary()

        output = caplog.text
        assert "ACCOUNT SUMMARY" in output
        assert "DU1899929" in output
        assert "1,000,000.00" in output
        assert "FLAT" in output
        assert "42" in output
        assert "5" in output
        assert "2026-02-20" in output

    def test_long_position_shows_long(self, caplog):
        """Should show LONG when CL position > 0."""
        acct = {
            "account": "DU1899929",
            "net_liquidation": 100000.0,
            "available_funds": 90000.0,
            "cl_position": 2,
            "cl_unrealized_pnl": 1000.0,
            "cl_realized_pnl": 500.0,
            "cl_market_value": 131000.0,
            "cl_avg_cost": 65000.0,
        }
        ts = {
            "total_signals": 0,
            "executed_trades": 0,
            "first_signal": None,
            "last_signal": None,
            "total_bars": 0,
        }
        trader = self._make_trader_stub(acct, ts)

        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            trader._print_account_summary()

        assert "LONG (2 contracts)" in caplog.text

    def test_short_position_shows_short(self, caplog):
        """Should show SHORT when CL position < 0."""
        acct = {
            "account": "DU1899929",
            "net_liquidation": 100000.0,
            "available_funds": 90000.0,
            "cl_position": -1,
            "cl_unrealized_pnl": -200.0,
            "cl_realized_pnl": 0.0,
            "cl_market_value": -65000.0,
            "cl_avg_cost": 65000.0,
        }
        ts = {
            "total_signals": 10,
            "executed_trades": 1,
            "first_signal": "2026-02-25T10:00:00",
            "last_signal": "2026-02-25T15:00:00",
            "total_bars": 50,
        }
        trader = self._make_trader_stub(acct, ts)

        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            trader._print_account_summary()

        assert "SHORT (-1 contract)" in caplog.text

    def test_handles_exception_gracefully(self, caplog):
        """Should warn and skip if account summary fails."""
        from src.live_execution import live_trader as lt_module

        trader = object.__new__(lt_module.LiveTrader)
        trader.manager = MagicMock()
        trader.manager.get_account_summary.side_effect = Exception("Connection lost")
        trader.telemetry = MagicMock()

        with caplog.at_level(logging.WARNING, logger="LiveTrader"):
            trader._print_account_summary()  # should not raise

        assert "Could not retrieve account summary" in caplog.text

    def test_no_signals_date_range(self, caplog):
        """Should show 'No signals recorded' when no trade history."""
        acct = {
            "account": "DU1899929",
            "net_liquidation": 100000.0,
            "available_funds": 90000.0,
            "cl_position": 0,
            "cl_unrealized_pnl": 0.0,
            "cl_realized_pnl": 0.0,
            "cl_market_value": 0.0,
            "cl_avg_cost": 0.0,
        }
        ts = {
            "total_signals": 0,
            "executed_trades": 0,
            "first_signal": None,
            "last_signal": None,
            "total_bars": 0,
        }
        trader = self._make_trader_stub(acct, ts)

        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            trader._print_account_summary()

        assert "No signals recorded" in caplog.text
