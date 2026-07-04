# Ticket Resolution Blueprint — livetest-trailing-stop_07042026_0205
**Ticket Directory:** `.agents/collab/tickets/livetest-trailing-stop_07042026_0205/`

## Bug Summary

The `_check_trailing_stop()` method in `live_trader.py` is **never executed** during livetest 1h-mode simulations (and would also be missed by 2h/4h strategies without an active 5m stream).

**Root cause:** Commit `922dea5` moved `_check_trailing_stop()` from `_on_new_bar()` (shared by all bar sizes) to `_on_bar_update_5m()` (5m-only path). In production, both 5m and 1h streams run concurrently, so the 5m path covers it. In livetest 1h-mode, there is no active 5m data stream — only `_on_bar_update_1h()` fires, which never calls `_check_trailing_stop()`.

**Impact:** All 1h-mode livetest simulations run without trailing stops, causing SL exits to fire at the original (wider) SL price rather than the tightened trailing-stop breakeven level. This inflates losses and deflates PnL in livetest reports vs backtest reports.

## Target Files
- `src/live_execution/live_trader.py`

## Required Changes

### `src/live_execution/live_trader.py` — `_on_bar_update_1h()`

Add a `_check_trailing_stop()` call at line ~2674, **after** `self.data_manager_1h.append_bar(new_row)` (line 2673) and **before** the `if self._bar_size == "1h":` dispatch (line 2675).

**This placement is bar-size agnostic** — it fires on every 1h bar regardless of whether `_bar_size` is `1h`, `2h`, or `4h`, and will automatically cover any future bar sizes that route through this callback.

```python
        # Check trailing stop on every 1h bar — bar-size agnostic.
        # In production, 5m bars already check via _on_bar_update_5m().
        # This ensures 1h-only paths (livetest, future bar sizes) also check.
        with self._ledger_lock:
            self._check_trailing_stop()
```

**Insertion point:** After line 2673 (`self.data_manager_1h.append_bar(new_row)`), before line 2675 (`if self._bar_size == "1h":`).

### Production Safety (verified by Impact Reviewer #2)

1. **No race conditions** — ib_insync dispatches all `updateEvent` callbacks on the same asyncio event loop thread (cooperative single-thread model). No two callbacks ever run simultaneously.
2. **`_ledger_lock`** guards against the heartbeat daemon thread, not concurrent bar callbacks. The fix correctly wraps the call matching the existing pattern at L2626-2627.
3. **Double-activation guard** — `if self._trailing_activated: return` (L1022) ensures once triggered from either the 5m or 1h path, all subsequent calls are no-ops. Flag only resets on position close.
4. **`rolling_df_5m` data source** — in production, the 1h callback reads `rolling_df_5m` last updated by the 5m stream (at most ~5 min stale). The monotonic `max()`/`min()` tracking of `_highest_high`/`_lowest_low` means stale reads are harmless. In livetest, `livetest_engine.py` already mirrors 1h bars into `rolling_df_5m` (lines 393-406).
5. **2h/4h strategies benefit** — more frequent trailing stop checks (every 1h bar vs only on boundary bars) is desirable, matching production behavior where 5m checks fire every 5 minutes.
6. **No interface, base class, or config changes.**

## Verification Plan

### Automated Tests
- Run trailing stop unit tests: `python -m pytest tests/test_simulated_execution.py -v`
- Run full test suite: `python -m pytest tests/ -v`

### Manual Verification
- Run a livetest in 1h-mode with trailing stops enabled and verify `TRAILING STOP: activated` log messages appear
- Compare livetest trades against backtest trades in parity mode to confirm trailing stop exits now match
