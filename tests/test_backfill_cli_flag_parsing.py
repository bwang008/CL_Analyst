"""CLI flag-parsing tests for scripts/backfill_roll_history.py.

Ticket: jit-roll-ratio-empty_07102026_1453 (Stage 1, CLI layer).
Covers the operator flag parsers added at TDD-Manager direction:
--reference-end (SYM=ISO_TIMESTAMP) and --extra-seam (SYM=CUTOFF:RATIO),
both repeatable and comma-separated. The tested core contracts in
tests/test_backfill_roll_history.py are untouched.
"""

import pandas as pd
import pytest

from scripts.backfill_roll_history import (
    BackfillValidationError,
    parse_extra_seams,
    parse_reference_ends,
)


class TestParseReferenceEnds:

    def test_repeatable_and_comma_separated(self):
        out = parse_reference_ends(
            ["ES=2026-06-16T23:59:59", "NG=2026-07-01T00:00:00,GC=2026-07-02T12:00:00"]
        )
        assert out == {
            "ES": pd.Timestamp("2026-06-16 23:59:59"),
            "NG": pd.Timestamp("2026-07-01 00:00:00"),
            "GC": pd.Timestamp("2026-07-02 12:00:00"),
        }

    def test_symbol_uppercased(self):
        out = parse_reference_ends(["es=2026-06-16T23:59:59"])
        assert list(out.keys()) == ["ES"]

    def test_empty_input_gives_empty_dict(self):
        assert parse_reference_ends([]) == {}
        assert parse_reference_ends(None) == {}

    def test_missing_equals_raises(self):
        with pytest.raises(BackfillValidationError):
            parse_reference_ends(["ES:2026-06-16"])

    def test_duplicate_symbol_raises(self):
        with pytest.raises(BackfillValidationError):
            parse_reference_ends(["ES=2026-06-16", "ES=2026-06-17"])

    def test_bad_timestamp_raises(self):
        with pytest.raises(BackfillValidationError):
            parse_reference_ends(["ES=not-a-timestamp"])


class TestParseExtraSeams:

    def test_iso_cutoff_with_colons_splits_at_last_colon(self):
        out = parse_extra_seams(["ES=2026-06-17T00:00:00:1.0090712749373991"])
        assert out == {
            "ES": [(pd.Timestamp("2026-06-17 00:00:00"), 1.0090712749373991)]
        }
        # Full float precision must survive parsing (no rounding).
        assert out["ES"][0][1] == 1.0090712749373991

    def test_multiple_seams_per_symbol_sorted_by_cutoff(self):
        out = parse_extra_seams([
            "CL=2026-06-12T11:00:00:0.9896682658617806",
            "CL=2026-05-01T00:00:00:1.01",
        ])
        cutoffs = [c for c, _ in out["CL"]]
        assert cutoffs == sorted(cutoffs)

    def test_comma_separated(self):
        out = parse_extra_seams(
            ["ES=2026-06-17T00:00:00:1.009,CL=2026-06-12T11:00:00:0.9897"]
        )
        assert set(out.keys()) == {"ES", "CL"}

    def test_missing_ratio_raises(self):
        with pytest.raises(BackfillValidationError):
            parse_extra_seams(["ES=2026-06-17T00_00_00"])

    def test_non_numeric_ratio_raises(self):
        with pytest.raises(BackfillValidationError):
            parse_extra_seams(["ES=2026-06-17T00:00:00:abc"])

    @pytest.mark.parametrize("bad_ratio", ["0", "-1.01", "1.0", "inf", "nan"])
    def test_invalid_ratio_values_raise(self, bad_ratio):
        with pytest.raises(BackfillValidationError):
            parse_extra_seams([f"ES=2026-06-17T00:00:00:{bad_ratio}"])

    def test_bad_cutoff_raises(self):
        with pytest.raises(BackfillValidationError):
            parse_extra_seams(["ES=garbage:1.009"])
