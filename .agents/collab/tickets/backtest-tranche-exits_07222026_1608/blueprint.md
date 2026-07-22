# Ticket Resolution Blueprint — backtest-tranche-exits_07222026_1608
**Ticket Directory:** `.agents/collab/tickets/backtest-tranche-exits_07222026_1608/`
**Parent context:** operator-authorized prerequisite for Stage 3 of `oco-leg-race-audit_07212026_1935` ("I do want multi lot sizing eventually, but I also need a way to backtest this effectively before we move it to live" -> "Follow through with your recommended approach and proceed", 2026-07-22). This ticket gives the BACKTEST ENGINE true multi-tranche (scale-out) exit support and runs a decision-gate study. Live/sim Stage 3 stays shelved until the study shows value. **This document is the single semantics contract a future live Stage 3 must match.**

## Problem
`agent/backtest_engine.py` has no partial-exit machinery: `_close_trade` closes the full position at one price/reason; only `tiered_exits[0]` is ever consulted. The optimizer has therefore never evaluated real scale-out ladders, and a hypothetical multi-rung config would backtest as a single rung-1 TP while live would place a real ladder (silent economics divergence — currently unreachable only because of the Stage-1 live gate and 1-lot sizing).

## Scope
- IN: engine tranche exits (single-position mode), shared lot allocator, config validation, decision-gate study script + report.
- OUT (explicitly): live execution changes, simulated_execution changes, optimizer search-space changes, concurrent-mode tranche support, TradeRecord dataframe/CSV schema changes.

## Target Files
- `agent/backtest_engine.py` — tranche state + fill/booking logic.
- `src/live_execution/strategies/execution_models.py` — shared `allocate_tranche_lots()` + ladder validation helper.
- `tests/test_backtest_tranche_exits.py` — NEW.
- `scripts/study_tranche_exits.py` + `reports/tranche_exit_study_<date>.md` — decision-gate study (post-GREEN step, Manager-owned).

## Semantics contract (v1)

### S1 — Activation and allocation
- A side is MULTI-TRANCHE when its `tiered_exits` has >= 2 rungs AND the shared allocator yields >= 2 nonzero tranches for the position's lots. Otherwise the position runs the EXISTING single-exit path bit-for-bit.
- Shared allocator `allocate_tranche_lots(total_lots: int, qty_pcts: list[float]) -> list[int]` in `execution_models.py`, reproducing the LIVE allocator (`live_trader.py:3384-3389`) exactly: rung i (non-last) gets `min(max(1, int(round(total*pct))), remaining)`; the LAST rung gets the remainder; zero-lot rungs are skipped. Truth-table tests pin equivalence for lots 1..5 across representative pct sets. (Live Stage 3 later switches its inline allocator to this shared function — that swap is Stage-3 scope.)
- Rung TP prices: `entry_fill ± tp_atr_mult_i * atr` exactly as the single path prices its TP from the same inputs.

### S2 — Per-bar management (mirrors the existing precedence; single-position mode)
1. `bars_held += 1`; extremes update (unchanged).
2. Evaluate SL against the bar (unchanged test) and each UNFILLED rung's TP (same directional tests as the single TP).
3. **SL breached -> the entire REMAINDER closes at the SL gap-fill price** (`_gap_fill_price(..., is_tp=False)`), reason `SL` (or `TRAILING_BE` when a trailing rung has ratcheted — the existing mapping). Unfilled rungs are void. This is deliberately pessimistic (SL wins even when rungs also breached), matching the engine's and sim's same-bar convention.
4. Else fill EVERY breached rung this bar at its own gap-aware TP price (`_gap_fill_price(..., is_tp=True)` per rung), slippage applied at booking exactly like `_close_trade` does; `remaining_lots -= rung_lots`. Remainder reaching 0 concludes the trade with final reason `TP`.
5. **Any fill consumes the bar** (the single path returns after a fill): time barrier, flatten overlays, and trailing ratchet are skipped on a bar that filled a rung or the SL; they resume next bar.
6. Time barrier / flatten overlays (unchanged conditions) close the REMAINDER at bar open with their existing reasons.
7. Trailing ladder ratchet (unchanged math, entry-anchored) moves the SL protecting the remainder.

### S3 — Booking (ONE TradeRecord per position; totals exact, schema stable)
- `gross_pnl_dollars` = sum over tranches of `side * (exit_fill_i - entry_fill) * contract_multiplier * lots_i`.
- `commission_dollars` = `2 * commission_per_side * total_lots` (identical total to today).
- `exit_price` / `exit_fill` = lots-weighted averages (pre-slippage / post-slippage respectively); `exit_dt` and `duration_bars` from the FINAL closing event; `exit_reason` = the FINAL closing event's reason (so a ladder that ends on the stop drives the SL cooldown, a completed ladder books TP).
- New optional field `TradeRecord.tranche_exits: Optional[list] = None` — list of `{lots, exit_dt, exit_price, exit_fill, reason}` — populated ONLY for multi-tranche positions; EXCLUDED from `to_dataframe()`/`to_csv()` (downstream parsers keep their schema).
- `on_exit`/cooldown/engine_state fire once, at the final close, with the final reason (unchanged call shape).

### S4 — Validation (crash loud, no silent defaults)
When `len(tiered_exits) >= 2` for a side: `sum(qty_pct)` within 1e-6 of 1.0 else ValueError; `tp_atr_mult` strictly increasing else ValueError. Multi-rung + concurrent mode (`max_concurrent > 1` path) -> ValueError at config time ("tranche exits v1 are single-position mode only"). Single-rung/absent `tiered_exits` -> zero new validation (identity).

### S5 — Identity fences (the load-bearing regression constraint)
Configs with absent or single-rung `tiered_exits` MUST produce byte-identical TradeRecords to the current engine — golden-value tests on a synthetic OHLC fixture pin entry/exit fills, PnL, reasons, and record fields against values captured from the CURRENT engine before the change. The repo's byte-repro culture and the owed canary depend on this.

## Blast radius
Engine + execution_models only; live path, sim adapter, parity harness, cloud pipeline untouched. Standing rule: the engine is part of the optimizer pipeline, so a CANARY manifest run is owed before the next cloud scout/prod batch that includes this change (identity fences make the expected canary delta zero).

## Decision-gate study (after GREEN; Manager-owned)
`scripts/study_tranche_exits.py`: for each locally-runnable fleet config/dataset, compare at lots {2, 3}: baseline single tuned TP vs ladder variants (50/50 at [0.7x, 1.3x] tuned tp_atr_mult; 60/40 at [0.8x, 1.5x]; 40/30/30 at [0.6x, 1.0x, 1.6x]), identical everything else. Metrics: net PnL, PF, max DD, MAR, exit distribution, trade count. Output: `reports/tranche_exit_study_<date>.md` with a GO / NO-GO recommendation for live Stage 3. No cloud calls; local data only; missing data for a symbol is reported, never fabricated.

## Test cases (RED targets)
1. Allocator truth table incl. live-equivalence pins (1..5 lots x {[1.0], [0.5,0.5], [0.33,0.33,0.34], [0.6,0.4]}); zero-rung skip; last-rung remainder.
2. Two-rung long: rung1 fills on its bar at gap-aware price, SL then covers remainder; remainder later stopped -> ONE record, exit_reason SL, gross = sum of tranche PnLs, commission 2*per_side*total_lots, tranche_exits has 2 entries.
3. Ladder completion: both rungs fill (different bars) -> final reason TP; weighted exit_price/exit_fill correct.
4. Same-bar pessimism: SL + rungs breached same bar -> whole remainder at SL gap price, rungs void.
5. Gap-through-both-rungs bar: both rungs fill at max(open, tp_i) per rung.
6. Fill-consumes-bar: rung fill on the time-barrier bar -> barrier deferred to next bar (mirrors existing TP/SL-beats-barrier precedence).
7. Time barrier / flatten overlay close the remainder at bar open with existing reasons.
8. Trailing ratchet moves the remainder's SL; TRAILING_BE reason on a post-ratchet stop of the remainder.
9. Validation: qty_pct sum != 1 -> ValueError; non-increasing tp mults -> ValueError; multi-rung + concurrent -> ValueError; single-rung -> no validation, no tranche_exits.
10. Identity fences (S5): golden byte-identical records for single-rung and absent-tiered_exits configs; to_dataframe columns unchanged.
