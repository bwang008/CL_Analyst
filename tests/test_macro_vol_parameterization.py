"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/features/macro_features.py
                            (module helpers vol_label_for /
                            has_external_macro_features /
                            external_macro_feature_names /
                            validate_external_macro_features; instrument-driven
                            vol label + Q1 missing-vol-column hard-raise in
                            MacroFeatureEngine._build_fred_features; per-symbol
                            file resolution pins),
                            src/live_execution/feature_pipeline.py
                            (build_live_features gains keyword-only
                            `instrument`; raises when external macro/COT
                            features are needed and instrument is None;
                            helper-based external classification — D1),
                            src/live_execution/live_trader.py
                            (_brain_instrument seam property; helper-based
                            _needs_macro; instrument-derived startup fetch
                            list ["VIX"] + [vol if != VIX] — D2; startup
                            exact-name validation in __init__ BEFORE
                            connect() — D3; engine sites pass instrument),
                            src/live_execution/adapters/ibkr_data_feed.py
                            (_INDEX_CONTRACT_SPECS map VIX/OVX/GVZ->CBOE,
                            DX->NYBOT; map-driven fetch_daily_close_async;
                            unknown index symbol raises)
Target Class/Function: vol_label_for, has_external_macro_features,
                       external_macro_feature_names,
                       validate_external_macro_features,
                       MacroFeatureEngine.__init__,
                       MacroFeatureEngine._build_fred_features,
                       MacroFeatureEngine._load_fred, MacroFeatureEngine._load_cot,
                       build_live_features,
                       LiveTrader.__init__, LiveTrader.start,
                       LiveTrader._brain_instrument,
                       IBKRDataFeedClient.fetch_daily_close_async,
                       _INDEX_CONTRACT_SPECS
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t4-macro-vol-parameterization_07042026_2147
Phase: RED — the module-level ghost imports (vol_label_for,
has_external_macro_features, external_macro_feature_names,
validate_external_macro_features from src.features.macro_features;
_INDEX_CONTRACT_SPECS from src.live_execution.adapters.ibkr_data_feed) do NOT
exist at HEAD 55a6a8f; this file fails at collection until the Coder
implements audit.md sections 3.1-3.4 (Manager rulings: Q1 ACKed hard-raise,
Q2 constructor None->CL kept as documented training-only shim, Q3 deferred
to T7).

Coverage (audit.md section 5 items 1-5, 7-18, 20-21 + blueprint hard
constraints + impact_review conditions 3/4; item 6 is the blocking HS14B
ledger parity gate — a harness run, not a unit test; item 19's LiveTrader
mute-activation halves are byte-untouched blocks pinned by the existing
tests/test_live_macro_refresh.py / tests/test_stale_data_detection.py):
  - TestVolLabelAndNaming: vol_label_for registry table (CL/MCL/NG->OVX,
    GC/MGC->GVZ, everything else->VIX) + lockstep with live_vol_index;
    external_macro_feature_names ground truth (CL 39+13, ES 29+13, GC 39+13
    incl. the 9 MACRO_GVZ_* + MACRO_VIX_GVZ_RATIO — impact_review section 1.2
    parquet-verified counts); MCL == CL set (brain-driven, constraint 3);
    internal MACRO_WIDTH_*/MACRO_POS_* never classified external (D1);
    enumerator lockstep with actually-built FRED+COT columns on synthetic
    frames; CL value pins bit-equal to an independent pandas reference;
    ES-shaped frame builds VIX-only, no duplicate columns, no skip-warning.
  - TestNeedsMacroClassification: has_external_macro_features truth table
    (item 1) + extensional identity with the legacy 6-prefix rule over
    CL/ES-shaped feature lists (hard constraint 1).
  - TestValidateExternalMacro: ES + MACRO_OVX_CHG_1D -> actionable
    ValueError; MACRO_VIX_GVZ_RATIO on ES -> ValueError (exact-name
    enumeration, item 10); GC + MACRO_GVZ_* -> no raise; VIX-proxy
    (ZC/ZS/SI) raise message dedupes vol-label stems (reviewer condition 4);
    shipped-style 233-name CL list cannot raise (item 5).
  - TestEngineInstrumentFiles: per-symbol CSV resolution (_es), CL
    instrument == legacy None shim byte-identical paths (Q2), Q1
    missing-vol-column raise (GC on ES-shaped file), missing FRED/COT file
    hard-raises stay symbol-aware (item 17), ES stale-DXY still raises
    StaleDataException (mute semantics symbol-independent, item 19).
  - TestBuildLiveFeaturesInstrument: external-needed + instrument=None ->
    ValueError BEFORE any engine construction; internal-only / non-macro
    lists + None -> backward compatible; instrument passed -> engine
    constructed with it (mock seam) (item 18).
  - TestLiveTraderMacroWiring: startup fetch order pins via the start()
    interception seam (CL ["VIX","OVX"] byte-order — D2; MCL brain-driven
    item 20; ES ["VIX"] never OVX item 7; GC ["VIX","GVZ"] item 12;
    ZC ["VIX"]); no-macro/internal-only configs never fetch; ES config +
    MACRO_OVX_* feature -> ValueError at __init__ before connect (D3,
    reviewer condition 3); _brain_instrument seam property (T3 pattern:
    context-first, structural fallback, unresolvable raises).
  - TestIndexContractSpecs: fetch_daily_close_async map routing
    (VIX/OVX/GVZ -> CBOE, DX -> NYBOT), unknown symbol raises listing the
    supported set (item 13), map content, registry invariant
    live_vol_index == volatility_index.replace("CLS","") and
    live_vol_index in _INDEX_CONTRACT_SPECS for all 15 entries (item 16,
    incl. NG per impact_review condition 2).

Out of scope, deliberately NOT tested here (blueprint scope guards):
  session-hours/watchdog/rollover (T5), generator (T6), fleet_runner,
  backtest engine / data_processor training call-site migration (T8),
  hourly index re-fetch (does not exist at HEAD — D5), GVZ IBKR
  entitlement (Q3 -> T7 canary), _STALE_THRESHOLDS retuning.

All I/O boundaries are mocked or tmp_path-backed: no real FRED/COT CSVs,
no IBKR gateway, no cloud. LiveTrader seams mirror
tests/test_instrument_context.py (DummyStrategy + MagicMock clients),
tests/test_tick_order_pricing.py (patched DataManager/Path.exists full
init + object.__new__ property seams). MacroFeatureEngine fixtures in
tests/test_stale_data_detection.py / tests/test_live_macro_refresh.py are
NOT modified (Coder owns the enumerated mechanical churn).
"""

from __future__ import annotations

import asyncio
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.core.instrument_master import INSTRUMENT_REGISTRY, get_instrument
from src.features.macro_features import MacroFeatureEngine, StaleDataException
from src.live_execution.feature_pipeline import build_live_features
from src.live_execution.instrument_context import InstrumentContext
from src.live_execution.live_trader import LiveTrader
from src.live_execution.adapters.ibkr_data_feed import IBKRDataFeedClient

# Ghost imports — the Coder implements these (audit.md section 3.1 / 3.4).
# RED: this file fails at collection until then. Do NOT "fix" the ImportError.
from src.features.macro_features import (  # noqa: E402
    external_macro_feature_names,
    has_external_macro_features,
    validate_external_macro_features,
    vol_label_for,
)
from src.live_execution.adapters.ibkr_data_feed import (  # noqa: E402
    _INDEX_CONTRACT_SPECS,
)

_TICKET = "t4-macro-vol-parameterization_07042026_2147"
_SEED = 20260704

# ---------------------------------------------------------------------------
# Ground-truth naming (impact_review section 1.2 — parquet-verified; windows
# hardcoded on purpose: importing CHANGE_WINDOWS/PCTILE_WINDOWS from the
# implementation would make the pin circular).
# ---------------------------------------------------------------------------

_CHANGE_WINDOWS = (1, 3, 7, 14, 35)
_PCTILE_WINDOWS = (14, 35, 60)

# The canonical 13 COT feature names (identical for every symbol — the
# Disaggregated and TFF adapters emit the same Date,OI,*_Net schema).
_COT_FEATURES = frozenset({
    "COT_OI", "COT_OI_CHG_1W", "COT_OI_CHG_3W", "COT_OI_CHG_5W",
    "COT_MM_NET", "COT_MM_NET_PCTILE_52W", "COT_MM_NET_PCTILE_14W",
    "COT_MM_NET_PCTILE_35W", "COT_MM_MOMENTUM_4W",
    "COT_PROD_NET", "COT_PROD_NET_PCTILE_52W",
    "COT_SPEC_NET", "COT_SPEC_NET_PCTILE_52W",
})

_INTERNAL_MACRO_NAMES = (
    "MACRO_WIDTH_1W", "MACRO_WIDTH_2W", "MACRO_WIDTH_1M",
    "MACRO_WIDTH_3M", "MACRO_WIDTH_6M",
    "MACRO_POS_1W", "MACRO_POS_2W", "MACRO_POS_1M",
    "MACRO_POS_3M", "MACRO_POS_6M",
)

# The legacy hardcoded prefix rule (live_trader.py:217-221 /
# feature_pipeline.py:250-254 at HEAD) — the extensional-identity reference.
_OLD_PREFIX_RULE = (
    "MACRO_VIX", "MACRO_OVX", "MACRO_DXY",
    "MACRO_YIELD_CURVE", "MACRO_FED_FUNDS", "COT_",
)


def _expected_fred(vol_label: str) -> frozenset[str]:
    """Enumerate the FRED-derived feature names _build_fred_features
    produces for a given vol label (dedup for VIX-proxy symbols)."""
    cols = list(dict.fromkeys(["VIX", vol_label, "DXY", "YIELD_CURVE"]))
    names: set[str] = set()
    for col in cols:
        names.add(f"MACRO_{col}")
        for w in _CHANGE_WINDOWS:
            names.add(f"MACRO_{col}_CHG_{w}D")
        for w in _PCTILE_WINDOWS:
            names.add(f"MACRO_{col}_PCTILE_{w}D")
    if vol_label != "VIX":
        names.add(f"MACRO_VIX_{vol_label}_RATIO")
    names.add("MACRO_YIELD_CURVE_SIGN")
    names.add("MACRO_FED_FUNDS")
    return frozenset(names)


def _expected_external(vol_label: str) -> frozenset[str]:
    return _expected_fred(vol_label) | _COT_FEATURES


def _shipped_style_cl_features() -> list[str]:
    """A 233-name HS14B-shaped list: the full CL external set (52) + the 5
    internal MACRO_WIDTH_* + 176 technical fillers (impact_review section 1.3:
    both shipped parity boosters carry 233 features and validation cannot
    raise for them)."""
    features = sorted(_expected_external("OVX"))
    features += [f"MACRO_WIDTH_{lbl}" for lbl in ("1W", "2W", "1M", "3M", "6M")]
    features += [f"TECH_FEAT_{i:03d}" for i in range(233 - len(features))]
    assert len(features) == 233
    return features


# ---------------------------------------------------------------------------
# Synthetic file fixtures (tmp_path CSVs — never the real data root)
# ---------------------------------------------------------------------------

def _write_fred_csv(path, vol_col: str | None, n_days: int = 80,
                    dxy_tail_repeat: int = 0):
    """FRED-shaped CSV mirroring scripts/download_macro_data.py output:
    Date,VIX,DXY,YIELD_CURVE,FED_FUNDS[,<vol_col>]."""
    rng = np.random.default_rng(_SEED)
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    data = {
        "Date": dates,
        "VIX": np.linspace(15.0, 27.0, n_days) + rng.normal(0, 0.05, n_days),
        "DXY": np.linspace(102.0, 108.0, n_days) + rng.normal(0, 0.02, n_days),
        "YIELD_CURVE": np.linspace(-0.6, 0.7, n_days),
        "FED_FUNDS": np.full(n_days, 4.25),
    }
    if vol_col is not None:
        data[vol_col] = (
            np.linspace(30.0, 42.0, n_days) + rng.normal(0, 0.05, n_days)
        )
    df = pd.DataFrame(data)
    if dxy_tail_repeat:
        df.loc[df.index[-dxy_tail_repeat:], "DXY"] = 119.2868
    df.to_csv(path, index=False)
    return path


def _write_cot_csv(path, n_weeks: int = 70):
    """COT CSV in the canonical adapter schema subset the engine consumes."""
    rng = np.random.default_rng(_SEED + 1)
    dates = pd.date_range("2025-01-07", periods=n_weeks, freq="7D")
    pd.DataFrame({
        "Date": dates,
        "OI": np.linspace(300_000.0, 340_000.0, n_weeks),
        "MM_Net": rng.normal(50_000.0, 5_000.0, n_weeks),
        "Prod_Net": rng.normal(-60_000.0, 5_000.0, n_weeks),
        "Spec_Net": rng.normal(5_000.0, 2_000.0, n_weeks),
    }).to_csv(path, index=False)
    return path


def _generate_ohlcv_4h(n_bars: int) -> pd.DataFrame:
    """Small seeded OHLCV frame; 4h bars keep build_live_features' minimum
    window at 210 bars (cheapest deterministic path through the pipeline)."""
    rng = np.random.default_rng(_SEED)
    idx = pd.date_range("2025-01-01", periods=n_bars, freq="4H")
    close = 70.0 + np.cumsum(rng.normal(0, 0.08, n_bars))
    high = close + np.abs(rng.normal(0, 0.10, n_bars))
    low = close - np.abs(rng.normal(0, 0.10, n_bars))
    open_ = np.roll(close, 1)
    open_[0] = 70.0
    volume = rng.integers(100, 10_000, n_bars).astype(float)
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close,
         "Volume": volume},
        index=idx,
    )
    df.index.name = "DateTime"
    return df


# ---------------------------------------------------------------------------
# LiveTrader seams (mirror tests/test_instrument_context.py and
# tests/test_tick_order_pricing.py — this file does NOT modify them)
# ---------------------------------------------------------------------------

class DummyStrategy:
    """Minimal strategy stub mirroring tests/test_live_macro_refresh.py."""

    def __init__(self, feature_names=None, config=None):
        self.feature_names = feature_names if feature_names is not None else ["MACD"]
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config if config is not None else {"execution_symbol": "CL"}


def _build_trader(feature_names, execution_symbol, tmp_path):
    """Full LiveTrader init with mocked DataManager + existence checks
    (the tests/test_tick_order_pricing.py _build_full_trader seam)."""
    cfg = {"nickname": f"{execution_symbol}_t4_wiring",
           "execution_symbol": execution_symbol}
    with patch("src.live_execution.live_trader.DataManager"), patch(
        "pathlib.Path.exists", return_value=True
    ):
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(feature_names=feature_names, config=cfg),
            db_path=str(tmp_path / "telemetry.db"),
            dry_run=True,
        )
    return trader


class _StopStart(Exception):
    """Sentinel raised from the first non-swallowed start() step AFTER the
    Step-3 macro daily-close fetch (_print_account_summary): everything the
    test needs has already executed; the heavy warm-start never runs."""


_DAILY_CLOSES = {"VIX": 20.5, "OVX": 33.25, "GVZ": 17.75}


def _fetch_symbol(call) -> str:
    if call.args:
        return call.args[0]
    return call.kwargs.get("symbol", call.kwargs.get("sym"))


def _run_start_capture_fetch(trader) -> MagicMock:
    """Drive start() through Step 3 (index daily-close fetch) with mocked
    adapters and stop deterministically at _print_account_summary."""
    trader._telegram = MagicMock()
    trader._shutdown = MagicMock()
    trader._print_account_summary = MagicMock(
        side_effect=_StopStart("intercepted after macro fetch")
    )
    fetch = MagicMock(
        side_effect=lambda *a, **k: _DAILY_CLOSES.get(
            a[0] if a else k.get("symbol", k.get("sym")), 11.11
        )
    )
    trader.data_client.fetch_daily_close = fetch
    with patch("src.live_execution.live_trader.signal.signal"):
        with pytest.raises(_StopStart):
            trader.start()
    return fetch


# ===========================================================================
# TestVolLabelAndNaming — naming source of truth (items 3-4, 11, 14-16, D1)
# ===========================================================================

class TestVolLabelAndNaming:
    # --- vol_label_for over the registry --------------------------------

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("CL", "OVX"), ("MCL", "OVX"), ("NG", "OVX"),
            ("GC", "GVZ"), ("MGC", "GVZ"),
            ("ES", "VIX"), ("MES", "VIX"), ("NQ", "VIX"), ("MNQ", "VIX"),
            ("ZC", "VIX"), ("ZS", "VIX"), ("SI", "VIX"), ("SIL", "VIX"),
            ("HG", "VIX"), ("PA", "VIX"),
        ],
    )
    def test_vol_label_for_registry_table(self, symbol, expected):
        assert vol_label_for(get_instrument(symbol)) == expected

    def test_vol_label_lockstep_with_live_vol_index(self):
        """The FRED column label doubles as the IB index symbol / override
        key — lockstep with the registry for ALL 15 entries (incl. NG,
        impact_review condition 2)."""
        for symbol, inst in INSTRUMENT_REGISTRY.items():
            assert vol_label_for(inst) == inst.live_vol_index, symbol

    # --- external_macro_feature_names ground truth -----------------------

    def test_external_names_cl_exact_set(self):
        names = set(external_macro_feature_names(get_instrument("CL")))
        assert names == set(_expected_external("OVX"))
        assert len(names) == 52  # 39 FRED + 13 COT
        assert "MACRO_VIX_OVX_RATIO" in names
        assert len(names & _COT_FEATURES) == 13

    def test_external_names_es_exact_set(self):
        names = set(external_macro_feature_names(get_instrument("ES")))
        assert names == set(_expected_external("VIX"))
        assert len(names) == 42  # 29 FRED + 13 COT
        # No OVX anywhere (impact_review 1.2: zero OVX/ratio in ES parquet)
        assert not any(n.startswith("MACRO_OVX") for n in names)
        assert not any("OVX" in n for n in names)
        assert not any("GVZ" in n for n in names)

    def test_external_names_gc_exact_set(self):
        names = set(external_macro_feature_names(get_instrument("GC")))
        assert names == set(_expected_external("GVZ"))
        assert len(names) == 52  # 39 FRED + 13 COT
        gvz_prefixed = {n for n in names if n.startswith("MACRO_GVZ")}
        assert len(gvz_prefixed) == 9  # raw + 5 CHG + 3 PCTILE
        assert "MACRO_VIX_GVZ_RATIO" in names

    def test_external_names_mcl_equals_cl(self):
        """Constraint 3: MCL uses CL's macro set (brain-driven)."""
        assert set(external_macro_feature_names(get_instrument("MCL"))) == set(
            external_macro_feature_names(get_instrument("CL"))
        )

    def test_internal_prefixes_never_external(self):
        """D1: AlphaFactory's internal MACRO_WIDTH_*/MACRO_POS_* are NOT
        file-backed and must never appear in any instrument's external set."""
        for symbol, inst in INSTRUMENT_REGISTRY.items():
            names = set(external_macro_feature_names(inst))
            for internal in _INTERNAL_MACRO_NAMES:
                assert internal not in names, (symbol, internal)
            assert not any(
                n.startswith(("MACRO_WIDTH_", "MACRO_POS_")) for n in names
            ), symbol

    # --- enumerator lockstep with the actual build (item 15) -------------

    @pytest.mark.parametrize(
        "symbol,vol_col,n_fred",
        [("CL", "OVX", 39), ("ES", None, 29), ("GC", "GVZ", 39)],
    )
    def test_enumerator_lockstep_with_built_columns(
        self, tmp_path, symbol, vol_col, n_fred
    ):
        """external_macro_feature_names(instr) == the union of the columns
        _build_fred_features + _build_cot_features actually produce."""
        fred = _write_fred_csv(tmp_path / "fred.csv", vol_col=vol_col)
        cot = _write_cot_csv(tmp_path / "cot.csv")
        inst = get_instrument(symbol)
        engine = MacroFeatureEngine(instrument=inst, fred_path=fred, cot_path=cot)
        fred_feats = engine._build_fred_features(check_staleness=False)
        cot_feats = engine._build_cot_features()
        assert len(fred_feats.columns) == n_fred, symbol
        assert len(cot_feats.columns) == 13, symbol
        built = set(fred_feats.columns) | set(cot_feats.columns)
        assert built == set(external_macro_feature_names(inst)), symbol

    # --- CL value pins (item 4: D4 must be output-identical) -------------

    def test_cl_fred_values_pinned_to_independent_reference(self, tmp_path):
        """Representative CL columns bit-equal to an independently computed
        pandas reference (raw ffill -> +1 day shift -> pct_change / ratio /
        rolling rank). Pins that the D4 vol-label change cannot move CL
        outputs."""
        fred = _write_fred_csv(tmp_path / "fred_cl.csv", vol_col="OVX")
        engine = MacroFeatureEngine(
            instrument=get_instrument("CL"),
            fred_path=fred,
            cot_path=tmp_path / "unused_cot.csv",
        )
        feats = engine._build_fred_features(check_staleness=False)

        raw = pd.read_csv(fred, parse_dates=["Date"]).set_index("Date").sort_index()
        raw = raw.ffill()
        raw.index = raw.index + pd.Timedelta(days=1)

        pd.testing.assert_series_equal(
            feats["MACRO_OVX_CHG_1D"], raw["OVX"].pct_change(1),
            check_names=False, check_exact=True, check_freq=False,
        )
        pd.testing.assert_series_equal(
            feats["MACRO_VIX_OVX_RATIO"],
            raw["VIX"] / raw["OVX"].replace(0, np.nan),
            check_names=False, check_exact=True, check_freq=False,
        )
        pd.testing.assert_series_equal(
            feats["MACRO_OVX_PCTILE_14D"],
            raw["OVX"].rolling(14).rank(pct=True),
            check_names=False, check_exact=True, check_freq=False,
        )
        pd.testing.assert_series_equal(
            feats["MACRO_YIELD_CURVE_SIGN"],
            (raw["YIELD_CURVE"] > 0).astype(int),
            check_names=False, check_exact=True, check_freq=False,
        )

    # --- ES-shaped frame: VIX only, deduped, quiet (item 11) -------------

    def test_es_frame_builds_vix_only_no_duplicates_no_skip_warning(
        self, tmp_path, caplog
    ):
        fred = _write_fred_csv(tmp_path / "fred_es.csv", vol_col=None)
        engine = MacroFeatureEngine(
            instrument=get_instrument("ES"),
            fred_path=fred,
            cot_path=tmp_path / "unused_cot.csv",
        )
        with caplog.at_level(logging.WARNING, logger="src.features.macro_features"):
            feats = engine._build_fred_features(check_staleness=False)
        assert len(feats.columns) == 29
        assert len(set(feats.columns)) == 29  # no duplicate columns
        assert not any("OVX" in c for c in feats.columns)
        # Instrument-driven label (D4): no file-sniffing "OVX" fallback, so
        # the legacy "FRED column 'OVX' not found — skipping" warning is gone.
        assert not any(
            "skipping" in rec.getMessage() for rec in caplog.records
        ), [rec.getMessage() for rec in caplog.records]


# ===========================================================================
# TestNeedsMacroClassification — has_external_macro_features (item 1, D1)
# ===========================================================================

class TestNeedsMacroClassification:
    @pytest.mark.parametrize(
        "features,expected",
        [
            (["MACRO_VIX"], True),
            (["COT_MM_NET"], True),
            (["MACRO_OVX_CHG_1D"], True),
            (["MACRO_GVZ_CHG_1D"], True),   # M2: GVZ features ARE external
            (["MACRO_DXY_CHG_1D"], True),
            (["MACRO_WIDTH_1W"], False),    # internal (AlphaFactory)
            (["MACRO_POS_1M"], False),      # internal (AlphaFactory)
            (["MACRO_WIDTH_1M", "MACRO_POS_3M"], False),
            (["MACD", "ATR_14"], False),
            ([], False),
            (["MACD", "MACRO_WIDTH_1M", "COT_OI"], True),
        ],
    )
    def test_truth_table(self, features, expected):
        assert has_external_macro_features(features) is expected

    def test_extensional_identity_with_old_prefix_rule_cl_es(self):
        """Hard constraint 1: for every feature drawn from CL/ES-shaped
        lists (external set + internal WIDTH/POS + technicals) the new rule
        agrees with the legacy 6-prefix rule name-by-name."""
        technicals = ["MACD", "ATR_14", "Time_Sin", "VOL_PARK_864",
                      "STRUC_HURST_100", "Volume_Log"]
        internals = list(_INTERNAL_MACRO_NAMES)
        cl_shaped = sorted(_expected_external("OVX")) + internals + technicals
        es_shaped = sorted(_expected_external("VIX")) + internals + technicals
        for name in cl_shaped + es_shaped:
            assert has_external_macro_features([name]) is name.startswith(
                _OLD_PREFIX_RULE
            ), name
        assert has_external_macro_features(cl_shaped) is True
        assert has_external_macro_features(es_shaped) is True
        assert has_external_macro_features(internals + technicals) is False


# ===========================================================================
# TestValidateExternalMacro — startup contract enforcement (items 5, 9-10,
# reviewer condition 4)
# ===========================================================================

class TestValidateExternalMacro:
    def test_es_with_ovx_feature_raises_actionable(self):
        with pytest.raises(ValueError) as ei:
            validate_external_macro_features(
                ["MACD", "MACRO_OVX_CHG_1D"], get_instrument("ES")
            )
        msg = str(ei.value)
        assert "MACRO_OVX_CHG_1D" in msg      # names the offending feature
        assert "ES" in msg                    # names the instrument
        assert "Buildable stems" in msg       # actionable: what IS buildable

    def test_es_with_vix_gvz_ratio_raises_exact_name_enumeration(self):
        """Item 10: MACRO_VIX_GVZ_RATIO starts with 'MACRO_VIX' — a prefix
        rule would wave it through; exact-name enumeration must reject it."""
        with pytest.raises(ValueError) as ei:
            validate_external_macro_features(
                ["MACRO_VIX_GVZ_RATIO"], get_instrument("ES")
            )
        assert "MACRO_VIX_GVZ_RATIO" in str(ei.value)

    def test_gc_gvz_features_do_not_raise(self):
        assert validate_external_macro_features(
            ["MACRO_GVZ_CHG_1D", "MACRO_GVZ_PCTILE_14D",
             "MACRO_VIX_GVZ_RATIO", "COT_MM_NET"],
            get_instrument("GC"),
        ) is None

    def test_es_full_buildable_set_does_not_raise(self):
        assert validate_external_macro_features(
            sorted(_expected_external("VIX")), get_instrument("ES")
        ) is None

    @pytest.mark.parametrize("symbol", ["ZC", "ZS", "SI"])
    def test_vix_proxy_raise_message_dedupes_vol_stems(self, symbol):
        """Reviewer condition 4: for VIX-proxy instruments MACRO_VIX* and
        MACRO_{vol_label}* are the same stem — the raise message must not
        list it twice."""
        with pytest.raises(ValueError) as ei:
            validate_external_macro_features(
                ["MACRO_GVZ_CHG_1D"], get_instrument(symbol)
            )
        msg = str(ei.value)
        assert msg.count("MACRO_VIX*") == 1, msg
        stems = re.findall(r"MACRO_[A-Z_]+\*", msg)
        assert len(stems) == len(set(stems)), f"duplicated stems in: {msg}"

    def test_cl_shipped_style_233_feature_list_cannot_raise(self):
        """Item 5 / hard constraint 1: every shipped CL feature set is a
        subset of the CL-buildable set — validation must be a no-op."""
        assert validate_external_macro_features(
            _shipped_style_cl_features(), get_instrument("CL")
        ) is None

    def test_mcl_accepts_cl_macro_features(self):
        """Constraint 3: brain-driven — CL's macro names validate for MCL."""
        assert validate_external_macro_features(
            ["MACRO_OVX_CHG_1D", "MACRO_VIX_OVX_RATIO", "COT_MM_NET"],
            get_instrument("MCL"),
        ) is None


# ===========================================================================
# TestEngineInstrumentFiles — per-symbol CSV resolution, Q1, item 17, 19
# ===========================================================================

class TestEngineInstrumentFiles:
    @staticmethod
    def _patch_data_root(monkeypatch, tmp_path):
        import src.features.macro_features as mf
        monkeypatch.setattr(mf, "get_data_path", lambda rel: tmp_path / rel)

    def test_es_engine_resolves_es_files(self, tmp_path, monkeypatch):
        self._patch_data_root(monkeypatch, tmp_path)
        engine = MacroFeatureEngine(instrument=get_instrument("ES"))
        assert engine.fred_path.name == "fred_macro_data_es.csv"
        assert engine.cot_path.name == "cftc_cot_es.csv"

    def test_gc_engine_resolves_gc_files(self, tmp_path, monkeypatch):
        self._patch_data_root(monkeypatch, tmp_path)
        engine = MacroFeatureEngine(instrument=get_instrument("GC"))
        assert engine.fred_path.name == "fred_macro_data_gc.csv"
        assert engine.cot_path.name == "cftc_cot_gc.csv"

    def test_cl_and_legacy_none_shim_resolve_identical_paths(
        self, tmp_path, monkeypatch
    ):
        """Q2 (Manager-accepted): instrument=None stays a DOCUMENTED
        training-only CL shim — byte-identical _cl.csv paths."""
        self._patch_data_root(monkeypatch, tmp_path)
        e_none = MacroFeatureEngine()
        e_cl = MacroFeatureEngine(instrument=get_instrument("CL"))
        assert str(e_none.fred_path) == str(e_cl.fred_path)
        assert str(e_none.cot_path) == str(e_cl.cot_path)
        assert e_cl.fred_path.name == "fred_macro_data_cl.csv"
        assert e_cl.cot_path.name == "cftc_cot_cl.csv"

    def test_q1_fred_file_missing_vol_column_raises(self, tmp_path):
        """Q1 (Manager-ACKed): a GC engine reading an ES-shaped file (no GVZ
        column) must hard-raise with a regenerate hint — no warn-skip, no
        silent no-trade."""
        es_shaped = _write_fred_csv(tmp_path / "fred_misprovisioned.csv",
                                    vol_col=None)
        engine = MacroFeatureEngine(
            instrument=get_instrument("GC"),
            fred_path=es_shaped,
            cot_path=tmp_path / "unused_cot.csv",
        )
        with pytest.raises(ValueError) as ei:
            engine._build_fred_features(check_staleness=False)
        msg = str(ei.value)
        assert "GVZ" in msg                   # the missing column
        assert "GC" in msg                    # the instrument
        assert "download_macro_data" in msg   # the regenerate hint

    def test_missing_fred_file_raises_symbol_aware(self, tmp_path, monkeypatch):
        self._patch_data_root(monkeypatch, tmp_path)
        engine = MacroFeatureEngine(instrument=get_instrument("ES"))
        with pytest.raises(FileNotFoundError) as ei:
            engine._load_fred()
        assert "fred_macro_data_es.csv" in str(ei.value)

    def test_missing_cot_file_raises_symbol_aware(self, tmp_path, monkeypatch):
        """Item 17 / constraint 4: the existing COT hard-raise is preserved
        and names the per-symbol path."""
        self._patch_data_root(monkeypatch, tmp_path)
        engine = MacroFeatureEngine(instrument=get_instrument("ES"))
        with pytest.raises(FileNotFoundError) as ei:
            engine._load_cot()
        assert "cftc_cot_es.csv" in str(ei.value)

    def test_es_engine_stale_dxy_still_raises_staledataexception(self, tmp_path):
        """Item 19 (engine half): mute semantics are symbol-independent —
        DXY staleness raises for an ES-instrument engine exactly as for CL."""
        stale = _write_fred_csv(tmp_path / "fred_es_stale.csv", vol_col=None,
                                dxy_tail_repeat=10)
        engine = MacroFeatureEngine(
            instrument=get_instrument("ES"),
            fred_path=stale,
            cot_path=tmp_path / "unused_cot.csv",
        )
        with pytest.raises(StaleDataException) as ei:
            engine._build_fred_features()
        assert "DXY" in ei.value.stale_series


# ===========================================================================
# TestBuildLiveFeaturesInstrument — feature_pipeline boundary (item 18)
# ===========================================================================

class TestBuildLiveFeaturesInstrument:
    @pytest.mark.parametrize(
        "features",
        [["MACRO_VIX"], ["COT_MM_NET"], ["MACRO_OVX_CHG_1D"]],
    )
    def test_external_macro_without_instrument_raises(self, features):
        """External macro needed + instrument=None -> ValueError BEFORE any
        engine construction (no silent CL default, no file I/O)."""
        df = _generate_ohlcv_4h(240)
        with patch(
            "src.live_execution.feature_pipeline.MacroFeatureEngine"
        ) as MockEngine:
            with pytest.raises(ValueError) as ei:
                build_live_features(df, features, lean=True, bar_size="4h")
            MockEngine.assert_not_called()
        msg = str(ei.value)
        assert "instrument" in msg
        assert features[0] in msg  # names an offending feature

    def test_internal_macro_only_without_instrument_backward_compatible(self):
        """D1 boundary: MACRO_WIDTH_*/MACRO_POS_* are AlphaFactory-internal —
        no instrument required, no engine, no raise."""
        df = _generate_ohlcv_4h(300)
        with patch(
            "src.live_execution.feature_pipeline.MacroFeatureEngine"
        ) as MockEngine:
            result = build_live_features(
                df, ["MACRO_WIDTH_1M", "MACRO_POS_1M"], lean=True,
                bar_size="4h",
            )
            MockEngine.assert_not_called()
        assert result is not None
        assert not result.iloc[-1].isna().any()

    def test_non_macro_features_without_instrument_still_work(self):
        """Parity-script pin (item 18): non-macro callers are unaffected."""
        df = _generate_ohlcv_4h(240)
        result = build_live_features(
            df, ["Time_Sin", "Time_Cos", "ATR_14", "Volume_Log"],
            lean=True, bar_size="4h",
        )
        assert result is not None
        assert not result.iloc[-1].isna().any()

    def test_instrument_passed_engine_constructed_with_it(self):
        """The engine must be constructed WITH the passed instrument
        (mock seam) and merge_all invoked on the working frame."""
        df = _generate_ohlcv_4h(240)
        cl = get_instrument("CL")
        with patch(
            "src.live_execution.feature_pipeline.MacroFeatureEngine"
        ) as MockEngine:
            MockEngine.return_value.merge_all.side_effect = (
                lambda work, **kw: work.assign(MACRO_VIX=20.0)
            )
            result = build_live_features(
                df, ["MACRO_VIX"], lean=True, bar_size="4h", instrument=cl,
            )
        MockEngine.assert_called_once()
        ca = MockEngine.call_args
        inst = ca.kwargs.get(
            "instrument", ca.args[0] if ca.args else None
        )
        assert inst is cl
        MockEngine.return_value.merge_all.assert_called_once()
        assert result is not None
        assert float(result["MACRO_VIX"].iloc[-1]) == pytest.approx(20.0)


# ===========================================================================
# TestLiveTraderMacroWiring — startup fetch, __init__ validation,
# _brain_instrument seam (items 2, 5, 7, 9, 12, 20; D2/D3)
# ===========================================================================

class TestLiveTraderMacroWiring:
    # --- startup daily-close fetch list (start() Step 3 seam) ------------

    @pytest.mark.parametrize(
        "exec_symbol,features,expected_syms",
        [
            # Item 2 / D2: CL byte-order pin — ["VIX","OVX"], exactly today's.
            ("CL", _shipped_style_cl_features(), ["VIX", "OVX"]),
            # Item 20: MCL brain=CL -> CL's fetch list (brain-driven).
            ("MCL", ["MACRO_VIX", "MACRO_OVX_CHG_1D", "COT_MM_NET"],
             ["VIX", "OVX"]),
            # Item 7: ES fetches VIX only — OVX must NEVER be requested.
            ("ES", ["MACRO_VIX_CHG_1D", "COT_MM_NET"], ["VIX"]),
            # Item 12: GC fetches VIX + GVZ.
            ("GC", ["MACRO_GVZ_CHG_1D"], ["VIX", "GVZ"]),
            # VIX-proxy grain: exactly ["VIX"], no duplicate VIX fetch.
            ("ZC", ["MACRO_VIX"], ["VIX"]),
        ],
    )
    def test_startup_fetch_list_by_instrument(
        self, tmp_path, exec_symbol, features, expected_syms
    ):
        trader = _build_trader(features, exec_symbol, tmp_path)
        assert trader._needs_macro is True
        fetch = _run_start_capture_fetch(trader)
        assert [_fetch_symbol(c) for c in fetch.call_args_list] == expected_syms
        assert fetch.call_count == len(expected_syms)
        assert set(trader._macro_daily_closes.keys()) == set(expected_syms)
        for sym in expected_syms:
            assert trader._macro_daily_closes[sym] == _DAILY_CLOSES[sym]

    def test_es_never_fetches_ovx(self, tmp_path):
        trader = _build_trader(["MACRO_VIX_CHG_1D"], "ES", tmp_path)
        fetch = _run_start_capture_fetch(trader)
        assert "OVX" not in [_fetch_symbol(c) for c in fetch.call_args_list]

    def test_no_macro_config_never_fetches(self, tmp_path):
        trader = _build_trader(["MACD", "ATR_14"], "CL", tmp_path)
        assert trader._needs_macro is False
        fetch = _run_start_capture_fetch(trader)
        fetch.assert_not_called()

    def test_internal_macro_only_config_never_fetches(self, tmp_path):
        """D1 at the trader level: WIDTH/POS-only models must not trigger
        index fetches / FRED loads / Safety-Mute exposure."""
        trader = _build_trader(["MACRO_WIDTH_1M", "MACRO_POS_1M"], "CL", tmp_path)
        assert trader._needs_macro is False
        fetch = _run_start_capture_fetch(trader)
        fetch.assert_not_called()

    def test_gc_needs_macro_detects_gvz(self, tmp_path):
        """M2 pin (item 12): MACRO_GVZ_* must flip _needs_macro True."""
        trader = _build_trader(["MACRO_GVZ_CHG_1D"], "GC", tmp_path)
        assert trader._needs_macro is True

    # --- __init__ validation BEFORE connect (item 9, D3, condition 3) ----

    def test_es_config_with_ovx_feature_raises_at_init_before_connect(
        self, tmp_path
    ):
        data_client = MagicMock()
        exec_client = MagicMock()
        with patch("src.live_execution.live_trader.DataManager"), patch(
            "pathlib.Path.exists", return_value=True
        ):
            with pytest.raises(ValueError) as ei:
                LiveTrader(
                    data_client=data_client,
                    exec_client=exec_client,
                    strategy=DummyStrategy(
                        feature_names=["MACD", "MACRO_OVX_CHG_1D"],
                        config={"nickname": "ES_bad_model",
                                "execution_symbol": "ES"},
                    ),
                    db_path=str(tmp_path / "telemetry.db"),
                    dry_run=True,
                )
        msg = str(ei.value)
        assert "MACRO_OVX_CHG_1D" in msg
        assert "ES" in msg
        # Reviewer condition 3: raise precedes connect()/any network
        # side-effect — the constructor never touched either adapter.
        data_client.connect.assert_not_called()
        exec_client.connect.assert_not_called()

    def test_cl_hs14b_style_config_constructs(self, tmp_path):
        """Item 5: the shipped-style CL feature set is CL-buildable, so the
        new __init__ validation cannot raise — construction succeeds."""
        trader = _build_trader(_shipped_style_cl_features(), "CL", tmp_path)
        assert trader._needs_macro is True
        assert trader._execution_symbol == "CL"

    # --- _brain_instrument seam property (T3 pattern) ---------------------

    def test_brain_instrument_prefers_context(self):
        trader = LiveTrader.__new__(LiveTrader)
        trader._instrument_context = InstrumentContext(
            execution_symbol="MCL",
            brain_symbol="CL",
            execution_instrument=get_instrument("MCL"),
            brain_instrument=get_instrument("CL"),
        )
        assert trader._brain_instrument is get_instrument("CL")

    def test_brain_instrument_full_init_context_path(self, tmp_path):
        trader = _build_trader(["MACRO_VIX"], "ZC", tmp_path)
        assert trader._brain_instrument is get_instrument("ZC")

    def test_brain_instrument_structural_fallback(self):
        """__new__ seam with only _execution_symbol set: micro -> parent,
        outright -> itself (structural derivation, NOT a CL default)."""
        trader = LiveTrader.__new__(LiveTrader)
        trader._execution_symbol = "MCL"
        assert trader._brain_instrument is get_instrument("CL")

        trader2 = LiveTrader.__new__(LiveTrader)
        trader2._execution_symbol = "GC"
        assert trader2._brain_instrument is get_instrument("GC")

    def test_brain_instrument_unknown_symbol_raises(self):
        trader = LiveTrader.__new__(LiveTrader)
        trader._execution_symbol = "XX"
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            _ = trader._brain_instrument

    def test_brain_instrument_nothing_set_raises(self):
        """No context AND no _execution_symbol -> raises mentioning the
        missing seam attribute (never a silent CL default)."""
        trader = LiveTrader.__new__(LiveTrader)
        with pytest.raises(AttributeError, match="_execution_symbol"):
            _ = trader._brain_instrument


# ===========================================================================
# TestIndexContractSpecs — data feed index routing (items 13, 16)
# ===========================================================================

def _make_feed():
    """IBKRDataFeedClient with a fully mocked manager (async methods are
    AsyncMock — no gateway, no event loop side effects)."""
    ctx = InstrumentContext(
        execution_symbol="CL",
        brain_symbol="CL",
        execution_instrument=get_instrument("CL"),
        brain_instrument=get_instrument("CL"),
    )
    with patch(
        "src.live_execution.adapters.ibkr_data_feed.IBKRConnectionManager"
    ):
        feed = IBKRDataFeedClient(instrument_context=ctx)
    feed.manager = MagicMock()
    feed.manager.qualify_contract_async = AsyncMock(side_effect=lambda c: c)
    feed.manager.fetch_daily_close_async = AsyncMock(return_value=17.42)
    return feed


class TestIndexContractSpecs:
    @pytest.mark.parametrize(
        "symbol,exchange",
        [("VIX", "CBOE"), ("OVX", "CBOE"), ("GVZ", "CBOE"), ("DX", "NYBOT")],
    )
    def test_fetch_daily_close_async_index_routing(self, symbol, exchange):
        feed = _make_feed()
        with patch("ib_insync.Index") as MockIndex:
            sentinel = MagicMock(name=f"{symbol}_contract")
            MockIndex.return_value = sentinel
            result = asyncio.run(feed.fetch_daily_close_async(symbol))
        assert result == 17.42
        MockIndex.assert_called_once()
        vals = list(MockIndex.call_args.args) + list(
            MockIndex.call_args.kwargs.values()
        )
        assert vals[0] == symbol
        assert exchange in vals
        assert "USD" in vals
        # The (possibly qualification-passed-through) contract reaches the
        # manager fetch.
        feed.manager.fetch_daily_close_async.assert_awaited_once()
        assert (
            feed.manager.fetch_daily_close_async.await_args.args[0] is sentinel
        )

    def test_unknown_index_symbol_raises_listing_supported(self):
        feed = _make_feed()
        with pytest.raises(ValueError) as ei:
            asyncio.run(feed.fetch_daily_close_async("XX"))
        msg = str(ei.value)
        assert "XX" in msg
        assert "GVZ" in msg  # actionable: the supported set is enumerated
        feed.manager.fetch_daily_close_async.assert_not_awaited()

    def test_index_contract_specs_map_content(self):
        assert _INDEX_CONTRACT_SPECS["VIX"] == ("CBOE", "USD")
        assert _INDEX_CONTRACT_SPECS["OVX"] == ("CBOE", "USD")
        assert _INDEX_CONTRACT_SPECS["GVZ"] == ("CBOE", "USD")
        assert _INDEX_CONTRACT_SPECS["DX"] == ("NYBOT", "USD")

    def test_registry_live_vol_index_invariant(self):
        """Item 16 (+ impact_review condition 2, NG included): for ALL 15
        registry entries live_vol_index == volatility_index.replace('CLS','')
        and the IB fetch symbol is covered by _INDEX_CONTRACT_SPECS."""
        assert len(INSTRUMENT_REGISTRY) >= 15
        for symbol, inst in INSTRUMENT_REGISTRY.items():
            assert inst.live_vol_index == inst.volatility_index.replace(
                "CLS", ""
            ), symbol
            assert inst.live_vol_index in _INDEX_CONTRACT_SPECS, symbol
        # NG explicitly (the audit section 2 omission the reviewer flagged):
        assert get_instrument("NG").live_vol_index == "OVX"
