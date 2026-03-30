"""
Tests for CLOnlyLogFilter — suppresses ib_insync logs for non-CL symbols.
"""

import logging

import pytest

from src.live_execution.live_trader import CLOnlyLogFilter


@pytest.fixture
def log_filter():
    return CLOnlyLogFilter()


def _make_record(msg: str) -> logging.LogRecord:
    """Create a minimal LogRecord with the given message."""
    return logging.LogRecord(
        name="ib_insync.wrapper",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# Messages that SHOULD be suppressed (non-CL)
# ---------------------------------------------------------------------------

class TestSuppressedMessages:
    """Non-CL messages should be filtered out."""

    def test_stock_position(self, log_filter):
        record = _make_record(
            "position: Position(account='DU1899929', "
            "contract=Stock(conId=13977, symbol='XOM', exchange='NYSE'), "
            "position=0.0, avgCost=0.0)"
        )
        assert log_filter.filter(record) is False

    def test_stock_portfolio_update(self, log_filter):
        record = _make_record(
            "updatePortfolio: PortfolioItem("
            "contract=Stock(conId=272093, symbol='MSFT', right='0'), "
            "position=0.0, marketPrice=397.91, realizedPNL=-11860.1)"
        )
        assert log_filter.filter(record) is False

    def test_stock_symbol_only(self, log_filter):
        """Even without Stock() wrapper, symbol='V' should be caught."""
        record = _make_record(
            "position: Position(contract=Future(symbol='V'), position=0)"
        )
        assert log_filter.filter(record) is False

    def test_commission_report_stock_context(self, log_filter):
        """Stock() in message body should still be suppressed."""
        record = _make_record(
            "commissionReport linked to Stock(conId=10885, symbol='COP')"
        )
        assert log_filter.filter(record) is False


# ---------------------------------------------------------------------------
# Messages that SHOULD pass through (CL-related or generic)
# ---------------------------------------------------------------------------

class TestPassedMessages:
    """CL and generic connection messages should NOT be filtered."""

    def test_cl_position(self, log_filter):
        """CL position messages are suppressed (verbose IBKR callback dump)."""
        record = _make_record(
            "position: Position(account='DU1899929', "
            "contract=Future(symbol='CL', exchange='NYMEX'), "
            "position=1.0, avgCost=65.50)"
        )
        # Verbose callback dumps are now suppressed for ALL symbols
        # (redundant with our [TRADE] lines)
        assert log_filter.filter(record) is False

    def test_cl_portfolio_update(self, log_filter):
        """CL portfolio updates are suppressed (verbose IBKR callback dump)."""
        record = _make_record(
            "updatePortfolio: PortfolioItem("
            "contract=ContFuture(symbol='CL', exchange='NYMEX'), "
            "position=1.0, unrealizedPNL=500.0)"
        )
        assert log_filter.filter(record) is False

    def test_connection_warning(self, log_filter):
        record = _make_record(
            "Warning 2104, reqId -1: Market data farm connection is OK:usfarm"
        )
        assert log_filter.filter(record) is True

    def test_api_connection_ready(self, log_filter):
        record = _make_record("API connection ready")
        assert log_filter.filter(record) is True

    def test_generic_error(self, log_filter):
        record = _make_record(
            "Error 10182, reqId 5: keepUpToDate subscriptions lost"
        )
        assert log_filter.filter(record) is True

    def test_exec_details_cl(self, log_filter):
        """CL exec details are suppressed (verbose IBKR callback dump)."""
        record = _make_record(
            "execDetails Execution(execId='abc', exchange='NYMEX', "
            "side='BOT', shares=1.0, price=65.50)"
        )
        # Verbose callback dumps are now suppressed for ALL symbols
        assert log_filter.filter(record) is False

    def test_commission_report_plain(self, log_filter):
        """Commission reports are suppressed (verbose IBKR callback dump)."""
        record = _make_record(
            "commissionReport: CommissionReport(execId='abc', "
            "commission=2.25, realizedPNL=100.0)"
        )
        assert log_filter.filter(record) is False
