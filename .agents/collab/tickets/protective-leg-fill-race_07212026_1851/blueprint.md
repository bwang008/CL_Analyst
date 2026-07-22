# Ticket Resolution Blueprint — protective-leg-fill-race_07212026_1851
**Ticket Directory:** `.agents/collab/tickets/protective-leg-fill-race_07212026_1851/`
**Status:** Independent second-opinion audit. Auditor RCA performed directly (full git
archaeology, beyond the workflow's normal 5-commit restriction, per the task's explicit
mandate to reconstruct history). Impact-Reviewer reviewed independently (own subagent,
did not see severity classification) and returned **Request Revision** on Fix A/B (blast
radius clean, but two concrete correctness defects found and must be incorporated —
folded into "Required Changes" below as mandatory conditions) and **Request Human
Authorization** on Fix C (must be split into its own ticket). This document is the final
blueprint incorporating the Reviewer's corrections. **Per task scope: STOP here. No
TDD/implementation in this session — deploy and implementation remain human-gated.**

## Bug Summary — is the race real?

**Yes.** Two related, currently-unmitigated race windows exist in the live TP/SL
protective-order lifecycle, both stemming from the same design property: sibling-leg
cancellation is performed by our own Python process *after* observing a fill, over the
network, rather than guaranteed atomically by the broker.

**Race A — sibling TP/SL fill race.** `place_child_orders`
([ibkr_client.py:1572-1641](src/live_execution/ibkr_client.py#L1572-L1641)) submits the
TP (LMT) and SL (STP, `triggerMethod=1`) as fully independent standalone broker orders —
no `parentId`, no `ocaGroup`, nothing linking them broker-side. Both rest with
`outsideRth=True` ([:1621](src/live_execution/ibkr_client.py#L1621),
[:1634](src/live_execution/ibkr_client.py#L1634)), i.e. live through the thinnest,
gappiest overnight hours. `_on_standard_execution_event`
([live_trader.py:6363-6472](src/live_execution/live_trader.py#L6363-L6472)) observes a
`Filled` event on either tracked id, fires a **fire-and-forget** bulk
`cancel_open_orders` for the sibling
([:6444-6451](src/live_execution/live_trader.py#L6444-L6451) →
[ibkr_execution.py:284-313](src/live_execution/adapters/ibkr_execution.py#L284-L313) →
plain `ib.cancelOrder()`, no ack wait), and immediately calls `_reset_position_state()`
([:1271-1297](src/live_execution/live_trader.py#L1271-L1297)), which clears
`_tp_order_ids = []` and `_sl_order_id = None`. If the sibling **also** fills at the
broker before that cancel lands — plausible in a fast/gappy market, since the STP leg
converts to a MARKET order on trigger and can print several ticks past the trigger
level, well into the LMT leg's marketable range — its fill event arrives later, no
longer matches any tracked id, and falls into the generic "else" branch
([:6473-6490](src/live_execution/live_trader.py#L6473-L6490)): logged as
`"[TRADE] UNRECOGNIZED FILL"` at **ERROR level only** — no Telegram, no health event, no
correction. **Net broker effect: the full quantity is closed twice in the same
direction — a fully REVERSED, completely unprotected position** (both legs consumed, no
resting TP or SL left).

**Detection gap.** `_check_naked_position`
([:6234-6260](src/live_execution/live_trader.py#L6234-L6260)) — the fast kill switch —
early-returns at [:6248-6249](src/live_execution/live_trader.py#L6248-L6249) whenever
`self._active_trade_id is None`, which is *already true* by the time the second fill
lands (the first fill's processing just reset it). It structurally cannot see this
condition. The only remaining net is the **hourly** `_housekeeping_sweep`'s
UNTRACKED/NAKED branch
([:3177-3196](src/live_execution/live_trader.py#L3177-L3196)), which the code's own
comment says "stays human-only" — i.e. **detect-only, up to ~1 hour of a fully naked,
reversed, unmanaged live position**, alert-only, no auto-flatten.

**Race B — exit vs. stale resting leg (TIME BARRIER path).** `_check_time_barrier`
([:1651-1786](src/live_execution/live_trader.py#L1651-L1786)) cancels the resting legs
fire-and-forget ([:1717](src/live_execution/live_trader.py#L1717)) and, in the **same
callback tick**, immediately submits a brand-new full-quantity closing order
([:1734](src/live_execution/live_trader.py#L1734)) — with `_sl_order_id`/`_tp_order_ids`
cleared at [:1731-1732](src/live_execution/live_trader.py#L1731-L1732) *before* the new
exit is even submitted and with no verification the cancel actually landed. If a resting
leg fills anyway, the same double-close/reversal class results — this is precisely the
"exit order plus a still-live protective leg" scenario named in the task.

Tellingly, the codebase **already half-recognizes this hazard class**:
`_reconcile_pending_position_state`'s "BINDING CONDITION 1"
([:1857-1865](src/live_execution/live_trader.py#L1857-L1865)) explicitly documents
*"ib_insync fires cancelOrder fire-and-forget ... and a fast fill can still cross at the
exchange (the race documented at ibkr_client.py:1583-1588)"* and applies a
re-scan-before-re-arm guard — but **only** for deciding whether to re-arm protection
after retiring a stranded, *unfilled* time-barrier exit order. That discipline was never
extended to Race A or to `_check_time_barrier`'s own cancel-then-submit sequence (Race
B).

**Parity/test blind spot.** The simulated execution adapter's matching engine
([adapters/simulated_execution.py:588-650](src/live_execution/adapters/simulated_execution.py#L588-L650),
`on_bar_feed`) enforces **strict mutual exclusivity** between TP/SL — "if BOTH trigger on
the same bar → SL wins," loser popped from the dict, zero-latency, zero-race — to match
`BacktestEngine`'s pessimistic same-bar convention. The entire existing
SimulatedExecution-backed regression suite is therefore **structurally incapable** of
reproducing this race. It can only ever manifest against the live IBKR adapter, which is
exactly why it has persisted uncaught.

## Severity, likelihood, and the prior-incident claim

**Severity: HIGH if it fires** — a full reversal doubles directional exposure in the
wrong direction *and* removes all protection simultaneously, with no automated
correction for up to an hour and no immediate alert. **Likelihood: low-per-exit but
non-negligible cumulatively** across 5 symbols × every exit × continuous operation,
concentrated exactly in the conditions the system is most exposed to overnight
(`outsideRth=True` on every protective leg) and around news-driven spikes.

**The task asked me to verify, not assume, the internal claim that this race was "the
mechanism behind" the prior $296k SI naked-short incident.** I read the full, already
audited and approved blueprint for `reconnect-false-flat-oob_07082026_0731`. Its root
cause is **verifiably different**: a stale/unpopulated `ib.positions()` cache on
reconnect made the app believe a real, still-open position was flat, so it *proactively
cancelled* that position's genuine, still-good TP/SL — leaving it **naked** (unprotected)
but **not reversed** (nothing ever filled twice; the position itself was never
touched, only its protection was pulled out from under it). That mechanism is already
fixed (commit `246c598` + the `settle-confirm-event-loop` reconciler). **The internal
citation does not hold up against the code or the incident's own audited root cause and
should be corrected in the record.** Race A/B are a distinct, still-open exposure — and
capable of an even worse outcome (reversed, not merely naked) than the incident they've
been informally attributed to.

## Historical design reconstruction (why native OCA is safe here)

Commit `0a983d5` (2026-03-18) introduced `place_child_orders` using **native `parentId`
bracket linkage** (`tp.transmit=False`, `sl.transmit=True`, both `parentId =
parent_order_id`) — the classic IB bracket pattern. One week later, commit `4a50a4f`
(2026-03-25) ripped `parentId` out entirely. Reason (docstring,
[ibkr_client.py:1587-1596](src/live_execution/ibkr_client.py#L1587-L1596)): because this
codebase places TP/SL **after** the entry fills (two-phase, prices computed from the
actual fill price), by submission time the parent order is already `Filled`/terminal at
IBKR, and referencing it via `parentId` causes a hard rejection — **IBKR Error 201
("Parent order is being cancelled")** — reliably triggered by fast/split fills. The fix
replaced *all* broker-side linkage with hand-rolled "software OCA." **Native IBKR OCA
groups (`ocaGroup`/`ocaType`) were never used at any point in this repo's history** — the
historical failure (`parentId` + terminal parent) is orthogonal to `ocaGroup`, which
requires no parent-order reference of any kind. A second, unrelated historical change
(`064a776`, 2026-05-13) removed strategy-driven NET_TO_ZERO/FLIP exits in favor of
"bracket-only exits" (TP/SL/Trailing/Time-Barrier are the only exit paths) — any fix here
must preserve that invariant.

**How the proposed fix avoids re-introducing the historical problem:** it adds
`ocaGroup`/`ocaType` tags to the existing standalone orders. It does **not** add
`parentId`, does **not** change `transmit` semantics (both legs stay independently
`transmit=True`), and does **not** make TP/SL submission depend on the parent order's
state in any way — so the Error-201 failure mode (referencing a terminal parent) cannot
recur. It also does not touch the strategy/signal layer, so the bracket-only invariant
from `064a776` is untouched.

## Recommended fix on the merits

### Fix A (primary) — native OCA group on the TP/SL pair
**File:** `src/live_execution/ibkr_client.py`, function `place_child_orders`
(~1572-1641). No interface/signature change; no adapter changes (`ibkr_execution.py` is a
pass-through; `simulated_execution.py`'s bar-level matcher already achieves the same
*outcome* by a different, backtest-appropriate mechanism and must not change).

Set `ocaGroup` (str) and `ocaType = 1` ("cancel all remaining orders with block") on
**both** the TP order(s) and the SL order before `self.ib.placeOrder(...)`.

**Mandatory condition 1 (Impact-Reviewer, load-bearing):** do **not** derive `ocaGroup`
from `parent_order_id`. `_verify_and_heal_protective_legs`
([live_trader.py:2492-2630](src/live_execution/live_trader.py#L2492-L2630), already
implemented and live — called from startup recovery *and* the hourly housekeeping heal
sweep at three call sites: [:2105](src/live_execution/live_trader.py#L2105),
[:2482](src/live_execution/live_trader.py#L2482),
[:3234](src/live_execution/live_trader.py#L3234)) calls `place_child_orders(...,
parent_order_id=0, ...)` with a **literal hardcoded 0**
([:2590](src/live_execution/live_trader.py#L2590), comment: `# no parent — standalone`)
on every heal re-placement. Deriving `ocaGroup` from that value would give every
heal-placed bracket, across every symbol and every process, the **identical** group
string — a fill on one instrument's healed TP could cause IBKR to cancel an *unrelated*
instrument's SL: a cross-symbol naked-position bug worse than the one this ticket exists
to close. Generate the group id independently and guaranteed-unique per bracket instead
(e.g. a fresh UUID4 hex per `place_child_orders` call, optionally prefixed with
`self._execution_symbol` for log readability) — never reuse or derive it from a value
that can legitimately repeat.

**Mandatory condition 2:** verify (paper account is sufficient) whether IBKR scopes
`ocaGroup` matching per API-client-connection or account-wide, since the supervisor runs
5 sibling `LiveTrader` child *processes*, each its own IBKR client id/session. Condition
1's fix (independently-unique ids) makes the system safe either way, but the assumption
should be confirmed, not left implicit.

**Informational (no action required):** grouping all TP-ladder rungs (`_tp_order_ids`
can hold multiple ids — see the `tp_price: float | list[tuple[int, float]]` parameter
and its loop in `place_child_orders`) plus the SL into one `ocaType=1` group does not
create a *new* problem — `_on_standard_execution_event` already treats any single
tracked id's fill as a full close today. That existing ladder behavior has a separate,
pre-existing partial-fill/kill-switch-disarm question that is out of scope for this
ticket and should get its own ticket if confirmed.

Keep the existing software-side cancel
([live_trader.py:6444-6451](src/live_execution/live_trader.py#L6444-L6451)) as
defense-in-depth — it becomes an idempotent no-op once native OCA has already cleared
the sibling, not a replacement for it.

### Fix B (required companion) — fast detection of a residual double-fill
A live real-money system should not rely on a single broker-side guarantee with zero
independent verification, and the current silent-drop behavior is a defect regardless of
Fix A's effectiveness.

**File:** `src/live_execution/live_trader.py`. Track the TP/SL order ids cleared by the
most recent `_reset_position_state()` call (new small piece of state, e.g.
`self._recently_closed_leg_ids`, populated from the about-to-be-cleared
`_tp_order_ids`/`_sl_order_id` at the top of `_reset_position_state`, bounded lifetime —
cleared on the next entry). In `_on_standard_execution_event`'s "else" branch
(~6473-6490), before logging the generic "UNRECOGNIZED FILL," check whether `order_id`
is in that recently-cleared set; if so, this is the race actually happening, not a
mundane orphan fill.

**Mandatory condition 3 (Impact-Reviewer, load-bearing):** "trigger the same flatten
machinery `_check_naked_position` uses" is a **no-op as originally worded** — by the time
the stale second fill arrives, `_active_trade_id` is already `None`, and
`_check_naked_position`'s first line returns immediately on exactly that condition.
Extract the actual flatten steps (cancel remaining orders, market-close, ledger close,
CRITICAL Telegram, state reset) into a small **un-gated shared helper** that both
`_check_naked_position` and this new branch call, rather than calling
`_check_naked_position()` itself and relying on its guard clause.

**Mandatory condition 4 (Impact-Reviewer):** use `get_cached_position` (local cache), not
`get_position`, from this new branch. `_on_standard_execution_event` runs inside the
already-running `ib_insync` event loop (same callback chain as bar updates), and
`get_position` can trigger a blocking reconnect — exactly the "event loop is already
running" failure class this codebase has built extensive machinery elsewhere
(`settle-confirm-event-loop_07202026_0713`) to avoid. This matches the existing A-2
constraint already documented at
[live_trader.py:2990-2996](src/live_execution/live_trader.py#L2990-L2996).

### Fix C (separate finding — human-authorization required, split into its own ticket)
**Not part of this ticket's implementation scope.** Race B's root fix — deferring the
new time-barrier closing order to a genuinely-idle reconciler tick that first verifies
whether a fill already landed on the torn-down legs (reusing the exact
submit-and-defer/idle-reconciler architecture the codebase just built for the
structurally identical problem) — is a real state-machine change to
`_check_time_barrier` + `_reconcile_pending_position_state`, the same machinery that
received `killswitch-pending-exit-guard_07202026_1805` **the day before** this ticket
opened (2026-07-20). Both the Auditor and the independently-reasoning Impact-Reviewer
concur this must go through the Mandatory Human-Authorization Guardrail and ship as
its own follow-up ticket, not bundled here — the blast radius of this ticket should stay
reviewable, and that state machine is currently too fresh and load-bearing to touch
twice in one week without a human explicitly signing off.

## Blast radius summary (Impact-Reviewer's independent determination)
- **Interface Rule:** not triggered by Fix A or Fix B. `ExecutionClient`'s abstract
  `place_child_orders`/`close_position` signatures are untouched; both adapters
  (`ibkr_execution.py` pass-through, `simulated_execution.py`'s separate bar-level
  matcher) need no edits.
- **Base Class Rule:** not triggered. `LiveTrader` has no subclasses in the repo (leaf
  class); `place_child_orders` is a leaf method on `IBKRConnectionManager`.
- **Refactor Veto:** not triggered by Fix A or Fix B (single function / single small
  addition, single file each). **Triggered by Fix C** (two coordinated functions forming
  one state machine, freshly modified by a live incident fix) → Mandatory Human
  Authorization, separate ticket.
- Confirmed the legacy `parentId`-based `place_bracket_order` path
  ([ibkr_client.py:1360-1496](src/live_execution/ibkr_client.py#L1360-L1496)) is dead in
  the live entry path (the call site at
  [live_trader.py:5420](src/live_execution/live_trader.py#L5420) never passes
  `tp_price`/`sl_price`, so the adapter always routes to entry-only) — no parallel,
  inconsistent bracket-construction path is left behind by this fix.

## Target Files
- `src/live_execution/ibkr_client.py` — `place_child_orders` (Fix A: ocaGroup/ocaType,
  independently-unique group id per Mandatory Condition 1).
- `src/live_execution/live_trader.py` — `_reset_position_state`,
  `_on_standard_execution_event` (Fix B: recently-cleared-id tracking + new branch),
  plus extraction of a shared un-gated flatten helper used by both
  `_check_naked_position` and the new branch (Mandatory Condition 3).
- No changes to `src/live_execution/interfaces/execution_interface.py`,
  `src/live_execution/adapters/ibkr_execution.py`, or
  `src/live_execution/adapters/simulated_execution.py`.
- Explicitly OUT of scope for this ticket: `_check_time_barrier` /
  `_reconcile_pending_position_state` (Fix C — separate, human-authorized ticket).

## Required Changes
1. In `place_child_orders`, generate a fresh, independently-unique `ocaGroup` string
   (not derived from `parent_order_id`, which is `0` on every heal re-placement) and set
   it + `ocaType=1` on every TP leg and the SL leg before transmission. No other field
   changes; `transmit=True` and the absence of `parentId` stay exactly as today.
2. In `_reset_position_state`, before clearing `_tp_order_ids`/`_sl_order_id`, snapshot
   the ids being cleared into a new bounded-lifetime set (`_recently_closed_leg_ids` or
   equivalent).
3. In `_on_standard_execution_event`'s unrecognized-fill branch, check the new set
   before falling through to the generic ERROR-only log. On a match: log CRITICAL, send
   an immediate Telegram alert stating a reversal/double-fill is suspected, emit a health
   event, and call the **new shared flatten helper** (extracted from
   `_check_naked_position`'s body, called by both) using `get_cached_position` — never
   `get_position` — from this in-callback context.
4. Do not touch `_check_time_barrier` or `_reconcile_pending_position_state` in this
   ticket (Fix C is out of scope; open a new ticket with its own human-authorization
   request referencing this blueprint).

## Test cases (deliverable d)
Using the SIMULATED adapter alone cannot exercise Race A/B (Finding 6) — these need a
harness that can independently deliver two fill events for what were a TP/SL pair,
either via a fake/mock `ExecutionClient` in a `LiveTrader`-level unit test, or a
dedicated IBKR-adapter-level integration test that asserts `ocaGroup`/`ocaType` are
actually set on the placed orders (the broker-side guarantee itself is not something a
unit test can prove — that must be paper-verified per Mandatory Condition 2).

1. **ocaGroup/ocaType set correctly:** `place_child_orders` sets identical `ocaGroup` and
   `ocaType=1` on TP and SL orders; two separate calls (two separate trades / two
   separate symbols, including one going through the heal path with
   `parent_order_id=0`) produce **different** `ocaGroup` values — the collision this
   ticket must not introduce.
2. **Tiered TP ladder:** a multi-rung TP list produces N TP orders + 1 SL order, all
   sharing one `ocaGroup`.
3. **Residual double-fill detection (Fix B):** simulate the race directly — feed a Filled
   event for the SL id (triggers the existing exit path, ids cleared), then feed a
   second Filled event for the (now-untracked) TP id. Assert: CRITICAL log, Telegram
   sent, health event emitted, and the flatten helper is invoked (mock the exec client
   and assert `close_position`/`cancel_open_orders`/`telemetry.close_position` are
   called) — not the previous silent "UNRECOGNIZED FILL"-only path.
4. **`get_cached_position` used, not `get_position`, in the new branch** — assert the
   mock exec client's `get_position` is never called from this code path (regression
   guard against re-introducing an in-callback blocking-reconnect hazard).
5. **Trailing-stop modify preserves OCA membership:** after `_check_trailing_stop`
   modifies the resting SL order's `auxPrice` via `modify_order` (which re-transmits the
   *same* `ib_insync` Order object per its own docstring at
   [ibkr_execution.py:239-254](src/live_execution/adapters/ibkr_execution.py#L239-L254)),
   assert `ocaGroup`/`ocaType` on that Order object are unchanged — a moving/trailing
   stop must not fall out of its OCA pairing.
6. **Fast-vs-slow fill / partial-fill non-trigger:** a partial fill (`status !=
   "Filled"`) on either leg must not trigger the exit path or the new residual-detection
   branch — only a genuine full `"Filled"` status does (regression guard, existing
   behavior, must not change).
7. **No regression on the existing OOB/reconnect-false-flat and
   killswitch-pending-exit-guard test suites** — this ticket's changes must not touch
   `_check_time_barrier`/`_reconcile_pending_position_state`, so
   `tests/test_time_barrier_exit_fill_confirmation.py`,
   `tests/test_settle_confirm_loop_deferral.py`, and
   `tests/test_oob_entry_state_recovery.py` should be byte-identical in behavior; run
   them as a regression fence, not to be modified by this ticket.
8. Full fast suite green: `conda run -n trader python -m pytest tests/ -m "not slow"`.

## Deferred (separate ticket, human-authorization required)
Fix C (see above) — submit-and-defer the TIME BARRIER closing order to the idle
reconciler instead of a same-tick cancel-then-submit, reusing the
`settle-confirm-event-loop` architecture. Also out of scope: the pre-existing,
Reviewer-flagged question of whether a partial TP-ladder-rung fill should currently be
disarming `_active_trade_id`/the kill switch while other rungs' contracts remain open —
real but unrelated to this ticket's race, deserves its own ticket if confirmed.
