"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/core/instrument_master.py
                            (APPEND-ONLY: round_to_tick + cached _tick_grid,
                            audit section 3.1 helper VERBATIM — the
                            power-of-ten fast path is MANDATORY),
                            src/live_execution/ibkr_client.py
                            (S1 delete _CL_TICK_SIZE; S2/S4/S5 marketable-
                            limit branches resolve tick from
                            get_instrument(<contract|pos.contract>.symbol),
                            buffer = 2*tick, snap via round_to_tick; X1/X2
                            NYMEX injection replaced by registry exchange;
                            R1 adaptive-exit snap — ZERO signature changes),
                            src/live_execution/live_trader.py
                            (_tick_size property mirroring the T2
                            _brain_symbol seam pattern; S6 trailing SL snap;
                            S7 six child-price sites snap; R2 recovery
                            re-place snap)
Target Class/Function: round_to_tick, _tick_grid,
                       IBKRConnectionManager.place_bracket_order,
                       IBKRConnectionManager.place_entry_order,
                       IBKRConnectionManager.close_cl_position,
                       IBKRConnectionManager.close_cl_position_market,
                       LiveTrader._tick_size,
                       LiveTrader._check_trailing_stop,
                       LiveTrader._place_bracket_children_on_fill,
                       LiveTrader._recover_inherited_position
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t3-tick-order-pricing_07042026_1954
Phase: RED — round_to_tick / _tick_grid do NOT exist in
src.core.instrument_master yet; this file fails at collection (ghost import
per TDD protocol) until the Coder implements audit.md section 3 under the
blueprint's CRITICAL float-precision constraint.

Coverage (audit.md section 6 items 1-18 + blueprint test requirements +
impact_review REC-1/REC-2, manager rulings R1/R2 adopted):
  - TestRoundToTickCLParity: THE regression pin — bit-level identity of
    round_to_tick(x, 0.01) with legacy round(x, 2) on the adversarial
    x.xx5 / negative / -0.0 / large-magnitude set, a seeded 100k uniform
    sweep over +/-200 plus the full half-cent lattice; GC 0.10 vs
    round(x,1) and NG 0.001 vs round(x,3) sweeps; REC-1 composition pin
    (round_to_tick(x +/- 2*0.01, 0.01) bit-equal round(x +/- 0.02, 2))
    closing the parity-gate invisibility of the marketable-limit sites.
  - TestRoundToTickGeneralGrids: ES/NQ/ZC/ZS 0.25, GC 0.10, SI 0.005,
    NG 0.001, HG 0.0005 — outputs on-grid (tolerance-free exact Decimal
    remainder on the shortest repr), idempotent, nearest/half-even
    semantics spot pins (6000.12->6000.00, 6000.13->6000.25, half-even
    at exactly representable .5 quotients), _tick_grid classification
    table for every registry tick.
  - TestRoundToTickErrors: NaN/inf price raises; tick <= 0 / NaN / inf
    raises; None raises. No silent defaults.
  - TestMarketableLimitTickAware: mocked IB manager (zero signature
    changes) — ES ml entry/exit on the 0.25 grid with buffer 2*0.25;
    CL ml bit-identical to today's round(limit +/- 0.02, 2) incl. an
    off-grid base and a seeded composition sweep through the REAL
    place_entry_order; GC/NG entry spot pins; unknown contract symbol
    raises with no order placed; _CL_TICK_SIZE attribute GONE.
    R1 (adaptive-exit snap) pins live HERE, not in the LiveTrader class:
    the R1 site is ibkr_client.close_cl_position's adaptive branch —
    identity for on-grid inputs, snap for off-grid inputs.
  - TestExitExchangeFromRegistry: close_cl_position(+_market) inject
    pos.contract.exchange from the registry via pos.contract.symbol
    (CL->NYMEX regression pin, MCL->NYMEX, ES->CME, GC->COMEX, ZC->CBOT),
    injected BEFORE placeOrder; a position in an UNREGISTERED symbol is
    SKIPPED by the pre-existing filter — no order, no raise (blueprint:
    filter semantics unchanged; governs over audit item 11's variant).
  - TestLiveTraderTickSites: _tick_size property (context -> execution
    instrument tick, NOT brain; __new__ seam structural fallback via
    _execution_symbol; unresolvable raises); S6 trailing — ES lands on
    the 0.25 grid, CL branch pinned by a seeded sweep through the REAL
    _check_trailing_stop (REC-2 — the parity gate runs --disable-trailing
    so S6 has unit-pin-only coverage); S7 — THE naked-stop scenario:
    ES entry fill -> TP and SL children tick-valid on 0.25 (scalar,
    tiered, and SELL-side mirror; lot arithmetic byte-identical), GC
    tiered on 0.10, CL byte-identical to legacy incl. the half-cent
    fill; R2 recovery re-place snap (#18) — CL ledger prices pass
    through bit-identical, off-grid ES ledger row snapped.

Out of scope, deliberately NOT tested here (blueprint scope guards):
  - the Q3 hasattr/modify_order dead-path (separate ticket
    live-trailing-modify-order-dead_07042026_2012) — trailing tests
    assert only on the computed SL price, never on modify_order;
  - entry-price snapping for adaptive/market entries (deferred, 3.4);
  - backtest engine / parity harness / configurable_strategy rounding;
  - tests/test_bracket_order.py churn (Coder owns the 4 mechanical
    contract.symbol = "CL" additions).

All I/O boundaries are mocked (patched ib_insync IB, MagicMock exec
clients, MagicMock telemetry); no IBKR gateway, no real data files.
LiveTrader seams mirror tests/test_live_trader_bugs.py (__new__) and
tests/test_symbol_data_paths.py (full init with mocked DataManager).
"""

from __future__ import annotations

import math
import random
import struct
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.core.instrument_master import INSTRUMENT_REGISTRY, get_instrument
from src.live_execution.ibkr_client import IBKRConnectionManager
from src.live_execution.instrument_context import (
    InstrumentContext,
    resolve_instrument_context,
)
from src.live_execution.live_trader import LiveTrader

# Ghost import — the Coder appends these to src/core/instrument_master.py
# (audit.md section 3.1 VERBATIM). RED: fails at collection until then.
from src.core.instrument_master import (  # noqa: E402
    _tick_grid,
    round_to_tick,
)

import src.live_execution.ibkr_client as ibkr_client_module

_SEED = 20260704

# Audit test-list item 1 adversarial set (x.xx5 half-cents, negatives,
# -0.0, large ES-magnitude, near-integer artifacts).
_CL_ADVERSARIAL = [
    2.675, 2.665, 1.005, 65.005, 65.015, 65.025, 100.115, 0.005, 0.015,
    -0.005, -2.675, -37.635, -0.0, 0.0, 6012.375, 19.99999999999999,
]


def _bits(x: float) -> bytes:
    """IEEE-754 bit pattern — the ONLY comparison that catches -0.0 and
    half-even direction flips."""
    return struct.pack("<d", x)


def _on_grid(value: float, tick: float) -> bool:
    """Tolerance-free grid check: exact Decimal remainder on the shortest
    repr (no float epsilon anywhere)."""
    return Decimal(repr(value)) % Decimal(repr(tick)) == 0


def _half_cent_lattice(step: int = 1):
    """Every multiple of 0.005 in [-200, 200] (the adversarial lattice)."""
    return (k / 200.0 for k in range(-40000, 40001, step))


# ===========================================================================
# TestRoundToTickCLParity — the bitwise crux (audit items 1-2, REC-1)
# ===========================================================================

class TestRoundToTickCLParity:
    def test_adversarial_explicit_cases_bitwise(self):
        """Item 1: every adversarial value bit-equal to legacy round(x, 2).
        A naive round(x/0.01)*0.01 flips 2.675->2.68, 2.665->2.66,
        65.025->65.02, 0.005->0.0 — all must stay legacy."""
        for x in _CL_ADVERSARIAL:
            actual = round_to_tick(x, 0.01)
            expected = round(x, 2)
            assert _bits(actual) == _bits(expected), (
                f"round_to_tick({x!r}, 0.01) = {actual!r} != "
                f"round({x!r}, 2) = {expected!r} (bitwise)"
            )

    def test_negative_zero_sign_preserved(self):
        """-0.0 pin: round(-0.0024, 2) is -0.0 — the helper must preserve
        the sign bit exactly (checked via copysign AND bit pattern)."""
        for x in (-0.0024, -0.0, -0.004999):
            actual = round_to_tick(x, 0.01)
            expected = round(x, 2)
            assert _bits(actual) == _bits(expected)
            assert math.copysign(1.0, actual) == math.copysign(1.0, expected)
        # The canonical case is genuinely negative zero:
        assert math.copysign(1.0, round_to_tick(-0.0024, 0.01)) == -1.0

    def test_seeded_100k_sweep_zero_bitwise_deviations(self):
        """Item 1 sweep: 100k seeded uniform(-200, 200) + the FULL half-cent
        lattice — ZERO bitwise deviations vs round(x, 2)."""
        rng = random.Random(_SEED)
        mismatches = []
        for _ in range(100_000):
            x = rng.uniform(-200.0, 200.0)
            if _bits(round_to_tick(x, 0.01)) != _bits(round(x, 2)):
                mismatches.append(x)
                if len(mismatches) >= 5:
                    break
        assert not mismatches, (
            f"bitwise deviations vs round(x, 2) at e.g. {mismatches}"
        )
        lattice_mismatches = [
            x for x in _half_cent_lattice()
            if _bits(round_to_tick(x, 0.01)) != _bits(round(x, 2))
        ]
        assert not lattice_mismatches, (
            f"{len(lattice_mismatches)} half-cent lattice deviations, "
            f"e.g. {lattice_mismatches[:5]}"
        )

    def test_gc_grid_bitwise_equals_round_1(self):
        """Item 2: 0.10 is a power-of-ten tick -> literally round(x, 1)."""
        assert _bits(round_to_tick(2341.13, 0.10)) == _bits(round(2341.13, 1))
        rng = random.Random(_SEED)
        for _ in range(20_000):
            x = rng.uniform(-4000.0, 4000.0)
            assert _bits(round_to_tick(x, 0.10)) == _bits(round(x, 1)), x

    def test_ng_grid_bitwise_equals_round_3(self):
        """Item 2: 0.001 is a power-of-ten tick -> literally round(x, 3)."""
        rng = random.Random(_SEED)
        for _ in range(20_000):
            x = rng.uniform(-20.0, 20.0)
            assert _bits(round_to_tick(x, 0.001)) == _bits(round(x, 3)), x
        for x in _CL_ADVERSARIAL:
            assert _bits(round_to_tick(x, 0.001)) == _bits(round(x, 3)), x

    def test_rec1_composition_pin_marketable_limit_expression(self):
        """REC-1: the ASSEMBLED S2/S4/S5 expression —
        round_to_tick(x + 2*0.01, 0.01) bit-equal legacy round(x + 0.02, 2)
        (and the SELL side) — pins the marketable-limit sites at the
        property level (they are invisible to the HS14B parity gate)."""
        assert _bits(2 * 0.01) == _bits(0.02), "buffer doubling must be exact"
        tick = get_instrument("CL").tick_size
        assert _bits(2 * tick) == _bits(0.02), (
            "registry CL tick must reproduce the legacy $0.02 buffer exactly"
        )
        rng = random.Random(_SEED)
        samples = [rng.uniform(-200.0, 200.0) for _ in range(50_000)]
        samples += list(_half_cent_lattice(step=4))
        samples += _CL_ADVERSARIAL
        for x in samples:
            assert _bits(round_to_tick(x + 2 * 0.01, 0.01)) == _bits(
                round(x + 0.02, 2)
            ), f"BUY-side composition mismatch at {x!r}"
            assert _bits(round_to_tick(x - 2 * 0.01, 0.01)) == _bits(
                round(x - 0.02, 2)
            ), f"SELL-side composition mismatch at {x!r}"


# ===========================================================================
# TestRoundToTickGeneralGrids — 0.25 / 0.10 / 0.005 / 0.001 / 0.0005
# (audit items 3-4, 6)
# ===========================================================================

class TestRoundToTickGeneralGrids:
    @pytest.mark.parametrize(
        "price,expected",
        [
            # Nearest semantics (audit section 2 deviation 1 — NOT ceil):
            (6000.10, 6000.00),
            (6000.12, 6000.00),
            (6000.13, 6000.25),
            (6000.60, 6000.50),
            (6012.37, 6012.25),
            # On-grid identity:
            (6000.75, 6000.75),
            (6012.50, 6012.50),
            # Negative:
            (-0.30, -0.25),
            # Half-even at exactly representable .5 quotients:
            (6000.125, 6000.00),
            (6000.375, 6000.50),
        ],
    )
    def test_es_quarter_grid_spot_cases(self, price, expected):
        actual = round_to_tick(price, 0.25)
        assert actual == expected, f"round_to_tick({price}, 0.25)"
        assert _on_grid(actual, 0.25)

    def test_zc_quarter_grid_spot_case(self):
        """ZC quotes cents/bushel on the same 0.25 grid (audit section 2)."""
        actual = round_to_tick(450.37, 0.25)
        assert actual == 450.25
        assert _on_grid(actual, 0.25)

    @pytest.mark.parametrize(
        "price,expected",
        [(32.0024, 32.0), (32.0026, 32.005)],
    )
    def test_si_half_cent_grid_spot_cases(self, price, expected):
        actual = round_to_tick(price, 0.005)
        assert actual == expected
        assert _on_grid(actual, 0.005)

    def test_hg_grid_spot_case(self):
        actual = round_to_tick(3.9273, 0.0005)
        assert actual == 3.9275
        assert _on_grid(actual, 0.0005)

    def test_gc_tenth_grid_spot_case(self):
        actual = round_to_tick(2341.13, 0.10)
        assert actual == 2341.1
        assert _on_grid(actual, 0.10)

    def test_all_registry_ticks_outputs_on_grid(self):
        """Item 6 sweep: seeded random prices through EVERY registry tick —
        every output on-grid (exact Decimal remainder, no tolerance)."""
        rng = random.Random(_SEED)
        ticks = sorted({inst.tick_size for inst in INSTRUMENT_REGISTRY.values()})
        assert ticks, "registry must not be empty"
        for tick in ticks:
            for _ in range(4_000):
                x = rng.uniform(-8000.0, 8000.0)
                out = round_to_tick(x, tick)
                assert _on_grid(out, tick), (
                    f"round_to_tick({x!r}, {tick!r}) = {out!r} is off-grid"
                )

    def test_idempotence_all_registry_ticks(self):
        """Item 6: round_to_tick(round_to_tick(x, t), t) bit-equal to
        round_to_tick(x, t) for every registry tick."""
        rng = random.Random(_SEED + 1)
        ticks = sorted({inst.tick_size for inst in INSTRUMENT_REGISTRY.values()})
        for tick in ticks:
            for _ in range(4_000):
                x = rng.uniform(-8000.0, 8000.0)
                once = round_to_tick(x, tick)
                twice = round_to_tick(once, tick)
                assert _bits(twice) == _bits(once), (
                    f"not idempotent at ({x!r}, {tick!r}): "
                    f"{once!r} -> {twice!r}"
                )

    def test_tick_grid_classification_table(self):
        """Item 6: _tick_grid (decimals, is_power_of_ten) for every registry
        tick — routing correctness is what guarantees CL byte-identity."""
        expected = {
            0.01: (2, True),
            0.25: (2, False),
            0.10: (1, True),
            0.005: (3, False),
            0.001: (3, True),
            0.0005: (4, False),
        }
        registry_ticks = {inst.tick_size for inst in INSTRUMENT_REGISTRY.values()}
        assert registry_ticks == set(expected), (
            "registry tick census changed — extend the classification table"
        )
        for tick, klass in expected.items():
            assert _tick_grid(tick) == klass, f"_tick_grid({tick!r})"


# ===========================================================================
# TestRoundToTickErrors — no silent defaults (audit item 5)
# ===========================================================================

class TestRoundToTickErrors:
    @pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_price_raises(self, price):
        with pytest.raises(ValueError):
            round_to_tick(price, 0.01)

    @pytest.mark.parametrize(
        "tick", [0.0, -0.01, float("nan"), float("inf"), -0.0]
    )
    def test_invalid_tick_raises(self, tick):
        with pytest.raises(ValueError):
            round_to_tick(65.00, tick)

    def test_none_price_raises(self):
        with pytest.raises((TypeError, ValueError)):
            round_to_tick(None, 0.01)

    def test_none_tick_raises(self):
        with pytest.raises((TypeError, ValueError)):
            round_to_tick(65.00, None)


# ===========================================================================
# Mocked IB manager helpers (mirrors tests/test_bracket_order.py fixtures)
# ===========================================================================

def _make_manager():
    """IBKRConnectionManager with a mocked IB connection and a realistic
    mocked bracket. Returns (mgr, parent_order_mock)."""
    with patch("src.live_execution.ibkr_client.IB") as MockIB:
        mgr = IBKRConnectionManager(host="127.0.0.1", port=4002, client_id=99)
        mgr.ib = MockIB.return_value
        mgr.ib.isConnected.return_value = True

        parent = MagicMock()
        parent.orderType = "LMT"
        parent.lmtPrice = 0.0
        tp_child = MagicMock()
        tp_child.orderType = "LMT"
        sl_child = MagicMock()
        sl_child.orderType = "STP"
        mgr.ib.bracketOrder.return_value = [parent, tp_child, sl_child]
        mgr.ib.placeOrder.side_effect = lambda c, o: MagicMock(order=o)
    return mgr, parent


def _make_contract(symbol: str) -> MagicMock:
    contract = MagicMock()
    contract.symbol = symbol
    return contract


def _make_position(symbol: str, qty: int) -> MagicMock:
    pos = MagicMock()
    pos.contract = MagicMock()
    pos.contract.symbol = symbol
    pos.position = qty
    return pos


def _make_close_manager(positions):
    """Manager whose ib.positions() returns the given positions and whose
    placeOrder records (contract.exchange at call time, order)."""
    mgr, _ = _make_manager()
    mgr.ib.positions.return_value = positions
    placed = []

    def _capture(c, o):
        placed.append((getattr(c, "exchange", None), o))
        return MagicMock(order=o)

    mgr.ib.placeOrder.side_effect = _capture
    return mgr, placed


# ===========================================================================
# TestMarketableLimitTickAware — S2/S4/S5 + R1 (audit items 7-10)
# ===========================================================================

class TestMarketableLimitTickAware:
    # --- S4: place_bracket_order marketable_limit -----------------------

    def test_bracket_ml_es_buy_grid_and_buffer(self):
        """ES ml BUY: buffer = 2*0.25 = 0.50; 6000.25 -> 6000.75 on-grid."""
        mgr, parent = _make_manager()
        mgr.place_bracket_order(
            contract=_make_contract("ES"),
            action="BUY", quantity=1,
            limit_price=6000.25, tp_price=6003.00, sl_price=5998.00,
            entry_mode="marketable_limit",
        )
        assert parent.orderType == "LMT"
        assert parent.lmtPrice == 6000.75
        assert _on_grid(parent.lmtPrice, 0.25)

    def test_bracket_ml_es_sell_grid_and_buffer(self):
        mgr, parent = _make_manager()
        mgr.place_bracket_order(
            contract=_make_contract("ES"),
            action="SELL", quantity=1,
            limit_price=6000.25, tp_price=5998.00, sl_price=6003.00,
            entry_mode="marketable_limit",
        )
        assert parent.lmtPrice == 5999.75
        assert _on_grid(parent.lmtPrice, 0.25)

    def test_bracket_ml_es_off_grid_base_snaps_nearest(self):
        """Off-grid bar close 6000.10 + 0.50 = 6000.60 -> NEAREST 6000.50
        (audit section 2: nearest/half-even, NOT ceil-for-BUY)."""
        mgr, parent = _make_manager()
        mgr.place_bracket_order(
            contract=_make_contract("ES"),
            action="BUY", quantity=1,
            limit_price=6000.10, tp_price=6003.00, sl_price=5998.00,
            entry_mode="marketable_limit",
        )
        assert parent.lmtPrice == 6000.50
        assert _on_grid(parent.lmtPrice, 0.25)

    def test_bracket_ml_cl_bit_identical_to_legacy(self):
        """CL regression pins: 65.00 -> 65.02 (BUY) / 64.98 (SELL) plus an
        off-grid base bit-equal to the legacy expression round(x+-0.02, 2)."""
        for action, base, legacy in (
            ("BUY", 65.00, round(65.00 + 0.02, 2)),
            ("SELL", 65.00, round(65.00 - 0.02, 2)),
            ("BUY", 72.505, round(72.505 + 0.02, 2)),
            ("SELL", 72.505, round(72.505 - 0.02, 2)),
        ):
            mgr, parent = _make_manager()
            mgr.place_bracket_order(
                contract=_make_contract("CL"),
                action=action, quantity=1,
                limit_price=base, tp_price=base + 0.5, sl_price=base - 0.5,
                entry_mode="marketable_limit",
            )
            assert _bits(parent.lmtPrice) == _bits(legacy), (
                f"{action} @ {base}: {parent.lmtPrice!r} != legacy {legacy!r}"
            )
        # Readability pins (audit item 7 exact values):
        mgr, parent = _make_manager()
        mgr.place_bracket_order(
            contract=_make_contract("CL"), action="BUY", quantity=1,
            limit_price=65.00, tp_price=65.50, sl_price=64.50,
            entry_mode="marketable_limit",
        )
        assert parent.lmtPrice == 65.02

    # --- S5: place_entry_order marketable_limit -------------------------

    def test_entry_ml_es_grid(self):
        mgr, _ = _make_manager()
        mgr.place_entry_order(
            _make_contract("ES"), "BUY", 1, 6000.25,
            entry_mode="marketable_limit",
        )
        order = mgr.ib.placeOrder.call_args[0][1]
        assert order.lmtPrice == 6000.75
        assert _on_grid(order.lmtPrice, 0.25)

    def test_entry_ml_gc_and_ng_power_of_ten_grids(self):
        """GC 2341.10 -> 2341.30 (buffer 0.20); NG 2.675 -> 2.677 (buffer
        0.002) — bit-equal to the round(x + 2*tick, n) reference."""
        mgr, _ = _make_manager()
        mgr.place_entry_order(
            _make_contract("GC"), "BUY", 1, 2341.10,
            entry_mode="marketable_limit",
        )
        order = mgr.ib.placeOrder.call_args[0][1]
        assert _bits(order.lmtPrice) == _bits(round(2341.10 + 2 * 0.10, 1))
        assert order.lmtPrice == 2341.30
        assert _on_grid(order.lmtPrice, 0.10)

        mgr, _ = _make_manager()
        mgr.place_entry_order(
            _make_contract("NG"), "BUY", 1, 2.675,
            entry_mode="marketable_limit",
        )
        order = mgr.ib.placeOrder.call_args[0][1]
        assert _bits(order.lmtPrice) == _bits(round(2.675 + 2 * 0.001, 3))
        assert order.lmtPrice == 2.677
        assert _on_grid(order.lmtPrice, 0.001)

    def test_entry_ml_cl_seeded_composition_sweep_bit_identical(self):
        """REC-1 at the SITE level: the real place_entry_order, CL contract,
        seeded prices (incl. half-cent shapes) — lmtPrice bit-equal to the
        legacy round(limit +/- 0.02, 2) on every sample, both sides."""
        mgr, _ = _make_manager()
        contract = _make_contract("CL")
        rng = random.Random(_SEED + 2)
        prices = [rng.uniform(1.0, 150.0) for _ in range(40)]
        prices += [rng.randrange(200, 30000) / 200.0 for _ in range(40)]
        for action, sign in (("BUY", +1.0), ("SELL", -1.0)):
            for px in prices:
                mgr.ib.placeOrder.reset_mock()
                mgr.place_entry_order(
                    contract, action, 1, px, entry_mode="marketable_limit",
                )
                order = mgr.ib.placeOrder.call_args[0][1]
                legacy = round(px + sign * 0.02, 2)
                assert _bits(order.lmtPrice) == _bits(legacy), (
                    f"{action} @ {px!r}: {order.lmtPrice!r} != {legacy!r}"
                )

    # --- S2: close_cl_position marketable_limit -------------------------

    def test_close_ml_es_long_and_short_grid(self):
        # LONG -> SELL exit 2 ticks below: 6000.25 - 0.50 = 5999.75
        mgr, placed = _make_close_manager([_make_position("ES", 2)])
        mgr.close_cl_position(
            symbol="ES", exit_mode="marketable_limit", current_price=6000.25,
        )
        order = placed[0][1]
        assert order.action == "SELL"
        assert order.lmtPrice == 5999.75
        assert _on_grid(order.lmtPrice, 0.25)

        # SHORT -> BUY exit 2 ticks above: 6000.25 + 0.50 = 6000.75
        mgr, placed = _make_close_manager([_make_position("ES", -2)])
        mgr.close_cl_position(
            symbol="ES", exit_mode="marketable_limit", current_price=6000.25,
        )
        order = placed[0][1]
        assert order.action == "BUY"
        assert order.lmtPrice == 6000.75
        assert _on_grid(order.lmtPrice, 0.25)

    def test_close_ml_cl_bit_identical_to_legacy(self):
        """CL close ml pins: 72.50 -> 72.48/72.52 plus off-grid base
        72.505 bit-equal to legacy round(x -/+ 0.02, 2)."""
        for qty, base, legacy, action in (
            (2, 72.50, round(72.50 - 0.02, 2), "SELL"),
            (-2, 72.50, round(72.50 + 0.02, 2), "BUY"),
            (2, 72.505, round(72.505 - 0.02, 2), "SELL"),
            (-2, 72.505, round(72.505 + 0.02, 2), "BUY"),
        ):
            mgr, placed = _make_close_manager([_make_position("CL", qty)])
            mgr.close_cl_position(
                exit_mode="marketable_limit", current_price=base,
            )
            order = placed[0][1]
            assert order.action == action
            assert _bits(order.lmtPrice) == _bits(legacy), (
                f"qty={qty} @ {base}: {order.lmtPrice!r} != {legacy!r}"
            )

    # --- R1: close_cl_position adaptive exit snap -----------------------

    def test_close_adaptive_cl_on_grid_identity(self):
        """R1 identity pin: legitimate on-grid input passes through
        BIT-IDENTICAL (today's behavior: lmtPrice == 72.50 raw)."""
        mgr, placed = _make_close_manager([_make_position("CL", 2)])
        mgr.close_cl_position(exit_mode="adaptive", current_price=72.50)
        order = placed[0][1]
        assert order.algoStrategy == "Adaptive"
        assert _bits(order.lmtPrice) == _bits(72.50)

    def test_close_adaptive_cl_off_grid_snapped(self):
        """R1: hypothetical off-grid input -> snapped (today: rejected exit,
        stuck position; after: valid order)."""
        mgr, placed = _make_close_manager([_make_position("CL", 2)])
        mgr.close_cl_position(exit_mode="adaptive", current_price=72.503)
        order = placed[0][1]
        assert _bits(order.lmtPrice) == _bits(round(72.503, 2))

    def test_close_adaptive_es_off_grid_snapped_on_grid_identity(self):
        mgr, placed = _make_close_manager([_make_position("ES", 1)])
        mgr.close_cl_position(
            symbol="ES", exit_mode="adaptive", current_price=6000.10,
        )
        order = placed[0][1]
        assert order.lmtPrice == 6000.00
        assert _on_grid(order.lmtPrice, 0.25)

        mgr, placed = _make_close_manager([_make_position("ES", 1)])
        mgr.close_cl_position(
            symbol="ES", exit_mode="adaptive", current_price=6000.25,
        )
        order = placed[0][1]
        assert _bits(order.lmtPrice) == _bits(6000.25)

    # --- Fail-fast + dead constant ---------------------------------------

    def test_bracket_ml_unknown_symbol_raises_no_order(self):
        """Blueprint no-silent-default: unknown contract symbol raises
        ValueError naming the symbol; NOTHING is transmitted."""
        mgr, _ = _make_manager()
        with pytest.raises(ValueError, match="XX"):
            mgr.place_bracket_order(
                contract=_make_contract("XX"),
                action="BUY", quantity=1,
                limit_price=65.00, tp_price=65.50, sl_price=64.50,
                entry_mode="marketable_limit",
            )
        mgr.ib.placeOrder.assert_not_called()

    def test_cl_tick_size_constant_gone(self):
        """S1: _CL_TICK_SIZE deleted — dead constants invite reuse."""
        assert not hasattr(IBKRConnectionManager, "_CL_TICK_SIZE")
        assert not hasattr(ibkr_client_module, "_CL_TICK_SIZE")


# ===========================================================================
# TestExitExchangeFromRegistry — X1/X2 (audit item 11)
# ===========================================================================

class TestExitExchangeFromRegistry:
    _TABLE = [
        ("CL", "NYMEX"),   # regression pin — legacy behavior preserved
        ("MCL", "NYMEX"),
        ("ES", "CME"),
        ("GC", "COMEX"),
        ("ZC", "CBOT"),
    ]

    @pytest.mark.parametrize("symbol,exchange", _TABLE)
    def test_close_position_injects_registry_exchange(self, symbol, exchange):
        """close_cl_position: pos.contract.exchange comes from the registry
        via pos.contract.symbol and is set BEFORE placeOrder."""
        pos = _make_position(symbol, 1)
        mgr, placed = _make_close_manager([pos])
        trade = mgr.close_cl_position(symbol=symbol)
        assert trade is not None
        assert len(placed) == 1
        exchange_at_place_time, order = placed[0]
        assert exchange_at_place_time == exchange
        assert pos.contract.exchange == exchange
        assert order.action == "SELL"

    @pytest.mark.parametrize("symbol,exchange", _TABLE)
    def test_close_position_market_injects_registry_exchange(
        self, symbol, exchange
    ):
        pos = _make_position(symbol, -1)
        mgr, placed = _make_close_manager([pos])
        trade = mgr.close_cl_position_market(symbol=symbol)
        assert trade is not None
        assert len(placed) == 1
        exchange_at_place_time, order = placed[0]
        assert exchange_at_place_time == exchange
        assert order.action == "BUY"

    def test_unregistered_position_symbol_skipped_close(self):
        """Blueprint filter semantics UNCHANGED: a held position in an
        unregistered symbol never matches the requested (registry-validated)
        symbol -> skipped exactly as today. No order, no raise — the
        registry lookup must NOT run for non-matching positions."""
        mgr, placed = _make_close_manager([_make_position("XX", 3)])
        result = mgr.close_cl_position(symbol="CL")
        assert result is None
        assert placed == []

    def test_unregistered_position_symbol_skipped_close_market(self):
        mgr, placed = _make_close_manager([_make_position("XX", 3)])
        result = mgr.close_cl_position_market(symbol="CL")
        assert result is None
        assert placed == []

    def test_foreign_position_skipped_matched_position_closed(self):
        """Mixed book: the unregistered position is skipped, the matched ES
        position closes on CME — one order total."""
        positions = [_make_position("XX", 3), _make_position("ES", 1)]
        mgr, placed = _make_close_manager(positions)
        trade = mgr.close_cl_position(symbol="ES")
        assert trade is not None
        assert len(placed) == 1
        assert placed[0][0] == "CME"


# ===========================================================================
# LiveTrader seam helpers (mirror tests/test_live_trader_bugs.py __new__
# seams and tests/test_symbol_data_paths.py full-init seam)
# ===========================================================================

class DummyStrategy:
    """Minimal strategy stub mirroring tests/test_instrument_context.py."""

    def __init__(self, feature_names=None, config=None):
        self.feature_names = feature_names if feature_names is not None else ["MACD"]
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config if config is not None else {}


def _build_full_trader(cfg: dict, tmp_path):
    """Full LiveTrader init with mocked DataManager + existence checks
    (the tests/test_symbol_data_paths.py seam)."""
    with patch("src.live_execution.live_trader.DataManager"), patch(
        "pathlib.Path.exists", return_value=True
    ):
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(config=cfg),
            db_path=str(tmp_path / "telemetry.db"),
            dry_run=True,
        )
    return trader


def _make_children_trader(symbol: str):
    """__new__ seam for _place_bracket_children_on_fill (S7)."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = symbol
    trader.exec_client = MagicMock()
    tp_trade = MagicMock()
    tp_trade.order.orderId = 124
    sl_trade = MagicMock()
    sl_trade.order.orderId = 125
    trader.exec_client.place_child_orders.return_value = [tp_trade, sl_trade]
    trader._active_trade_id = None
    trader.telemetry = MagicMock()
    trader._utc_iso_now = MagicMock(return_value="2026-07-04T00:00:00Z")
    trader._build_event_id = MagicMock(return_value="evt_t3")
    trader._last_decision_context_by_order_id = {}
    return trader


def _run_children(trader, *, order_id=123, fill_price, ctx):
    trader._last_decision_context_by_order_id[order_id] = ctx
    trader._place_bracket_children_on_fill(
        order_id=order_id,
        fill_price=fill_price,
        action_str=ctx["entry_action"],
        qty=float(ctx.get("lots", 1)),
        contract=MagicMock(),
    )
    return trader.exec_client.place_child_orders.call_args.kwargs


def _make_trailing_trader(symbol: str, *, entry, atr, mult, offset_long,
                          bar_high, bar_low):
    """__new__ seam for _check_trailing_stop (S6), long side."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = symbol
    trader._trailing_activated = False
    # log-cosmetics-cancel-bounce_07222026_2330 (mechanical stub repair):
    # the no-op skip guard reads the tracked SL cache; None = no skip.
    trader._tracked_sl_price = None
    trader._entry_price = entry
    trader._atr_at_entry = atr
    trader._trade_trailing_atr_mult = None
    trader._trailing_atr_mult = mult
    trader._trailing_sl_atr_offset_long = offset_long
    trader._trailing_sl_atr_offset_short = offset_long
    # Mechanical stub repair (live-trailing-ladder-phase3_07232026_0035):
    # _check_trailing_stop now resolves per-side ladders; None = the legacy
    # single-rung path this suite pins.
    trader._trailing_ladder_long = None
    trader._trailing_ladder_short = None
    trader._position_side = 1
    trader._highest_high = entry
    trader._lowest_low = entry
    trader._position_bars_held = 1
    trader._active_trade_id = None
    trader.telemetry = MagicMock()
    trader._utc_iso_now = MagicMock(return_value="2026-07-04T00:00:00Z")
    trader.rolling_df_5m = pd.DataFrame([{"High": bar_high, "Low": bar_low}])
    trader._sl_order_id = 888
    raw_order = MagicMock()
    raw_order.auxPrice = 0.0
    evt = SimpleNamespace(
        symbol=symbol, order_id="888",
        raw_event=SimpleNamespace(order=raw_order),
    )
    trader._open_orders = {"888": evt}
    trader.exec_client = MagicMock()
    return trader, raw_order


def _make_recovery_trader(symbol: str, *, tp_price, sl_price, quantity=1):
    """__new__ seam driving _recover_inherited_position to the step-5
    re-place branch (both TP/SL missing on IBKR, prices in the ledger)."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = symbol
    trader.telemetry = MagicMock()
    trader.telemetry.get_open_position.return_value = {
        "trade_id": "t3-recovery",
        "side": "LONG",
        "entry_price": 6012.50,
        "quantity": quantity,
        "tp_order_id": None,
        "sl_order_id": None,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "atr_at_entry": 1.0,
        "entry_bar_time": None,
        "trailing_atr_mult": None,
        "max_hold_bars": None,
    }
    trader.exec_client = MagicMock()
    trader.exec_client.get_position.return_value = quantity
    trader.exec_client.get_open_trades.return_value = []
    tp_trade = MagicMock()
    tp_trade.order.orderId = 224
    sl_trade = MagicMock()
    sl_trade.order.orderId = 225
    trader.exec_client.place_child_orders.return_value = [tp_trade, sl_trade]
    trader.rolling_df_5m = None
    trader._open_orders = {}
    trader._front_month_local_symbol = f"{symbol}U6"
    return trader


# ===========================================================================
# TestLiveTraderTickSites — _tick_size, S6, S7, R2 (audit items 13-18)
# ===========================================================================

class TestLiveTraderTickSites:
    # --- _tick_size property (item 17) -----------------------------------

    def test_tick_size_from_context_full_init_es(self, tmp_path):
        """Full ES init: the property resolves 0.25 from the
        InstrumentContext wired by __init__."""
        cfg = {"nickname": "ES_style", "execution_symbol": "ES",
               "bar_size": "1h"}
        trader = _build_full_trader(cfg, tmp_path)
        assert trader._tick_size == 0.25

    def test_tick_size_uses_execution_instrument_not_brain(self):
        """Orders are placed on the EXECUTION contract — a synthetic
        exec-ES/brain-CL context must yield 0.25, never 0.01."""
        trader = LiveTrader.__new__(LiveTrader)
        trader._instrument_context = InstrumentContext(
            execution_symbol="ES",
            brain_symbol="CL",
            execution_instrument=get_instrument("ES"),
            brain_instrument=get_instrument("CL"),
        )
        assert trader._tick_size == 0.25

    def test_tick_size_seam_structural_fallback_cl(self):
        """__new__ seam with only _execution_symbol='CL' (the legacy test
        seam population) -> 0.01 via the registry, mirroring _brain_symbol."""
        trader = LiveTrader.__new__(LiveTrader)
        trader._execution_symbol = "CL"
        assert trader._tick_size == 0.01

    def test_tick_size_seam_unknown_symbol_raises(self):
        trader = LiveTrader.__new__(LiveTrader)
        trader._execution_symbol = "XX"
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            _ = trader._tick_size

    def test_tick_size_seam_nothing_set_raises(self):
        """No context AND no _execution_symbol -> raises mentioning the
        missing seam attribute (NOT a silent CL default)."""
        trader = LiveTrader.__new__(LiveTrader)
        with pytest.raises(AttributeError, match="_execution_symbol"):
            _ = trader._tick_size

    # --- S6: trailing stop (item 16 + REC-2) ------------------------------

    def test_trailing_sl_es_lands_on_quarter_grid(self):
        """ES trailing: entry 6012.50, offset product 0.47 -> raw SL 6012.97
        must be snapped to 6013.00 (0.25 grid) before hitting the order."""
        trader, raw_order = _make_trailing_trader(
            "ES", entry=6012.50, atr=1.0, mult=1.0, offset_long=0.47,
            bar_high=6014.00, bar_low=6010.00,
        )
        trader._check_trailing_stop()
        assert trader._trailing_activated is True
        assert raw_order.auxPrice == 6013.00
        assert _on_grid(raw_order.auxPrice, 0.25)
        assert trader._tracked_sl_price == 6013.00

    def test_trailing_sl_cl_seeded_sweep_bit_identical_legacy(self):
        """REC-2: the parity gate runs --disable-trailing, so S6's CL
        byte-identity rests ENTIRELY on this pin — a seeded sweep through
        the REAL _check_trailing_stop, bit-compared to legacy
        round(entry + offset, 2) on every sample."""
        trader, raw_order = _make_trailing_trader(
            "CL", entry=70.0, atr=1.0, mult=0.0, offset_long=0.5,
            bar_high=0.0, bar_low=0.0,
        )
        rng = random.Random(_SEED + 3)
        mismatches = []
        for i in range(500):
            if i % 2 == 0:
                entry = rng.uniform(1.0, 150.0)
                off = rng.uniform(0.0, 3.0)
            else:
                entry = round(rng.uniform(1.0, 150.0), 2)
                off = rng.randrange(0, 600) / 200.0  # half-cent lattice
            trader._trailing_activated = False
            trader._tracked_sl_price = None  # same stub repair as above
            trader._entry_price = entry
            trader._highest_high = entry
            trader._lowest_low = entry
            trader._trailing_sl_atr_offset_long = off
            raw_order.auxPrice = 0.0

            trader._check_trailing_stop()

            expected = round(entry + off * 1.0, 2)  # legacy S6 expression
            if _bits(raw_order.auxPrice) != _bits(expected):
                mismatches.append((entry, off, raw_order.auxPrice, expected))
        assert not mismatches, (
            f"{len(mismatches)} CL trailing bitwise deviations, "
            f"e.g. {mismatches[:5]}"
        )

    # --- S7: bracket children (items 13-15) -------------------------------

    def test_children_es_naked_stop_scenario(self):
        """THE blocker test (item 14): a FILLED ES entry must produce
        tick-valid TP/SL children — cent-grid children draw IBKR Error 110
        and leave a naked position."""
        trader = _make_children_trader("ES")
        kwargs = _run_children(
            trader, fill_price=6012.50,
            ctx={"tp_offset": 3.47, "sl_offset": 2.31, "entry_action": "BUY",
                 "lots": 1, "tiered_tp_offsets": []},
        )
        assert kwargs["action"] == "SELL"
        assert kwargs["tp_price"] == 6016.00
        assert kwargs["sl_price"] == 6010.25
        assert _on_grid(kwargs["tp_price"], 0.25)
        assert _on_grid(kwargs["sl_price"], 0.25)

    def test_children_es_sell_side_mirror(self):
        """Item 15: short entry mirror — SL above, TP below, both on-grid."""
        trader = _make_children_trader("ES")
        kwargs = _run_children(
            trader, fill_price=6012.50,
            ctx={"tp_offset": 3.47, "sl_offset": 2.31, "entry_action": "SELL",
                 "lots": 1, "tiered_tp_offsets": []},
        )
        assert kwargs["action"] == "BUY"
        assert kwargs["tp_price"] == 6009.00
        assert kwargs["sl_price"] == 6014.75
        assert _on_grid(kwargs["tp_price"], 0.25)
        assert _on_grid(kwargs["sl_price"], 0.25)

    def test_children_es_tiered_grid_and_lot_arithmetic(self):
        """Item 14 tiered variant: every (lots, price) tick-valid; the lot
        split arithmetic is byte-identical to today (4 lots @ 50/50 -> 2/2)."""
        trader = _make_children_trader("ES")
        kwargs = _run_children(
            trader, fill_price=6012.50,
            ctx={"tp_offset": None, "sl_offset": 2.31, "entry_action": "BUY",
                 "lots": 4,
                 "tiered_tp_offsets": [(0.5, 1.13), (0.5, 2.87)]},
        )
        assert kwargs["quantity"] == 4
        assert kwargs["tp_price"] == [(2, 6013.75), (2, 6015.25)]
        for lots, price in kwargs["tp_price"]:
            assert isinstance(lots, int)
            assert _on_grid(price, 0.25)
        assert kwargs["sl_price"] == 6010.25

    def test_children_gc_tiered_on_tenth_grid(self):
        """Item 15: GC tiered TPs land on the 0.10 grid (power-of-ten path
        -> bit-equal to round(fill + off, 1))."""
        trader = _make_children_trader("GC")
        kwargs = _run_children(
            trader, fill_price=2341.13,
            ctx={"tp_offset": None, "sl_offset": 1.27, "entry_action": "BUY",
                 "lots": 2,
                 "tiered_tp_offsets": [(0.5, 0.83), (0.5, 1.94)]},
        )
        expected_prices = [round(2341.13 + 0.83, 1), round(2341.13 + 1.94, 1)]
        assert [p for _, p in kwargs["tp_price"]] == expected_prices
        assert [l for l, _ in kwargs["tp_price"]] == [1, 1]
        for _, price in kwargs["tp_price"]:
            assert _on_grid(price, 0.10)
        assert _bits(kwargs["sl_price"]) == _bits(round(2341.13 - 1.27, 1))
        assert _on_grid(kwargs["sl_price"], 0.10)

    def test_children_cl_bit_identical_to_legacy(self):
        """Item 13: CL children bit-equal to legacy round(fill +/- off, 2) —
        the audit's stated values TP 65.60 / SL 64.84."""
        trader = _make_children_trader("CL")
        kwargs = _run_children(
            trader, fill_price=65.13,
            ctx={"tp_offset": 0.47, "sl_offset": 0.29, "entry_action": "BUY",
                 "lots": 1, "tiered_tp_offsets": []},
        )
        assert _bits(kwargs["tp_price"]) == _bits(round(65.13 + 0.47, 2))
        assert _bits(kwargs["sl_price"]) == _bits(round(65.13 - 0.29, 2))
        assert kwargs["tp_price"] == 65.60
        assert kwargs["sl_price"] == 64.84

    def test_children_cl_half_cent_fill_bit_identical(self):
        """Item 13 adversarial: half-cent fill 65.005 — the exact shape where
        a naive quotient implementation flips the last bit vs legacy."""
        trader = _make_children_trader("CL")
        kwargs = _run_children(
            trader, fill_price=65.005,
            ctx={"tp_offset": 0.47, "sl_offset": 0.29, "entry_action": "BUY",
                 "lots": 1, "tiered_tp_offsets": []},
        )
        assert _bits(kwargs["tp_price"]) == _bits(round(65.005 + 0.47, 2))
        assert _bits(kwargs["sl_price"]) == _bits(round(65.005 - 0.29, 2))

    # --- R2: recovery re-place snap (item 18) -----------------------------

    def test_recovery_replace_cl_ledger_prices_identity(self):
        """R2 identity: legitimately-stored CL prices (they were snapped at
        S7 when created) pass through bit-identical."""
        trader = _make_recovery_trader("CL", tp_price=65.60, sl_price=64.84,
                                       quantity=2)
        trader._recover_inherited_position()
        kwargs = trader.exec_client.place_child_orders.call_args.kwargs
        assert kwargs["parent_order_id"] == 0
        assert kwargs["action"] == "SELL"
        assert kwargs["quantity"] == 2
        assert _bits(kwargs["tp_price"]) == _bits(65.60)
        assert _bits(kwargs["sl_price"]) == _bits(64.84)

    def test_recovery_replace_es_off_grid_ledger_row_snapped(self):
        """R2: an off-grid ES ledger row (pre-T3 shape / manual DB edit) is
        snapped on re-place — the recovery path exists to PREVENT naked
        positions and must not itself draw Error 110."""
        trader = _make_recovery_trader("ES", tp_price=6016.03,
                                       sl_price=6010.19)
        trader._recover_inherited_position()
        kwargs = trader.exec_client.place_child_orders.call_args.kwargs
        assert kwargs["tp_price"] == 6016.00
        assert kwargs["sl_price"] == 6010.25
        assert _on_grid(kwargs["tp_price"], 0.25)
        assert _on_grid(kwargs["sl_price"], 0.25)
