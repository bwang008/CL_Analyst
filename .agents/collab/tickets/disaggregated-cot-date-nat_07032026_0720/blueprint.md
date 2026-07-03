# Ticket Resolution Blueprint — disaggregated-cot-date-nat_07032026_0720

## Bug Summary
Downloading CFTC **Disaggregated** COT data for any commodity symbol crashes on the current environment (`trader` conda env, **pandas 1.5.3**):

```
File "scripts/download_macro_data.py", line 505, in download_cot_data
    combined["Date"].min().strftime("%Y-%m-%d"),
ValueError: NaTType does not support strftime
```

**Root cause:** `_normalize_cot_columns()` parses the report date at [line 283](../../../scripts/download_macro_data.py) with `pd.to_datetime(..., format="mixed", errors="coerce")`. `format="mixed"` was introduced in **pandas 2.0**; on pandas 1.5.3 it is treated as a literal strftime format, so **every** date fails to match and is coerced to `NaT`. Downstream, `combined = combined.dropna(subset=["Date"])` (line ~490) then drops **all** rows, leaving an empty frame whose `["Date"].min()` is `NaT`, which crashes the summary `strftime` at line 505.

This is the same class of bug already fixed for the **TFF** (financial-futures) path, where date parsing was routed through a new tolerant helper `_parse_cot_date()` ([line 288](../../../scripts/download_macro_data.py)). The disaggregated normalizer was intentionally left on `format="mixed"` at that time to preserve CL byte-identity — but that path is broken on this env.

**Impact / blast radius:** blocks regenerating COT for **all commodity symbols** (NG, GC, HG, PA — the disaggregated report), which blocks building their feature datasets (COT is a mandatory macro input). Discovered while executing `build-symbol-pipeline` for **NG** (2026-07-03): FRED downloaded fine, COT crashed. The existing `cftc_cot_cl.csv` on disk still has valid dates only because it was generated on an older pandas (≥2.0); any refresh on the current env fails. ES/NQ (TFF path) are unaffected — already fixed.

## Target Files
- `scripts/download_macro_data.py`

## Required Changes
1. **Line 283** (`_normalize_cot_columns`): replace the `format="mixed"` date parse
   ```python
   if "Date" in result.columns:
       result["Date"] = pd.to_datetime(result["Date"], format="mixed", errors="coerce")
   ```
   with a call to the existing tolerant helper, so both COT report families share one date parser:
   ```python
   result["Date"] = _parse_cot_date(df).values
   ```
   `_parse_cot_date` prefers the ISO `Report_Date_as_YYYY-MM-DD` column (pandas auto-detects ISO reliably on 1.5.3 — no format kwarg) and falls back to explicit `%Y%m%d` / `%y%m%d` for the older 8-/6-digit `As_of_Date_In_Form_*` columns.

   > Note: `_parse_cot_date` reads the **raw** `df` date columns, whereas the current code parses the already-renamed `result["Date"]`. Confirm the disaggregated raw frame exposes one of the recognized date columns (`Report_Date_as_YYYY-MM-DD` / `As_of_Date_In_Form_YYYYMMDD`); if the "Date" mapping in the col_map is the only source, either pass the mapped column name through or extend `_parse_cot_date`'s candidate list. Keep the single-helper design.

## Rationale
`format="mixed"` provides no benefit here (the CFTC date columns are single, well-known formats) and is the sole cause of the failure on pandas 1.5.3. Auto-detect + explicit fallbacks are byte-identical for CL's ISO dates while making the path work on the current env — and it de-duplicates date logic across the disaggregated and TFF adapters.

## Test Plan (TDD — required before implementing)
1. **RED first:** add a disaggregated date-parsing test to `tests/test_cot_adapters.py` (a `DisaggregatedAdapter`/`_normalize_cot_columns` fixture with a real `Report_Date_as_YYYY-MM-DD` value) asserting the parsed `Date` equals the expected `Timestamp` — this fails on the current code (NaT).
2. **CL byte-identical guard:** the existing `test_adapter_matches_legacy_normalizer` must stay green; additionally assert a small disaggregated fixture yields the same canonical rows (incl. non-NaT dates) as a frozen expectation.
3. Implement the change; watch both go GREEN.
4. **Full regression:** `conda run -n trader python -m pytest -q` — no regressions vs. the current baseline (706 passed, 1 skipped as of 2026-07-02).
5. **Integration:** re-run `conda run -n trader python scripts/download_macro_data.py --symbol NG` and confirm `cftc_cot_ng.csv` is written with a valid monotonic weekly date range and 0 NaT.

## Status
Reported per user request (SDLC — no code changed). NG `build-symbol-pipeline` execution is **paused at Phase 2** pending approval of this fix. NG Databento price data (2010-06-06→2026-07-02) and `fred_macro_data_ng.csv` were already downloaded successfully and are unaffected.
