# Ticket Resolution Blueprint — live-trader-inference-ratio_07032026_2132
**Ticket Directory:** `.agents/collab/tickets/live-trader-inference-ratio_07032026_2132/`

## Bug Summary
The `LiveTrader` correctly implements a two-stream split-brain architecture where feature generation requires ratio-adjusted continuous series data. While 1H models correctly JIT calculate ratio adjustment via `self.data_manager_1h.get_ratio_adjusted_df()`, non-1H (5M) models fall back to `rolling_df` directly, which references `self.rolling_df_5m` (unadjusted raw prices). This causes 5M models to compute inference features on raw unadjusted data rather than ratio-adjusted continuous contract data. 
The data cache natively stores raw unadjusted bars and monitors rollover metadata (`.roll_metadata.json`), so ratio adjustment must be executed just-in-time in memory via `DataManager.get_ratio_adjusted_df()`.

## Target Files
- `src/live_execution/live_trader.py`

## Required Changes
Modify `_on_new_bar()` fallback logic to explicitly request the ratio-adjusted dataframe from the 5M data manager rather than falling back to `rolling_df` directly.
Specifically, change the fallback from `ratio_adjusted_df = rolling_df` to `ratio_adjusted_df = self.data_manager_5m.get_ratio_adjusted_df()`.
