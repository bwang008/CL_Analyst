# log-cosmetics-cancel-bounce_07222026_2330 — quiet expected cancel bounces + ASCII log hygiene

**Operator request 2026-07-22 ~23:15 PT:** roll two cosmetic log fixes into
one ticket: (1) the Error 10148 quiet-down offered after the OCA
sibling-cancel investigation, (2) the `→` arrow escapes seen in log
sinks. Investigation added a systemic root cause for (2) and surfaced a
third item (no-op trailing modifies) which is order-routing-adjacent and
HELD for separate operator approval.

## 1. Expected-cancel-bounce downgrade (Error 10148/10147)

Evidence: 5x `ib_insync.wrapper: Error 10148 ... state: Cancelled` today —
every protective-leg fill triggers the child's belt-and-suspenders sibling
cancel, which races the broker's own ocaType=2 server-side cancel and
bounces. Logged at ERROR by ib_insync; pollutes every SL/TP fill and every
fleet_health scan.

Fix (NO order-routing change — the redundant cancel stays):
- `log_config.py`: module registry `register_expected_cancel_bounce(order_id)`
  (timestamped, stale-pruned, 300s TTL) + `ExpectedCancelBounceFilter`
  attached to the `ib_insync.wrapper` logger: an Error 10147/10148 whose
  reqId is a REGISTERED, FRESH id is downgraded to INFO with an
  "(expected: ...)" annotation; any other 10147/10148 stays ERROR — that
  asymmetry is the point (a bounce for an id we never cancelled is the
  real anomaly).
- Registration at the ONLY two `ib.cancelOrder` chokepoints:
  `ibkr_client.cancel_open_cl_orders` (the OCA bulk teardown) and
  `IBKRExecAdapter.cancel_orders_by_ids` (OOB-recovery primitive).

## 2. ASCII log hygiene

Evidence: operator saw `... modified SL order 110: 2.94 → 2.94`. The
literal escape lives in `reports/fleet_stderr/*.stderr.log`: the child's own
console StreamHandler is the one UNSANITIZED path (fleet/per-cid file logs
use AsciiFormatter; the runner sanitizes only its echo, the sink keeps raw
child bytes; Windows backslashreplace turns the real arrow into `→`).
Source sweep: the arrow at live_trader.py:1701 is the only `→` in live
code; ~40 em-dashes in log strings would escape the same way in that sink.

Fix:
- live_trader.py:1701 `→` -> `->` at source (ascii_safe doctrine).
- `_setup_file_logging` also applies AsciiFormatter to the root logger's
  existing StreamHandler(s) — sanitizing the child's stderr stream itself,
  which fixes ALL non-ASCII (em-dashes included) in the stderr sinks in one
  place without churning 40 format strings. Tracebacks bypass logging
  formatters entirely, so crash-capture fidelity is untouched.
- Out of scope: `databento_data_builder.py` arrows (offline tooling, not a
  fleet sink).

## 3. HELD — no-op trailing modifies (needs operator go)

`TRAILING STOP: modified SL order 110: 2.94 -> 2.94` at 19:15 and 23:00 —
the ratchet recomputes, rounds to the SAME price, and still transmits a
Modify + ledger persist + 2 log lines. Proposed: skip the broker round-trip
when the rounded new SL equals the tracked price. Provably a no-op, but it
touches the order-request path -> human gate; NOT implemented here.

## Tests

tests/test_log_cosmetics.py: filter downgrades registered+fresh 10148 and
10147 to INFO with annotation; unregistered stays ERROR; stale-TTL stays
ERROR; unrelated messages untouched; registry prunes stale entries;
_setup_file_logging installs the wrapper filter + Ascii console formatter
(idempotent). Deploy = fleet restart (operator).

## Status
- [x] Blueprint (items 1+2 operator-approved 23:15 PT; item 3 held)
- [x] Items 1+2 implemented, tests green (8 new; full suite 2652/0)
- [x] Item 3 APPROVED ~00:15 PT 07-23 + operator caught display rounding:
      trailing log prints %.2f while NG ticks 0.001 — "2.94 -> 2.94" may
      be a REAL 2.937->2.941 move hidden by rounding. Addendum: full-
      precision (%g) prices in trailing lines + skip transmit only when
      the ACTUAL to-be-transmitted price equals the tracked one.
- [x] Addendum implemented (no-op skip + re-latch + %.10g precision; 2 new
      tests + 4 stub repairs; full suite 2654/0). FOLLOW-UP OPENED:
      _trailing_activated latch lost across reconnects (memory:
      trailing-latch-lost-on-reconnect) — the skip guard re-latches per
      bar but the root cause is untfixed.
- [x] Committed (deploy pending operator restart)
