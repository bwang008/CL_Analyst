# Audit — live-trailing-modify-order-dead_07042026_2012

Ticket-Auditor RCA. Repo HEAD `5db573a` (branch `development`, T1/T2/T3 merged). All line numbers at HEAD.
Bug: `LiveTrader._check_trailing_stop` transmits the trailing SL modification via
`if hasattr(self.exec_client, "modify_order"): self.exec_client.modify_order(evt.order_id, evt)`
(`src/live_execution/live_trader.py:1125`), but the production adapter `IBKRExecutionClient`
(`src/live_execution/adapters/ibkr_execution.py`) has **no `modify_order`** — the SL modification
is silently never sent to IBKR while logs, telemetry, and the ledger all record it as done.

---

## 1. Root cause

### 1.1 Timeline (git blame + bounded log, exact commits)

| Date | Commit | What happened |
|---|---|---|
| 2026-03-09 | `8fe9fd1` | Trailing stop born. Transmit was **direct and correct**: `self.manager.ib.placeOrder(c, o)` — re-place the same ib_insync Order with updated `auxPrice`, the documented ib_insync modify flow. Worked in production Mar 9 → Jun 13. |
| 2026-06-13 | `61bb864` | **The regression.** "HUGE UPDATE" adapter-modularization refactor replaced the direct `placeOrder` with `if hasattr(self.exec_client, "modify_order"): self.exec_client.modify_order(evt.order_id, evt)` — and the newly created `IBKRExecutionClient` was written **without** `modify_order`. Production transmit dead from this commit forward. Diff hunk (from `git show 61bb864`): `- self.manager.ib.placeOrder(c, o)` → `+ if hasattr(self.exec_client, "modify_order"): ...`. |
| 2026-06-16 | `c5111ca` | Reshaped the surrounding block to extract `raw_order` via `evt.raw_event.order` (lines 1120–1124). Guard preserved unchanged. |
| 2026-06-17 | `db32561` | livetest engine added `modify_order` to **SimulatedExecution only** (`simulated_execution.py:381`). From here on the parity/livetest harness exercises trailing end-to-end while production silently doesn't — the gap became invisible to every test. |
| 2026-07-04 | `957ced7` | 1h-binding fix (separate ticket) — `_check_trailing_stop` now also called from `_on_bar_update_1h`. Unrelated to transmit. |

### 1.2 The three-party contract mismatch

- **Interface** (`src/live_execution/interfaces/execution_interface.py`): `ExecutionClient` ABC declares
  `connect/disconnect/is_connected/register_order_status_callback/get_position/get_account_summary/place_bracket_order/place_child_orders/cancel_open_orders/close_position/register_error_callback` (all abstract) plus concrete-default `get_open_trades` / `resolve_contract`. **`modify_order` is not declared at all** — neither abstract nor default. The `hasattr` guard is the only "contract", and it silently resolves the mismatch in favor of doing nothing.
- **SimulatedExecution.modify_order(order_id, event=None) -> None** (`simulated_execution.py:381–405`): looks up `_resting_orders[int(order_id)]`; reads the new price from `event.raw_event.order.auxPrice` (the caller has **already mutated** the raw order — the new price travels by side effect, not as an argument); sets `resting.price`. Silent no-op (debug log) if the order isn't found or the event lacks a price.
- **IBKRExecutionClient**: no method. Only two `ExecutionClient` subclasses exist in the repo (grep-verified), so the production adapter is the only gap.

### 1.3 The lie surface — state mutated when the transmit is skipped

When trailing triggers on the IBKR adapter, `_check_trailing_stop` (live_trader.py:1106–1169) does ALL of the following with **zero broker interaction**:

1. **`raw_order.auxPrice = new_sl`** (`:1124`) — mutates the live ib_insync `Order` object inside the cached `Trade` (`evt.raw_event`). This poisons every later local read of the SL:
   - the periodic `[PNL]` status line reads this exact field (`:3014`) → ops log continuously shows the trailed SL as live;
   - any recovery/diagnostic that inspects the cached order sees the phantom price.
2. **`log.info("TRAILING STOP: modified SL order %s: %.2f → %.2f", ...)`** (`:1126–1129`) — false statement of a broker-side modification.
3. **`self._trailing_activated = True`** (`:1130`) — latch. `_check_trailing_stop` early-returns forever after (`:1046–1047`), so the modification is **never retried** for the trade's lifetime.
4. **`self._tracked_sl_price = new_sl`** (`:1131`) — the fallback cache for the `[PNL]` line (`:3023–3024`) also lies.
5. **`telemetry.update_position_sl(trade_id, new_sl, sl_order_id)`** (`:1135`) — the **ledger** now persists the phantom SL. On restart, recovery (`:1451–1478`) verifies TP/SL **by order-id only** (never by price), finds the original SL order resting at the OLD price, declares "verified", and re-seeds `_tracked_sl_price` from the ledger's phantom value — the lie survives restarts.
6. **`telemetry.log_decision_state(event_type="TRAILING_ACTIVATED", sl_price=new_sl, trailing_activated=True, ...)`** (`:1144–1159`) — analytics/parity snapshots poisoned.

**Broker reality:** the resting STP child keeps its ORIGINAL bracket `auxPrice`. Every trailing-triggered live trade since 2026-06-13 runs with the wide original SL; the tightening the backtest models (typically to entry ± offset·ATR, i.e., near-breakeven) never happens. Risk direction is strictly adverse: on a retrace, realized loss exceeds modeled loss by (original SL − trailed SL) × point value; trades the backtest scores as small trailing exits can realize as full stop-outs. The user's HS14B paper instance (running since 2026-07-02) is affected for its entire run.

---

## 2. Correct IBKR modification mechanics (ib_insync)

- **Modify = re-`placeOrder` the SAME `Order` object** (same `orderId`, unchanged `permId`) with updated fields, on the **same client session** (same `clientId`) that placed it. ib_insync's `IB.placeOrder(contract, order)` detects the existing `orderId` and issues an order modification to TWS/Gateway, returning the same tracked `Trade`. This is exactly what the pre-refactor code did (`self.manager.ib.placeOrder(c, o)`, commit `8fe9fd1`).
- **`evt.raw_event` carries everything needed.** Both `_open_orders` population paths store the live ib_insync `Trade`:
  - order-status callbacks: `ibkr_execution.py:_on_order_status` builds `StandardExecutionEvent(..., raw_event=trade)` (`:54`) → `live_trader.py:_on_standard_execution_event` stores it (`:3990`);
  - startup recovery: `get_open_trades` builds events from `ib.openTrades()` with `raw_event=trade` (`:185`) → stored at `:1457`.
  A `Trade` holds `.order` (the exact `Order` object ib_insync tracks — the very object `_check_trailing_stop` already mutates at `:1124`) and `.contract` (already qualified). So the adapter can transmit with `self.manager.ib.placeOrder(event.raw_event.contract, event.raw_event.order)` — **no contract qualification, no order-id mapping needed**. Same-client constraint holds: the exec adapter's session placed the bracket and would send the modify.
- **Event-loop safety** (this fires inside the 5m/1h bar-update callback):
  - `ib.placeOrder` is a plain **synchronous** method (message send + Trade bookkeeping; no `run_until_complete`). Prior art proving callback-safety: `place_bracket_order` / `place_child_orders` / `close_cl_position` all call `ib.placeOrder` and are invoked from `_on_new_bar` inside bar callbacks in production today (`ibkr_client.py:1187, 1263, 1318, 1333, 600`). The pre-refactor trailing code also called it from this same callback context for 3 months.
  - The dangerous family is `reqHistoricalData` / `qualifyContracts` / `reqContractDetails` (sync wrappers call `loop.run_until_complete` → "This event loop is already running"). The codebase's prior art for avoiding those from callbacks: `resolve_contract()` startup caching (`ibkr_execution.py:65–100`, raises `RuntimeError` if not pre-resolved) and `_deferred_resubscribe` (`live_trader.py:2357–2372`, `loop.call_soon` + `ensure_future` + async API). `modify_order` needs **none** of that because the qualified contract rides in on the `Trade`.

---

## 3. Scope statement — 1h-vs-5m binding (Pitfall #3)

**Out of scope for this ticket; already fixed on HEAD anyway.** Commit `957ced7` (2026-07-04) added the 1h-side call. Current state:
- `_on_bar_update_5m` calls `_check_trailing_stop()` under `_ledger_lock` **unconditionally** on every closed 5m bar (`live_trader.py:2710–2711`) — not gated on `_bar_size`.
- `_on_bar_update_1h` also calls it (`:2762–2763`, "bar-size agnostic" comment).
- Live runs always subscribe Stream A (5m brain bars, `:2121–2128`) and additionally Stream B (1h) when `_bar_size in ("1h","2h","4h")` (`:2131–2139`). **Therefore trailing IS reachable for live 1h models at 5m granularity — confirmed.** Regression tests exist: `tests/test_trailing_stop_1h.py`, `tests/test_trailing_stop_5m_scheduling.py`.
- This ticket fixes only the TRANSMIT path inside `_check_trailing_stop` plus the IBKR adapter. (Noted in passing, not ours: `_check_trailing_stop` reads `rolling_df_5m.iloc[-1]` (`:1054`) even when invoked from the 1h callback — a harness/granularity concern for the 5m-harness ticket.)

---

## 4. Severity + regression determination

- **Severity: HIGH.** Multi-line, cross-file change (adapter method + interface declaration + call-site failure semantics) on a live-money risk-control path; a currently-running instance is affected; the defect makes realized risk exceed modeled risk on every trailing-triggered trade.
- **Regression: YES — and recent, not longstanding.** The T3 audit (§Q3) believed this longstanding; blame proves otherwise. Trailing transmit **worked in production from 2026-03-09 (`8fe9fd1`) until 2026-06-13**, when the `61bb864` modularization refactor swapped the working direct `ib.placeOrder` for a guarded call to a method the new production adapter never got. It was then **masked from 2026-06-17** (`db32561`) when the sim adapter alone gained `modify_order`, making every parity/livetest run green while production stayed dead. Classic refactor regression + test-double divergence. Untouched by T1/T2/T3 (T2 `f02ec5e` touched the adapter but only `resolve_contract` exchange lookup).

---

## 5. Proposed fix (localized — no refactor)

### 5.1 `execution_interface.py` — declare the contract
Add to `ExecutionClient`:
```python
@abstractmethod
def modify_order(self, order_id, event=None) -> Any:
    """Transmit a modification of a resting order to the venue.

    The caller has already written the new price into
    event.raw_event.order (e.g. auxPrice for a STP order).
    Implementations MUST raise on failure — never silently no-op.
    """
```
Abstract (not a raising default): only two subclasses exist in-repo (IBKR + Simulated) and both will implement it; any future adapter missing it fails at **instantiation**, the loudest possible point (no-silent-defaults rule). Test stubs built with `object.__new__` (existing seam convention, e.g. `test_tick_order_pricing.py`) bypass ABC and are unaffected.

### 5.2 `ibkr_execution.py` — implement `modify_order`
```python
def modify_order(self, order_id, event=None) -> Any:
    if event is None or getattr(event, "raw_event", None) is None:
        raise ValueError(f"modify_order({order_id}): event.raw_event (ib_insync Trade) is required")
    trade = event.raw_event
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    if order is None or contract is None:
        raise ValueError(f"modify_order({order_id}): raw_event lacks .order/.contract")
    if str(order.orderId) != str(order_id):
        raise ValueError(f"modify_order: order_id mismatch {order_id} != {order.orderId}")
    if not self.is_connected():
        raise RuntimeError(f"modify_order({order_id}): IBKR not connected")
    log.info("MODIFY ORDER: re-placing orderId=%s auxPrice=%s", order.orderId, getattr(order, "auxPrice", None))
    return self.manager.ib.placeOrder(contract, order)  # sync; callback-safe (see §2)
```
Semantics: re-place the SAME Order object (already mutated by the caller) on the same client session — the ib_insync modify flow, identical to the pre-`61bb864` behavior. No qualification calls → no event-loop hazard. Raises loudly on every precondition failure.

### 5.3 `live_trader.py:_check_trailing_stop` — remove guard, transmit-then-commit
Replace line 1125 and reorder the block so **no state is committed until the transmit succeeds**:
```python
raw_order = getattr(getattr(evt, "raw_event", None), "order", None)
old_sl = getattr(raw_order, "auxPrice", 0.0) or 0.0 if raw_order else 0.0
if raw_order is not None:
    raw_order.auxPrice = new_sl
try:
    self.exec_client.modify_order(evt.order_id, evt)   # direct — missing method fails loudly
except Exception:
    if raw_order is not None:
        raw_order.auxPrice = old_sl                    # un-poison the cached Trade
    log.exception(
        "TRAILING STOP: SL modify transmit FAILED for order %s "
        "(SL remains %.2f at broker) — will retry on next bar", order_id, old_sl)
    return
# --- success only below this line ---
log.info("TRAILING STOP: modified SL order %s: %.2f → %.2f", order_id, old_sl, new_sl)
self._trailing_activated = True
self._tracked_sl_price = new_sl
... update_position_sl ... TRAILING_ACTIVATED snapshot ...   (unchanged)
```
**Failure semantics:** on transmit failure — restore `raw_order.auxPrice`, do NOT set `_trailing_activated` / `_tracked_sl_price`, do NOT write `update_position_sl` or the `TRAILING_ACTIVATED` snapshot. Because `_trailing_activated` stays `False` and `_highest_high`/`_lowest_low` persist, the trigger condition re-fires on the **next 5m bar** → natural retry with zero extra machinery. The targeted `except` must catch the transmit before the block's generic outer `try/except` (`:1168`) does, because the generic catch would leave the mutated `auxPrice` poisoned. The sim adapter keeps its price-read-from-mutated-order semantics — the caller-mutates-then-transmits convention is preserved for both adapters.

Files changed: `execution_interface.py` (+~10), `ibkr_execution.py` (+~20), `live_trader.py` (~15 lines restructured inside one method). No signature or behavior change anywhere else.

### 5.4 TDD test list
Adapter (mocked `IBKRConnectionManager`/`ib`):
- **A1** `modify_order` calls `ib.placeOrder` exactly once with `(event.raw_event.contract, event.raw_event.order)` and returns the Trade.
- **A2** raises on `event=None` / missing `raw_event` / missing `.order`/`.contract` — no silent no-op.
- **A3** raises on `order_id` mismatch with `raw_event.order.orderId`.
- **A4** raises when `is_connected()` is False.
- **A5** event-loop pin: asserts NO call to `qualifyContracts`/`reqContractDetails`/`reqHistoricalData` during `modify_order`.

Interface/parity:
- **I1** `ExecutionClient` cannot be subclass-instantiated without `modify_order` (TypeError at construction).
- **I2** signature parity: `inspect.signature` of `SimulatedExecution.modify_order` and `IBKRExecutionClient.modify_order` compatible (`(order_id, event=None)`).

Call-site (`object.__new__` seam trader, per `test_tick_order_pricing.py` convention):
- **C1** guard removal: exec client stub WITHOUT `modify_order` → `AttributeError` surfaces at call time (caught by the new except → state NOT committed, error logged); with the method → called exactly once with `(evt.order_id, evt)`.
- **C2** success path commits everything: `_trailing_activated=True`, `_tracked_sl_price==new_sl`, `update_position_sl` called with `new_sl`, `TRAILING_ACTIVATED` snapshot logged, `raw_order.auxPrice==new_sl`.
- **C3** no-lie-on-failure: `modify_order` raises → `_trailing_activated` stays False, `_tracked_sl_price` unchanged, `update_position_sl` NOT called, no `TRAILING_ACTIVATED` snapshot, **`raw_order.auxPrice` restored to old value**, error logged.
- **C4** retry-on-next-bar: first call fails, second call re-triggers and commits on success.
- **C5** sim-adapter integration: real `SimulatedExecution` — trigger updates `_resting_orders[sl_oid].price` to `new_sl` (livetest behavior preserved).

Regression guards: existing S6 pins in `tests/test_tick_order_pricing.py` (computed SL price, which deliberately never asserted on `modify_order` — see its header note re Q3) and `tests/test_trailing_stop_1h.py` / `test_trailing_stop_5m_scheduling.py` must stay green.

---

## 6. Open questions for the manager (human authorization)

- **Q1 (operational, urgent):** The HS14B paper instance has run with dead trailing since 2026-07-02 (bug live in the codebase since 06-13). (a) Restart it onto the fixed build ASAP — user's process, needs their go-ahead. (b) Its ledger/decision-state rows contain phantom `TRAILING_ACTIVATED`/`update_position_sl` entries — decide whether to back-annotate or just document the cutover date for any PnL analysis (relates to the standing "ensembles need re-scoring" item).
- **Q2 (interface change ACK):** making `modify_order` `@abstractmethod` is technically breaking for out-of-tree `ExecutionClient` subclasses (none exist in-repo). Recommend YES per no-silent-defaults; needs explicit ACK since it's an interface contract change.
- **Q3 (escalation policy, recommend defer):** on repeated transmit failure, should the trader escalate (Telegram alert / kill-switch after N consecutive failed modifies)? This ticket ships log+retry-on-next-bar; alerting policy is a product decision — suggest a follow-up ticket if wanted.

## 7. Verification evidence

- `hasattr` guard + missing adapter method confirmed at HEAD by grep + full read of `ibkr_execution.py` (no `modify_order` anywhere in the class).
- Blame (bounded): `git blame -L 1120,1130 live_trader.py`, `-L 378,406 simulated_execution.py`; `git log -n 5` on both files + adapter; `git show 61bb864` hunk shows `- self.manager.ib.placeOrder(c, o)` → `+ if hasattr(...)`.
- `raw_event`=Trade confirmed at both population sites (`ibkr_execution.py:54, :185`); callback-safety of sync `placeOrder` confirmed by five existing production callsites invoked from bar callbacks.
- Subclass census: exactly 2 `ExecutionClient` subclasses repo-wide.
