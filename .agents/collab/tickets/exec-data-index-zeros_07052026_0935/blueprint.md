# Ticket Resolution Blueprint — exec-data-index-zeros_07052026_0935
**Ticket Directory:** `.agents/collab/tickets/exec-data-index-zeros_07052026_0935/`

## Bug Summary
`gcp/vm_e2e_pipeline.py` `run_backtest()` reads the execution parquet raw:
`exec_df = pd.read_parquet(exec_data_path)` — no index normalization. All non-CL
`<SYM>_raw.parquet` files (verified: `ZC_raw.parquet`) have an **int64 RangeIndex with
DateTime as a column**. The engine then does `ohlcv_exec_df.reindex(ohlcv.index)`
against a DatetimeIndex → all-NaN exec columns → **0 trades / $0 for every cloud
baseline metric on every non-CL symbol** (ES investigation f165b9d; reproduced on
ZC_01B batch_20260705_0458: all 24 metric rows zero while models/predictions healthy).
Downstream, `unified_pair_optimizer.py` parses these zeroed tables → its
pass-filter (`pnl_opt > 0 …`) fails for ALL models → every candidate gets the -1e6
robustness penalty → **top_pairs selection degenerates to arbitrary tie-breaking**
for non-CL batches.

This is the previously root-caused, fix-approved item (memory: es-baseline-zeros;
fix A+B approved 2026-07-04, never implemented).

## Target Files
- `gcp/vm_e2e_pipeline.py` (run_backtest exec load path)
- `tests/test_vm_pipeline_exec_load.py` (new)

## Required Changes
1. Normalize the exec frame after load (parquet or csv): if the index is not a
   DatetimeIndex, look for a `DateTime` column → `to_datetime` + `set_index`;
   otherwise attempt `pd.to_datetime(index)`; RAISE with a clear message if
   neither works (no silent fallback — house rule).
2. Emit a one-line log of the normalized exec range so cloud logs show the join is
   real.
3. Test with an int64-indexed fixture parquet (mirrors ZC_raw layout) proving
   normalization; test the raise path for garbage input; test the no-op path for an
   already-DatetimeIndex frame (CL parity).
