# Proposed fix -- protective-leg-fill-race_07212026_1851

## Bug being fixed
Live TP/SL exit legs are independent, unlinked broker orders (no parentId, no
ocaGroup). The "cancel the other leg on fill" behavior is done entirely in our
own Python (`live_trader.py:_on_standard_execution_event`, software OCA),
*after* observing a fill notification, via a fire-and-forget `ib.cancelOrder`.
If the sibling leg also fills at the broker before that cancel lands (gappy /
fast / thin overnight market -- both legs rest `outsideRth=True`), BOTH legs
close the position: a full double-close = a REVERSED position, left totally
unprotected. The internal fill-handler cannot even recognize the second fill
as a problem: `_reset_position_state()` clears the tracked TP/SL ids as soon
as the FIRST fill is processed, so the second fill's id no longer matches
anything and falls into a generic "UNRECOGNIZED FILL" branch that only logs
an ERROR line -- no alert, no correction. The only net that can catch it is
an *hourly* housekeeping sweep, and only as a detect-only alert (no
auto-flatten). A second, related race exists on the TIME BARRIER exit path,
where a resting leg is cancelled (fire-and-forget) and a brand-new full-size
closing order is submitted in the SAME callback tick, with no verification
the cancel actually landed first.

## Fix A (primary) -- native OCA group on the TP/SL pair
**File:** `src/live_execution/ibkr_client.py`, function `place_child_orders`
(currently ~1572-1641). No other file needs to change for this piece.

Generate a per-trade-unique `ocaGroup` string (e.g. derived from
`parent_order_id`, which is already a parameter -- no new parameter needed)
and set it, with `ocaType = 1` ("cancel all remaining orders with block"), on
BOTH the TP order and the SL order before `self.ib.placeOrder(...)`. Both
orders keep `transmit=True` independently and keep NO `parentId`, exactly as
today. This makes "the other leg is cancelled when one fills" an atomic
guarantee performed by the exchange/IBKR matching engine, not something our
process has to observe-then-execute over the network. It does not reintroduce
the historical `parentId`/Error-201 problem (commit 4a50a4f, 2026-03-25):
that failure was specifically caused by referencing a parent order's id after
the parent had already gone terminal; `ocaGroup` carries no parent reference
of any kind, so a two-phase, post-fill-priced child placement is unaffected.

No signature change to `place_child_orders`, `ExecutionClient` interface, or
either adapter. `adapters/ibkr_execution.py::place_child_orders` is a
pass-through and needs no edit. `adapters/simulated_execution.py`'s matching
engine already enforces same-bar TP/SL mutual exclusivity via a different,
bar-level mechanism (pessimistic SL-wins) appropriate for backtest parity --
that should NOT change; it is not being asked to model ocaGroup, it already
achieves the same *outcome* by construction for its own domain.

Existing software-side cancel in `live_trader.py:6444-6451`
(`_on_standard_execution_event`) should be KEPT as defense-in-depth (an
idempotent no-op if the sibling is already gone via native OCA) -- not
removed. The ocaGroup tag is the thing that actually closes the race; the
software cancel remains a harmless backstop for any leg that, for some
unrelated reason (e.g. a manual TWS cancel), didn't have OCA membership.

## Fix B (required companion) -- fast detection of a residual double-fill
Even with native OCA, a live real-money system should not rely on a single
broker-side guarantee with no independent verification, and the current
silent-drop behavior is a defect in its own right regardless of Fix A.

**File:** `src/live_execution/live_trader.py`. Track the TP/SL order ids
cleared by the most recent `_reset_position_state()` call (small addition,
e.g. `self._recently_closed_leg_ids`, populated at the top of
`_reset_position_state` from the about-to-be-cleared `_tp_order_ids` /
`_sl_order_id`, with a bounded lifetime -- cleared on the next entry). In
`_on_standard_execution_event`'s "else" branch (currently ~6473-6490), before
logging the generic "UNRECOGNIZED FILL", check whether `order_id` is in that
recently-cleared set. If so, this is not a mundane orphan fill -- it is the
race actually happening: log CRITICAL, send an immediate Telegram alert,
emit a health event, and trigger the SAME flatten machinery
`_check_naked_position` uses (rather than waiting for the next hourly
housekeeping sweep). This does not change `_check_naked_position`'s own
gating (still requires `_active_trade_id`), it adds a *new*, narrowly-scoped
trigger for exactly this one condition.

## Fix C (separate finding, Race B: exit vs. stale resting leg on TIME BARRIER)
**File:** `src/live_execution/live_trader.py`, `_check_time_barrier`
(~1651-1786) and `_reconcile_pending_position_state` (~1788-1971).

Recommend NOT tagging the new exit into the old OCA group (that would
require exposing OCA membership through `close_position`'s signature --
an interface change on `ExecutionClient`, larger blast radius). Instead,
apply the SAME pattern the codebase already built and shipped for the
structurally identical problem (`settle-confirm-event-loop_07202026_0713`):
`_check_time_barrier` cancels the resting legs (fire-and-forget, as today)
and DEFERS -- it does not submit the new closing order in the same callback
tick. A new idle-tick reconciler branch verifies, on a genuinely-idle poll,
whether a fill landed on the old leg ids during the teardown window
(non-blocking local-cache reads only, same A-2 constraint the sweep already
follows); if so, that IS the real close (book it, done, no new order --
double-close impossible by construction); if the legs are confirmed off the
book with no fill, only THEN submit the fresh closing order and proceed
through the existing submit-and-defer flow unchanged.

This is a genuine state-machine change to the SAME machinery that received
`killswitch-pending-exit-guard_07202026_1805` days ago (2026-07-20) after a
real live incident there. I am flagging this for likely HUMAN AUTHORIZATION
regardless of the formal veto rules, given how fresh and load-bearing that
state machine currently is, and would suggest splitting it into its own
follow-up ticket rather than bundling it with Fix A/B, to keep this ticket's
blast radius reviewable.

## What I need from you (Impact-Reviewer)
1. Map blast radius for Fix A and Fix B against the Interface Rule / Base
   Class Rule / Refactor Veto. My read: Fix A touches one function in one
   file, zero signature changes -- should not trip anything. Fix B adds one
   small piece of per-instance state and one new branch in one existing
   function, also single-file -- please confirm or challenge that read.
2. Confirm whether Fix C should be REQUIRED for this ticket to be considered
   complete, or is legitimately severable into its own ticket given the
   human-authorization flag and its proximity to very recent, fragile,
   already-shipped live-order-routing logic.
3. Flag anything I am underweighting: multi-leg/tiered TP ladders
   (`_tp_order_ids` can hold >1 id -- `place_child_orders` already loops over
   a list of `(qty, price)` tuples for tiered TPs) interacting with a single
   shared `ocaGroup` across N TP legs + 1 SL leg (all N+1 orders in ONE OCA
   group, `ocaType=1`); the pending, operator-approved-but-unimplemented
   `unprotected-leg-verification_07082026_0315` heal path, which will need to
   assign matching ocaGroup membership to any leg it re-places (and possibly
   MODIFY a surviving leg's ocaGroup if it predates this fix) -- flag this as
   a coordination note for whoever implements that ticket, not something to
   solve here.
