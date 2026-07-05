from dataclasses import dataclass
from typing import Dict, Optional, Tuple

@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    tick_size: float
    tick_value: float            # USD per tick
    cftc_code: str
    volatility_index: str        # FRED series, training side
    exchange: str                # IBKR exchange string: NYMEX/CME/COMEX/CBOT
    multiplier: int              # IB contract multiplier (units per quoted price point)
    quote_unit_usd: float        # USD per quoted price unit (1.0; 0.01 for grains quoted in cents)
    active_months: str           # MGL month codes, e.g. "HMUZ", "FGHJKMNQUVXZ"
    roll_reference: str          # "LTD" (last trade) or "FND" (first notice, physically delivered)
    roll_buffer_days: int        # calendar days before roll_reference to roll
    session_hours_ct: Tuple[Tuple[str, str], ...]  # ((open, close), ...) America/Chicago;
                                 # wraps midnight; approximate — T5 uses IB tradingHours as authority
    bars_per_day_5m: int         # conservative provisioning floor (CL pinned to legacy 288)
    bars_per_day_1h: int         # conservative provisioning floor (CL pinned to legacy 24)
    live_vol_index: str          # IBKR CBOE index symbol for daily-close fetch ("VIX"/"OVX"/"GVZ")
    micro_of: Optional[str] = None   # parent symbol if this IS a micro (MCL→"CL"); None otherwise
    slippage_ticks: int = 1

# Session shorthand: most CME Globex outrights trade 17:00–16:00 CT.
_GLOBEX_SESSION: Tuple[Tuple[str, str], ...] = (("17:00", "16:00"),)
# CBOT grains: overnight 19:00–07:45 CT + day 08:30–13:20 CT (daily halts between).
_GRAINS_SESSION: Tuple[Tuple[str, str], ...] = (("19:00", "07:45"), ("08:30", "13:20"))

# Invariant (test-enforced): tick_value == tick_size * multiplier * quote_unit_usd.
# Micros are execution-only: they inherit the parent's cftc_code and
# volatility_index (impact_review C1) and must never enter training
# pipelines under their own codes.
INSTRUMENT_REGISTRY: Dict[str, Instrument] = {
    "CL": Instrument(
        symbol="CL",
        name="Crude Oil Light Sweet",
        tick_size=0.01,
        tick_value=10.00,
        cftc_code="067651",
        volatility_index="OVXCLS",
        exchange="NYMEX",
        multiplier=1000,
        quote_unit_usd=1.0,
        active_months="FGHJKMNQUVXZ",
        roll_reference="LTD",
        roll_buffer_days=6,        # = legacy _EXPIRY_BUFFER_DAYS (zero-change)
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=288,       # legacy data_manager/live_trader constant (zero-change)
        bars_per_day_1h=24,        # legacy constant (zero-change)
        live_vol_index="OVX",
    ),
    "MCL": Instrument(
        symbol="MCL",
        name="Micro WTI Crude Oil",
        tick_size=0.01,
        tick_value=1.00,
        cftc_code="067651",        # inherited from CL (C1: execution-only micro)
        volatility_index="OVXCLS",  # inherited from CL (C1)
        exchange="NYMEX",
        multiplier=100,
        quote_unit_usd=1.0,
        active_months="FGHJKMNQUVXZ",
        roll_reference="LTD",
        roll_buffer_days=6,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=288,
        bars_per_day_1h=24,
        live_vol_index="OVX",
        micro_of="CL",
    ),
    "ES": Instrument(
        symbol="ES",
        name="E-Mini S&P 500",
        tick_size=0.25,
        tick_value=12.50,
        cftc_code="13874A",
        volatility_index="VIXCLS",
        exchange="CME",
        multiplier=50,
        quote_unit_usd=1.0,
        active_months="HMUZ",
        roll_reference="LTD",
        roll_buffer_days=8,        # volume-roll Monday ≈ 8 cal days pre 3rd-Friday expiry
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
    ),
    "MES": Instrument(
        symbol="MES",
        name="Micro E-Mini S&P 500",
        tick_size=0.25,
        tick_value=1.25,
        cftc_code="13874A",        # inherited from ES (C1)
        volatility_index="VIXCLS",  # inherited from ES (C1)
        exchange="CME",
        multiplier=5,
        quote_unit_usd=1.0,
        active_months="HMUZ",
        roll_reference="LTD",
        roll_buffer_days=8,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
        micro_of="ES",
    ),
    "NG": Instrument(
        symbol="NG",
        name="Natural Gas",
        tick_size=0.001,
        tick_value=10.00,
        cftc_code="023651",
        volatility_index="OVXCLS", # Energy proxy
        exchange="NYMEX",
        multiplier=10000,
        quote_unit_usd=1.0,
        active_months="FGHJKMNQUVXZ",
        roll_reference="LTD",
        roll_buffer_days=6,        # LTD = 3 biz days before delivery-month start
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="OVX",
    ),
    "HG": Instrument(
        symbol="HG",
        name="Copper",
        tick_size=0.0005,
        tick_value=12.50,
        cftc_code="085692",
        volatility_index="VIXCLS",
        exchange="COMEX",
        multiplier=25000,
        quote_unit_usd=1.0,
        active_months="HKNUZ",
        roll_reference="FND",
        roll_buffer_days=3,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
    ),
    "GC": Instrument(
        symbol="GC",
        name="Gold",
        tick_size=0.10,
        tick_value=10.00,
        cftc_code="088691",
        volatility_index="GVZCLS",
        exchange="COMEX",
        multiplier=100,
        quote_unit_usd=1.0,
        active_months="GJMQVZ",    # serials listed but illiquid — filter!
        roll_reference="FND",      # last biz day of month before delivery
        roll_buffer_days=3,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="GVZ",
    ),
    "MGC": Instrument(
        symbol="MGC",
        name="Micro Gold",
        tick_size=0.10,
        tick_value=1.00,
        cftc_code="088691",        # inherited from GC (C1)
        volatility_index="GVZCLS",  # inherited from GC (C1)
        exchange="COMEX",
        multiplier=10,
        quote_unit_usd=1.0,
        active_months="GJMQVZ",
        roll_reference="FND",
        roll_buffer_days=3,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="GVZ",
        micro_of="GC",
    ),
    "PA": Instrument(
        symbol="PA",
        name="Palladium",
        # CORRECTED in T1 (audit §4.1 flag, impact_review V6 approved):
        # NYMEX Palladium is 100 troy oz, tick $0.10 = $10.00. The prior
        # 0.05/$5.00 entry was wrong per NYMEX spec.
        tick_size=0.10,
        tick_value=10.00,
        cftc_code="075651",
        volatility_index="VIXCLS",
        exchange="NYMEX",
        multiplier=100,
        quote_unit_usd=1.0,
        active_months="HMUZ",
        roll_reference="FND",
        roll_buffer_days=3,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
    ),
    "NQ": Instrument(
        symbol="NQ",
        name="E-Mini Nasdaq 100",
        tick_size=0.25,
        tick_value=5.00,
        cftc_code="209742",
        volatility_index="VIXCLS",
        exchange="CME",
        multiplier=20,
        quote_unit_usd=1.0,
        active_months="HMUZ",
        roll_reference="LTD",
        roll_buffer_days=8,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
    ),
    "MNQ": Instrument(
        symbol="MNQ",
        name="Micro E-Mini Nasdaq 100",
        tick_size=0.25,
        tick_value=0.50,
        cftc_code="209742",        # inherited from NQ (C1)
        volatility_index="VIXCLS",  # inherited from NQ (C1)
        exchange="CME",
        multiplier=2,
        quote_unit_usd=1.0,
        active_months="HMUZ",
        roll_reference="LTD",
        roll_buffer_days=8,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
        micro_of="NQ",
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
        volatility_index="VIXCLS", # No FRED grain vol index; VIX proxy per HG/PA precedent
        exchange="CBOT",
        multiplier=5000,
        quote_unit_usd=0.01,      # quoted in cents/bushel
        active_months="HKNUZ",
        roll_reference="FND",
        roll_buffer_days=3,
        session_hours_ct=_GRAINS_SESSION,
        bars_per_day_5m=200,
        bars_per_day_1h=16,
        live_vol_index="VIX",
    ),
    "ZS": Instrument(
        symbol="ZS",
        name="Soybeans",
        # Soybeans: 5,000 bu; 1/4-cent tick = $12.50. Databento GLBX quotes
        # cents/bushel (~1000+), so quarter-cent tick = 0.25 price units.
        tick_size=0.25,
        tick_value=12.50,
        cftc_code="005602",       # SOYBEANS - CHICAGO BOARD OF TRADE (Disaggregated)
        volatility_index="VIXCLS", # No FRED grain vol index; VIX proxy per HG/PA precedent
        exchange="CBOT",
        multiplier=5000,
        quote_unit_usd=0.01,      # quoted in cents/bushel
        active_months="FHKNQUX",
        roll_reference="FND",
        roll_buffer_days=3,
        session_hours_ct=_GRAINS_SESSION,
        bars_per_day_5m=200,
        bars_per_day_1h=16,
        live_vol_index="VIX",
    ),
    "SI": Instrument(
        symbol="SI",
        name="Silver",
        # Silver: 5,000 troy oz; $0.005/oz tick = $25.00.
        tick_size=0.005,
        tick_value=25.00,
        cftc_code="084691",       # SILVER - COMMODITY EXCHANGE (Disaggregated)
        volatility_index="VIXCLS", # Cboe VXSLV discontinued, no FRED silver vol; VIX proxy
        exchange="COMEX",
        multiplier=5000,
        quote_unit_usd=1.0,
        active_months="HKNUZ",
        roll_reference="FND",
        roll_buffer_days=3,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
    ),
    "SIL": Instrument(
        symbol="SIL",
        name="Micro Silver",
        # Micro Silver (1,000-oz): $0.005/oz tick = $5.00.
        tick_size=0.005,
        tick_value=5.00,
        cftc_code="084691",       # inherited from SI (C1)
        volatility_index="VIXCLS", # inherited from SI (C1)
        exchange="COMEX",
        multiplier=1000,
        quote_unit_usd=1.0,
        active_months="HKNUZ",
        roll_reference="FND",
        roll_buffer_days=3,
        session_hours_ct=_GLOBEX_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
        micro_of="SI",
    ),
}

def get_instrument(symbol: str) -> Instrument:
    """Retrieve instrument metadata by symbol."""
    upper_symbol = symbol.upper()
    if upper_symbol not in INSTRUMENT_REGISTRY:
        raise ValueError(f"Unknown instrument symbol: {symbol}")
    return INSTRUMENT_REGISTRY[upper_symbol]
