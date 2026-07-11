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
    roll_ratio_tolerance: float  # T5: ratio-space noise floor for roll adjustment —
                                 # |1 - ratio| <= tolerance means the roll is DETECTED but the
                                 # adjustment (cache/ledger scale + roll_history) is SKIPPED.
                                 # CL/MCL 0.01 = legacy _ROLL_PRICE_TOLERANCE (pinned); all
                                 # others 0.001 (10 bps — below real ES/GC roll gaps, far above
                                 # overlap-median noise; Q2 ACKed). REQUIRED — no default.
    micro_of: Optional[str] = None   # parent symbol if this IS a micro (MCL→"CL"); None otherwise
    slippage_ticks: int = 1
    # IBKR search symbol — differs from the registry key ONLY when a product
    # shares its parent's IB symbol. COMEX Micro Silver (1,000 oz) is NOT a
    # standalone IB symbol like MGC/MES: IBKR lists it under symbol "SI" with
    # tradingClass "SIL". None → the IB symbol equals `symbol`.
    ib_symbol: Optional[str] = None
    # IBKR tradingClass filter. REQUIRED whenever several futures share one IB
    # symbol so reqContractDetails / position matching pick the right product:
    # COMEX "SI" hosts BOTH full SI (×5000, tradingClass "SI") and micro SIL
    # (×1000, tradingClass "SIL"). None → single-product symbol (CL/ES/MGC/…),
    # no filter applied (byte-identical to the pre-tradingClass behavior).
    ib_trading_class: Optional[str] = None

    @property
    def ib_search_symbol(self) -> str:
        """Symbol passed to IBKR contract construction (defaults to `symbol`)."""
        return self.ib_symbol or self.symbol

# Session shorthand: most CME Globex outrights trade 17:00–16:00 CT.
_GLOBEX_SESSION: Tuple[Tuple[str, str], ...] = (("17:00", "16:00"),)
# CBOT grains: overnight 19:00–07:45 CT + day 08:30–13:20 CT (daily halts between).
_GRAINS_SESSION: Tuple[Tuple[str, str], ...] = (("19:00", "07:45"), ("08:30", "13:20"))
# CME equity index outrights: 17:00-16:00 CT with a REAL 15:15-15:30 CT daily
# halt (Mon-Fri) — T5 impact_review V4 sourced finding, C4 amendment landed in
# T7 (t7-es-ops-runway). Segment 1 wraps midnight (17:00 → 15:15 next day);
# segment 2 is the 15:30-16:00 post-halt reopen. Carried by EXACTLY
# ES/MES/NQ/MNQ (micros move with parents — pinned).
_EQUITY_SESSION: Tuple[Tuple[str, str], ...] = (("17:00", "15:15"), ("15:30", "16:00"))

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
        # jit-roll-ratio-empty_07102026_1453 (Stage 2, Amendment 2): 0.01 ->
        # 0.001, deliberately REVERSING the T5 zero-change pin — real CL roll
        # gaps (0.2-2.8%) were silently tolerance-swallowed even when
        # correctly witnessed. 10 bps matches every other symbol.
        roll_ratio_tolerance=0.001,
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
        # jit-roll-ratio-empty_07102026_1453: moves with CL (0.01 -> 0.001) —
        # MCL shares CL's metadata-file semantics (parent-shared file).
        roll_ratio_tolerance=0.001,
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
        session_hours_ct=_EQUITY_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
        roll_ratio_tolerance=0.001,  # 10 bps: real ES roll gaps are ~20-70 bps (Q2)
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
        session_hours_ct=_EQUITY_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
        roll_ratio_tolerance=0.001,
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
        roll_ratio_tolerance=0.001,
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
        roll_ratio_tolerance=0.001,
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
        roll_ratio_tolerance=0.001,  # 10 bps: real GC roll gaps are ~50-100 bps (Q2)
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
        roll_ratio_tolerance=0.001,
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
        roll_ratio_tolerance=0.001,
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
        session_hours_ct=_EQUITY_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
        roll_ratio_tolerance=0.001,
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
        session_hours_ct=_EQUITY_SESSION,
        bars_per_day_5m=276,
        bars_per_day_1h=23,
        live_vol_index="VIX",
        roll_ratio_tolerance=0.001,
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
        roll_ratio_tolerance=0.001,
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
        roll_ratio_tolerance=0.001,
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
        roll_ratio_tolerance=0.001,
        # Full silver and micro silver share IBKR symbol "SI"; pin the
        # tradingClass so reqContractDetails / position matching resolve the
        # ×5000 contract deterministically (not by IBKR return order).
        ib_trading_class="SI",
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
        roll_ratio_tolerance=0.001,
        micro_of="SI",
        # IBKR has NO future under symbol "SIL" (that's the Global X Silver
        # Miners ETF stock). Micro Silver lists under symbol "SI" +
        # tradingClass "SIL" (localSymbols SILU6, SILZ6, …), multiplier 1000.
        ib_symbol="SI",
        ib_trading_class="SIL",
    ),
}

def get_instrument(symbol: str) -> Instrument:
    """Retrieve instrument metadata by symbol."""
    upper_symbol = symbol.upper()
    if upper_symbol not in INSTRUMENT_REGISTRY:
        raise ValueError(f"Unknown instrument symbol: {symbol}")
    return INSTRUMENT_REGISTRY[upper_symbol]


def contract_matches(
    symbol: str,
    contract_symbol: Optional[str],
    contract_trading_class: Optional[str] = None,
) -> bool:
    """True if an IBKR contract belongs to the registry `symbol`.

    Matches on the IBKR search symbol and — only when the instrument pins an
    ``ib_trading_class`` — on tradingClass too. That tradingClass gate is what
    keeps full silver (IB symbol "SI", tclass "SI", ×5000) and micro silver
    (IB symbol "SI", tclass "SIL", ×1000) from being confused in position /
    portfolio reads. Single-product symbols (CL/ES/MGC/…) pin no tradingClass,
    so they match on IB symbol alone — byte-identical to the legacy
    ``contract.symbol == symbol`` comparison.

    Tolerant by design: an unknown `symbol` (not in the registry) falls back to
    a plain IB-symbol comparison and never raises, preserving the legacy
    "unheld symbol reads as flat" contract of cache-position reads.
    """
    try:
        inst = get_instrument(symbol)
    except ValueError:
        return contract_symbol == symbol
    if contract_symbol != inst.ib_search_symbol:
        return False
    if inst.ib_trading_class is not None and (
        contract_trading_class != inst.ib_trading_class
    ):
        return False
    return True


def registry_symbol_for_contract(
    contract_symbol: Optional[str],
    contract_trading_class: Optional[str] = None,
) -> Optional[str]:
    """Reverse of ``ib_search_symbol``/``ib_trading_class``: map an IBKR
    contract back to its registry (config) symbol.

    A micro-silver fill arrives from IBKR as symbol "SI" + tradingClass "SIL"
    and must be stamped back to "SIL" so it routes to the SIL live instance
    (whose ``execution_symbol`` is "SIL"). Single-product contracts map to
    themselves. An unknown contract passes through unchanged (never raises).
    """
    if contract_symbol is None:
        return contract_symbol
    for key, inst in INSTRUMENT_REGISTRY.items():
        if inst.ib_search_symbol != contract_symbol:
            continue
        if inst.ib_trading_class is None or (
            inst.ib_trading_class == contract_trading_class
        ):
            return key
    return contract_symbol


def dollars_per_point(symbol: str) -> float:
    """USD PnL per 1.0 quoted-price-unit move per contract.

    This is the value backtest engines call ``contract_multiplier``:
    CL 1000.0, ES 50.0, NG 10000.0, ZC 50.0 (5000 bu x $0.01/cent).
    """
    inst = get_instrument(symbol)
    return float(inst.multiplier) * float(inst.quote_unit_usd)


def default_slippage_points(symbol: str) -> float:
    """Default per-side slippage in quoted price units (slippage_ticks x tick_size).

    CL resolves to 0.01 — identical to the legacy hardcoded constant.
    """
    inst = get_instrument(symbol)
    return float(inst.slippage_ticks) * float(inst.tick_size)


# ---------------------------------------------------------------------------
# T3 (t3-tick-order-pricing): order-price quantization helper.
# APPEND-ONLY block — transcribed VERBATIM from the ticket audit section 3.1
# (validated bit-exact by auditor + reviewer; the power-of-ten fast path is
# MANDATORY for CL byte-identity with legacy round(price, 2)).
# ---------------------------------------------------------------------------

import math
from decimal import Decimal
from functools import lru_cache

@lru_cache(maxsize=None)
def _tick_grid(tick_size: float) -> tuple[int, bool]:
    """(decimals, is_power_of_ten) for a tick size, via its shortest repr."""
    if not (isinstance(tick_size, float) and math.isfinite(tick_size) and tick_size > 0):
        raise ValueError(f"Invalid tick_size {tick_size!r}: must be a finite float > 0")
    d = Decimal(str(tick_size)).normalize()
    return max(0, -d.as_tuple().exponent), d.as_tuple().digits == (1,)

def round_to_tick(price: float, tick_size: float) -> float:
    """Snap a price to the instrument tick grid, round-half-even to nearest.

    Power-of-ten ticks (0.01, 0.1, 0.001) use round(price, n) — BIT-IDENTICAL
    to the legacy CL cent rounding by construction (hard constraint: a naive
    round(price/tick)*tick mismatches round(price, 2) on ~1.4% of inputs).
    Other ticks (0.25, 0.005) round the tick count and reconstruct the
    canonical n-decimal double. Raises on non-finite price / invalid tick.
    """
    if not math.isfinite(price):
        raise ValueError(f"Cannot round non-finite price {price!r} to tick")
    nd, pow10 = _tick_grid(tick_size)
    if pow10:
        return round(price, nd)
    return round(round(price / tick_size) * tick_size, nd)
