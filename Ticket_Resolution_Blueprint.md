# Ticket Resolution Blueprint

## Bug Summary
The trailing stop logic in the live execution environment fails to trigger on intrabar price movements. The root cause is a scheduling mismatch: the `_check_trailing_stop()` method is currently invoked inside `_on_new_bar()`. Because the strategy operates on a `1h` resolution, `_on_new_bar()` is only called once per hour when the 1H bar closes. 

Compounding the issue, `_check_trailing_stop()` explicitly reads from `self.rolling_df_5m.iloc[-1]`. When evaluated at the end of the hour, it only checks the extremes of the very last 5-minute bar (e.g., 18:55) and completely ignores the highest/lowest points reached during the first 55 minutes of the hour. As a result, price spikes that hit the trailing trigger threshold (such as the 68.80 high) are entirely missed by the tracking state.

## Target Files
- `c:\Users\bwang\Documents\GitHub\CL_Analyst_Development\src\live_execution\live_trader.py`

## Required Changes
1. **Remove Trailing Stop from Inference Cycle**: Locate `self._check_trailing_stop()` inside the `_on_new_bar()` method (around line 2952) and remove it. Trailing stops should not be bound to the strategy's inference timeframe.
2. **Inject into 5-Minute Callback**: Locate the `_on_bar_update_5m()` method (around line 2616). Add a call to `self._check_trailing_stop()` inside a `with self._ledger_lock:` block so that it executes unconditionally on every newly closed 5-minute bar, regardless of the overarching strategy resolution.
   - Example implementation in `_on_bar_update_5m`:
     ```python
     with self._ledger_lock:
         self._check_trailing_stop()
         if self._bar_size == "5m":
             self._on_new_bar(bar_time, self.rolling_df_5m, "5m")
     ```
3. **Verify State Updates**: This change will allow `_highest_high` and `_lowest_low` to be updated sequentially with every 5-minute bar's extremes, ensuring intra-hour price spikes correctly trigger the `modify_order` call to IBKR.
