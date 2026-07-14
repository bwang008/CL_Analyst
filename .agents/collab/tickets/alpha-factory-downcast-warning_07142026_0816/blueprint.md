# Ticket Resolution Blueprint — alpha-factory-downcast-warning_07142026_0816
**Ticket Directory:** `.agents/collab/tickets/alpha-factory-downcast-warning_07142026_0816/`

## Bug Summary
The live fleet still emits `FutureWarning: Downcasting behavior in 'replace' is deprecated` from `src/features/alpha_factory.py:292` on every hourly inference, even though fix commit `0fa4dd7` is deployed and loaded (deployment verified: stderr logs cite line `:287` before the 2026-07-13 restart and `:292` after — the running code IS the fixed code).

`0fa4dd7` fixed the wrong layer: the warning is raised *inside* `DataFrame.replace()` whenever the frame contains a downcast-eligible object-dtype column; chaining `.infer_objects()` after the call cannot suppress it. The live frames DO contain such a column: the **`DateTime` column of `DataManager._df` gets upcast to object dtype by the 1H reconnect-gap backfill path**:

- `live_trader.py:4216-4217` appends backfilled bars via `row.to_frame().T` from `iterrows()` — the 1-row frame is all-object dtype.
- `data_manager.py:563-577` (`append_bar`) coerces `Open/High/Low/Close/Volume` back to numeric (fix from prior ticket `1h-reconnect-object-dtype_07082026_0032`) but **skips `DateTime`** — so `pd.concat` permanently upcasts the cache's `datetime64[ns]` DateTime column to object.
- Every hourly inference then builds features from that poisoned frame, and `replace([inf,-inf], nan)` at `alpha_factory.py:292` performs the deprecated object→datetime64 downcast and warns.

A restart temporarily silences it (pyarrow normalizes dtypes on cache save) until the first IBKR reconnect backfill re-poisons the frame — matching the user's observation that the warning returned after restart.

**Severity: LOW** — cosmetic today; auditor verified feature outputs are byte-identical (the downcast restores `datetime64[ns]`). **Not a recent regression** — residual tail of the incompletely-fixed `1h-reconnect-object-dtype_07082026_0032` ticket. Fast-tracked per workflow (LOW + not a regression). Auditor reproduced the bug end-to-end on the fleet interpreter (base Anaconda, pandas 2.2.2) with the real cache, and verified the proposed fix produces zero warnings with clean dtypes and byte-identical output values.

## Target Files
- `src/live_execution/data_manager.py` (primary fix — `append_bar`)

## Required Changes
**In `DataManager.append_bar`, immediately after the existing `pd.to_numeric` OHLCV coercion loop (around line 577):** coerce the incoming row's `DateTime` column back to datetime dtype before it is concatenated into `_df`. Logical requirements:

1. If the incoming 1-row frame has a `DateTime` column, convert it with `pd.to_datetime(..., errors="raise")`. `errors="raise"` is mandatory — a corrupt/unparseable bar timestamp must crash loudly per the repo's no-silent-defaults rule, never coerce to NaT.
2. The coercion must live in `append_bar` itself (shared by the 1h and 5m managers) so both paths are covered, mirroring how the prior OHLCV coercion was placed.
3. Add a comment explaining the constraint: an object-dtype `DateTime` row upcasts the cache's `datetime64` column on concat, and every downstream `replace([inf,-inf])` then fires the pandas 2.x downcast FutureWarning.

**Test requirements (TDD):**
- A test that pushes a bar through the exact live append path (`iterrows()` row → `.to_frame().T` → `append_bar`) against a manager whose `_df` has a `datetime64[ns]` DateTime column, and asserts the resulting `_df["DateTime"]` dtype is still `datetime64[ns]` (not object).
- A test (or extension of the above) asserting that running the inf-replace feature step over the post-append frame raises no `FutureWarning` (e.g. via `pytest.warns` absence / `warnings.simplefilter("error", FutureWarning)` around the call).
- A test that a bar with an unparseable `DateTime` value raises (no silent NaT).

**Explicitly out of scope (optional hardening, NOT part of this blueprint):** wrapping the replace sites at `alpha_factory.py:292` / `feature_pipeline.py:313` in `pd.option_context("future.no_silent_downcasting", True)`. The primary fix fully resolves the reported symptom; the hardening would also need a pandas-version guard for the 1.5.3 trader env. Revisit only if a new object-dtype passenger appears.

**Deployment note:** the fix is inert for the running fleet until the next operator restart (code is loaded at process start). No cherry-pick needed — the fleet runs `development` directly from this working tree.
