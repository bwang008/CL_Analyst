# Ticket Resolution Blueprint — 1h-reconnect-object-dtype_07082026_0032
**Ticket Directory:** `.agents/collab/tickets/1h-reconnect-object-dtype_07082026_0032/`
**Status:** Reviewer APPROVED (no human authorization required). Ready for `/tdd-manager`.

## Bug Summary
Live fleet goes **hourly-blind** after any Gateway/TWS reconnect: all children throw
`TypeError: loop of ufunc does not support argument 0 of type float which has no callable log method`
(surfacing as `AttributeError: 'float' object has no attribute 'log'`) at every top-of-hour 1H bar
close, so the 1H HourSet signal (the primary decision cadence) is silently skipped fleet-wide until a
manual restart. eventkit swallows the exception, so children stay alive and keep trading the 5m stream
— the hourly evaluation just stops.

**Root cause (confirmed, reproduced byte-for-byte in the trader env / pandas 1.5.3):**
On reconnect, the gap-backfill loop appends historical bars with
`for _, row in new_bars.iterrows(): self.data_manager_1h.append_bar(row.to_frame().T)`
(`src/live_execution/live_trader.py:3685` for 1H; twin at `:3624` for 5M). `new_bars` carries a
datetime64 `DateTime` column alongside float64 OHLCV, so `iterrows()` yields an **object-dtype Series**,
and `.to_frame().T` produces an all-object single-row DataFrame. `DataManager.append_bar` then does
`self._df = pd.concat([self._df, row])` (`src/live_execution/data_manager.py:484`), which **upcasts the
in-memory cache's float64 OHLCV columns (Close included) to object dtype** (in-memory only; the on-disk
parquet caches stay float64 — which is why a restart clears it and it recurs on the next reconnect).
`get_ratio_adjusted_df()` returns `self._df.copy()`, and the shared feature engine
`AlphaFactory.__init__` (`src/features/alpha_factory.py:180`) runs `np.log(self.close / self.close.shift(1))`,
which cannot vectorize an object-dtype Series → the crash.

`append_bar`'s own `if isinstance(row, pd.Series): row = row.to_frame().T` (data_manager.py:473-474) is the
same object-upcast for any raw-Series caller — the same fix covers it. The other reconnect concat
(`pd.concat([rolling_df_1h, new_bars])`, live_trader.py:3675) uses the properly-typed slice, stays
float64, and is NOT a crash path (Reviewer-verified).

**Not a recent regression:** backfill transpose (live_trader.py:3624/3685) = commit `cece7d55` (2026-06-01);
append_bar Series transpose (data_manager.py:473-474) = commit `465af9c2` (2026-02-24).

## Target Files
- `src/live_execution/data_manager.py` — the only production change (method `append_bar`, ~lines 470-485).
- `tests/test_data_manager.py` — new regression tests.
- `tests/test_reconnect_recovery_fixes.py` — must remain green (existing reconnect coverage; no change expected).
- DO NOT touch `src/features/alpha_factory.py` — shared backtest/training/live engine; backtest parity is sacred and must stay byte-identical.

## Required Changes
1. In `DataManager.append_bar`, AFTER the `Series → row.to_frame().T` transpose and the index
   normalization (the `set_index(DatetimeIndex(...))` block), and BEFORE `self._df = pd.concat([...])`,
   coerce each present OHLCV column to numeric:
   for each `col` in `("Open", "High", "Low", "Close", "Volume")`, if `col in row.columns`, set
   `row[col] = pd.to_numeric(row[col], errors="raise")`.
2. Use `errors="raise"` (NOT `"coerce"`): a genuinely non-numeric/corrupt bar must fail LOUDLY at
   ingestion — never silently NaN or default (project rule: invalid required fields raise; no silent nulls,
   no `try/except: pass`, no band-aid). This is the single ingestion choke point (steady-state appends,
   both reconnect backfills, and any Series caller all funnel through `append_bar`).
3. No signature/return-type change (method still mutates `self._df` in place, returns `None`).

## Tests (condition of approval — land green)
- RED repro: build an `ib_bars`-style frame (datetime64 `DateTime` column + float64 OHLCV), take a single
  row via `next(df.iterrows())[1].to_frame().T`, call `dm.append_bar(...)`, and assert
  `dm.dataframe["Close"].dtype == np.float64`; full-pipeline assert that `AlphaFactory` over
  `get_ratio_adjusted_df()` no longer raises. (Landing spot: `tests/test_data_manager.py`,
  `TestAppendAndFlush`, ~line 286.)
- LOUD-FAIL: appending a bar with a non-numeric OHLCV value (e.g. "NOTANUM") raises `ValueError`.
- Regression: existing `tests/test_data_manager.py` and `tests/test_reconnect_recovery_fixes.py` stay green,
  and the full fast suite passes: `conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"`.

## Non-blocking note (Reviewer)
`pd.to_numeric` on a string-numeric Volume can yield int64 rather than float64 — harmless (Volume is not
log-transformed; a lone int64 column concats fine) and the real reconnect path already delivers float64.
No extra handling required.

## Parity Assessment
Zero backtest/training parity risk. `alpha_factory.py` is untouched and stays byte-identical; coercion
changes no VALUES (already-float data merely regains float dtype), and backtest/training never call
`DataManager.append_bar` (livetest has its own independent mock). Live-only ingestion seam.

## Severity
HIGH operational (fleet-wide hourly-blindness on every reconnect until manual restart); the patch itself is
small and localized (a coercion loop at one seam).
