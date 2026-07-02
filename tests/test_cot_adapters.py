"""
Tests for CFTC COT report adapters (disaggregated commodity vs. TFF financial).

Guards:
  * The symbol -> report-type registry (unmapped symbol must raise).
  * DisaggregatedAdapter reproduces the existing canonical schema (CL path
    regression guard — no behavior change vs. legacy _normalize_cot_columns).
  * TffAdapter maps the CFTC "Traders in Financial Futures" categories onto the
    canonical commodity-role schema per the approved mapping:
        MM   <- Leveraged Funds
        Prod <- Asset Manager / Institutional
        Spec <- Dealer / Intermediary

Fixtures use REAL CFTC column names and REAL E-mini S&P 500 (code 13874A)
values from the 2026-06-23 TFF report.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.instrument_master import get_instrument
from scripts.download_macro_data import (
    CotReportAdapter,
    DisaggregatedAdapter,
    TffAdapter,
    get_cot_adapter,
    _normalize_tff_columns,
    _normalize_cot_columns,
)

CANONICAL_COLS = {"Date", "OI", "MM_Long", "MM_Short",
                  "Prod_Long", "Prod_Short", "Spec_Long", "Spec_Short"}


def _tff_fixture() -> pd.DataFrame:
    """One real ES TFF row (2026-06-23) with the modern TFF column names."""
    return pd.DataFrame([{
        "Market_and_Exchange_Names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
        "Report_Date_as_YYYY-MM-DD": "2026-06-23",
        "CFTC_Contract_Market_Code": "13874A",
        "Open_Interest_All": 1980254,
        "Dealer_Positions_Long_All": 112578,
        "Dealer_Positions_Short_All": 868478,
        "Asset_Mgr_Positions_Long_All": 1171421,
        "Asset_Mgr_Positions_Short_All": 178692,
        "Lev_Money_Positions_Long_All": 185058,
        "Lev_Money_Positions_Short_All": 558526,
        "Other_Rept_Positions_Long_All": 62151,
        "Other_Rept_Positions_Short_All": 48090,
    }])


def _disagg_fixture() -> pd.DataFrame:
    """One synthetic disaggregated (commodity) row with real CFTC column names."""
    return pd.DataFrame([{
        "Report_Date_as_YYYY-MM-DD": "2026-06-23",
        "Open_Interest_All": 500000,
        "M_Money_Positions_Long_All": 200000,
        "M_Money_Positions_Short_All": 120000,
        "Prod_Merc_Positions_Long_All": 90000,
        "Prod_Merc_Positions_Short_All": 150000,
        "Swap_Positions_Long_All": 60000,
        "Swap_Positions_Short_All": 40000,
    }])


class TestCotRegistry:
    def test_es_maps_to_tff(self):
        adapter = get_cot_adapter(get_instrument("ES"))
        assert isinstance(adapter, TffAdapter)
        assert isinstance(adapter, CotReportAdapter)

    def test_cl_maps_to_disaggregated(self):
        adapter = get_cot_adapter(get_instrument("CL"))
        assert isinstance(adapter, DisaggregatedAdapter)

    def test_unmapped_symbol_raises(self):
        class _Fake:
            symbol = "ZZ"
        with pytest.raises((ValueError, KeyError)):
            get_cot_adapter(_Fake())


class TestTffAdapter:
    def test_canonical_schema(self):
        out = _normalize_tff_columns(_tff_fixture())
        assert CANONICAL_COLS.issubset(set(out.columns))

    def test_approved_category_mapping(self):
        out = _normalize_tff_columns(_tff_fixture())
        row = out.iloc[0]
        # MM <- Leveraged Funds
        assert row["MM_Long"] == 185058
        assert row["MM_Short"] == 558526
        # Prod <- Asset Manager / Institutional
        assert row["Prod_Long"] == 1171421
        assert row["Prod_Short"] == 178692
        # Spec <- Dealer / Intermediary
        assert row["Spec_Long"] == 112578
        assert row["Spec_Short"] == 868478
        assert row["OI"] == 1980254

    def test_date_parsed(self):
        out = _normalize_tff_columns(_tff_fixture())
        assert pd.Timestamp(out.iloc[0]["Date"]) == pd.Timestamp("2026-06-23")

    def test_adapter_urls_are_financial_and_start_2010(self):
        a = TffAdapter()
        assert "fut_fin_txt" in a.year_url(2015)
        assert a.combined_url is None
        assert a.start_year == 2010


class TestDisaggregatedAdapterRegression:
    """CL path must be unchanged: adapter delegates to legacy normalizer."""

    def test_adapter_matches_legacy_normalizer(self):
        fx = _disagg_fixture()
        via_adapter = DisaggregatedAdapter().normalize(fx.copy())
        via_legacy = _normalize_cot_columns(fx.copy())
        pd.testing.assert_frame_equal(via_adapter, via_legacy)

    def test_disagg_roles(self):
        out = _normalize_cot_columns(_disagg_fixture())
        row = out.iloc[0]
        assert row["MM_Long"] == 200000 and row["MM_Short"] == 120000
        assert row["Prod_Long"] == 90000 and row["Prod_Short"] == 150000
        assert row["Spec_Long"] == 60000 and row["Spec_Short"] == 40000

    def test_disagg_urls_are_commodity(self):
        a = DisaggregatedAdapter()
        assert "fut_disagg_txt" in a.year_url(2015)
        assert a.combined_url is not None
        assert a.start_year == 2006
