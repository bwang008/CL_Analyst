"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/data_manager.py
                            (DataManager.__init__ gains KEYWORD-ONLY
                            allow_shallow_bootstrap: bool = False, stored,
                            plus instance attr shallow_bootstrapped=False;
                            initialize() Step-1 else-branch: when allowed
                            AND data_client is not None ->
                            _shallow_bootstrap_from_ibkr(): ONE non-chunked
                            fetch_historical_bars_by_duration(
                            duration_str="5 D", continuous=True,
                            bar_size=self.bar_size, what_to_show="TRADES",
                            use_rth=False), _drop_incomplete_bar,
                            RuntimeError on empty BEFORE any save_cache()
                            (C3), then save_cache(); otherwise the
                            BYTE-IDENTICAL FileNotFoundError of
                            data_manager.py:303-308 (C2 — allow=True with
                            data_client None still raises it);
                            _update_training_ledger(): first-run ledger
                            CREATION branch gated while seedless
                            (allow_shallow_bootstrap AND seed absent) with
                            a LOUD skip log — run 2+ (cache present, seed
                            absent, ledger absent) must boot cleanly (C5)),
                            src/live_execution/live_trader.py
                            (5m DataManager constructed with
                            allow_shallow_bootstrap=(self._bar_size in
                            ("1h","2h","4h")); _shallow_5m_bootstrap state
                            flag; _warm_start reads
                            data_manager_5m.shallow_bootstrapped and emits
                            a loud SHALLOW 5M banner; start() Telegram
                            Mode stamp mirrors the T7 HOURLY-ONLY block),
                            configs/strategies/ES01B_Sharpe_E03_07042026.json
                            (REMOVE the "enable_5m_stream": false key —
                            Coder owns the config edit, SAME COMMIT as the
                            code per impact_review C1; this file only pins
                            the outcome)
Target Class/Function: DataManager.__init__ / initialize /
                       _shallow_bootstrap_from_ibkr /
                       _update_training_ledger,
                       LiveTrader.__init__ / _warm_start / _check_stale_bars
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: seedless-5m-live-stream_07052026_0546
Phase: RED — at HEAD f165b9d:
  - DataManager.__init__ has no allow_shallow_bootstrap parameter: every
    construction passing it fails with TypeError (unexpected keyword
    argument), and the signature-default / shallow_bootstrapped attribute
    pins fail with AttributeError.
  - initialize() Step-1 else-branch raises the No-Silent-Bootstrap
    FileNotFoundError unconditionally — the happy-path / empty-fetch /
    run-2 tests fail before any bootstrap machinery exists.
  - live_trader constructs the 5m DataManager WITHOUT the wiring kwarg
    (kwargs.get returns None, not True/False) and never sets
    _shallow_5m_bootstrap.
  - The shipped ES01B config still carries "enable_5m_stream": false —
    the shipped-config default-5m pins fail until the Coder removes the
    key (same commit as the code, C1).
  Deliberate pins that pass at Red AND Green (documented per-test):
  the byte-identical default-path raise, the T7 synthetic flag-false
  opt-out, and the MES trailing frame-presence pin.

Governing documents (this file transcribes their pins, it does not invent):
  - blueprint.md (seedless-5m-live-stream_07052026_0546) — GOVERNING on
    conflict: hard constraints (CL byte-identity, no silent forks, T7
    opt-out stays green, scope guards) + the test requirements list.
  - audit.md §5 — file-by-file design (5.1 data_manager, 5.2 live_trader,
    5.3 ES01B key removal, 5.5 TDD list) and §1.4 ledger-skip rule.
  - impact_review.md §4 binding conditions C1-C6 — C2 byte-pins, C3 single
    "5 D" fetch + raise-before-save, C4 test-name honesty (the renamed
    key-absent pin lives in tests/test_hourly_only_equity_session.py),
    C5 run-2 seedless ledger pin (THE regression pin of this ticket).

All I/O boundaries are mocked or tmp_path-local: MagicMock data clients
(fetch_historical_bars_by_duration stubbed with deterministic frames in the
exact ib_bars_to_dataframe shape — DateTime as BOTH index and column), all
seed/cache/ledger/roll-metadata paths passed explicitly into tmp_path, no
IBKR gateway, no Telegram (MagicMock), no real data files. LiveTrader
construction reuses the T5/T7 seams imported from
tests/test_hourly_only_equity_session.py (tests/ is a package) — reuse, not
duplication.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.live_execution.data_manager import DataManager

# T5/T7 construction seams — reused, not duplicated (tests/ is a package).
# test_hourly_only_equity_session.py is Strict-Lock; importing is read-only.
from tests.test_hourly_only_equity_session import (
    _bar_df,
    _build_trader_with_mock_dm,
    _es_hourly_only_cfg,
    _frozen_lt_clock,
    _load_es01b,
    _make_inposition_trader,
    _mock_1h_manager,
    _utc_from_ct,
    _watchdog_stub,
)

_TICKET = "seedless-5m-live-stream_07052026_0546"


# ---------------------------------------------------------------------------
# Helpers (tests/test_data_manager.py construction conventions)
# ---------------------------------------------------------------------------

def _write_cl_seed_csv(path: Path, n_days: int = 3) -> None:
    """Mock CL seed CSV in the legacy semicolon format (deterministic —
    no RNG): Date;Time;Open;High;Low;Close;Volume."""
    n_bars = n_days * 288
    start = datetime(2026, 6, 1)
    rows = []
    price = 70.0
    for i in range(n_bars):
        dt = start + timedelta(minutes=5 * i)
        price += 0.001
        rows.append(
            f"{dt.strftime('%d/%m/%Y')};{dt.strftime('%H:%M')};"
            f"{price:.4f};{price + 0.02:.4f};{price - 0.02:.4f};"
            f"{price:.4f};1000"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _ibkr_5m_frame(n_bars: int = 1440) -> pd.DataFrame:
    """A ~5-day 5m frame in the EXACT ib_bars_to_dataframe shape (audit
    §1.2: DateTime as BOTH index and column). Ends on the last COMPLETE
    5m bar so _drop_incomplete_bar is a no-op and the post-bootstrap gap
    stays < 10 min (backfill fast-path)."""
    end = (
        pd.Timestamp.now(tz="UTC").tz_localize(None).floor("5min")
        - pd.Timedelta(minutes=5)
    )
    idx = pd.date_range(end=end, periods=n_bars, freq="5min")
    idx.name = "DateTime"
    close = np.linspace(5300.0, 5310.0, n_bars)
    return pd.DataFrame(
        {
            "DateTime": idx,
            "Open": close - 0.25,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": 100.0,
        },
        index=idx,
    )


def _mock_feed(frame: pd.DataFrame) -> MagicMock:
    """Data client whose duration fetch returns a fresh copy per call
    (keyword-only, matching the pinned call shape)."""
    client = MagicMock()
    client.fetch_historical_bars_by_duration.side_effect = (
        lambda **kw: frame.copy()
    )
    return client


def _write_cache_parquet(path: Path, n_bars: int = 600) -> None:
    """Write a warm-start cache in save_cache's exact on-disk shape
    (DateTime as a plain column, index dropped), ending on the last
    complete 5m bar so run-2 backfill takes the <10-min fast path."""
    end = (
        pd.Timestamp.now(tz="UTC").tz_localize(None).floor("5min")
        - pd.Timedelta(minutes=5)
    )
    idx = pd.date_range(end=end, periods=n_bars, freq="5min")
    close = np.linspace(5200.0, 5210.0, n_bars)
    df = pd.DataFrame(
        {
            "DateTime": idx,
            "Open": close,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": 50.0,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, engine="pyarrow")


def _small_ohlcv(n_bars: int = 10) -> pd.DataFrame:
    """Tiny OHLCV frame with DateTime index+column (ledger seed stub)."""
    idx = pd.date_range("2026-05-01", periods=n_bars, freq="5min")
    idx.name = "DateTime"
    return pd.DataFrame(
        {
            "DateTime": idx,
            "Open": 70.0,
            "High": 70.5,
            "Low": 69.5,
            "Close": 70.2,
            "Volume": 100.0,
        },
        index=idx,
    )


def _es_paths(tmp_path: Path) -> dict:
    """Explicit tmp-local ES artifact paths (nothing exists until a test
    creates it — no machine-state dependence)."""
    return dict(
        seed_path=str(tmp_path / "raw" / "es-5m_bk.csv"),
        cache_path=str(tmp_path / "processed" / "warm_start_cache_ES.parquet"),
        master_ledger_path=str(
            tmp_path / "processed" / "es_continuous_master.parquet"
        ),
        roll_metadata_path=str(
            tmp_path / "processed" / ".roll_metadata_ES.json"
        ),
    )


def _cl_paths(tmp_path: Path) -> dict:
    return dict(
        seed_path=str(tmp_path / "raw" / "cl-5m_bk.csv"),
        cache_path=str(tmp_path / "processed" / "warm_start_cache.parquet"),
        master_ledger_path=str(
            tmp_path / "processed" / "cl_continuous_master.parquet"
        ),
        roll_metadata_path=str(tmp_path / "processed" / ".roll_metadata.json"),
    )


def _expected_seed_missing_message(symbol: str, seed_path: str) -> str:
    """TRANSCRIBED byte-for-byte from data_manager.py:303-308 at HEAD
    f165b9d (impact_review C2: the default-path/5m-model raise message
    stays BYTE-IDENTICAL — this helper is the pin)."""
    return (
        f"Seed file not found for {symbol}: {Path(seed_path)}\n"
        f"The warm-start seed must exist before the live trader starts.\n"
        f"Check CL_DATA_ROOT ({os.environ.get('CL_DATA_ROOT', '(not set)')}) "
        f"and verify the file is present at the expected path."
    )


# ===========================================================================
# 1. TestShallowBootstrapDataManager — DataManager-level design (audit §5.1)
# ===========================================================================

class TestShallowBootstrapDataManager:
    """Ticket seedless-5m-live-stream_07052026_0546 — DataManager pins."""

    # -- CL-with-seed byte-identity pins (blueprint hard constraint 1) ----

    def test_cl_seed_branch_default_param_never_bootstraps(
        self, tmp_path, caplog
    ):
        """CL with a seed present: initialize() takes the seed->cache
        branch exactly as at HEAD; the new param DEFAULTS False (signature
        pin) and the bootstrap is never consulted. At Red the
        allow_shallow_bootstrap / shallow_bootstrapped attribute pins fail
        (AttributeError — the param does not exist yet)."""
        paths = _cl_paths(tmp_path)
        _write_cl_seed_csv(Path(paths["seed_path"]))
        with patch.object(
            DataManager, "_shallow_bootstrap_from_ibkr", create=True
        ) as boot:
            dm = DataManager(symbol="CL", data_client=None, **paths)
            with caplog.at_level(logging.INFO):
                df = dm.initialize()
            boot.assert_not_called()
        assert Path(paths["cache_path"]).exists(), (
            "seed branch must still create the cache (byte-identical "
            "pipeline)"
        )
        assert len(df) > 0
        assert dm.allow_shallow_bootstrap is False, (
            "allow_shallow_bootstrap must DEFAULT False at the DataManager "
            "level (signature pin — every existing construction site stays "
            "byte-identical)"
        )
        assert dm.shallow_bootstrapped is False
        assert not any(
            "SHALLOW" in r.getMessage() for r in caplog.records
        ), "no SHALLOW disclosure may fire on the seed branch"

    def test_cl_cache_present_allow_true_is_dead_code(self, tmp_path):
        """CL-with-cache + allow_shallow_bootstrap=True (the 1h-model
        wiring value): the cache branch wins, the bootstrap is DEAD CODE
        (presence-conditional — impact_review §1.6), and the window is
        byte-identical to the default construction. Red: TypeError
        (unexpected keyword argument allow_shallow_bootstrap)."""
        paths = _cl_paths(tmp_path)
        _write_cl_seed_csv(Path(paths["seed_path"]))
        dm1 = DataManager(symbol="CL", data_client=None, **paths)
        n1 = len(dm1.initialize())
        assert Path(paths["cache_path"]).exists()

        with patch.object(
            DataManager, "_shallow_bootstrap_from_ibkr", create=True
        ) as boot:
            dm2 = DataManager(
                symbol="CL",
                data_client=None,
                allow_shallow_bootstrap=True,
                **paths,
            )
            df2 = dm2.initialize()
            boot.assert_not_called()
        assert len(df2) == n1, (
            "allow=True over an existing cache must construct the "
            "byte-identical window (bar count pin)"
        )
        assert dm2.shallow_bootstrapped is False

    # -- Shallow bootstrap happy path (C3) --------------------------------

    def test_happy_path_bootstraps_once_saves_cache_loudly(
        self, tmp_path, caplog
    ):
        """No seed, no cache, allow=True, mocked client returns ~5 days of
        5m bars: initialize() succeeds, the rolling window is populated,
        shallow_bootstrapped is True, the cache file is WRITTEN (run 2+
        self-healing), the bootstrap is exactly ONE non-chunked "5 D"
        fetch (C3), and the mode is LOUDLY disclosed. Red: TypeError on
        the construction kwarg."""
        paths = _es_paths(tmp_path)
        client = _mock_feed(_ibkr_5m_frame())
        dm = DataManager(
            symbol="ES",
            data_client=client,
            bar_size="5 mins",
            allow_shallow_bootstrap=True,
            **paths,
        )
        with caplog.at_level(logging.INFO):
            df = dm.initialize()

        assert dm.shallow_bootstrapped is True
        assert Path(paths["cache_path"]).exists(), (
            "the bootstrap must save_cache() so run 2+ warm-starts from "
            "the cache"
        )
        assert len(df) >= 1400, (
            f"rolling window must carry the fetched ~5-day window, got "
            f"{len(df)} bars"
        )
        assert df.index.is_monotonic_increasing

        calls = client.fetch_historical_bars_by_duration.call_args_list
        five_d = [c for c in calls if c.kwargs.get("duration_str") == "5 D"]
        assert len(five_d) == 1, (
            f"C3: the bootstrap must be exactly ONE non-chunked '5 D' "
            f"fetch, got {len(five_d)} (all calls: "
            f"{[c.kwargs.get('duration_str') for c in calls]})"
        )
        kw = five_d[0].kwargs
        assert kw.get("continuous") is True
        assert kw.get("bar_size") == "5 mins"
        assert kw.get("what_to_show") == "TRADES"
        assert kw.get("use_rth") is False

        assert any(
            "SHALLOW 5M BOOTSTRAP" in r.getMessage() for r in caplog.records
        ), "the shallow mode must be loudly disclosed (SHALLOW 5M BOOTSTRAP)"

    def test_empty_fetch_raises_before_any_cache_write(self, tmp_path):
        """Empty IBKR return -> RuntimeError BEFORE any save_cache() (C3:
        never a silent empty window, never a cache founded on nothing).
        Red: TypeError on the construction kwarg."""
        paths = _es_paths(tmp_path)
        client = _mock_feed(pd.DataFrame())
        dm = DataManager(
            symbol="ES",
            data_client=client,
            bar_size="5 mins",
            allow_shallow_bootstrap=True,
            **paths,
        )
        with pytest.raises(RuntimeError):
            dm.initialize()
        assert not Path(paths["cache_path"]).exists(), (
            "C3: the empty-fetch RuntimeError must fire BEFORE any cache "
            "write — no cache file may exist"
        )

    # -- Byte-identical raise pins (C2) ------------------------------------

    def test_allow_true_without_data_client_keeps_byte_identical_raise(
        self, tmp_path
    ):
        """allow=True + data_client=None + no seed/cache: a bootstrap that
        CANNOT fetch must not soft-succeed — the BYTE-IDENTICAL
        FileNotFoundError of data_manager.py:303-308 survives (C2).
        Red: TypeError on the construction kwarg."""
        paths = _es_paths(tmp_path)
        dm = DataManager(
            symbol="ES",
            data_client=None,
            bar_size="5 mins",
            allow_shallow_bootstrap=True,
            **paths,
        )
        with pytest.raises(FileNotFoundError) as excinfo:
            dm.initialize()
        assert str(excinfo.value) == _expected_seed_missing_message(
            "ES", paths["seed_path"]
        ), "C2: the raise message must stay byte-identical"

    def test_default_allow_false_seedless_raise_byte_identical_guard_pin(
        self, tmp_path
    ):
        """Default construction (no kwarg) + no seed/cache + a live data
        client: the raise fires byte-identically and the client is never
        consulted — the dormant 5m-model NaN-feature guard. The raise
        itself passes at Red; the signature-default attribute pin fails
        (AttributeError) until the param exists."""
        paths = _es_paths(tmp_path)
        client = MagicMock()
        dm = DataManager(
            symbol="ES", data_client=client, bar_size="5 mins", **paths
        )
        assert dm.allow_shallow_bootstrap is False, (
            "allow_shallow_bootstrap must default False (guard pin)"
        )
        with pytest.raises(FileNotFoundError) as excinfo:
            dm.initialize()
        assert str(excinfo.value) == _expected_seed_missing_message(
            "ES", paths["seed_path"]
        )
        client.fetch_historical_bars_by_duration.assert_not_called()

    # -- C5: the run-2 pin (THE test) --------------------------------------

    def test_run2_cache_present_seed_ledger_absent_boots_cleanly(
        self, tmp_path, caplog
    ):
        """C5 (impact_review §4.5 — the highest-value regression pin of
        this ticket): run 2+ state = cache PRESENT, seed ABSENT, master
        ledger ABSENT, allow=True. initialize() must boot CLEANLY through
        the cache branch: NO bootstrap call, NO FileNotFoundError from
        _update_training_ledger's first-run _load_full_seed() branch
        (audit §1.1 second raise site), ledger accrual SKIPPED with a loud
        log, no ledger file founded. Red: TypeError on the construction
        kwarg."""
        paths = _es_paths(tmp_path)
        _write_cache_parquet(Path(paths["cache_path"]))
        client = _mock_feed(pd.DataFrame())  # backfill no-op if consulted
        with patch.object(
            DataManager, "_shallow_bootstrap_from_ibkr", create=True
        ) as boot:
            dm = DataManager(
                symbol="ES",
                data_client=client,
                bar_size="5 mins",
                allow_shallow_bootstrap=True,
                **paths,
            )
            with caplog.at_level(logging.INFO):
                df = dm.initialize()  # MUST NOT raise — the C5 crash
            boot.assert_not_called()

        assert len(df) > 0
        assert dm.shallow_bootstrapped is False, (
            "run 2 is a normal cache warm-start, not a bootstrap"
        )
        assert not Path(paths["master_ledger_path"]).exists(), (
            "no training ledger may be founded while seedless "
            "(no-silent-misdata, audit §1.4)"
        )
        msgs = [r.getMessage().lower() for r in caplog.records]
        assert any(("ledger" in m) and ("skip" in m) for m in msgs), (
            "the seedless ledger-accrual skip must be LOUDLY logged"
        )

    # -- Ledger-gate correctness (audit §1.4 rule) --------------------------

    def test_seedless_gate_never_calls_load_full_seed(self, tmp_path, caplog):
        """While seedless (allow=True, seed absent, ledger absent),
        _update_training_ledger must return WITHOUT attempting
        _load_full_seed() and without founding a ledger, logging the skip
        loudly. Red: TypeError on the construction kwarg."""
        paths = _es_paths(tmp_path)
        dm = DataManager(
            symbol="ES",
            data_client=MagicMock(),
            bar_size="5 mins",
            allow_shallow_bootstrap=True,
            **paths,
        )
        with patch.object(dm, "_load_full_seed") as loader, patch.object(
            dm, "_save_ledger"
        ) as saver:
            with caplog.at_level(logging.INFO):
                dm._update_training_ledger()
        loader.assert_not_called()
        saver.assert_not_called()
        assert not Path(paths["master_ledger_path"]).exists()
        msgs = [r.getMessage().lower() for r in caplog.records]
        assert any(("ledger" in m) and ("skip" in m) for m in msgs)

    def test_cl_with_seed_still_accrues_ledger_spy_pin(self, tmp_path):
        """CL byte-identity for the ledger gate: seed PRESENT (gate
        predicate 'allow AND seed absent' is False even with allow=True)
        -> the first-run creation branch still loads the full seed and
        saves the ledger exactly as at HEAD. Red: TypeError on the
        construction kwarg."""
        paths = _cl_paths(tmp_path)
        _write_cl_seed_csv(Path(paths["seed_path"]))
        dm = DataManager(
            symbol="CL",
            data_client=MagicMock(),
            allow_shallow_bootstrap=True,
            **paths,
        )
        with patch.object(
            dm, "_load_full_seed", return_value=_small_ohlcv()
        ) as loader, patch.object(
            dm, "_fetch_ibkr_range", return_value=None
        ), patch.object(dm, "_save_ledger") as saver:
            dm._update_training_ledger()
        loader.assert_called_once()
        saver.assert_called_once()


# ===========================================================================
# 2. TestLiveTraderShallowWiring — live_trader wiring + disclosure (audit
#    §5.2) and the ES01B default-5m outcome (audit §5.3, C1)
# ===========================================================================

def _shallow_warm_start_trader(tmp_path, *, shallow: bool):
    """Narrowest _warm_start seam: real 1h-config construction (mocked
    DataManager class), then stub managers — the 5m manager reports
    shallow_bootstrapped per the parameter."""
    cfg = {
        "nickname": "ES_shallow_banner_style",
        "execution_symbol": "ES",
        "bar_size": "1h",
    }
    trader, _ = _build_trader_with_mock_dm(cfg, tmp_path)
    dm5 = MagicMock()
    dm5.initialize.return_value = _ibkr_5m_frame(n_bars=64)
    dm5.shallow_bootstrapped = shallow
    trader.data_manager_5m = dm5
    trader.data_manager_1h = _mock_1h_manager(tmp_path)
    return trader


class TestLiveTraderShallowWiring:
    """Ticket seedless-5m-live-stream_07052026_0546 — LiveTrader pins."""

    @pytest.mark.parametrize("bar_size", ["1h", "2h", "4h"])
    def test_hourly_config_wires_allow_shallow_bootstrap_true(
        self, tmp_path, bar_size
    ):
        """Hourly models (the exact T7 flag-legal set 1h/2h/4h) construct
        the 5m DataManager with allow_shallow_bootstrap=True — only 5m
        MODELS consume deep 5m features. The DEEP 1h manager gets no
        shallow wiring (its hard seed requirement is untouched). Red: the
        kwarg is absent from the construction call (None is not True)."""
        cfg = {
            "nickname": f"ES_shallow_wiring_{bar_size}",
            "execution_symbol": "ES",
            "bar_size": bar_size,
        }
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)
        assert MockDM.call_count == 2
        first, second = MockDM.call_args_list
        assert first.kwargs.get("bar_size") == "5 mins"
        assert first.kwargs.get("symbol") == "ES"
        assert first.kwargs.get("allow_shallow_bootstrap") is True, (
            f"{bar_size} config must construct the 5m DataManager with "
            f"allow_shallow_bootstrap=True, got "
            f"{first.kwargs.get('allow_shallow_bootstrap')!r}"
        )
        assert second.kwargs.get("bar_size") == "1 hour"
        assert second.kwargs.get("allow_shallow_bootstrap") in (None, False), (
            "the 1h manager's deep window stays hard-required — no shallow "
            "bootstrap wiring on the 1h construction"
        )

    def test_5m_config_wires_allow_false_nan_guard(self, tmp_path):
        """bar_size '5m' -> allow_shallow_bootstrap=False: a seedless 5m
        MODEL still hard-raises (the parity-note NaN-feature guard stays
        dormant but intact). Red: kwarg absent (None is not False)."""
        cfg = {
            "nickname": "CL_5m_style",
            "execution_symbol": "CL",
            "bar_size": "5m",
        }
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)
        assert MockDM.call_count == 1
        only = MockDM.call_args_list[0]
        assert only.kwargs.get("bar_size") == "5 mins"
        assert only.kwargs.get("allow_shallow_bootstrap") is False, (
            "5m-model config must pass allow_shallow_bootstrap=False "
            f"explicitly, got {only.kwargs.get('allow_shallow_bootstrap')!r}"
        )

    # -- ES01B shipped-config outcome (key removed by the Coder — C1) -----

    def test_es01b_shipped_config_constructs_5m_manager_by_default(
        self, tmp_path
    ):
        """The shipped ES01B config (enable_5m_stream key REMOVED) rides
        the default-true path: the 5m DataManager IS constructed (brain
        ES, execution MES) with allow_shallow_bootstrap=True (bar_size
        1h). FAILS at Red: the config still carries the false key, so
        _enable_5m_stream resolves False and data_manager_5m is None —
        green only when the Coder removes the key in the SAME commit as
        the code (C1)."""
        cfg = _load_es01b()
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)
        assert trader._enable_5m_stream is True, (
            "ES01B must resolve enable_5m_stream True by DEFAULT (key "
            "removed — C1/C4)"
        )
        assert trader.data_manager_5m is not None
        assert MockDM.call_count == 2
        first = MockDM.call_args_list[0]
        assert first.kwargs.get("symbol") == "ES"  # brain stream
        assert first.kwargs.get("execution_symbol") == "MES"
        assert first.kwargs.get("bar_size") == "5 mins"
        assert first.kwargs.get("allow_shallow_bootstrap") is True

    def test_es01b_watchdog_anchors_5m_15min(self):
        """With the key removed, ES01B is 5m-enabled: the watchdog anchors
        _last_bar_time_5m against the 30-min threshold
        (31 min stale during equity OPEN -> True; 10 min -> False).
        FAILS at Red on the flag resolution (key still present/false).
        (Vector 16 -> 31 / prose 15-min -> 30-min per the 2026-07-06
        directive, ticket watchdog-telegram-throttle_07062026_0007;
        the historical name keeps its original anchor-identity meaning.)"""
        live = _load_es01b().get("live_config", {})
        flag = bool(live.get("enable_5m_stream", True))
        assert flag is True, (
            "ES01B must ride the default-true 5m path — the watchdog "
            "re-anchors to _last_bar_time_5m/30-min only then (C1/C4)"
        )
        now = _utc_from_ct(2026, 7, 7, 12, 0)  # Tue 12:00 CT — equity OPEN
        stale = _watchdog_stub("MES", enable_5m=flag)
        stale._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=31))
        with _frozen_lt_clock(now):
            assert stale._check_stale_bars() is True, (
                "31-min-stale 5m anchor during OPEN must trigger the "
                "30-min watchdog"
            )
        fresh = _watchdog_stub("MES", enable_5m=flag)
        fresh._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=10))
        with _frozen_lt_clock(now):
            assert fresh._check_stale_bars() is False
        fresh.data_client.disconnect.assert_not_called()

    def test_es01b_trailing_5m_frame_drives_when_present_pin(self):
        """Pin (passes at Red AND Green): trailing selects by frame
        PRESENCE — the MES/ES01B variant of the existing CL pin
        test_5m_frame_still_drives_when_present_pin (cited, not changed).
        With both frames present and divergent highs, the 5m frame drives
        on the MES 0.25 tick grid. Ticket
        seedless-5m-live-stream_07052026_0546 (narrow ES01B assertion)."""
        trader, raw_order, evt = _make_inposition_trader(
            "MES", df_5m=_bar_df(71.5), df_1h=_bar_df(69.9, low=69.0)
        )
        trader._check_trailing_stop()
        trader.exec_client.modify_order.assert_called_once_with(
            evt.order_id, evt
        )
        assert trader._trailing_activated is True
        assert trader._highest_high == 71.5  # 5m High, not the 1h 69.9
        assert trader._tracked_sl_price == 70.5  # on the MES 0.25 grid
        assert raw_order.auxPrice == 70.5

    # -- Loud disclosure (3-surface discipline, narrowest seam) ------------

    def test_warm_start_banner_when_shallow(self, tmp_path, caplog):
        """When the 5m manager reports shallow_bootstrapped, _warm_start
        must set _shallow_5m_bootstrap and emit the loud SHALLOW 5M banner
        (the log surface of the 3-surface discipline; the Telegram Mode
        stamp reads the same state flag). Red: the flag/state never exists
        and no banner fires."""
        trader = _shallow_warm_start_trader(tmp_path, shallow=True)
        with caplog.at_level(logging.INFO):
            trader._warm_start()
        assert getattr(trader, "_shallow_5m_bootstrap", None) is True, (
            "_warm_start must latch the manager's shallow_bootstrapped "
            "state into _shallow_5m_bootstrap (Telegram Mode stamp reads it)"
        )
        assert any(
            "SHALLOW 5M" in r.getMessage() for r in caplog.records
        ), "the SHALLOW 5M banner must fire when the bootstrap happened"

    def test_warm_start_no_banner_when_not_shallow(self, tmp_path, caplog):
        """Anti-noise control: a normal (cache/seed) warm start sets the
        state False and emits NO shallow banner. Red: the state attribute
        does not exist (getattr None is not False)."""
        trader = _shallow_warm_start_trader(tmp_path, shallow=False)
        with caplog.at_level(logging.INFO):
            trader._warm_start()
        assert getattr(trader, "_shallow_5m_bootstrap", None) is False
        assert not any(
            "SHALLOW 5M" in r.getMessage() for r in caplog.records
        )

    # -- T7 opt-out stays valid (blueprint hard constraint 3) --------------

    def test_t7_flag_false_fixture_still_opts_out(self, tmp_path):
        """Pin (passes at Red AND Green): the T7 synthetic flag-false
        fixture (reused by import — not duplicated) still constructs
        hourly-only: no 5m manager at all, hence no bootstrap wiring.
        enable_5m_stream:false remains a valid, explicit opt-out."""
        cfg = _es_hourly_only_cfg(tmp_path)
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)
        assert trader._enable_5m_stream is False
        assert trader.data_manager_5m is None
        assert MockDM.call_count == 1, (
            "flag-false must construct ONLY the 1h manager — the shallow "
            "bootstrap never applies to an opted-out instance"
        )
