# TDD Result — disaggregated-cot-date-nat_07032026_0720

## Outcome: ✅ PASS (742 passed, 0 failed)

## Bug Summary
`_normalize_cot_columns()` in `scripts/download_macro_data.py` used `pd.to_datetime(..., format="mixed")` on line 283 — a pandas 2.0+ feature. On the current env (pandas 1.5.3), this silently coerced every CFTC report date to `NaT`, crashing downstream at `NaT.strftime()`. This blocked COT data downloads for **all commodity symbols** (CL, NG, HG, GC, PA — the disaggregated report family). The TFF (financial) path was already fixed via `_parse_cot_date()`.

## Changes Made

### `scripts/download_macro_data.py` (line 283)
**One-line fix:** replaced `format="mixed"` with `_parse_cot_date(df).values`, reusing the existing pandas-1.5.x-compatible helper that the TFF normalizer already uses at line 354.

```diff
     if "Date" in result.columns:
-        result["Date"] = pd.to_datetime(result["Date"], format="mixed", errors="coerce")
+        result["Date"] = _parse_cot_date(df).values
```

### `tests/test_cot_adapters.py`
Added 2 tests to `TestDisaggregatedAdapterRegression`:
- `test_disagg_date_parsed` — asserts the parsed Date equals the expected `Timestamp("2026-06-23")`, not NaT
- `test_disagg_date_not_nat` — asserts zero NaT values in the Date column

Both confirmed **RED** on pre-fix code, **GREEN** after fix.

## Test Results
- **Full suite:** 742 passed, 0 failed (257s)
- **COT adapter tests:** 12/12 passed (including CL regression guard `test_adapter_matches_legacy_normalizer`)
- **No regressions** vs baseline

## Files Changed
| File | Change |
|------|--------|
| `scripts/download_macro_data.py` | Line 283: replaced `format="mixed"` with `_parse_cot_date(df).values` |
| `tests/test_cot_adapters.py` | Added `test_disagg_date_parsed` + `test_disagg_date_not_nat` |
