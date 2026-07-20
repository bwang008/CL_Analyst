# TDD Result — reconnect-backfill-inprogress-hourbar_07202026_1120

**Outcome:** GREEN. Full fast suite **2442 passed, 1 skipped** (prior baseline 2439 +
the 3 new tests; zero regressions). Verified by the TDD-Manager running
`conda run -n trader python -m pytest tests/ -m "not slow"`.

**Impact-Reviewer APPROVED (inline variant). Deploy operator-gated — INERT until the
operator restarts the fleet (should land on the SAME restart as settle-confirm
731ebed). Canary required.**

## What the fix does

The reconnect backfill no longer stitches the IBKR fetch's in-progress (not-yet-closed)
tail bar into the rolling window. Only bars whose close is at/before `now`
(`index + fetch_bar_duration <= now`) are kept; the in-progress tail is left for the
live stream to deliver when it completes (firing `NEW <N> BAR` + inference normally).
This stops both the skipped-inference and the next-hour feature corruption.

## Source change — `src/live_execution/live_trader.py` only (+20 lines: 2 filters + comments)

In `_backfill_reconnect_gap_async`, one completeness filter inserted before the
existing `new_bars = chunk_df[chunk_df.index > self._last_bar_time_*]` in each block:

- **5M block:** `chunk_df = chunk_df[(chunk_df.index + pd.Timedelta("5 min")) <= now]`
- **1H block:** `chunk_df = chunk_df[(chunk_df.index + pd.Timedelta("1 hour")) <= now]`

Reuses the tz-naive-UTC `now` from `:4491` (no recompute); duration from the
**fetch-literal** bar-size (not `self._bar_size`, which can be 2h/4h); no new imports.

## Binding conditions (all met)

1. **Inline in BOTH blocks**, fetch-literal duration vs `now`, before the `new_bars`
   filter. Empty-after-drop handled by the existing `len(new_bars) > 0` else branch.
2. **LOUD** — plain boolean filter, no inner `try/except`, no silent fall-through; the
   outer `except Exception: log.exception(...)` stays intact.
3. **Dedup untouched** — `_on_bar_update_1h`/`_on_bar_update_5m` `bar_time <=
   _last_bar_time_*` guard unchanged (fixing the sole producer of the bad
   `_last_bar_time` resolves the bug).

## Tests (no existing test weakened)

- **NEW** `tests/test_reconnect_backfill_incomplete_bar.py` (3 cases): 1H in-progress
  tail not stitched + `_last_bar_time_1h` not advanced (completed bars still stitched,
  no gap/double-count); the completed same-timestamp bar then fires `_on_new_bar`
  (end-to-end — the missed hour no longer missed); 5M twin. `now` frozen
  deterministically (datetime subclass, mid-hour 16:17 UTC) so the real stitch/dedup
  runs on any host TZ.
- The Tester correctly did NOT touch the strict-locked `test_reconnect_recovery_fixes.py`
  (its empty-df-mock R1 tests stay green) or `test_shallow_5m_bootstrap.py`.

## Parity

The backtest/training series is completed-OHLCV only; dropping the not-yet-closed bar
makes the live rolling window match the trained distribution — it changes which bars
enter ONLY by removing one that never should have entered. No COMPLETED bar's stitching
changes.

## CANARY

After restart, verify a reconnect+backfill that lands mid-hour does NOT skip the
following hourly inference and does NOT leave a partial bar in the window. The daily
~14:15 PT gateway restart triggers a backfill — the next hourly inference for all
children firing normally after it is the canary.

## Out of scope

Any past-poisoned cache rows (the fix stops NEW occurrences). Unrelated to
settle-confirm-event-loop (731ebed).
