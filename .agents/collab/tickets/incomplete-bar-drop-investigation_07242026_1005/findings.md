# incomplete-bar-drop-investigation_07242026_1005 — RESOLVED: working as designed

**Operator question 2026-07-24 ~10:05 PT:** "Dropping incomplete current bar
at 2026-07-24 17:00:00" — is the data corrupted? What causes it?

## Verdict

NOT corruption — the message is the anti-corruption GUARD firing, shipped as
part of the reconnect-backfill fix (commit 394fa68, ticket
reconnect-backfill-inprogress-bar). Its absence, not its presence, would be
the worry.

## Cause chain (all verified today)

1. `DataManager._drop_incomplete_bar` (src/live_execution/data_manager.py:810-829):
   IBKR historical requests RETURN the currently-forming bar. Before 394fa68,
   that partial bar (partial volume, mid-bar close) was stitched into the
   series — the exact cause of the 2026-07-20 fleet-wide skipped-inference
   incident and next-bar corruption. The guard now drops any final bar whose
   close time hasn't been reached and logs this INFO line.
2. Why it fires HOURLY on NG right now: the pending NGQ26 -> NGU26 roll
   (parked 07-23 17:00) retries every 1h bar; each retry fetches fresh bars
   to test whether the CONTFUT seam has appeared; each fetch includes the
   in-progress bar -> dropped -> the seam check then runs on complete bars
   only. All 11 of today's drop lines are NG roll retries (11/11 grep
   correlation), each followed by "seam has not appeared ... RETRY".
3. The "17:00:00" label is the UTC bar label: at 10:00:11 PT the 17:00 UTC
   bar was 11 seconds old — the definition of incomplete.

## Data integrity verification (read-only)

warm_start_cache_NG_1h.parquet tail: complete hourly bars only, ZERO
duplicates, spacing 1h (one 2h gap = the daily 5-6pm ET halt, expected),
newest stored bar complete. Live in-memory series fresh (heartbeat
bar=0.1h). No partial bars anywhere.

## When the hourly message stops

When CONTFUT flips its lead to NGU26 (~NGQ26 last-trade-date 2026-07-29),
the seam appears, the roll resolves, and the hourly retries end. Occasional
drops will still appear after reconnect backfills — by design, fleet-wide.
Do NOT clear the pending roll manually (CL 07-16 precedent).

## Action

None. No fix needed; machinery covered by tickets
reconnect-backfill-inprogress-bar (guard) and roll-seam-preflip (retry loop).
