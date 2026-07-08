# Ticket Resolution Blueprint — add-hourset-15b_07072026_1724
**Ticket Directory:** `.agents/collab/tickets/add-hourset-15b_07072026_1724/`

## Bug Summary
The user requested a new dataset generation pipeline, `HourSet_15B`, which inherits the feature and target layout of `HourSet_14B` but introduces three new short-horizon target columns: `1x0.5 ATR 1H`, `2x1 2H`, and `2x1 1H`. The `HourSet_14B` currently has exactly 68 target columns and no short horizons under 3H. Adding these natively inside the `14B` logic would break downstream sweeps; hence, a discrete `15B` endpoint is needed.

## Target Files
- `src/data_processor.py`

## Required Changes
1. **Routing Addition**: Update `src/data_processor.py` dispatch logic in the `run()` method to map `HourSet_15B` to a new method called `process_hourset_15b`.
2. **Method Clone & Inject**: Create `process_hourset_15b` by duplicating the existing `process_hourset_13b` logic entirely. Inside the new method, directly beneath the existing target suite injections (Step 6), append the new short-horizon targets:
    - `TARGET_TRIPLE_1x0p5_1H` (1.0 TP, 0.5 SL, 1H Horizon, 14 ATR period)
    - `TARGET_TRIPLE_2x1_2H` (2.0 TP, 1.0 SL, 2H Horizon, 14 ATR period)
    - `TARGET_TRIPLE_2x1_1H` (2.0 TP, 1.0 SL, 1H Horizon, 14 ATR period)
3. **Documentation Update**: Append the `HourSet_15B` definition to the `DATASET_VERSIONS` dictionary at the top of the file, noting it contains the new short-horizon targets alongside the `HourSet_14B` core suite.
