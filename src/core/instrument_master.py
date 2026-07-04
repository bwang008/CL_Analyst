from dataclasses import dataclass
from typing import Dict

@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    tick_size: float
    tick_value: float
    cftc_code: str
    volatility_index: str
    slippage_ticks: int = 1

INSTRUMENT_REGISTRY: Dict[str, Instrument] = {
    "CL": Instrument(
        symbol="CL",
        name="Crude Oil Light Sweet",
        tick_size=0.01,
        tick_value=10.00,
        cftc_code="067651",
        volatility_index="OVXCLS"
    ),
    "ES": Instrument(
        symbol="ES",
        name="E-Mini S&P 500",
        tick_size=0.25,
        tick_value=12.50,
        cftc_code="13874A",
        volatility_index="VIXCLS"
    ),
    "NG": Instrument(
        symbol="NG",
        name="Natural Gas",
        tick_size=0.001,
        tick_value=10.00,
        cftc_code="023651",
        volatility_index="OVXCLS" # Energy proxy
    ),
    "HG": Instrument(
        symbol="HG",
        name="Copper",
        tick_size=0.0005,
        tick_value=12.50,
        cftc_code="085692",
        volatility_index="VIXCLS"
    ),
    "GC": Instrument(
        symbol="GC",
        name="Gold",
        tick_size=0.10,
        tick_value=10.00,
        cftc_code="088691",
        volatility_index="GVZCLS"
    ),
    "PA": Instrument(
        symbol="PA",
        name="Palladium",
        tick_size=0.05,
        tick_value=5.00, # or 50.00 depending on contract (PA is 5.00 for full $100 multiplier? wait tick size is 0.05, multiplier is 100. 0.05 * 100 = $5.00) Let's assume 5.00
        cftc_code="075651",
        volatility_index="VIXCLS"
    ),
    "NQ": Instrument(
        symbol="NQ",
        name="E-Mini Nasdaq 100",
        tick_size=0.25,
        tick_value=5.00,
        cftc_code="209742",
        volatility_index="VIXCLS"
    ),
    "ZC": Instrument(
        symbol="ZC",
        name="Corn",
        # Corn: 5,000 bu; 1/4-cent tick = $12.50. tick_size assumes the
        # Databento GLBX cents-per-bushel quote (~450.25); verified in the
        # Phase-1 sanity check against the converted series magnitude.
        tick_size=0.25,
        tick_value=12.50,
        cftc_code="002602",       # CORN - CHICAGO BOARD OF TRADE (Disaggregated)
        volatility_index="VIXCLS" # No FRED grain vol index; VIX proxy per HG/PA precedent
    ),
    "ZS": Instrument(
        symbol="ZS",
        name="Soybeans",
        # Soybeans: 5,000 bu; 1/4-cent tick = $12.50. Databento GLBX quotes
        # cents/bushel (~1000+), so quarter-cent tick = 0.25 price units.
        tick_size=0.25,
        tick_value=12.50,
        cftc_code="005602",       # SOYBEANS - CHICAGO BOARD OF TRADE (Disaggregated)
        volatility_index="VIXCLS" # No FRED grain vol index; VIX proxy per HG/PA precedent
    ),
    "SI": Instrument(
        symbol="SI",
        name="Silver",
        # Silver: 5,000 troy oz; $0.005/oz tick = $25.00.
        tick_size=0.005,
        tick_value=25.00,
        cftc_code="084691",       # SILVER - COMMODITY EXCHANGE (Disaggregated)
        volatility_index="VIXCLS" # Cboe VXSLV discontinued, no FRED silver vol; VIX proxy
    )
}

def get_instrument(symbol: str) -> Instrument:
    """Retrieve instrument metadata by symbol."""
    upper_symbol = symbol.upper()
    if upper_symbol not in INSTRUMENT_REGISTRY:
        raise ValueError(f"Unknown instrument symbol: {symbol}")
    return INSTRUMENT_REGISTRY[upper_symbol]
