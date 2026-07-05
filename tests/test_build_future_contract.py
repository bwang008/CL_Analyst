"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/ibkr_client.py (build_future_contract NEW,
                            build_cl_contract/build_mcl_contract -> thin wrappers,
                            fetch_historical_bars / fetch_historical_bars_by_duration
                            (+async) gain REQUIRED keyword-only symbol,
                            get_front_month_contract(_async) exchange from registry),
                            src/live_execution/adapters/ibkr_execution.py
                            (resolve_contract exchange from registry — 1-liner)
Target Class/Function: build_future_contract, build_cl_contract, build_mcl_contract,
                       IBKRConnectionManager.fetch_historical_bars,
                       IBKRConnectionManager.fetch_historical_bars_by_duration(_async),
                       IBKRConnectionManager.get_front_month_contract(_async),
                       IBKRExecutionClient.resolve_contract
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t2-symbol-data-paths_07042026_1815
Phase: RED — build_future_contract does NOT exist yet; this file fails at
collection (ghost import per TDD protocol) until the Coder implements
audit.md section 4 (D5/D6) + section 5.1 and the manager-approved
ibkr_execution.py:73 1-liner.

Coverage (audit.md section 6 tests 1-9 and 31, amended by impact_review C3):
  - build_future_contract: CL/MCL byte parity with the legacy builders on BOTH
    branches (C3: includeExpired=True on the ContFuture branch, includeExpired
    left at the ib_insync default False on the front-month Future branch);
    registry exchange resolution (ES->CME, ZC->CBOT, GC->COMEX, NG->NYMEX);
    unknown symbol raises get_instrument's ValueError; continuous=False without
    contract_month raises the VERBATIM legacy message; keyword-only signature.
  - Legacy wrappers stay importable and field-identical (protects
    scripts/download_ibkr_history.py).
  - Manager fetch methods REQUIRE keyword-only symbol (TypeError without);
    symbol="ES" requests a qualified ContFuture(ES, CME); symbol="CL" request
    is byte-identical to today's build_cl_contract output (regression pin).
  - get_front_month_contract(_async): search Future exchange from the registry;
    _EXPIRY_BUFFER_DAYS pinned at 6 (T5 changes the source deliberately).
  - ibkr_execution.resolve_contract: ES resolves on CME; CL stays NYMEX
    (keeps tests/test_ibkr_adapters.py CL assertion green).

All IBKR I/O is mocked (no gateway, no network). Contract objects are pure
ib_insync dataclasses compared field-by-field via dataclasses.asdict.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from ib_insync import ContFuture, Future

# Ghost import — the Coder adds build_future_contract (audit.md D6).
from src.live_execution.ibkr_client import (  # noqa: E402
    IBKRConnectionManager,
    build_cl_contract,
    build_future_contract,
    build_mcl_contract,
)

# Verbatim legacy error message (audit D6 — must not drift by one character).
_MONTH_REQUIRED_MSG = (
    "contract_month is required when continuous=False (format: YYYYMM)."
)


def _cdict(contract) -> dict:
    """Every field of an ib_insync Contract, for byte-level comparison."""
    return dataclasses.asdict(contract)


# ===========================================================================
# build_future_contract — CL/MCL byte parity with the legacy builders
# (audit tests 1-2, impact_review C3 pins BOTH branches)
# ===========================================================================

class TestBuildFutureContractParity:
    def test_cl_continuous_byte_identical(self):
        new = build_future_contract("CL", continuous=True)
        legacy = build_cl_contract(continuous=True)
        assert _cdict(new) == _cdict(legacy)
        # Explicit literal pins (today's byte values — not derived):
        assert new.secType == "CONTFUT"
        assert new.symbol == "CL"
        assert new.exchange == "NYMEX"
        assert new.currency == "USD"
        assert new.includeExpired is True  # C3: ContFuture branch
        assert new.lastTradeDateOrContractMonth == ""

    def test_cl_month_byte_identical(self):
        new = build_future_contract("CL", continuous=False, contract_month="202609")
        legacy = build_cl_contract(continuous=False, contract_month="202609")
        assert _cdict(new) == _cdict(legacy)
        assert new.secType == "FUT"
        assert new.symbol == "CL"
        assert new.lastTradeDateOrContractMonth == "202609"
        assert new.exchange == "NYMEX"
        assert new.currency == "USD"
        # C3: the front-month Future branch must NOT set includeExpired —
        # it keeps the ib_insync default (False).
        assert new.includeExpired is False

    def test_mcl_continuous_byte_identical(self):
        new = build_future_contract("MCL", continuous=True)
        legacy = build_mcl_contract(continuous=True)
        assert _cdict(new) == _cdict(legacy)
        assert new.secType == "CONTFUT"
        assert new.symbol == "MCL"
        assert new.exchange == "NYMEX"
        assert new.currency == "USD"
        assert new.includeExpired is True

    def test_mcl_month_byte_identical(self):
        new = build_future_contract("MCL", continuous=False, contract_month="202609")
        legacy = build_mcl_contract(continuous=False, contract_month="202609")
        assert _cdict(new) == _cdict(legacy)
        assert new.secType == "FUT"
        assert new.symbol == "MCL"
        assert new.includeExpired is False  # C3


# ===========================================================================
# build_future_contract — registry exchange resolution (audit test 3)
# ===========================================================================

class TestBuildFutureContractRegistry:
    @pytest.mark.parametrize(
        "symbol,expected_exchange",
        [("ES", "CME"), ("ZC", "CBOT"), ("GC", "COMEX"), ("NG", "NYMEX")],
    )
    def test_registry_exchange_contfuture(self, symbol, expected_exchange):
        contract = build_future_contract(symbol, continuous=True)
        assert contract.secType == "CONTFUT"
        assert contract.symbol == symbol
        assert contract.exchange == expected_exchange
        assert contract.currency == "USD"
        assert contract.includeExpired is True

    @pytest.mark.parametrize(
        "symbol,expected_exchange",
        [("ES", "CME"), ("ZC", "CBOT"), ("GC", "COMEX"), ("NG", "NYMEX")],
    )
    def test_registry_exchange_future_month(self, symbol, expected_exchange):
        contract = build_future_contract(
            symbol, continuous=False, contract_month="202612"
        )
        assert contract.secType == "FUT"
        assert contract.symbol == symbol
        assert contract.exchange == expected_exchange
        assert contract.currency == "USD"
        assert contract.lastTradeDateOrContractMonth == "202612"
        assert contract.includeExpired is False  # C3 on the generic branch too

    def test_es_continuous_every_field_pinned(self):
        """Ticket pin: ES ContFuture with exchange CME from the registry and
        includeExpired=True — EVERY field must equal the reference contract."""
        expected = ContFuture(
            symbol="ES", exchange="CME", currency="USD", includeExpired=True
        )
        assert _cdict(build_future_contract("ES", continuous=True)) == _cdict(expected)

    def test_explicit_exchange_and_currency_override_win(self):
        """D6: exchange=None -> registry; an explicit exchange/currency must be
        honored (the legacy wrappers rely on this for their own parameters)."""
        contract = build_future_contract(
            "CL", continuous=True, exchange="TESTX", currency="EUR"
        )
        assert contract.exchange == "TESTX"
        assert contract.currency == "EUR"


# ===========================================================================
# build_future_contract — failure modes (audit tests 4-5, D6 signature)
# ===========================================================================

class TestBuildFutureContractErrors:
    def test_unknown_symbol_raises_continuous(self):
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            build_future_contract("ZZ", continuous=True)

    def test_unknown_symbol_raises_month_form(self):
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            build_future_contract("ZZ", continuous=False, contract_month="202609")

    @pytest.mark.parametrize("symbol", ["CL", "ES"])
    def test_month_required_message_verbatim(self, symbol):
        with pytest.raises(ValueError, match=re.escape(_MONTH_REQUIRED_MSG)):
            build_future_contract(symbol, continuous=False)

    def test_continuous_is_keyword_only(self):
        """D6 signature: build_future_contract(symbol, *, continuous, ...)."""
        with pytest.raises(TypeError):
            build_future_contract("ES", True)  # noqa — positional must fail


# ===========================================================================
# Legacy wrappers (audit test 6 — protects scripts/download_ibkr_history.py)
# ===========================================================================

class TestLegacyWrappers:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"continuous": True},
            {"continuous": False, "contract_month": "202609"},
            {"continuous": True, "exchange": "TESTX", "currency": "EUR"},
        ],
    )
    def test_build_cl_contract_delegates(self, kwargs):
        assert _cdict(build_cl_contract(**kwargs)) == _cdict(
            build_future_contract("CL", **kwargs)
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"continuous": True},
            {"continuous": False, "contract_month": "202609"},
        ],
    )
    def test_build_mcl_contract_delegates(self, kwargs):
        assert _cdict(build_mcl_contract(**kwargs)) == _cdict(
            build_future_contract("MCL", **kwargs)
        )


# ===========================================================================
# IBKRConnectionManager fetch methods — required keyword-only symbol
# (audit tests 7-8). self.ib and I/O seams are fully mocked.
# ===========================================================================

def _make_manager() -> IBKRConnectionManager:
    mgr = IBKRConnectionManager()
    mgr.ib = MagicMock()
    mgr.ib.isConnected.return_value = True
    mgr.ensure_connected = MagicMock()
    mgr.qualify_contract = MagicMock(side_effect=lambda c: c)
    mgr.qualify_contract_async = AsyncMock(side_effect=lambda c: c)
    mgr._request_historical_data = MagicMock(return_value=[])
    mgr._request_historical_data_async = AsyncMock(return_value=[])
    return mgr


class TestManagerFetchSymbolRequired:
    def test_fetch_historical_bars_requires_symbol(self):
        mgr = _make_manager()
        with pytest.raises(TypeError):
            mgr.fetch_historical_bars(days_back=1)

    def test_fetch_by_duration_requires_symbol(self):
        mgr = _make_manager()
        with pytest.raises(TypeError):
            mgr.fetch_historical_bars_by_duration(duration_str="1 D")

    def test_fetch_by_duration_async_requires_symbol(self):
        mgr = _make_manager()
        with pytest.raises(TypeError):
            # Signature binding fails at call time — no await needed.
            mgr.fetch_historical_bars_by_duration_async(duration_str="1 D")


class TestManagerFetchBuildsRequestedContract:
    def _captured_contract(self, mgr):
        return mgr._request_historical_data.call_args.kwargs["contract"]

    def test_by_duration_es_builds_cme_contfuture(self):
        mgr = _make_manager()
        df = mgr.fetch_historical_bars_by_duration(
            duration_str="2 D", symbol="ES", continuous=True
        )
        assert isinstance(df, pd.DataFrame)
        contract = self._captured_contract(mgr)
        expected = ContFuture(
            symbol="ES", exchange="CME", currency="USD", includeExpired=True
        )
        assert _cdict(contract) == _cdict(expected)

    def test_by_duration_cl_request_byte_identical_to_today(self):
        """THE regression pin: a symbol='CL' request must produce exactly the
        contract today's unconditional build_cl_contract() produced."""
        mgr = _make_manager()
        mgr.fetch_historical_bars_by_duration(
            duration_str="2 D", symbol="CL", continuous=True
        )
        contract = self._captured_contract(mgr)
        # Pin against explicit literals (today's bytes)...
        assert contract.secType == "CONTFUT"
        assert contract.symbol == "CL"
        assert contract.exchange == "NYMEX"
        assert contract.currency == "USD"
        assert contract.includeExpired is True
        assert contract.lastTradeDateOrContractMonth == ""
        # ... and against the legacy builder output, every field.
        assert _cdict(contract) == _cdict(build_cl_contract(continuous=True))

    def test_days_back_variant_builds_requested_symbol(self):
        mgr = _make_manager()
        mgr.fetch_historical_bars(days_back=2, symbol="ES", continuous=True)
        contract = self._captured_contract(mgr)
        assert contract.secType == "CONTFUT"
        assert contract.symbol == "ES"
        assert contract.exchange == "CME"

    def test_async_variant_builds_requested_symbol(self):
        mgr = _make_manager()
        asyncio.run(
            mgr.fetch_historical_bars_by_duration_async(
                duration_str="2 D", symbol="ES", continuous=True
            )
        )
        contract = mgr._request_historical_data_async.call_args.kwargs["contract"]
        assert contract.secType == "CONTFUT"
        assert contract.symbol == "ES"
        assert contract.exchange == "CME"
        assert contract.includeExpired is True

    def test_contract_month_form_passes_through(self):
        mgr = _make_manager()
        mgr.fetch_historical_bars_by_duration(
            duration_str="2 D",
            symbol="ES",
            continuous=False,
            contract_month="202603",
        )
        contract = self._captured_contract(mgr)
        assert contract.secType == "FUT"
        assert contract.symbol == "ES"
        assert contract.exchange == "CME"
        assert contract.lastTradeDateOrContractMonth == "202603"
        assert contract.includeExpired is False  # C3


# ===========================================================================
# get_front_month_contract(_async) — search exchange from the registry
# (audit test 9; buffer pinned at 6 — T5 changes the source deliberately)
# ===========================================================================

def _fake_detail(local_symbol: str = "XXZ9"):
    return SimpleNamespace(
        contract=SimpleNamespace(
            lastTradeDateOrContractMonth="20991219",
            localSymbol=local_symbol,
            conId=42,
        )
    )


class TestFrontMonthRegistryExchange:
    @pytest.mark.parametrize(
        "symbol,expected_exchange",
        [("ES", "CME"), ("CL", "NYMEX"), ("MCL", "NYMEX")],
    )
    def test_search_contract_exchange(self, symbol, expected_exchange):
        mgr = _make_manager()
        mgr.ib.reqContractDetails.return_value = [_fake_detail(f"{symbol}Z9")]

        local_sym, month_str = mgr.get_front_month_contract(symbol=symbol)

        assert local_sym == f"{symbol}Z9"
        assert month_str == "209912"
        search = mgr.ib.reqContractDetails.call_args[0][0]
        assert search.secType == "FUT"
        assert search.symbol == symbol
        assert search.exchange == expected_exchange
        assert search.currency == "USD"

    def test_search_contract_exchange_async(self):
        mgr = _make_manager()
        mgr.ib.reqContractDetailsAsync = AsyncMock(
            return_value=[_fake_detail("ESZ9")]
        )

        local_sym, month_str = asyncio.run(
            mgr.get_front_month_contract_async(symbol="ES")
        )

        assert local_sym == "ESZ9"
        assert month_str == "209912"
        search = mgr.ib.reqContractDetailsAsync.call_args[0][0]
        assert search.symbol == "ES"
        assert search.exchange == "CME"

    def test_expiry_buffer_still_six_days(self):
        """Pin: T2 must NOT re-source the buffer from roll_buffer_days —
        that is T5 scope (switching now would silently change ES to 8)."""
        assert IBKRConnectionManager._EXPIRY_BUFFER_DAYS == 6


# ===========================================================================
# ibkr_execution.resolve_contract — registry exchange (audit test 31,
# manager-approved 1-liner). Narrow unit seam mirroring
# tests/test_ibkr_adapters.py::_make_client_and_manager.
# ===========================================================================

def _make_exec_client_and_manager():
    from src.live_execution.adapters.ibkr_execution import IBKRExecutionClient

    with patch(
        "src.live_execution.adapters.ibkr_execution.IBKRConnectionManager"
    ) as MockMgr:
        mock_mgr = MockMgr.return_value
        mock_mgr.ib = MagicMock()
        client = IBKRExecutionClient()
    return client, mock_mgr


class TestResolveContractRegistryExchange:
    def test_resolve_contract_es_uses_cme(self):
        client, mock_mgr = _make_exec_client_and_manager()
        mock_mgr.get_front_month_contract.return_value = ("ESZ5", "202512")
        qualified = MagicMock()
        qualified.localSymbol = "ESZ5"
        qualified.conId = 12345
        mock_mgr.qualify_contract.return_value = qualified

        client.resolve_contract("ES")

        mock_mgr.get_front_month_contract.assert_called_once_with(symbol="ES")
        pre_qualification = mock_mgr.qualify_contract.call_args[0][0]
        assert pre_qualification.secType == "FUT"
        assert pre_qualification.symbol == "ES"
        assert pre_qualification.localSymbol == "ESZ5"
        assert pre_qualification.exchange == "CME"
        assert client._cached_contracts["ES"] is qualified

    def test_resolve_contract_cl_still_nymex(self):
        """Regression twin: CL behavior byte-identical (keeps the existing
        tests/test_ibkr_adapters.py CL assertion green)."""
        client, mock_mgr = _make_exec_client_and_manager()
        mock_mgr.get_front_month_contract.return_value = ("CLN6", "202607")
        qualified = MagicMock()
        qualified.localSymbol = "CLN6"
        qualified.conId = 304037461
        mock_mgr.qualify_contract.return_value = qualified

        client.resolve_contract("CL")

        pre_qualification = mock_mgr.qualify_contract.call_args[0][0]
        expected = Future(symbol="CL", localSymbol="CLN6", exchange="NYMEX")
        assert _cdict(pre_qualification) == _cdict(expected)
