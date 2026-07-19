"""Tests for ASCII-safe output helpers (src.live_execution.ascii_safe).

Guards the output-boundary transliteration that keeps operator logs and
Telegram messages free of mojibake and emoji.
"""

import logging

from src.live_execution.ascii_safe import AsciiFormatter, to_ascii


class TestToAscii:
    def test_ascii_passthrough_unchanged(self):
        s = "alive | bar= 41.2h | pos=  0 | conn=T | OPEN"
        assert to_ascii(s) is s  # fast path returns the same object

    def test_em_dash_becomes_hyphen(self):
        assert to_ascii("CLOSED (weekend — opens Sun 6pm ET)") == \
            "CLOSED (weekend - opens Sun 6pm ET)"

    def test_arrows_spelled_out(self):
        assert to_ascii("Sun 18:00 → Fri 17:00") == "Sun 18:00 -> Fri 17:00"
        assert to_ascii("A ↔ B") == "A <-> B"

    def test_symbols_spelled_out(self):
        assert to_ascii("3×14mo blocks") == "3x14mo blocks"
        assert to_ascii("±$40k") == "+/-$40k"
        assert to_ascii("≈70%") == "~70%"
        assert to_ascii(">= vs ≥") == ">= vs >="

    def test_emoji_dropped(self):
        assert to_ascii("🚀 *ENTRY FILLED*") == " *ENTRY FILLED*"
        assert to_ascii("⚠️ SL HIT") == " SL HIT"  # incl. variation selector
        assert to_ascii("💓📊🛑🚨🔌🤖🔄") == ""

    def test_accented_letters_folded(self):
        assert to_ascii("café résumé") == "cafe resume"

    def test_no_replacement_char_survives(self):
        # A literal U+FFFD (the "�" the operator saw) must not pass through.
        assert "�" not in to_ascii("weekend � opens Sun")


class TestAsciiFormatter:
    def _record(self, msg):
        return logging.LogRecord(
            name="LiveTrader", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )

    def test_formats_to_ascii(self):
        fmt = AsciiFormatter("%(message)s")
        out = fmt.format(self._record("market=CLOSED (weekend — opens Sun 6pm ET) 🚀"))
        assert out == "market=CLOSED (weekend - opens Sun 6pm ET) "

    def test_does_not_mutate_record(self):
        # Non-mutating: other handlers / caplog must still see the original.
        fmt = AsciiFormatter("%(message)s")
        rec = self._record("a — b")
        fmt.format(rec)
        assert rec.msg == "a — b"
