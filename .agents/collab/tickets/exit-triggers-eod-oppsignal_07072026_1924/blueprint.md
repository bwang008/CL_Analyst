# Ticket Resolution Blueprint — exit-triggers-eod-oppsignal_07072026_1924
**Ticket Directory:** `.agents/collab/tickets/exit-triggers-eod-oppsignal_07072026_1924/`

## Summary (feature ticket, 2 phases — backtester only, default-off)
Two independent, config-gated exit enhancements to the backtest engine. Both must be
byte-identical no-ops for every existing config (flag absent = off). Live trader is
explicitly OUT OF SCOPE (deferred, human-gated) — the fleet runs `stable-fleet`;
this work lands on `training-update`.

Builds on the `weekend_flatten` overlay already implemented and A/B'd on this branch
(uncommitted): `WeekendFlattenConfig` in `src/live_execution/strategy_config.py`,
`WEEKEND_FLATTEN` ExitReason + data-driven flatten-bar precompute in
`agent/backtest_engine.py`, tests in `tests/test_weekend_flatten.py`, harness
`agent/ab_weekend_flatten.py`.

## Phase 1 — EOD flatten trigger (`eod_flatten`)

### Investigation facts (empirical, 2026-07-07)
All five fleet datasets (CL/ES/GC/NG/SI HourSet parquets) show:
- dominant 1h bar spacing; a **2h gap ~4x/week** (daily 17:00–18:00 ET maintenance
  halt) — pre-gap bar hours cluster 17–21 with rare seams;
- a **~50h gap 1x/week** (weekend); pre-gap weekday 3/4 (Thu before Friday holidays
  → holiday-weekends are caught with no calendar);
- rare 4–6h intraday gaps (outages/early closes).

Therefore EOD detection is data-driven like the weekend trigger: a bar is an
**EOD-flatten bar** iff gap-to-next-bar ∈ `[eod.min_gap_hours, weekend_threshold)`,
where `weekend_threshold` = the weekend block's `min_gap_hours` if configured, else
the module default (40.0). The two bar-sets are **disjoint by construction**, so
WEEKEND/EOD attribution never overlaps and an EOD-only arm does NOT flatten Fridays
(that is the weekend trigger's job — required for clean A/B attribution).

### Target files
- `src/live_execution/strategy_config.py`
- `agent/backtest_engine.py`
- `tests/test_weekend_flatten.py` (extend; shared overlay family)
- `agent/ab_weekend_flatten.py` (extend harness to trigger-combination arms)

### Required changes
1. `strategy_config.py`: parse optional `eod_flatten` block
   `{enabled, profit_atr_mult, min_gap_hours=2.0}` mirroring `parse_weekend_flatten`
   exactly: absent → None (off); `enabled:true` without explicit `profit_atr_mult`
   → raise (house rule: no silent null defaults). Add `eod_flatten` field to
   `StrategyConfig`.
2. `backtest_engine.py`: add `ExitReason.EOD_FLATTEN`; accept `eod_flatten` in
   `__init__` + `from_config`; in `run()` precompute `_eod_flatten_bars` using the
   gap band above (weekend precompute unchanged); generalize the trigger helper to
   return which trigger fired (weekend checked first; sets disjoint anyway); fire in
   BOTH `_on_in_position` and `_check_position` AFTER TP/SL and TIME_BARRIER
   (existing precedence untouched); fill = bar open (TIME_BARRIER convention).
   Profit gate identical to weekend: `side*(bar_open - entry_fill)/atr_at_entry >=
   profit_atr_mult`.
3. Report: add `EOD_FLATTEN` to both exit-distribution renderers.
4. Tests (TDD): parse validation; EOD fires on 2h-gap bar for a winner; does NOT
   fire on weekend bar (band exclusivity) or plain 1h bars; TP/SL/TIME_BARRIER
   precedence over EOD; default-off byte-identical vs no-block config.
5. Harness: replace threshold arms with trigger arms — `none / eod / weekend /
   both` — at profit gates 0.0 and 1.0; keep holdout-only decision framing and
   aggregate table.

### Validation gate (Phase 1)
Full fast suite green (10–13 pre-existing ES01B/HourSet15B failures on this branch
are expected/unrelated — proven by stash A/B earlier this session); then run the
4-arm A/B across all five fleet_manifest configs (holdout window). Commit only the
ticket's files.

## Phase 2 — opposite-signal profit-close (`conflict_resolution: "close_existing_position_if_profit"`)

### Investigation facts
- Extension point: `TieredEnsembleStrategy.on_bar` IN-POSITION conflict block,
  `src/live_execution/strategies/execution_models.py` ~762–806. Existing modes
  `hold` / `close_existing_position` / `reverse_position` already emit
  `Order(action="EXIT")`; the engine plumbing for EXIT (`_run_single_strategy` →
  `_close_trade(..., ExitReason.SIGNAL_EXIT)`) already exists — no new engine exit
  path needed.
- `EngineState` (~38–51) has NO entry price or floating PnL.
- **Price-basis trap (design deviation from the original prompt, deliberate):**
  `on_bar` receives the BRAIN close (ratio-adjusted); `_entry_fill` is on the EXEC
  (raw) basis. `side*(brain_close - exec_entry_fill)` mixes bases and is wrong.
  Fix: the ENGINE computes the profit signal on the exec basis and publishes it in
  `EngineState`; the strategy only consumes it.

### Target files
- `src/live_execution/strategies/execution_models.py`
- `agent/backtest_engine.py`
- new `tests/test_opposite_signal_profit_close.py`
- `agent/ab_weekend_flatten.py` (add feature-A arm for the Phase-2 A/B)

### Required changes
1. `EngineState`: add `entry_price: Optional[float] = None` (exec-basis entry fill)
   and `floating_pnl_points: Optional[float] = None` (sign-aware,
   `side*(exec_close - entry_fill)`, gross). Engine populates both each bar in the
   strategy loops (single mode: from `_entry_fill`/`row.exec_Close`; concurrent
   strategy mode: from `open_positions[0]`); None when flat.
2. `TieredEnsembleStrategy`: append `"close_existing_position_if_profit"` to
   `VALID_CONFLICT_MODES` (tuple extended, existing entries untouched; the
   deprecated `JointPortfolioStrategy` mode list is NOT touched). New branch in the
   conflict block:
   - `opposite_ok` — as computed by existing modes (post consecutive/cooldown
     filters).
   - `same_ok` = current side's own signal still fires (`buy_ok` if long else
     `sell_ok`, same post-filter values).
   - `profitable` = `state.floating_pnl_points is not None and
     state.floating_pnl_points > 0` (GROSS — exit slippage/commission may flip a
     marginal winner; accepted for v1, documented).
   - `opposite_ok and (not same_ok) and profitable` → `[Order(action="EXIT", ...)]`
     else HOLD. Both-firing → HOLD; losing → HOLD.
   - Exit reason stays `SIGNAL_EXIT` (existing EXIT plumbing; conflict-mode exits
     are already SIGNAL_EXIT — attribution via exit_distribution deltas).
3. Tests: mode validation accepts new mode / rejects typos; EXIT fired only when
   opposite fires AND same side stopped confirming AND green on the EXEC basis
   (test must use diverging brain vs exec prices to pin the basis); HOLD when
   losing / when both fire / when flat-path; existing three modes byte-identical.
4. Optimizer: NO search-space changes (mode is opt-in via config only; house rule —
   no optimizer crutches).

### Validation gate (Phase 2)
Suite green (same pre-existing exceptions); then the individual-toggle A/B across
fleet_manifest configs (NO combinations): baseline / weekend-only / eod-only /
featureA-only (featureA arm = override `conflict_resolution` to the new mode,
recording each config's original mode in the output). Commit.

## Live parity (deferred — requires human go-ahead)
Live `_on_new_bar` does NOT route through `strategy.on_bar()`; both features need a
mirrored gate there (`self._entry_price` exists at live_trader ~613) plus a parity
test. Not in this ticket. Do not wire until A/B justifies and a human authorizes.

## Constraint review notes (for Impact-Reviewer)
- Interface Rule: `EngineState` gains two OPTIONAL defaulted fields — additive, no
  call-site breaks; `BacktestEngine.__init__` gains keyword-only optional params.
- Base Class Rule: `BaseExecutionStrategy` untouched; only `TieredEnsembleStrategy`
  (concrete) mode tuple extended.
- Refactor Veto: no rewrites; all changes are additive branches behind default-off
  config.
