# Ticket Resolution Blueprint — reconnect-backfill-inprogress-hourbar_07202026_1120
**Ticket Directory:** `.agents/collab/tickets/reconnect-backfill-inprogress-hourbar_07202026_1120/`

> ## ✅ Impact-Reviewer APPROVED (localized inline variant, 3 conditions)
> No human-authorization gate on the inline path. The DRY/shared-helper variant was
> explicitly NOT approved (would touch `_drop_incomplete_bar`'s 4 dependents across
> two files = Base Class + Refactor veto) — **implement the localized inline variant
> only.** Deploy is operator-gated; the operator is about to restart to deploy the
> settle-confirm fix, so this should land on the SAME restart. **Canary required.**

## Bug Summary

**Incident:** 2026-07-20 the **10:00 AM PT (16:00 UTC) hourly inference fired for NONE
of the 5 fleet children** — a full hour's 1H trading decision silently skipped
fleet-wide (positions held + protected; not a naked/safety issue). Confirmed from
`reports/fleet/fleet_20260720.log`: `NEW 1H BAR: 2026-07-20 16:00:00` is absent for
every child; `15:00:00` (~09:00 PT) and `17:00:00` (~11:00 PT) are present.

**Root cause (verified by Auditor + Impact-Reviewer, file:line):** two
independently-correct pieces interact.

- **(A) The reconnect backfill stitches the still-forming bar.**
  `LiveTrader._backfill_reconnect_gap_async` (`src/live_execution/live_trader.py`,
  1H block ~`:4557-4616`, 5M block ~`:4494-4555`) fetches via
  `fetch_historical_bars_by_duration_async(... bar_size="1 hour"/"5 mins" ...)`
  (`:4572`), which bottoms out in `ibkr_client.py` `reqHistoricalDataAsync` with
  `endDateTime=""` (=now, `ibkr_client.py:1050`) and `keepUpToDate=False`
  (`:587-595`). An empty end-time makes IBKR return the **currently-forming (partial)
  bar as the last row**; `keepUpToDate=False` only disables the live push, it does not
  exclude the partial. The stitch filter `new_bars =
  chunk_df[chunk_df.index > self._last_bar_time_1h]` (`:4581`; 5M twin `:4519`) drops
  only OLDER bars — **nothing drops the incomplete tail.** So the partial `16:00` is
  stitched into `rolling_df_1h`, written to the DataManager cache, and
  `self._last_bar_time_1h` is advanced to `16:00` (`:4596`).
- **(B) The completed bar is then suppressed by the timestamp-only dedup.**
  `_on_bar_update_1h` (`:4754-4807`): when the live stream delivers the COMPLETED
  `16:00` bar at 17:00 UTC, dedup `if ... bar_time <= self._last_bar_time_1h: return`
  (`:4779-4780`) sees `16:00 <= 16:00` → early `return` BEFORE the `NEW 1H BAR` log,
  the rolling append, the `append_bar`, and `_on_new_bar(...)`. The completed bar is
  dropped entirely → no inference. (5M twin dedup at `:4721`.)

**Secondary harm (confirmed):** the completed bar returns early and never overwrites,
so the **PARTIAL `16:00` bar persists** in `rolling_df_1h` and the DataManager cache
(`append_bar`'s keep="last" dedup never gets the completed bar to supersede it). The
NEXT hour's inference (`17:00`/11:00 PT) then builds features from a window whose
`16:00` row is the partial bar → **silent feature corruption of the following hour**,
with the poisoned cache row never superseded.

**Trigger breadth (confirmed):** NOT limited to the final minutes. Gap is measured
against the last completed-bar label with threshold `>70 min` (`:4562`), so **any
reconnect+backfill completing after ~MM:10 of any hour** stitches that hour's
in-progress bar and skips its inference. 10:00 PT was incidental; recurrence is
material given the fleet's frequent nightly/daily flaps. **The 5M path has the
identical latent bug.**

**Severity: HIGH** (silent fleet-wide missed decision + next-hour feature corruption
on a real-money account; no crash/alert — only operator eyeballing caught it).
**NOT a recent regression** — latent since the 1H reconnect backfill was added
2026-06-01 (`cece7d55`); dedup dates to 2026-03-29 (`df373b6b`).

## Target Files

- `src/live_execution/live_trader.py` — `_backfill_reconnect_gap_async` (both the 1H
  and 5M stitch blocks). **The only source file changed** (inline variant).
- `tests/test_reconnect_recovery_fixes.py` — extend (its existing R1 tests mock the
  fetch to return an empty df, so the stitch block is never driven with a partial
  tail) OR a new focused test file; the TDD-Tester's call.

## Required Changes (localized inline variant ONLY)

### The invariant this establishes

> **The reconnect backfill never stitches an incomplete (not-yet-closed) bar.** Only
> bars whose close time is at or before `now` (`bar_end = index + fetch_bar_duration
> <= now`) enter the rolling window / cache from the backfill; any in-progress tail
> bar is left for the live stream to deliver when it completes (which fires
> `NEW <N> BAR` + inference normally).

### Change — drop the incomplete final bar in BOTH backfill blocks

In `_backfill_reconnect_gap_async`, **before** the `new_bars = chunk_df[chunk_df.index
> self._last_bar_time_*]` filter, drop any incomplete tail bar from the fetched
`chunk_df`, in BOTH the 5M block (before `:4519`) and the 1H block (before `:4581`):

- **Completeness test:** `chunk_df = chunk_df[(chunk_df.index + <fetch_bar_duration>)
  <= now]`, where `<fetch_bar_duration>` is derived from the **FETCH literal**
  bar-size (`"1 hour"` → `pd.Timedelta("1 hour")`, `"5 mins"` → `pd.Timedelta("5
  min")`) — **NOT `self._bar_size`** (which may be `2h`/`4h` and would mis-size the
  test). Use the already-computed tz-naive-UTC `now` (`:4491`); `chunk_df.index` is
  tz-naive UTC (fetch `make_naive=True`), so the comparison is apples-to-apples (same
  basis as the existing `> _last_bar_time` filter and `_drop_incomplete_bar`).
- This mirrors the existing `DataManager._drop_incomplete_bar`
  (`data_manager.py:810-829`, already applied in the 4 other fetch paths `:848`,
  `:915`, `:1412`, `:1792`) — the reconnect backfill is the one fetch path that omits
  it. You MAY call the fetch with the completeness already handled, but the approved
  variant is the **inline drop in each block** (do NOT factor a shared helper — that
  DRY refactor was NOT approved).

### BINDING CONDITIONS (Impact-Reviewer)

1. **Apply the drop INLINE in both the 5M and 1H blocks**, computing completeness from
   the **fetch literal** bar-size against the existing `now` (`:4491`), inserted
   **before** the `new_bars = ...` filter. Empty-after-drop is already handled by the
   existing `len(new_bars) > 0` else branch (0 stitched → `_last_bar_time_*` unchanged
   → the completed bar arrives fresh on the live stream and fires normally).
2. **Keep it LOUD** — no `try/except: pass`, no silent fall-through to keeping the
   partial. (The existing `_drop_incomplete_bar` swallows exceptions; do NOT replicate
   that swallow in the new inline code.)
3. **Do NOT touch the dedup at (B)** — it is an intentional monotonic idempotency
   guard; it only misfired because (A) advanced `_last_bar_time_*` to a partial label.
   Removing the sole producer of that condition (A) fixes both the skip and the
   corruption. Touching the dedup is out of scope and riskier.

## Correctness / parity (verified — preserve these properties)

- **No new gap, no double-count.** Genuine multi-hour outage: completed missed bars
  satisfy `bar_end <= now` and are still stitched; only the single in-progress tail is
  deferred to the live stream, which redelivers it (as `bars[-2]`) when it completes —
  the same mechanism every normal hour uses. Incident case: `new_bars` empty →
  `_last_bar_time` stays `15:00` → fresh `16:00` fires cleanly.
- **`now`-staleness is safe-directional:** `now` is read once at `:4491` before the
  async fetch, so it is slightly old at return — that makes the completeness test
  STRICTER (never keeps a partial); any just-completed tail it drops is redelivered by
  the live stream.
- **Parity moves the right way:** the backtest/training series is completed-OHLCV
  only. Dropping the partial makes the live rolling window match the trained
  distribution — it changes which bars enter ONLY by removing a bar that never should
  have entered.

## Tests (new red regression required — NO test loosening)

Existing `tests/test_reconnect_recovery_fixes.py` R1 tests (`:352-390`) assert only
fetch issuance and mock the fetch `.empty` (truthy) so the stitch block is skipped —
they cannot catch this. Do NOT weaken them. Add:

1. **The regression:** mock `fetch_historical_bars_by_duration_async` to return a
   `chunk_df` whose TAIL bar has `bar_end > now` (an in-progress bar) plus one or more
   genuinely-completed earlier bars. Assert: (a) the in-progress tail is **NOT
   stitched** and `_last_bar_time_1h` is **NOT advanced** to it (stays at the last
   completed label); (b) the completed earlier bars ARE stitched (no gap, no
   double-count); (c) a subsequent `_on_bar_update_1h` for the completed
   same-timestamp bar fires `NEW 1H BAR` / `_on_new_bar` (inference runs).
2. **5M twin:** the analogous case for the 5M block.
3. The test must control `now` (freeze/inject, or build bar timestamps relative to
   real `now`) since the backfill reads the wall clock — the existing R1 gap-anchor
   test models wall-clock handling; reuse that scaffolding.
4. Full fast suite green: `conda run -n trader python -m pytest tests/ -m "not slow"`.

## Hard constraints

- **NO CHEAP FIXES:** principled `bar_end <= now` (never a magic constant / hardcoded
  timestamp); no null-defaulting; no `try/except: pass`; no blind sleeps/retries; no
  loosening tests.
- **Live/backtest parity** is the sensitive point — the fix must feed inference the
  same completed-bar series the model trained on (this fix moves parity the right way;
  verify no divergence).
- Confine source changes to `src/live_execution/live_trader.py`. If a
  signature/interface/shared-helper change appears necessary, STOP and report — the
  DRY refactor is NOT authorized.
- **Deploy operator-gated.** Commit with "deploy pending operator restart." Branch =
  current fleet working branch; stage file-by-file, leave operator WIP untouched.

## CANARY

Before wide redeploy (per the standing rule + Reviewer): after restart, the first
reconnect+backfill that lands mid-hour must NOT skip that hour's inference and must
NOT leave a partial bar in the window. Hard to force on demand, so at minimum verify
via a post-restart reconnect (or the daily 14:15 gateway restart, which triggers a
backfill) that the following hourly inference fires for all children.

## Out of scope

The already-poisoned cache rows from past occurrences (if any persist) — a separate
data concern; this fix stops NEW occurrences. The settle-confirm-event-loop fix
(731ebed) is a different subsystem.
