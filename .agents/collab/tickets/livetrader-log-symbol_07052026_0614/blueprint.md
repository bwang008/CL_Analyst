# Ticket Resolution Blueprint — livetrader-log-symbol_07052026_0614
**Ticket Directory:** `.agents/collab/tickets/livetrader-log-symbol_07052026_0614/`

## Bug Summary
Logs and heartbeat messages lack execution context (the specific bot symbol or model), causing them to comingle in environments with multiple bots. This makes it difficult to trace errors or identify which bot sent a heartbeat. The fix involves automatically prefixing all standard logs and Telegram alerts with the execution symbol.

## Target Files
- `src/live_execution/utils/telegram_alert.py`
- `src/live_execution/live_trader.py`

## Required Changes
- In `src/live_execution/utils/telegram_alert.py`, update `TelegramAlerter` to accept an optional `prefix` parameter during initialization. Modify the `send` method so that it automatically prepends this prefix to any message it sends.
- In `src/live_execution/live_trader.py`, update the `LiveTrader` initialization to pass `prefix=self._execution_symbol` when instantiating `TelegramAlerter`. This ensures all telemetry, heartbeats, and warnings sent to Telegram carry the symbol prefix.
- In `src/live_execution/live_trader.py`, inject a custom `logging.Filter` into the module-level `LiveTrader` logger during `__init__`. This filter should dynamically prepend `[{self._execution_symbol}] ` to `record.msg` for all standard logs. This ensures both local file logs and `_TelegramLogCapture` forwarded messages have the correct symbol prefix.
