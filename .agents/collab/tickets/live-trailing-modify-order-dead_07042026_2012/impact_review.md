# Impact Review — live-trailing-modify-order-dead_07042026_2012

Ticket-Impact-Reviewer verdict on the Ticket-Auditor's proposal (audit.md). Repo HEAD `5db573a`,
branch `development`. All claims below independently re-verified against source and git history —
not taken from the audit.

## VERDICT: APPROVE — with 3 binding conditions (C-1..C-3) and 2 precision notes

Constraint analysis first, then verification results, then the conditions.

---

## 1. Constraint rules

- **Interface Rule — TRIGGERED.** New `@abstractmethod modify_order` on `ExecutionClient`
  (`src/live_execution/interfaces/execution_interface.py`). Business justification is strong and
  specific: the absent interface declaration IS the root cause (the `hasattr` guard silently
  resolved the contract gap in favor of doing nothing on the production adapter for 3 weeks on a
  live-money risk path). A localized alternative (raising default method) was considered and
  rejected for a defensible reason: abstract fails at instantiation — the loudest point — per the
  user's standing no-silent-defaults rule. Manager has ACKed Q2. **Exception granted.**
- **Base Class Rule — TRIGGERED** (same change, same justification). Blast radius independently
  measured: exactly 2 concrete subclasses repo-wide (`IBKRExecutionClient`,
  `SimulatedExecution`); zero `MagicMock(spec=...)`/`create_autospec` usages; zero duck-typed
  ExecutionClient subclasses in tests/scripts (full census in §2.3). **Exception granted.**
- **Refactor Veto — NOT TRIGGERED.** Three files but one component-unit: interface declaration
  (+~10), one new adapter method (+~20), one method-internal reorder at the single call site
  (~15 lines inside `_check_trailing_stop`). No component is rewritten; no signature used by other
  modules changes; no other call sites exist (`modify_order` grep: 1 call site repo-wide).
  Human authorization is therefore NOT required for the code change. (Q1 — restarting the live
  HS14B instance and back-annotating phantom ledger rows — remains a USER decision per the
  manager ruling; it is operational, not part of this code approval.)

---

## 2. Independent verification results

### 2.1 Regression timeline — CONFIRMED exactly as audited
- `8fe9fd1` (2026-03-09): trailing transmit was direct and correct —
  `o.auxPrice = new_sl; self.manager.ib.placeOrder(c, o)` (verified in the commit diff).
- `61bb864` (2026-06-13): diff hunk shows `- self.manager.ib.placeOrder(c, o)` →
  `+ if hasattr(self.exec_client, "modify_order"): self.exec_client.modify_order(evt.order_id, evt)`,
  and `git show 61bb864:src/live_execution/adapters/ibkr_execution.py` lists 12 methods —
  **no `modify_order`**. Production transmit dead from this commit.
- `db32561` (2026-06-17): +26 lines to `src/live_execution/adapters/simulated_execution.py` only —
  `modify_order` added to the sim adapter alone, masking the gap in every livetest/parity run.
- HEAD: `modify_order` exists in exactly two places in `src/`: the sim adapter (`:381`) and the
  guarded call site (`live_trader.py:1125`). `ibkr_execution.py` at HEAD (188 lines, fully read):
  no `modify_order`. Timeline claim **TRUE in full**.

### 2.2 ib_insync modify mechanics — CONFIRMED
- Both `raw_event` population paths carry the live ib_insync `Trade`:
  `_on_order_status` builds `StandardExecutionEvent(..., raw_event=trade)` (`ibkr_execution.py:54`)
  and `get_open_trades` does the same (`:185`) — the recovery path even pre-filters trades lacking
  `.order`/`.contract` (`:172-175`), so every event from that path satisfies the new method's
  preconditions by construction. Events are stored into `_open_orders` at `live_trader.py:3990`
  (callback path) and `:1457` (recovery path). **CONFIRMED.**
- Same-session constraint holds: the adapter's `self.manager.ib` both places brackets and receives
  the `orderStatusEvent` (`ibkr_execution.py:32`), so the Trade being re-placed belongs to the
  session that would transmit the modify.
- In-callback sync `placeOrder` prior art: **7** sync `self.ib.placeOrder(...)` sites in
  `ibkr_client.py` (`:514, :600, :1187, :1263, :1318, :1324, :1333`) — the audit said 5; the true
  count is higher, which strengthens the claim. `close_cl_position` / `place_child_orders` run from
  bar/fill callbacks in production today, and the pre-refactor trailing code itself called
  `ib.placeOrder` from this exact 5m callback for 3 months. No `qualifyContracts` /
  `reqContractDetails` / `reqHistoricalData` in the proposed method → no
  `run_until_complete` re-entry hazard. **CONFIRMED** (A5 pin in the TDD list locks this in).

### 2.3 Abstractmethod blast radius — CONFIRMED BOUNDED
- Subclass census (grep, `!.agents/**`): exactly 2 — `IBKRExecutionClient`, `SimulatedExecution`.
  Sim already implements `modify_order`; IBKR gains it in this fix → both remain instantiable.
- Direct instantiations: `tests/test_ibkr_adapters.py:39,:50`, `tests/test_build_future_contract.py:407`
  (IBKR, mocked manager — fine post-fix); `tests/test_simulated_execution.py:33,:39` and
  `scripts/livetest_engine.py:627` (sim — has the method). `ExecutionFactory.create` returns IBKR
  only. **No instantiation breaks.**
- Test doubles: every `exec_client` in the entire test suite is a plain `MagicMock()` (28 sites
  enumerated; zero `spec=` usages). `MagicMock` auto-provides `modify_order` and never raises →
  the direct (unguarded) call succeeds and the commit path runs, so every existing green test that
  reaches the transmit region stays green:
  - `tests/test_tick_order_pricing.py` S6 seam (`:807`, asserts `_trailing_activated is True` at
    `:902` and post-mutation `auxPrice` at `:903/:935`) — still green under transmit-then-commit.
  - `tests/test_trailing_stop_log_format.py` (STRICT-LOCK, drives the REAL method to the log
    sites with `MagicMock` exec_client at `:71`) — still green, see condition C-3.
  - `tests/test_trailing_stop_1h.py` / `test_trailing_stop_5m_scheduling.py` /
    `test_exit_bar_semantics.py` / `test_live_trader_bugs.py` — all mock `_check_trailing_stop`
    itself; immune.
  **The audit's claim "existing suites stay green" is verified, not assumed.**

### 2.4 No-lie-on-failure semantics — CONFIRMED, and the reorder is necessary
Current commit-before-transmit surface verified at HEAD (`live_trader.py`):
`raw_order.auxPrice = new_sl` (`:1124`) → guarded no-op transmit (`:1125`) → false success log
(`:1126-1129`) → `_trailing_activated = True` latch, never retried (`:1130`, early-return `:1046`)
→ `_tracked_sl_price = new_sl` (`:1131`) → ledger `update_position_sl` (`:1135`) →
`TRAILING_ACTIVATED` snapshot (`:1144`). Downstream readers of the poisoned state verified:
`[PNL]` line reads `raw_order.auxPrice` (`:3014`) with `_tracked_sl_price` fallback (`:3023`);
restart recovery verifies TP/SL **by order-id only** (`:1458-1461`) and re-seeds
`_tracked_sl_price` from the ledger's phantom value (`:1472`) — the lie survives restarts.
**All six lie-surface claims in audit §1.3 are TRUE.**
The proposed ordering (mutate auxPrice → transmit → on failure restore auxPrice + return with
NOTHING committed) leaves `_trailing_activated=False` and persistent `_highest_high`/`_lowest_low`,
and `_check_trailing_stop` runs unconditionally under `_ledger_lock` on every closed 5m bar
(`:2710-2711`) and 1h bar (`:2762-2763`) → the trigger re-fires next bar. Natural retry
**CONFIRMED**. The targeted inner `except` is required: the existing outer generic handler
(`:1107`/`:1168-1169`) would otherwise swallow a transmit failure AFTER the `:1124` mutation,
leaving the cached Trade poisoned — the audit correctly identified this.

### 2.5 Parity surface (sim vs proposed IBKR) — one real drift, see C-1/C-2
Shared semantics preserved: both read the new price from the caller-mutated
`event.raw_event.order.auxPrice` (side-channel convention), same signature `(order_id, event=None)`.
Drift found (sim `simulated_execution.py:381-405`):
- sim silently no-ops (log.debug) when the order id is not in `_resting_orders`;
- sim silently no-ops when `event`/`raw_event`/`.order`/`auxPrice` is missing;
- proposed IBKR raises on malformed event / id mismatch / disconnect.
The proposed interface docstring says "Implementations MUST raise on failure — never silently
no-op" — **the unchanged sim adapter would violate the very contract being introduced.**
Nuance: the not-found case is defensible as a no-op, because live IBKR also does NOT fail
synchronously when re-placing an already-filled/cancelled order (venue rejection arrives via the
async errorEvent) — a raising sim would be stricter than live. But the malformed-event cases are
sync-detectable caller-contract violations in BOTH adapters and must behave identically, or the
parity gate loses meaning for trailing once a 5m harness exists. Resolved via conditions C-1/C-2.

### 2.6 Trailing-DISABLED parity gate path — NO BEHAVIOR CHANGE, with one precision correction
`scripts/ledger_parity_check.py:61-85` disables trailing by walking the config and setting every
`trailing_atr_mult` (top-level, per-side, tiers) to sentinel `10000.0`. Precision correction to
the framing in my brief: `_check_trailing_stop` **IS still reached** on every bar under
`--disable-trailing` — it runs to the trigger check and exits at `if not triggered: return`
(`:1081-1082`) because the trigger needs a `10000×ATR` move. Everything this fix touches sits
strictly BELOW that line (the `for evt ...` transmit/commit block). Therefore the gate path is
byte-identical before/after the fix. **CONFIRMED — no gate impact.**

---

## 3. Binding conditions (blueprint/TDD must incorporate)

- **C-1 (contract consistency — sim adapter).** Align `SimulatedExecution.modify_order` with the
  new interface contract for the sync-detectable failure class: raise (ValueError) on
  `event is None` / missing `raw_event` / missing `.order` / missing-or-None `auxPrice` — exactly
  the malformed-event surface the IBKR implementation raises on. Keep the order-not-found branch a
  no-op (mirrors live venue-async-rejection reality) but upgrade its `log.debug` to `log.warning`.
  This is +~6 lines in the sim adapter, inside the already-in-scope method family; it does not
  expand the Refactor Veto surface. Verified impact: livetest normal path always passes a
  well-formed event (harness populates `_open_orders` from sim `get_open_trades`/callbacks whose
  `raw_event.order.auxPrice` always exists — `simulated_execution.py:198-208`), so behavior only
  changes where the sim was already lying.
- **C-2 (docstring accuracy).** Reword the interface docstring's failure clause to match C-1
  reality: "Implementations MUST raise when the modification cannot be transmitted (malformed
  event, order-id mismatch, or disconnected venue). Venue-side rejection of a transmitted modify
  is reported asynchronously via the error callback, not by this method." The current draft
  ("never silently no-op") over-promises what EITHER adapter can deliver synchronously.
- **C-3 (STRICT-LOCK log pin).** `tests/test_trailing_stop_log_format.py` is Strict-Lock and
  asserts EXACTLY ONE log record containing `"modified SL order"` after a successful pass through
  the real method (`:110-123`). The implementation must (a) keep that exact substring in the
  success log, (b) emit it only AFTER a successful transmit, and (c) ensure the failure-path
  message does NOT contain the substring `"modified SL order"` (the audit's proposed
  "SL modify transmit FAILED ..." text complies). Add this constraint explicitly to the blueprint
  so the implementer doesn't reword the success log.

## 4. Non-blocking notes

- **N-1.** Audit cites 5 in-callback `placeOrder` sites; true count in `ibkr_client.py` is 7
  (adds `:514`, `:1324`). Strengthens the callback-safety argument; no action.
- **N-2.** In the proposed call-site snippet, if `raw_order is None` the transmit is still
  attempted and IBKR raises ValueError (missing `.order`) → caught → loud log + retry every bar.
  That is acceptable (loud, no lie) but will log an exception every 5m bar until position close.
  Q3 (escalation on repeated failure) is already spun off — this note belongs to that ticket.
- **N-3.** Q1 (restart live HS14B instance + phantom-row back-annotation for every trailing-
  triggered trade since 2026-06-13/07-02) remains open with the USER. This approval covers the
  code fix only; the fix is inert for the running instance until restart.

## 5. Summary

Approve. The fix is the minimal correct unit for a contract-mismatch regression: declare the
contract, implement the missing production method with the historically-proven transmit
(`8fe9fd1` semantics), and make the call site transmit-then-commit so state can never lie about
the broker again. Interface/Base-Class rules are triggered but carry a strong, manager-ACKed
justification rooted in the no-silent-defaults rule; the Refactor Veto is not triggered. Blast
radius independently measured at 2 subclasses, 1 call site, 0 breaking tests. Conditions C-1..C-3
close the only genuine gap found (sim/IBKR failure-semantics drift vs the new docstring) and pin
the Strict-Lock log constraint.

— Ticket-Impact-Reviewer, 2026-07-04
