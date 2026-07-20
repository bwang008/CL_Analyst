# Ticket Dashboard — reconnect-backfill-inprogress-hourbar_07202026_1120

**Ticket Directory:** `.agents/collab/tickets/reconnect-backfill-inprogress-hourbar_07202026_1120/`
**Source:** operator noticed the 10AM PT (16:00 UTC) 1H inference bar did not come in
2026-07-20. TDD-Manager confirmed from `reports/fleet/fleet_20260720.log`: the
`NEW 1H BAR: 2026-07-20 16:00:00` event fired for NONE of the 5 children (bars present
at 15:00 UTC @09:00 PT and 17:00 UTC @11:00 PT, gap at 16:00 UTC). A connectivity flap
09:57-09:59 PT triggered a reconnect backfill at 09:59:38 (=16:59:38 UTC) that logged
`RECONNECT BACKFILL (1H): stitched 1 bars, latest=2026-07-20 16:00:00` — i.e. it
stitched the IN-PROGRESS 16:00 UTC bar (which does not close until 17:00 UTC =
10:00 PT). Hypothesis: the completed 16:00 bar delivered by the live stream at
10:00 PT was then deduped against the backfilled in-progress bar -> NEW 1H BAR event
suppressed -> 10AM inference skipped fleet-wide. NOT a safety issue (positions held +
protected), but a missed decision cycle + a possibly-partial bar left in the rolling
window. Unrelated to the settle-confirm-event-loop fix (different subsystem: data
ingestion / reconnect backfill vs order-routing).

[2026-07-20T11:22 PT] | reconnect-backfill-inprogress-hourbar_07202026_1120 | TICKET-MANAGER | STATUS: Ticket minted, workspace created. Evidence confirmed from log. Spawning Ticket-Auditor. Waiting for root-cause + proposed fix.
[2026-07-20T11:35 PT] | reconnect-backfill-inprogress-hourbar_07202026_1120 | TICKET-MANAGER | STATUS: Auditor replied. ROOT CAUSE VERIFIED: (A) _backfill_reconnect_gap_async (live_trader.py:4557-4616 1H, 4494-4555 5M) fetches with endDateTime=""/keepUpToDate=False -> IBKR returns the IN-PROGRESS final bar; stitch filter :4581 only drops OLDER bars, nothing drops the incomplete tail -> partial 16:00 stitched + _last_bar_time_1h advanced to 16:00. (B) _on_bar_update_1h dedup :4779 is timestamp-only (bar_time <= _last_bar_time_1h -> early return BEFORE NEW 1H BAR/append/inference) -> completed 16:00 bar dropped entirely. WORSE THAN FRAMED: (1) partial bar PERSISTS in window+cache -> corrupts NEXT hour's (17:00) features; (2) trigger is ANY reconnect+backfill after ~MM:10 of any hour (threshold :4562 >70min), not just final minutes -> 10:00 PT incidental; recurrence material given frequent flaps; (3) 5M path has IDENTICAL latent bug. FIX: apply the EXISTING DataManager._drop_incomplete_bar guard (data_manager.py:810-829, already used in 4 other fetch paths :848/:915/:1412/:1792) to the backfill's 1H+5M stitch, before the new_bars filter, completeness from fetch bar-size vs now (UTC-naive verified). Preserves gap-fill (completed missed bars still stitched; only in-progress tail deferred to live stream) + IMPROVES parity (window = completed bars only, matching training). SEVERITY HIGH (silent fleet-wide missed decision + feature corruption), NOT a recent regression (latent since 06-01 cece7d55). New red test needed (existing suite mocks empty fetch). Proceeding to Step 3 Impact-Reviewer (severity/regression WITHHELD). Waiting.
[2026-07-20T11:48 PT] | reconnect-backfill-inprogress-hourbar_07202026_1120 | TICKET-MANAGER | STATUS: Reviewer APPROVED (localized inline variant) — independently verified claims A-D from source, correctness/parity clear (no gap/double-count, now-staleness safe-directional, parity improved). 3 binding conditions: (1) inline drop in BOTH 5M+1H blocks, completeness from FETCH literal bar-size vs now:4491, before the new_bars filter; (2) keep it LOUD (no try/except swallow); (3) do NOT touch the dedup (B) — fixing (A) alone is sufficient. DRY/shared-helper variant NOT approved (would need human auth — Base Class + Refactor veto over _drop_incomplete_bar's 4 dependents). No human gate on the inline path. BLUEPRINT WRITTEN -> blueprint.md. Handing to /tdd-manager (inline variant); deploys on the same operator restart as settle-confirm 731ebed. Canary required.
