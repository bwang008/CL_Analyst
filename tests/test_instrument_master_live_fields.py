"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/core/instrument_master.py
Target Class/Function: Instrument (dataclass), INSTRUMENT_REGISTRY, get_instrument
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t1-instrument-metadata_07042026_1543
Phase: RED — these tests MUST fail until the Coder extends the registry
per audit.md section 4.1 (blueprint: t1-instrument-metadata_07042026_1543).

Coverage (audit.md section 6, tests 1-5 + spec pins from the section 4.1 table):
  - Registry completeness of all new live-execution fields.
  - Invariant: tick_value == tick_size * multiplier * quote_unit_usd.
  - Micro entries (MCL/MES/MNQ/MGC/SIL) exist, micro_of set, and inherit
    parent cftc_code/volatility_index (impact_review condition C1).
  - CL legacy values byte-pinned (zero-behavior-change guard).
  - Verified contract-spec pins incl. the PA correction (0.10 tick / $10).
  - get_instrument now resolves micro symbols (case-insensitive) and still
    raises on unknown symbols.

NOTE: test_schemas.py keeps its existing get_instrument coverage untouched;
this file is the dedicated home for the T1 live-field additions.
"""

import re

import pytest

from src.core.instrument_master import INSTRUMENT_REGISTRY, get_instrument

# ---------------------------------------------------------------------------
# Contract constants (audit.md section 4.1 — verified against CME Group specs)
# ---------------------------------------------------------------------------

# micro symbol -> parent symbol (audit section 4.1 + impact_review C1)
MICRO_PARENTS = {
    "MCL": "CL",
    "MES": "ES",
    "MNQ": "NQ",
    "MGC": "GC",
    "SIL": "SI",
}

EXPECTED_SYMBOLS = {
    "CL", "ES", "NG", "HG", "GC", "PA", "NQ", "ZC", "ZS", "SI",
    "MCL", "MES", "MNQ", "MGC", "SIL",
}

VALID_EXCHANGES = {"NYMEX", "CME", "COMEX", "CBOT"}
VALID_MONTH_CODES = set("FGHJKMNQUVXZ")
VALID_ROLL_REFERENCES = {"LTD", "FND"}
_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")


# ---------------------------------------------------------------------------
# 1. Registry completeness (audit test 1)
# ---------------------------------------------------------------------------

def test_registry_contains_all_expected_symbols():
    """All 10 legacy symbols plus the 5 first-class micro entries exist."""
    missing = EXPECTED_SYMBOLS - set(INSTRUMENT_REGISTRY.keys())
    assert not missing, f"INSTRUMENT_REGISTRY missing entries: {sorted(missing)}"


@pytest.mark.parametrize("symbol", sorted(EXPECTED_SYMBOLS))
def test_registry_completeness(symbol):
    """Every entry carries every new required live field with a sane value.

    No silent null defaults (house rule): every field must be populated at
    registry construction time.
    """
    inst = INSTRUMENT_REGISTRY[symbol]

    # exchange
    assert inst.exchange in VALID_EXCHANGES, (
        f"{symbol}: exchange {inst.exchange!r} not in {sorted(VALID_EXCHANGES)}"
    )

    # multiplier / quote_unit_usd
    assert isinstance(inst.multiplier, int) and inst.multiplier > 0, (
        f"{symbol}: multiplier must be a positive int, got {inst.multiplier!r}"
    )
    assert inst.quote_unit_usd > 0, (
        f"{symbol}: quote_unit_usd must be > 0, got {inst.quote_unit_usd!r}"
    )

    # active_months: non-empty, only MGL month codes
    assert isinstance(inst.active_months, str) and inst.active_months, (
        f"{symbol}: active_months must be a non-empty string"
    )
    bad_codes = set(inst.active_months) - VALID_MONTH_CODES
    assert not bad_codes, (
        f"{symbol}: active_months contains invalid month codes {sorted(bad_codes)}"
    )

    # roll reference / buffer
    assert inst.roll_reference in VALID_ROLL_REFERENCES, (
        f"{symbol}: roll_reference {inst.roll_reference!r} not in LTD/FND"
    )
    assert isinstance(inst.roll_buffer_days, int) and inst.roll_buffer_days > 0, (
        f"{symbol}: roll_buffer_days must be a positive int"
    )

    # session hours: non-empty tuple of (open, close) HH:MM pairs (CT)
    assert isinstance(inst.session_hours_ct, tuple) and len(inst.session_hours_ct) >= 1, (
        f"{symbol}: session_hours_ct must be a non-empty tuple"
    )
    for pair in inst.session_hours_ct:
        assert len(pair) == 2, (
            f"{symbol}: session_hours_ct entries must be (open, close) pairs, got {pair!r}"
        )
        for hhmm in pair:
            assert isinstance(hhmm, str) and _HHMM_RE.match(hhmm), (
                f"{symbol}: session hour {hhmm!r} is not HH:MM"
            )

    # bar provisioning floors
    assert isinstance(inst.bars_per_day_5m, int) and inst.bars_per_day_5m > 0, (
        f"{symbol}: bars_per_day_5m must be a positive int"
    )
    assert isinstance(inst.bars_per_day_1h, int) and inst.bars_per_day_1h > 0, (
        f"{symbol}: bars_per_day_1h must be a positive int"
    )
    assert inst.bars_per_day_5m > inst.bars_per_day_1h, (
        f"{symbol}: 5m bar floor must exceed 1h bar floor"
    )

    # live vol index (IBKR/CBOE symbol — distinct from FRED volatility_index)
    assert isinstance(inst.live_vol_index, str) and inst.live_vol_index, (
        f"{symbol}: live_vol_index must be a non-empty string"
    )

    # pre-existing required fields must still be populated
    assert inst.cftc_code, f"{symbol}: cftc_code must be non-empty"
    assert inst.volatility_index, f"{symbol}: volatility_index must be non-empty"

    # micro_of: set only on micros, None on parents/outrights
    if symbol in MICRO_PARENTS:
        assert inst.micro_of == MICRO_PARENTS[symbol], (
            f"{symbol}: micro_of must be {MICRO_PARENTS[symbol]!r}, got {inst.micro_of!r}"
        )
    else:
        assert inst.micro_of is None, (
            f"{symbol}: micro_of must be None for non-micro entries, got {inst.micro_of!r}"
        )


# ---------------------------------------------------------------------------
# 2. Tick-value invariant (audit test 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol", sorted(EXPECTED_SYMBOLS))
def test_tick_value_invariant(symbol):
    """tick_value == tick_size * multiplier * quote_unit_usd for every entry.

    Grains (ZC/ZS, quoted in cents/bushel) prove the 0.01 quote-unit factor:
    0.25 * 5000 * 0.01 == 12.50.
    """
    inst = INSTRUMENT_REGISTRY[symbol]
    expected = inst.tick_size * inst.multiplier * inst.quote_unit_usd
    assert inst.tick_value == pytest.approx(expected, abs=1e-9), (
        f"{symbol}: tick_value {inst.tick_value} != tick_size {inst.tick_size} "
        f"* multiplier {inst.multiplier} * quote_unit_usd {inst.quote_unit_usd} "
        f"(= {expected})"
    )


# ---------------------------------------------------------------------------
# 3. Micro entries (audit test 3 + impact_review condition C1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("micro,parent", sorted(MICRO_PARENTS.items()))
def test_micro_entries_consistent(micro, parent):
    """Micros mirror the parent contract in everything except size.

    C1 (impact review): micros inherit the parent's cftc_code and
    volatility_index — micros are execution-only and must never enter
    training pipelines under their own codes.
    """
    assert micro in INSTRUMENT_REGISTRY, f"{micro} missing from INSTRUMENT_REGISTRY"
    assert parent in INSTRUMENT_REGISTRY, f"micro_of target {parent} missing"

    m = INSTRUMENT_REGISTRY[micro]
    p = INSTRUMENT_REGISTRY[parent]

    assert m.micro_of == parent

    # C1: training-side identity inherited from parent
    assert m.cftc_code == p.cftc_code, (
        f"{micro}: cftc_code must inherit parent {parent}'s ({p.cftc_code!r})"
    )
    assert m.volatility_index == p.volatility_index, (
        f"{micro}: volatility_index must inherit parent {parent}'s "
        f"({p.volatility_index!r})"
    )

    # Contract mechanics mirror the parent (audit section 4.1 table)
    assert m.exchange == p.exchange
    assert m.tick_size == p.tick_size
    assert m.quote_unit_usd == p.quote_unit_usd
    assert m.active_months == p.active_months
    assert m.roll_reference == p.roll_reference
    assert m.roll_buffer_days == p.roll_buffer_days
    assert m.session_hours_ct == p.session_hours_ct
    assert m.bars_per_day_5m == p.bars_per_day_5m
    assert m.bars_per_day_1h == p.bars_per_day_1h
    assert m.live_vol_index == p.live_vol_index

    # Size: micro is strictly smaller than the parent
    assert m.multiplier < p.multiplier
    assert m.tick_value < p.tick_value


# ---------------------------------------------------------------------------
# 4. CL legacy byte-pins (audit test 4 — zero-behavior-change guard)
# ---------------------------------------------------------------------------

def test_cl_legacy_values_unchanged():
    """CL's pre-existing field values are byte-identical after the extension."""
    cl = INSTRUMENT_REGISTRY["CL"]
    assert cl.symbol == "CL"
    assert cl.tick_size == 0.01
    assert cl.tick_value == 10.00
    assert cl.cftc_code == "067651"
    assert cl.volatility_index == "OVXCLS"
    assert cl.slippage_ticks == 1


def test_cl_live_fields_pinned():
    """CL new fields pinned to the legacy live-engine constants (audit 4.1).

    bars_per_day 288/24 = legacy data_manager/live_trader constants;
    roll_buffer_days 6 = current _EXPIRY_BUFFER_DAYS. Changing any of these
    is a T1 regression, not a spec update.
    """
    cl = INSTRUMENT_REGISTRY["CL"]
    assert cl.exchange == "NYMEX"
    assert cl.multiplier == 1000
    assert cl.quote_unit_usd == 1.0
    assert cl.active_months == "FGHJKMNQUVXZ"
    assert cl.roll_reference == "LTD"
    assert cl.roll_buffer_days == 6
    assert cl.bars_per_day_5m == 288
    assert cl.bars_per_day_1h == 24
    assert cl.live_vol_index == "OVX"
    assert cl.micro_of is None


# ---------------------------------------------------------------------------
# 5. Verified contract-spec pins (audit section 4.1 table)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "symbol,exchange,tick_size,tick_value,multiplier,quote_unit_usd",
    [
        ("CL", "NYMEX", 0.01, 10.00, 1000, 1.0),
        ("ES", "CME", 0.25, 12.50, 50, 1.0),
        ("NQ", "CME", 0.25, 5.00, 20, 1.0),
        ("GC", "COMEX", 0.10, 10.00, 100, 1.0),
        ("SI", "COMEX", 0.005, 25.00, 5000, 1.0),
        ("ZC", "CBOT", 0.25, 12.50, 5000, 0.01),
        ("ZS", "CBOT", 0.25, 12.50, 5000, 0.01),
        ("NG", "NYMEX", 0.001, 10.00, 10000, 1.0),
        ("HG", "COMEX", 0.0005, 12.50, 25000, 1.0),
        # PA CORRECTED in T1: legacy 0.05/$5.00 entry was wrong per NYMEX spec
        # (100 troy oz, $0.10 tick = $10.00). Approved by impact_review V6.
        ("PA", "NYMEX", 0.10, 10.00, 100, 1.0),
        # Micros
        ("MCL", "NYMEX", 0.01, 1.00, 100, 1.0),
        ("MES", "CME", 0.25, 1.25, 5, 1.0),
        ("MNQ", "CME", 0.25, 0.50, 2, 1.0),
        ("MGC", "COMEX", 0.10, 1.00, 10, 1.0),
        ("SIL", "COMEX", 0.005, 5.00, 1000, 1.0),
    ],
)
def test_verified_contract_specs(
    symbol, exchange, tick_size, tick_value, multiplier, quote_unit_usd
):
    inst = INSTRUMENT_REGISTRY[symbol]
    assert inst.exchange == exchange
    assert inst.tick_size == tick_size
    assert inst.tick_value == tick_value
    assert inst.multiplier == multiplier
    assert inst.quote_unit_usd == quote_unit_usd


@pytest.mark.parametrize(
    "symbol,active_months",
    [
        ("CL", "FGHJKMNQUVXZ"),
        ("NG", "FGHJKMNQUVXZ"),
        ("ES", "HMUZ"),
        ("NQ", "HMUZ"),
        ("GC", "GJMQVZ"),
        ("SI", "HKNUZ"),
        ("ZC", "HKNUZ"),
        ("ZS", "FHKNQUX"),
    ],
)
def test_active_months_pins(symbol, active_months):
    assert INSTRUMENT_REGISTRY[symbol].active_months == active_months


@pytest.mark.parametrize(
    "symbol,roll_reference",
    [
        ("CL", "LTD"),
        ("ES", "LTD"),
        ("NG", "LTD"),
        ("GC", "FND"),
        ("SI", "FND"),
        ("ZC", "FND"),
        ("ZS", "FND"),
    ],
)
def test_roll_reference_pins(symbol, roll_reference):
    assert INSTRUMENT_REGISTRY[symbol].roll_reference == roll_reference


@pytest.mark.parametrize(
    "symbol,live_vol_index",
    [
        ("CL", "OVX"),
        ("NG", "OVX"),
        ("ES", "VIX"),
        ("GC", "GVZ"),
        ("ZC", "VIX"),
    ],
)
def test_live_vol_index_pins(symbol, live_vol_index):
    assert INSTRUMENT_REGISTRY[symbol].live_vol_index == live_vol_index


# ---------------------------------------------------------------------------
# 6. get_instrument behavior (audit test 5 — extends test_schemas.py coverage)
# ---------------------------------------------------------------------------

def test_get_instrument_micro_symbols_resolve():
    """get_instrument('MCL') must succeed now that micros are first-class."""
    mcl = get_instrument("MCL")
    assert mcl.symbol == "MCL"
    assert mcl.micro_of == "CL"


def test_get_instrument_micro_lowercase_normalizes():
    """Case normalization preserved for micros (impact_review C2 analogue)."""
    assert get_instrument("mcl") is get_instrument("MCL")


def test_get_instrument_unknown_still_raises():
    """Unknown-symbol raise behavior is unchanged."""
    with pytest.raises(ValueError, match="Unknown instrument symbol"):
        get_instrument("NOT_A_SYMBOL")
