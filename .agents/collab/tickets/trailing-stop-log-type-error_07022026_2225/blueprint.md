# Ticket Resolution Blueprint — trailing-stop-log-type-error_07022026_2225

## Bug Summary
The `_check_trailing_stop()` method in `live_trader.py` triggers a `TypeError: %d format: a real number is required, not str` when logging the SL order modification. The root cause is that `evt.order_id` from IBKR events is a **string** (e.g., `'21'`), but the log format string uses `%d` which requires a numeric type. Python's logging module catches this internally (non-fatal), but it **suppresses the entire log line**, silently swallowing the SL modification confirmation.

The bug predates the recent scheduling fix (commit `922dea5`) — it was introduced in commit `8fe9fd14` (Mar 9) but was latent because the trailing stop rarely triggered when gated behind the hourly inference cycle.

Three sites in the same file use `%d` with IBKR order IDs that can be strings.

## Target Files
- `src/live_execution/live_trader.py`

## Required Changes
1. **Line 949** (`_check_entry_ttl` method): Change the format specifier from `%d` to `%s` in the log message `"ENTRY TTL: cancelling unfilled entry order %d ..."`. The variable `self._pending_entry_order_id` can be a string.

2. **Line 1096** (`_check_trailing_stop` method): Change the format specifier from `%d` to `%s` in the log message `"TRAILING STOP: modified SL order %d: %.2f → %.2f"`. The variable `order_id` (from `evt.order_id`) is a string.

3. **Line 1133** (`_check_trailing_stop` method): Change the format specifier from `%d` to `%s` in the log message `"TRAILING STOP: triggered but SL order %d not found ..."`. The variable `self._sl_order_id` can be a string.

**Rationale for `%s` over `int()` coercion:** `%s` tolerates both `str` and `int` order IDs without introducing a new failure mode. The rendered output is visually identical (`'21'` → `21` with either format).
