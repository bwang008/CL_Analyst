"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/adapters/ibkr_data_feed.py
                            (__init__ gains REQUIRED keyword-only
                            instrument_context; continuous routing via
                            build_future_contract — CL fallback dies; index
                            branches preserved verbatim; front-month exchange
                            from registry; fetch delegations pass
                            symbol=ctx.brain_symbol),
                            src/live_execution/interfaces/data_feed_interface.py
                            (get_front_month_contract "CL" default dropped — C6),
                            src/live_execution/data_manager.py
                            (DataPaths NEW, derive_data_paths NEW, DataManager
                            required keyword-only symbol, roll_metadata_path
                            instance attribute replaces module-global
                            _ROLL_METADATA_PATH),
                            src/live_execution/live_trader.py
                            (DataManager paths via
                            derive_data_paths(ctx.brain_symbol); brain
                            subscriptions via brain_symbol),
                            src/live_execution/cli.py
                            (seed/cache defaults derived post-ctx; factory gets
                            instrument_context; _merge_legacy_cid_caches gated
                            on ctx.brain_symbol == "CL" — C8)
Target Class/Function: IBKRDataFeedClient, DataFeedClient, derive_data_paths,
                       DataPaths, DataManager.__init__, LiveTrader.__init__,
                       LiveTrader._subscribe/_subscribe_front_month/
                       _deferred_resubscribe/_backfill_reconnect_gap_async,
                       cli.main
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t2-symbol-data-paths_07042026_1815
Phase: RED — derive_data_paths / DataPaths do NOT exist yet; this file fails
at collection (ghost import per TDD protocol) until the Coder implements
audit.md sections 4-5 under impact_review conditions C1-C8.

Coverage (audit.md section 6 tests 10-30, amended by C1/C3/C6/C8):
  - Adapter: required instrument_context; ES continuous -> ContFuture(ES, CME);
    the not-MCL->CL fallback is DEAD (unknown symbol raises, no CL contract
    built); CL/MCL subscriptions byte-identical to the legacy builders;
    VIX/OVX/DX Index branches pinned verbatim incl. qualification-failure
    tolerance; front-month Future exchange from the registry; all three fetch
    delegations forward symbol=ctx.brain_symbol (MCL context -> "CL").
  - SimulatedDataFeed byte-untouched (C6): constructor and fetch signatures
    unchanged, get_front_month_contract keeps its own "CL" default.
  - derive_data_paths: CL 7-tuple byte-identical to today's literals, expected
    values composed from the SAME get_data_path/get_data_root expressions
    currently in code (C1 — THE regression pin); ES table per audit D4;
    unknown symbol raises; expression-fidelity seam pin (5m seed via the
    existence-aware get_data_path, the other six composed from get_data_root —
    consumed via data_manager's module-level imports so the asymmetry is
    testable without touching the real filesystem).
  - DataManager: required keyword-only symbol (TypeError without; ValueError
    on unknown); roll_metadata_path per-instance (derived per symbol, explicit
    override wins, save/load round-trip through the instance attribute — the
    module-global _ROLL_METADATA_PATH is gone or unused by instances); missing
    non-CL seed raises the actionable FileNotFoundError naming the derived ES
    seed path + CL_DATA_ROOT hint; backfill/ledger fetches carry the symbol to
    IBKR through the data client (symbol is ADAPTER-BOUND per audit D1/test 26
    — the DataManager-facing DataFeedClient call signature stays byte-
    compatible with the untouched SimulatedDataFeed, which is exactly what the
    C7 parity harness requires).
  - LiveTrader: CL config constructs both DataManagers with the legacy paths
    byte-identical; ES config -> ES-derived names; MCL config shares the CL
    file set (D3, brain-keyed); live_config.seed_path_1h override preserved
    (absolute + relative-to-data-root); ES missing-1h-seed raises naming ES
    paths; brain streams subscribe brain_symbol continuous while the Hands
    stream stays execution_symbol (constraint 3); async resubscribe twin;
    reconnect backfill still calls the adapter WITHOUT a symbol kwarg.
  - CLI: CL defaults byte-identical; explicit --seed-path/--cache-path win;
    ES derived defaults + DataFeedFactory receives instrument_context;
    _merge_legacy_cid_caches gated on brain_symbol == "CL" (called for CL and
    for MCL with client_id != 1, NOT called for ES — C8).

All I/O boundaries are mocked: no IBKR gateway, no real data files touched
(tmp_path only), LiveTrader's DataManager construction is intercepted and the
1h seed/cache existence check is controlled via pathlib.Path.exists patches.
LiveTrader seam mirrors tests/test_instrument_context.py::TestLiveTraderWiring.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from ib_insync import ContFuture, Future, Index

from src.data_paths import get_data_path, get_data_root
from src.live_execution.adapters.ibkr_data_feed import IBKRDataFeedClient
from src.live_execution.adapters.simulated_data_feed import SimulatedDataFeed
from src.live_execution.ibkr_client import build_cl_contract, build_mcl_contract
from src.live_execution.instrument_context import resolve_instrument_context
from src.live_execution.interfaces.data_feed_interface import DataFeedClient
from src.live_execution.live_trader import LiveTrader

# Ghost import — the Coder adds DataPaths + derive_data_paths and the
# required keyword-only symbol on DataManager (audit.md D2/section 5.3).
from src.live_execution.data_manager import (  # noqa: E402
    DataManager,
    DataPaths,
    derive_data_paths,
)


# ===========================================================================
# Shared helpers
# ===========================================================================

def _cdict(contract) -> dict:
    """Every field of an ib_insync Contract, for byte-level comparison."""
    return dataclasses.asdict(contract)


def _expected_paths(symbol: str) -> dict:
    """Expected per-symbol artifact paths, composed from the SAME
    get_data_path/get_data_root expressions the current literals use (C1) and
    the audit D4 naming table (CL keeps its 3 legacy-name exceptions)."""
    sym_u = symbol.upper()
    sym_l = symbol.lower()
    root = get_data_root()
    if sym_u == "CL":
        cache_5m = root / "processed" / "warm_start_cache.parquet"
        cache_1h = root / "processed" / "warm_start_cache_1h.parquet"
        roll = root / "processed" / ".roll_metadata.json"
    else:
        cache_5m = root / "processed" / f"warm_start_cache_{sym_u}.parquet"
        cache_1h = root / "processed" / f"warm_start_cache_{sym_u}_1h.parquet"
        roll = root / "processed" / f".roll_metadata_{sym_u}.json"
    return {
        "seed_5m": str(get_data_path(f"raw/{sym_l}-5m_bk.csv")),
        "cache_5m": str(cache_5m),
        "ledger_5m": str(root / "processed" / f"{sym_l}_continuous_master.parquet"),
        "seed_1h": str(root / "processed" / f"{sym_u}_raw_1h.parquet"),
        "cache_1h": str(cache_1h),
        "ledger_1h": str(root / "processed" / f"{sym_l}_continuous_master_1h.parquet"),
        "roll_metadata": str(roll),
    }


def _make_ohlcv_df(n_bars: int = 50, start: str = "2020-01-01") -> pd.DataFrame:
    np.random.seed(7)
    dates = pd.date_range(start=start, periods=n_bars, freq="5min")
    close = 70.0 + np.cumsum(np.random.normal(0, 0.05, n_bars))
    df = pd.DataFrame(
        {
            "DateTime": dates,
            "Open": close - 0.02,
            "High": close + 0.05,
            "Low": close - 0.05,
            "Close": close,
            "Volume": np.random.randint(100, 5000, n_bars).astype(float),
        },
        index=dates,
    )
    df.index.name = "DateTime"
    return df


def _make_data_feed_adapter(execution_symbol: str):
    """IBKRDataFeedClient with a fully mocked IBKRConnectionManager, bound to
    the instrument context resolved from the given execution symbol."""
    ctx = resolve_instrument_context({"execution_symbol": execution_symbol})
    with patch(
        "src.live_execution.adapters.ibkr_data_feed.IBKRConnectionManager"
    ) as MockMgr:
        mock_mgr = MockMgr.return_value
        mock_mgr.qualify_contract.side_effect = lambda c: c
        adapter = IBKRDataFeedClient(
            host="127.0.0.1", port=4001, client_id=7, instrument_context=ctx
        )
    return adapter, mock_mgr


class DummyStrategy:
    """Minimal strategy stub mirroring tests/test_instrument_context.py."""

    def __init__(self, feature_names: list[str] | None = None, config: dict | None = None):
        self.feature_names = feature_names if feature_names is not None else ["MACD"]
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config if config is not None else {}


def _dm_tmp_kwargs(tmp_path: Path, symbol: str = "CL", **overrides) -> dict:
    kwargs = dict(
        symbol=symbol,
        seed_path=str(tmp_path / "seed.csv"),
        cache_path=str(tmp_path / "cache.parquet"),
        master_ledger_path=str(tmp_path / "ledger.parquet"),
        roll_metadata_path=str(tmp_path / "roll.json"),
        data_client=None,
    )
    kwargs.update(overrides)
    return kwargs


# ===========================================================================
# Adapter construction (audit test 10)
# ===========================================================================

class TestAdapterConstruction:
    def test_adapter_requires_instrument_context(self):
        """No silent CL: constructing the IBKR data feed without an
        instrument context must TypeError."""
        with patch(
            "src.live_execution.adapters.ibkr_data_feed.IBKRConnectionManager"
        ):
            with pytest.raises(TypeError, match="instrument_context"):
                IBKRDataFeedClient(host="127.0.0.1", port=4001, client_id=7)

    def test_adapter_constructs_and_passes_connection_params(self):
        ctx = resolve_instrument_context({"execution_symbol": "CL"})
        with patch(
            "src.live_execution.adapters.ibkr_data_feed.IBKRConnectionManager"
        ) as MockMgr:
            IBKRDataFeedClient(
                host="10.1.2.3", port=4009, client_id=27, instrument_context=ctx
            )
        MockMgr.assert_called_once_with(
            host="10.1.2.3",
            port=4009,
            client_id=27,
            readonly=True,
            fallback_ports=[7496],
        )


# ===========================================================================
# Adapter continuous routing (audit tests 11-12) — the CL fallback is DEAD
# ===========================================================================

class TestAdapterContinuousRouting:
    def test_continuous_subscription_builds_requested_symbol_es(self):
        adapter, mgr = _make_data_feed_adapter("CL")
        adapter.subscribe_live_bars("ES", continuous=True)
        contract = mgr.subscribe_live_bars.call_args.kwargs["contract"]
        expected = ContFuture(
            symbol="ES", exchange="CME", currency="USD", includeExpired=True
        )
        assert _cdict(contract) == _cdict(expected)

    def test_unknown_symbol_raises_no_cl_fallback(self):
        adapter, mgr = _make_data_feed_adapter("CL")
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            adapter.subscribe_live_bars("ZZ", continuous=True)
        mgr.subscribe_live_bars.assert_not_called()
        mgr.qualify_contract.assert_not_called()  # no CL contract even built

    def test_cl_subscription_byte_identical(self):
        adapter, mgr = _make_data_feed_adapter("CL")
        adapter.subscribe_live_bars("CL", continuous=True)
        contract = mgr.subscribe_live_bars.call_args.kwargs["contract"]
        assert _cdict(contract) == _cdict(build_cl_contract(continuous=True))
        assert contract.secType == "CONTFUT"
        assert contract.symbol == "CL"
        assert contract.exchange == "NYMEX"
        assert contract.currency == "USD"
        assert contract.includeExpired is True

    def test_mcl_subscription_byte_identical(self):
        adapter, mgr = _make_data_feed_adapter("MCL")
        adapter.subscribe_live_bars("MCL", continuous=True)
        contract = mgr.subscribe_live_bars.call_args.kwargs["contract"]
        assert _cdict(contract) == _cdict(build_mcl_contract(continuous=True))
        assert contract.symbol == "MCL"
        assert contract.exchange == "NYMEX"

    def test_async_continuous_subscription_es_and_cl(self):
        adapter, mgr = _make_data_feed_adapter("CL")
        mgr.qualify_contract_async = AsyncMock(side_effect=lambda c: c)
        mgr.subscribe_live_bars_async = AsyncMock(return_value=MagicMock())

        asyncio.run(adapter.subscribe_live_bars_async("ES", continuous=True))
        es_contract = mgr.subscribe_live_bars_async.await_args.kwargs["contract"]
        assert es_contract.secType == "CONTFUT"
        assert es_contract.symbol == "ES"
        assert es_contract.exchange == "CME"
        assert es_contract.includeExpired is True

        asyncio.run(adapter.subscribe_live_bars_async("CL", continuous=True))
        cl_contract = mgr.subscribe_live_bars_async.await_args.kwargs["contract"]
        assert _cdict(cl_contract) == _cdict(build_cl_contract(continuous=True))

    def test_futures_qualification_failure_still_raises(self):
        adapter, mgr = _make_data_feed_adapter("CL")
        mgr.qualify_contract.side_effect = ValueError("Failed to qualify")
        with pytest.raises(ValueError):
            adapter.subscribe_live_bars("CL", continuous=True)
        mgr.subscribe_live_bars.assert_not_called()


# ===========================================================================
# Adapter index branches — preserved verbatim (audit test 13)
# ===========================================================================

class TestAdapterIndexBranches:
    @pytest.mark.parametrize("symbol", ["VIX", "OVX"])
    def test_vix_ovx_index_branch_preserved(self, symbol):
        adapter, mgr = _make_data_feed_adapter("CL")
        adapter.subscribe_live_bars(symbol, what_to_show="MIDPOINT")
        call = mgr.subscribe_live_bars.call_args
        expected = Index(symbol, "CBOE", "USD")
        assert _cdict(call.kwargs["contract"]) == _cdict(expected)
        # The index branch forces TRADES regardless of the caller's value.
        assert call.kwargs["what_to_show"] == "TRADES"

    def test_dx_index_branch_preserved(self, ):
        adapter, mgr = _make_data_feed_adapter("CL")
        adapter.subscribe_live_bars("DX")
        contract = mgr.subscribe_live_bars.call_args.kwargs["contract"]
        expected = Index("DX", "NYBOT", "USD")
        assert _cdict(contract) == _cdict(expected)

    def test_index_qualification_failure_tolerated(self):
        """Index qualification may fail while reqHistoricalData still works —
        the tolerance branch must survive T2."""
        adapter, mgr = _make_data_feed_adapter("CL")
        mgr.qualify_contract.side_effect = ValueError("no qualification")
        adapter.subscribe_live_bars("VIX")  # must NOT raise
        contract = mgr.subscribe_live_bars.call_args.kwargs["contract"]
        assert contract.symbol == "VIX"
        assert contract.exchange == "CBOE"


# ===========================================================================
# Adapter front-month routing (audit test 14) + interface default drop (C6)
# ===========================================================================

class TestAdapterFrontMonth:
    @pytest.mark.parametrize(
        "symbol,local_sym,expected_exchange",
        [("ES", "ESZ5", "CME"), ("CL", "CLQ5", "NYMEX")],
    )
    def test_front_month_subscription_exchange_from_registry(
        self, symbol, local_sym, expected_exchange
    ):
        adapter, mgr = _make_data_feed_adapter("CL")
        mgr.get_front_month_contract.return_value = (local_sym, "202512")

        adapter.subscribe_live_bars(symbol, continuous=False)

        mgr.get_front_month_contract.assert_called_once_with(symbol=symbol)
        contract = mgr.subscribe_live_bars.call_args.kwargs["contract"]
        expected = Future(
            symbol=symbol, localSymbol=local_sym, exchange=expected_exchange
        )
        assert _cdict(contract) == _cdict(expected)

    def test_get_front_month_contract_default_dropped(self):
        """C6 honest diff: the abstract interface and the IBKR adapter drop
        the silent '= "CL"' default on get_front_month_contract."""
        abstract_param = inspect.signature(
            DataFeedClient.get_front_month_contract
        ).parameters["symbol"]
        adapter_param = inspect.signature(
            IBKRDataFeedClient.get_front_month_contract
        ).parameters["symbol"]
        assert abstract_param.default is inspect.Parameter.empty
        assert adapter_param.default is inspect.Parameter.empty


# ===========================================================================
# Adapter fetch delegations — symbol bound at construction (audit test 15)
# ===========================================================================

class TestAdapterFetchDelegation:
    @pytest.mark.parametrize(
        "execution_symbol,expected_brain",
        [("ES", "ES"), ("CL", "CL"), ("MCL", "CL")],
    )
    def test_fetch_delegation_uses_brain_symbol(
        self, execution_symbol, expected_brain
    ):
        adapter, mgr = _make_data_feed_adapter(execution_symbol)
        mgr.fetch_historical_bars.return_value = pd.DataFrame()
        mgr.fetch_historical_bars_by_duration.return_value = pd.DataFrame()
        mgr.fetch_historical_bars_by_duration_async = AsyncMock(
            return_value=pd.DataFrame()
        )

        adapter.fetch_historical_bars(days_back=3)
        kwargs = mgr.fetch_historical_bars.call_args.kwargs
        assert kwargs.get("symbol") == expected_brain
        assert kwargs.get("days_back") == 3

        adapter.fetch_historical_bars_by_duration(duration_str="2 D")
        kwargs = mgr.fetch_historical_bars_by_duration.call_args.kwargs
        assert kwargs.get("symbol") == expected_brain
        assert kwargs.get("duration_str") == "2 D"

        asyncio.run(adapter.fetch_historical_bars_by_duration_async(duration_str="2 D"))
        kwargs = mgr.fetch_historical_bars_by_duration_async.await_args.kwargs
        assert kwargs.get("symbol") == expected_brain


# ===========================================================================
# SimulatedDataFeed — byte-untouched (C6; parity harness dependency)
# ===========================================================================

class TestSimulatedDataFeedUnchanged:
    def test_constructor_signature_unchanged(self):
        params = list(inspect.signature(SimulatedDataFeed.__init__).parameters)
        assert params == ["self", "warmup_df", "replay_df"]
        df = _make_ohlcv_df(5, "2026-01-02")
        sim = SimulatedDataFeed(warmup_df=df, replay_df=df.iloc[0:0])
        assert sim.fetch_historical_bars_by_duration("1 D").equals(df)

    def test_fetch_and_front_month_signatures_unchanged(self):
        fetch_params = inspect.signature(
            SimulatedDataFeed.fetch_historical_bars_by_duration
        ).parameters
        assert "symbol" not in fetch_params
        fm_param = inspect.signature(
            SimulatedDataFeed.get_front_month_contract
        ).parameters["symbol"]
        assert fm_param.default == "CL"  # the sim keeps its own default


# ===========================================================================
# derive_data_paths (audit tests 16-18, condition C1)
# ===========================================================================

class TestDeriveDataPaths:
    def test_cl_paths_byte_identical_to_legacy_expressions(self):
        """THE regression pin (hard constraint 1): all 7 CL paths equal
        today's literals, expected values composed from the SAME
        get_data_path/get_data_root expressions currently in code (C1)."""
        expected = _expected_paths("CL")
        paths = derive_data_paths("CL")
        assert dataclasses.is_dataclass(paths)
        assert str(paths.seed_5m) == expected["seed_5m"]
        assert str(paths.cache_5m) == expected["cache_5m"]
        assert str(paths.ledger_5m) == expected["ledger_5m"]
        assert str(paths.seed_1h) == expected["seed_1h"]
        assert str(paths.cache_1h) == expected["cache_1h"]
        assert str(paths.ledger_1h) == expected["ledger_1h"]
        assert str(paths.roll_metadata) == expected["roll_metadata"]

    def test_es_paths_match_design_table(self):
        expected = _expected_paths("ES")
        paths = derive_data_paths("ES")
        assert str(paths.seed_5m) == expected["seed_5m"]  # raw/es-5m_bk.csv
        assert str(paths.cache_5m) == expected["cache_5m"]
        assert str(paths.ledger_5m) == expected["ledger_5m"]
        assert str(paths.seed_1h) == expected["seed_1h"]
        assert str(paths.cache_1h) == expected["cache_1h"]
        assert str(paths.ledger_1h) == expected["ledger_1h"]
        assert str(paths.roll_metadata) == expected["roll_metadata"]
        # Name-level pins from the ticket (independent of the data root):
        assert str(paths.cache_5m).endswith("warm_start_cache_ES.parquet")
        assert str(paths.ledger_5m).endswith("es_continuous_master.parquet")
        assert str(paths.seed_1h).endswith("ES_raw_1h.parquet")
        assert str(paths.cache_1h).endswith("warm_start_cache_ES_1h.parquet")
        assert str(paths.ledger_1h).endswith("es_continuous_master_1h.parquet")
        assert str(paths.roll_metadata).endswith(".roll_metadata_ES.json")
        assert str(paths.seed_5m).endswith("es-5m_bk.csv")

    def test_unknown_symbol_raises(self):
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            derive_data_paths("XX")

    @pytest.mark.parametrize(
        "symbol,cache_5m_name",
        [("CL", "warm_start_cache.parquet"), ("ES", "warm_start_cache_ES.parquet")],
    )
    def test_expression_fidelity_seed_via_get_data_path(self, symbol, cache_5m_name):
        """C1 asymmetry pin: ONLY the 5m seed goes through the existence-aware
        get_data_path(); the other six are composed from get_data_root().
        derive_data_paths lives in data_manager.py and consumes the module's
        get_data_path/get_data_root imports (audit D2) — patch them there."""
        sentinel_seed = Path("SENTINEL_SEED")
        sentinel_root = Path("SENTINEL_ROOT")
        with patch(
            "src.live_execution.data_manager.get_data_path",
            return_value=sentinel_seed,
        ) as mock_gdp, patch(
            "src.live_execution.data_manager.get_data_root",
            return_value=sentinel_root,
        ):
            paths = derive_data_paths(symbol)

        mock_gdp.assert_called_once_with(f"raw/{symbol.lower()}-5m_bk.csv")
        assert str(paths.seed_5m) == str(sentinel_seed)
        assert str(paths.cache_5m) == str(sentinel_root / "processed" / cache_5m_name)
        for field in ("ledger_5m", "seed_1h", "cache_1h", "ledger_1h", "roll_metadata"):
            assert str(getattr(paths, field)).startswith(
                str(sentinel_root / "processed")
            ), f"{field} must be composed from get_data_root()"


# ===========================================================================
# DataManager — required symbol + per-instance roll metadata
# (audit tests 19-21)
# ===========================================================================

class TestDataManagerSymbol:
    def test_requires_keyword_only_symbol(self, tmp_path):
        with pytest.raises(TypeError, match="symbol"):
            DataManager(
                seed_path=str(tmp_path / "seed.csv"),
                cache_path=str(tmp_path / "cache.parquet"),
                master_ledger_path=str(tmp_path / "ledger.parquet"),
                data_client=None,
            )

    def test_unknown_symbol_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            DataManager(**_dm_tmp_kwargs(tmp_path, symbol="XX"))

    def test_roll_metadata_path_derived_per_symbol(self, tmp_path):
        """CL keeps the legacy .roll_metadata.json; ES gets
        .roll_metadata_ES.json — derived structurally, per instance."""
        kwargs_cl = _dm_tmp_kwargs(tmp_path, symbol="CL")
        kwargs_cl.pop("roll_metadata_path")
        dm_cl = DataManager(**kwargs_cl)
        assert str(dm_cl.roll_metadata_path) == _expected_paths("CL")["roll_metadata"]

        kwargs_es = _dm_tmp_kwargs(tmp_path, symbol="ES")
        kwargs_es.pop("roll_metadata_path")
        dm_es = DataManager(**kwargs_es)
        assert str(dm_es.roll_metadata_path) == _expected_paths("ES")["roll_metadata"]

    def test_roll_metadata_override_wins_and_roundtrips(self, tmp_path):
        """Explicit roll_metadata_path override wins; save/load go through the
        instance attribute (replaces the module-global _ROLL_METADATA_PATH
        patched 8x in tests/test_rollover.py — C4)."""
        override = tmp_path / "custom_roll.json"
        dm = DataManager(
            **_dm_tmp_kwargs(
                tmp_path,
                symbol="CL",
                roll_metadata_path=str(override),
                front_month_id="CLZ5",
            )
        )
        assert str(dm.roll_metadata_path) == str(override)

        dm._save_roll_metadata()
        assert override.exists(), "save must write the instance path"
        saved = json.loads(override.read_text(encoding="utf-8"))
        assert saved["last_front_month"] == "CLZ5"

        dm2 = DataManager(
            **_dm_tmp_kwargs(
                tmp_path,
                symbol="CL",
                roll_metadata_path=str(override),
                front_month_id="CLF6",
            )
        )
        meta = dm2._load_roll_metadata()
        assert meta["last_front_month"] == "CLZ5"

    def test_missing_non_cl_seed_raises_actionable(self, tmp_path):
        """ES DataManager, no cache, no seed -> the existing hard
        FileNotFoundError fires with the DERIVED ES seed path in the message
        plus the CL_DATA_ROOT hint. Filesystem fully mocked."""
        kwargs = _dm_tmp_kwargs(tmp_path, symbol="ES")
        kwargs.pop("seed_path")  # force the derived ES default
        dm = DataManager(**kwargs)
        expected_seed = _expected_paths("ES")["seed_5m"]

        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(FileNotFoundError) as excinfo:
                dm.initialize()

        message = str(excinfo.value)
        assert expected_seed in message
        assert "CL_DATA_ROOT" in message


# ===========================================================================
# DataManager backfill/ledger fetches — symbol flows to IBKR through the
# data client (adapter-bound per audit D1/test 26; SimulatedDataFeed stays
# call-compatible per C6/C7)
# ===========================================================================

class TestDataManagerFetchSymbolFlow:
    def test_backfill_fetch_carries_symbol_through_data_client(self, tmp_path):
        """DataManager._backfill -> adapter -> IBKR: the ES-bound adapter must
        forward symbol='ES' on the DataManager-triggered backfill fetch."""
        adapter, mgr = _make_data_feed_adapter("ES")
        mgr.fetch_historical_bars_by_duration.return_value = pd.DataFrame()
        dm = DataManager(**_dm_tmp_kwargs(tmp_path, symbol="ES", data_client=adapter))
        dm._df = _make_ohlcv_df(30, "2020-01-01")

        dm._backfill()

        kwargs = mgr.fetch_historical_bars_by_duration.call_args.kwargs
        assert kwargs.get("symbol") == "ES"
        assert kwargs.get("continuous") is True

    def test_ledger_fetch_carries_symbol_through_data_client(self, tmp_path):
        adapter, mgr = _make_data_feed_adapter("ES")
        mgr.fetch_historical_bars_by_duration.return_value = pd.DataFrame()
        dm = DataManager(**_dm_tmp_kwargs(tmp_path, symbol="ES", data_client=adapter))

        result = dm._fetch_ibkr_range(pd.Timestamp("2020-01-01"))

        assert result is None  # empty IBKR response
        kwargs = mgr.fetch_historical_bars_by_duration.call_args.kwargs
        assert kwargs.get("symbol") == "ES"
        assert kwargs.get("continuous") is True

    def test_backfill_compatible_with_unchanged_simulated_feed(self, tmp_path):
        """C6/C7: the DataManager-facing fetch call must remain accepted by
        the UNTOUCHED SimulatedDataFeed (the parity harness path). If T2 made
        DataManager pass extra kwargs to the data client, this would
        TypeError and the HS14B parity gate would break."""
        warmup = _make_ohlcv_df(50, "2026-01-02")
        sim = SimulatedDataFeed(warmup_df=warmup, replay_df=warmup.iloc[0:0])
        dm = DataManager(**_dm_tmp_kwargs(tmp_path, symbol="CL", data_client=sim))
        dm._df = _make_ohlcv_df(10, "2020-01-01")

        dm._backfill()  # must not raise

        assert len(dm._df) > 10, "warmup bars must have been stitched"


# ===========================================================================
# LiveTrader — DataManager construction paths (audit tests 22-24)
# Seam mirrors tests/test_instrument_context.py::TestLiveTraderWiring;
# DataManager construction intercepted, 1h existence check controlled via
# pathlib.Path.exists.
# ===========================================================================

def _build_trader_with_mock_dm(cfg, tmp_path, exists=True, data_client=None):
    with patch("src.live_execution.live_trader.DataManager") as MockDM, patch(
        "pathlib.Path.exists", return_value=exists
    ):
        trader = LiveTrader(
            data_client=data_client if data_client is not None else MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(config=cfg),
            db_path=str(tmp_path / "telemetry.db"),
            dry_run=True,
        )
    trader._telegram = MagicMock()
    return trader, MockDM


def _cfg(symbol: str, **extra) -> dict:
    cfg = {
        "nickname": f"{symbol}_style",
        "execution_symbol": symbol,
        "bar_size": "1h",
    }
    cfg.update(extra)
    return cfg


class TestLiveTraderDataManagerPaths:
    def test_cl_config_datamanager_paths_unchanged(self, tmp_path):
        """Hard constraint 1: a CL 1h config constructs both DataManagers with
        exactly the legacy paths + legacy roll metadata, byte-identical."""
        expected = _expected_paths("CL")
        trader, MockDM = _build_trader_with_mock_dm(_cfg("CL"), tmp_path)

        assert MockDM.call_count == 2
        k5 = MockDM.call_args_list[0].kwargs
        k1 = MockDM.call_args_list[1].kwargs

        assert k5.get("symbol") == "CL"
        assert str(k5.get("seed_path")) == expected["seed_5m"]
        assert str(k5.get("cache_path")) == expected["cache_5m"]
        assert str(k5.get("master_ledger_path")) == expected["ledger_5m"]
        assert str(k5.get("roll_metadata_path")) == expected["roll_metadata"]
        assert k5.get("bar_size") == "5 mins"
        assert k5.get("bars_per_day") == 288
        assert k5.get("data_client") is trader.data_client

        assert k1.get("symbol") == "CL"
        assert str(k1.get("seed_path")) == expected["seed_1h"]
        assert str(k1.get("cache_path")) == expected["cache_1h"]
        assert str(k1.get("master_ledger_path")) == expected["ledger_1h"]
        # Same roll-metadata file as the 5m manager (intra-symbol sharing).
        assert str(k1.get("roll_metadata_path")) == expected["roll_metadata"]
        assert k1.get("bar_size") == "1 hour"
        assert k1.get("bars_per_day") == 24

    def test_es_config_datamanager_paths(self, tmp_path):
        expected = _expected_paths("ES")
        trader, MockDM = _build_trader_with_mock_dm(_cfg("ES"), tmp_path)

        assert MockDM.call_count == 2
        k5 = MockDM.call_args_list[0].kwargs
        k1 = MockDM.call_args_list[1].kwargs

        assert k5.get("symbol") == "ES"
        assert str(k5.get("seed_path")) == expected["seed_5m"]
        assert str(k5.get("cache_path")) == expected["cache_5m"]
        assert str(k5.get("master_ledger_path")) == expected["ledger_5m"]
        assert str(k5.get("roll_metadata_path")) == expected["roll_metadata"]

        assert k1.get("symbol") == "ES"
        assert str(k1.get("seed_path")) == expected["seed_1h"]
        assert str(k1.get("cache_path")) == expected["cache_1h"]
        assert str(k1.get("master_ledger_path")) == expected["ledger_1h"]
        assert str(k1.get("roll_metadata_path")) == expected["roll_metadata"]

    def test_mcl_config_shares_cl_data_paths(self, tmp_path):
        """D3: data paths are keyed by ctx.brain_symbol — an MCL config
        legitimately shares CL's legacy file set."""
        expected = _expected_paths("CL")
        trader, MockDM = _build_trader_with_mock_dm(_cfg("MCL"), tmp_path)

        k5 = MockDM.call_args_list[0].kwargs
        assert k5.get("symbol") == "CL"
        assert str(k5.get("seed_path")) == expected["seed_5m"]
        assert str(k5.get("cache_path")) == expected["cache_5m"]
        assert str(k5.get("roll_metadata_path")) == expected["roll_metadata"]

    def test_seed_path_1h_override_absolute_wins(self, tmp_path):
        override = tmp_path / "custom_es_1h.parquet"
        cfg = _cfg("ES", live_config={"seed_path_1h": str(override)})
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)

        k1 = MockDM.call_args_list[1].kwargs
        assert str(k1.get("seed_path")) == str(override)

    def test_seed_path_1h_override_relative_resolves_against_data_root(
        self, tmp_path
    ):
        cfg = _cfg("ES", live_config={"seed_path_1h": "processed/custom_es_1h.parquet"})
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)

        k1 = MockDM.call_args_list[1].kwargs
        expected = str(get_data_root() / "processed/custom_es_1h.parquet")
        assert str(k1.get("seed_path")) == expected

    def test_es_missing_1h_seed_raises_with_es_paths(self, tmp_path):
        """Missing non-CL 1h cache AND seed -> the existing hard
        FileNotFoundError, message naming the DERIVED ES paths."""
        with patch("src.live_execution.live_trader.DataManager"), patch(
            "pathlib.Path.exists", return_value=False
        ):
            with pytest.raises(FileNotFoundError) as excinfo:
                LiveTrader(
                    data_client=MagicMock(),
                    exec_client=MagicMock(),
                    strategy=DummyStrategy(config=_cfg("ES")),
                    db_path=str(tmp_path / "telemetry.db"),
                    dry_run=True,
                )
        message = str(excinfo.value)
        assert "warm_start_cache_ES_1h.parquet" in message
        assert "ES_raw_1h.parquet" in message


# ===========================================================================
# LiveTrader — brain-stream symbols (audit tests 25-26, constraint 3)
# ===========================================================================

class TestLiveTraderBrainStream:
    def test_cl_config_stream_symbols(self, tmp_path):
        trader, _ = _build_trader_with_mock_dm(_cfg("CL"), tmp_path)

        trader._subscribe()
        calls = trader.data_client.subscribe_live_bars.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs.get("symbol") == "CL"
        assert calls[0].kwargs.get("continuous") is True
        assert calls[0].kwargs.get("bar_size") == "5 mins"
        assert calls[1].kwargs.get("symbol") == "CL"
        assert calls[1].kwargs.get("continuous") is True
        assert calls[1].kwargs.get("bar_size") == "1 hour"

        trader._subscribe_front_month()
        fm_call = trader.data_client.subscribe_live_bars.call_args_list[2]
        assert fm_call.kwargs.get("symbol") == "CL"
        assert fm_call.kwargs.get("continuous") is False

    def test_mcl_config_brain_cl_hands_mcl(self, tmp_path):
        """Constraint 3 (manager-ACKed behavior change): an MCL config's
        brain streams subscribe CL continuous; the Hands stream stays MCL."""
        trader, _ = _build_trader_with_mock_dm(_cfg("MCL"), tmp_path)

        trader._subscribe()
        calls = trader.data_client.subscribe_live_bars.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs.get("symbol") == "CL"
        assert calls[0].kwargs.get("continuous") is True
        assert calls[1].kwargs.get("symbol") == "CL"
        assert calls[1].kwargs.get("continuous") is True

        trader._subscribe_front_month()
        fm_call = trader.data_client.subscribe_live_bars.call_args_list[2]
        assert fm_call.kwargs.get("symbol") == "MCL"
        assert fm_call.kwargs.get("continuous") is False

    def test_deferred_resubscribe_async_twin_symbols(self, tmp_path):
        trader, _ = _build_trader_with_mock_dm(_cfg("MCL"), tmp_path)
        dc = trader.data_client
        dc.subscribe_live_bars_async = AsyncMock(return_value=MagicMock())
        trader._front_month_local_symbol = "MCLQ6"
        trader._front_month_str = "MCLQ6"

        asyncio.run(trader._deferred_resubscribe())

        awaits = dc.subscribe_live_bars_async.await_args_list
        assert len(awaits) == 3
        assert awaits[0].kwargs.get("symbol") == "CL"       # 5m brain
        assert awaits[0].kwargs.get("continuous") is True
        assert awaits[1].kwargs.get("symbol") == "CL"       # 1h brain
        assert awaits[1].kwargs.get("continuous") is True
        assert awaits[2].kwargs.get("symbol") == "MCL"      # hands
        assert awaits[2].kwargs.get("continuous") is False

    def test_reconnect_backfill_adapter_bound_no_symbol_kwarg(self, tmp_path):
        """Audit test 26: _backfill_reconnect_gap_async still calls the
        adapter's fetch_historical_bars_by_duration_async with NO symbol kwarg
        (the symbol is adapter-bound; the call stays SimulatedDataFeed-
        compatible)."""
        trader, _ = _build_trader_with_mock_dm(_cfg("CL"), tmp_path)
        dc = trader.data_client
        dc.fetch_historical_bars_by_duration_async = AsyncMock(
            return_value=pd.DataFrame()
        )
        trader._last_bar_time_5m = pd.Timestamp.now() - pd.Timedelta(hours=3)
        trader._last_bar_time_1h = None

        asyncio.run(trader._backfill_reconnect_gap_async())

        call = dc.fetch_historical_bars_by_duration_async.await_args
        assert call is not None, "reconnect backfill must still fetch"
        assert call.kwargs.get("continuous") is True
        assert call.kwargs.get("bar_size") == "5 mins"
        assert "duration_str" in call.kwargs
        assert "symbol" not in call.kwargs


# ===========================================================================
# CLI wiring (audit tests 27-30, condition C8)
# ===========================================================================

_CL_CLI_CFG = {
    "nickname": "HS14B_style",
    "execution_symbol": "CL",
    "bar_size": "1h",
}
_ES_CLI_CFG = {
    "nickname": "ES_style",
    "execution_symbol": "ES",
    "bar_size": "1h",
}
_MCL_CLI_CFG = {
    "nickname": "MCL_style",
    "execution_symbol": "MCL",
    "bar_size": "1h",
}


def _run_cli(config_dict: dict, argv_extra: list[str] | None = None):
    from src.live_execution import cli

    argv = ["cli", "--config", "dummy_config.json"] + (argv_extra or [])
    stub_strategy = SimpleNamespace(
        config=config_dict,
        feature_names=[],
        name="StubStrategy",
        direction="LONG",
    )
    with patch(
        "src.live_execution.strategies.configurable_strategy.ConfigurableStrategy",
        return_value=stub_strategy,
    ), patch(
        "src.live_execution.factories.DataFeedFactory.create",
        return_value=MagicMock(),
    ) as mock_data_create, patch(
        "src.live_execution.factories.ExecutionFactory.create",
        return_value=MagicMock(),
    ) as mock_exec_create, patch(
        "src.live_execution.live_trader.LiveTrader"
    ) as mock_trader_cls, patch(
        "src.live_execution.cli._setup_file_logging"
    ), patch(
        "src.live_execution.cli._merge_legacy_cid_caches"
    ) as mock_merge, patch(
        "sys.argv", argv
    ):
        cli.main()

    return SimpleNamespace(
        trader_cls=mock_trader_cls,
        data_create=mock_data_create,
        exec_create=mock_exec_create,
        merge=mock_merge,
    )


class TestCliPaths:
    def test_cli_default_paths_cl_byte_identical(self):
        """CL config, no --seed-path/--cache-path -> LiveTrader receives the
        legacy strings byte-for-byte (same get_data_path/get_data_root
        expressions as today's baked defaults)."""
        expected = _expected_paths("CL")
        res = _run_cli(_CL_CLI_CFG)

        kwargs = res.trader_cls.call_args.kwargs
        assert str(kwargs.get("seed_path")) == expected["seed_5m"]
        assert str(kwargs.get("cache_path")) == expected["cache_5m"]

    def test_cli_explicit_paths_win(self):
        res = _run_cli(
            _ES_CLI_CFG,
            argv_extra=[
                "--seed-path", "EXPLICIT_SEED.csv",
                "--cache-path", "EXPLICIT_CACHE.parquet",
            ],
        )
        kwargs = res.trader_cls.call_args.kwargs
        assert kwargs.get("seed_path") == "EXPLICIT_SEED.csv"
        assert kwargs.get("cache_path") == "EXPLICIT_CACHE.parquet"

    def test_cli_es_derived_paths_and_factory_receives_context(self):
        expected = _expected_paths("ES")
        res = _run_cli(_ES_CLI_CFG)

        kwargs = res.trader_cls.call_args.kwargs
        assert str(kwargs.get("seed_path")) == expected["seed_5m"]
        assert str(kwargs.get("cache_path")) == expected["cache_5m"]

        ctx = res.data_create.call_args.kwargs.get("instrument_context")
        assert ctx is not None, "DataFeedFactory.create must receive the context"
        assert ctx.execution_symbol == "ES"
        assert ctx.brain_symbol == "ES"

    def test_cli_cid_merge_called_for_cl(self):
        """Legacy cid-cache merge still runs for CL instances with
        client_id != 1, against the shared legacy cache path."""
        cfg = dict(_CL_CLI_CFG, live_config={"client_id": 7})
        res = _run_cli(cfg)

        res.merge.assert_called_once()
        merged_path = res.merge.call_args[0][0]
        assert str(merged_path) == _expected_paths("CL")["cache_5m"]

    def test_cli_cid_merge_not_called_for_es(self):
        """C8/section 1 hazard: legacy cid caches are CL bars — merging them
        into an ES cache is silent misdata. The gate must skip ES."""
        cfg = dict(_ES_CLI_CFG, live_config={"client_id": 7})
        res = _run_cli(cfg)

        res.merge.assert_not_called()

    def test_cli_cid_merge_still_called_for_mcl(self):
        """C8: the gate keys on ctx.brain_symbol == "CL" (NOT the execution
        symbol) — an MCL instance shares the CL cache and must still merge."""
        cfg = dict(_MCL_CLI_CFG, live_config={"client_id": 7})
        res = _run_cli(cfg)

        res.merge.assert_called_once()
        merged_path = res.merge.call_args[0][0]
        assert str(merged_path) == _expected_paths("CL")["cache_5m"]
