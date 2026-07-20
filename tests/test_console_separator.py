"""Tests for fleet_runner._ConsoleSeparator.

The runner is the console gateway for the interleaved child heartbeats: it
inserts one blank line between heartbeat ROUNDS (content-driven, so it tracks
the real cadence regardless of each bot's start phase) and ASCII-sanitizes the
echo. These tests lock that behavior.
"""

import io

from src.live_execution.fleet_runner import _ConsoleSeparator


def _run(lines):
    buf = io.BytesIO()
    sep = _ConsoleSeparator(buf)
    for ln in lines:
        sep.write_line((ln + "\n").encode("utf-8"))
    return buf.getvalue().decode("ascii")


def _hb(sym):
    return f"2026-07-19 10:39:00 [INFO] [{sym:<3}] alive | pos= 0 | conn=T | OPEN"


class TestRoundGrouping:
    def test_blank_line_inserted_when_a_symbol_repeats(self):
        out = _run([_hb("CL"), _hb("MES"), _hb("NG"),
                    _hb("CL"), _hb("MES")])
        # Exactly one round boundary (the second CL) -> exactly one blank line.
        assert out.count("\n\n") == 1
        # The blank precedes the repeat, not the first occurrence.
        first_round, second_round = out.split("\n\n")
        assert first_round.count("[CL ]") == 1
        assert second_round.startswith("2026-07-19") and "[CL ]" in second_round

    def test_no_leading_blank_before_first_round(self):
        out = _run([_hb("CL"), _hb("MES"), _hb("NG")])
        assert not out.startswith("\n")
        assert "\n\n" not in out  # no round completed yet

    def test_missing_bot_still_groups_on_repeat(self):
        # SIL never reports; the round still closes when CL comes back around.
        out = _run([_hb("CL"), _hb("MES"), _hb("MGC"),
                    _hb("CL"), _hb("MES")])
        assert out.count("\n\n") == 1

    def test_non_heartbeat_lines_do_not_trigger_or_break_grouping(self):
        lines = [
            _hb("CL"),
            "2026-07-19 10:39:00 [INFO] Front-month CL contract: CLU6",
            "2026-07-19 10:15:01 [INFO] [CL ] [HOUSEKEEPING] sweep: 0 actions",
            _hb("MES"),
            _hb("CL"),  # repeat -> one boundary despite the interstitial lines
        ]
        out = _run(lines)
        assert out.count("\n\n") == 1
        assert "Front-month CL contract" in out
        assert "HOUSEKEEPING" in out


class TestSanitize:
    def test_em_dash_and_emoji_are_stripped(self):
        line = "2026-07-19 10:39:00 [INFO] [CL ] alive | OPEN — 🚀 note"
        out = _run([line])
        assert out.isascii()
        assert "�" not in out and "—" not in out
        assert "OPEN - " in out  # em dash -> hyphen, emoji dropped

    def test_plain_line_passes_through_unchanged(self):
        out = _run(["2026-07-19 10:39:00 [INFO] visible to operator"])
        assert out == "2026-07-19 10:39:00 [INFO] visible to operator\n"
