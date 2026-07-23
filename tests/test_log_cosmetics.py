"""Ticket log-cosmetics-cancel-bounce_07222026_2330.

1. Expected-cancel-bounce downgrade: an ib_insync Error 10147/10148 whose
   reqId was registered via register_expected_cancel_bounce (a deliberate
   cancel we just issued) is downgraded ERROR -> INFO with an annotation;
   any other 10147/10148 stays ERROR. Order routing untouched.
2. ASCII hygiene: _setup_file_logging sanitizes the child's own console
   StreamHandler (the stderr-sink path that escaped non-ASCII as literal
   \\uXXXX) and installs the bounce filter idempotently.
"""

import logging

import pytest

from src.live_execution.ascii_safe import AsciiFormatter
from src.live_execution import log_config
from src.live_execution.log_config import (
    ExpectedCancelBounceFilter,
    register_expected_cancel_bounce,
    _expected_cancel_bounces,
)


def _record(msg: str, level=logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="ib_insync.wrapper", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    _expected_cancel_bounces.clear()
    yield
    _expected_cancel_bounces.clear()


class TestExpectedCancelBounceFilter:
    @pytest.mark.parametrize("code", ["10147", "10148"])
    def test_registered_fresh_id_downgraded_with_annotation(self, code):
        register_expected_cancel_bounce(106)
        rec = _record(
            f"Error {code}, reqId 106: OrderId 106 that needs to be "
            f"cancelled cannot be cancelled, state: Cancelled."
        )
        f = ExpectedCancelBounceFilter()
        assert f.filter(rec) is True  # never suppresses
        assert rec.levelno == logging.INFO
        assert rec.levelname == "INFO"
        assert "expected" in rec.getMessage()

    def test_unregistered_id_stays_error(self):
        rec = _record("Error 10148, reqId 999: OrderId 999 ... state: Cancelled.")
        assert ExpectedCancelBounceFilter().filter(rec) is True
        assert rec.levelno == logging.ERROR, (
            "a bounce for an id we never cancelled is the REAL anomaly and "
            "must stay ERROR"
        )

    def test_stale_registration_stays_error(self, monkeypatch):
        register_expected_cancel_bounce(106)
        _expected_cancel_bounces["106"] -= (
            log_config._EXPECTED_CANCEL_BOUNCE_TTL_SECONDS + 1
        )
        rec = _record("Error 10148, reqId 106: OrderId 106 ... Cancelled.")
        ExpectedCancelBounceFilter().filter(rec)
        assert rec.levelno == logging.ERROR

    def test_unrelated_messages_untouched(self):
        register_expected_cancel_bounce(106)
        rec = _record("Error 10182, reqId 106: Failed to request live updates")
        assert ExpectedCancelBounceFilter().filter(rec) is True
        assert rec.levelno == logging.ERROR
        assert "expected" not in rec.getMessage()

    def test_register_prunes_stale_entries(self):
        register_expected_cancel_bounce(1)
        _expected_cancel_bounces["1"] -= (
            log_config._EXPECTED_CANCEL_BOUNCE_TTL_SECONDS + 1
        )
        register_expected_cancel_bounce(2)
        assert "1" not in _expected_cancel_bounces
        assert "2" in _expected_cancel_bounces


class TestSetupFileLoggingHygiene:
    def test_console_sanitized_and_filter_installed_idempotently(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(log_config, "_LOG_DIR", tmp_path)
        root = logging.getLogger()
        wrapper = logging.getLogger("ib_insync.wrapper")
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console)
        added_file_handlers = []
        try:
            for _ in range(2):  # idempotent on repeat calls
                before = set(root.handlers)
                log_config._setup_file_logging(9999)
                added_file_handlers += [
                    h for h in root.handlers if h not in before
                ]
            assert isinstance(console.formatter, AsciiFormatter), (
                "the child's own console handler must transliterate to "
                "ASCII (it feeds the raw stderr crash sinks)"
            )
            bounce_filters = [
                f for f in wrapper.filters
                if isinstance(f, ExpectedCancelBounceFilter)
            ]
            assert len(bounce_filters) == 1
        finally:
            root.removeHandler(console)
            for h in added_file_handlers:
                root.removeHandler(h)
                h.close()
            for f in list(wrapper.filters):
                if isinstance(f, ExpectedCancelBounceFilter):
                    wrapper.removeFilter(f)


class TestArrowAtSource:
    def test_trailing_modify_log_line_is_ascii(self):
        # The one operator-facing arrow (live_trader.py:1701) — pinned ASCII
        # so the stderr sinks never show → again.
        import pathlib
        src = pathlib.Path("src/live_execution/live_trader.py").read_text(
            encoding="utf-8"
        )
        line = next(
            l for l in src.splitlines() if "TRAILING STOP: modified SL order" in l
        )
        assert line.isascii(), f"non-ASCII in trailing modify log line: {line!r}"


# ---------------------------------------------------------------------------
# Item 3 (operator go 2026-07-23): no-op trailing modify skip + full-precision
# prices (NG ticks 0.001 — %.2f hid the 3rd decimal: "2.94 -> 2.94" could be
# a real 2.937 -> 2.941 move)
# ---------------------------------------------------------------------------


from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pandas as pd  # noqa: E402

from src.live_execution.live_trader import LiveTrader  # noqa: E402


def _trailing_stub(*, tracked_sl, aux_price=2.85):
    lt = object.__new__(LiveTrader)
    lt._active_trade_id = "trade_1"
    lt._sl_order_id = 55
    lt._trailing_activated = False
    lt._entry_price = 2.905
    lt._atr_at_entry = 0.02
    lt._position_side = 1
    lt._trade_trailing_atr_mult = None
    lt._trailing_atr_mult = 1.0          # trigger at 2.925
    lt._trailing_sl_atr_offset_long = 1.6   # new_sl = 2.905 + 0.032 = 2.937
    lt._trailing_sl_atr_offset_short = 1.6
    # Mechanical stub repair (live-trailing-ladder-phase3_07232026_0035):
    # _check_trailing_stop now resolves per-side ladders; None = the legacy
    # single-rung path this suite pins.
    lt._trailing_ladder_long = None
    lt._trailing_ladder_short = None
    # _tick_size is a property; feed it via the instrument-context seam
    lt._instrument_context = SimpleNamespace(
        execution_instrument=SimpleNamespace(tick_size=0.001)
    )
    lt._tracked_sl_price = tracked_sl
    lt._highest_high = 0.0
    lt._lowest_low = float("inf")
    lt.rolling_df_5m = pd.DataFrame(
        {"High": [2.93], "Low": [2.90]},
        index=[pd.Timestamp("2026-07-23 06:00:00")],
    )
    lt.rolling_df_1h = None
    lt._execution_symbol = "NG"
    lt._open_orders = {
        55: SimpleNamespace(
            symbol="NG", order_id=55,
            raw_event=SimpleNamespace(
                order=SimpleNamespace(auxPrice=aux_price)
            ),
        )
    }
    lt.exec_client = MagicMock()
    lt.telemetry = MagicMock()
    lt._last_decision_context_by_order_id = {}
    return lt


class TestNoOpTrailingSkip:
    def test_identical_price_skips_transmit_and_relatches(self, caplog):
        lt = _trailing_stub(tracked_sl=2.937)
        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_not_called()
        assert lt._trailing_activated is True, (
            "the resting stop IS the trail's price — a lost latch "
            "(reconnect state loss) must be re-armed"
        )
        assert any("no-op modify skipped" in r.getMessage()
                   for r in caplog.records)

    def test_real_move_still_transmits_with_full_precision(self, caplog):
        lt = _trailing_stub(tracked_sl=2.85, aux_price=2.85)
        with caplog.at_level(logging.INFO, logger="LiveTrader"):
            lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_called_once()
        assert lt._trailing_activated is True
        assert lt._tracked_sl_price == pytest.approx(2.937)
        modified = next(
            r.getMessage() for r in caplog.records
            if "modified SL order" in r.getMessage()
        )
        assert "2.85 -> 2.937" in modified, (
            f"full tick precision required (NG=0.001); got: {modified}"
        )
