# Ticket Resolution Blueprint — t1-instrument-metadata_07042026_1543
**Ticket Directory:** `.agents/collab/tickets/t1-instrument-metadata_07042026_1543/`

## Requirement Summary
T1 of the multi-symbol live-gaps program (parent analysis:
`.agents/collab/tickets/multi-symbol-live-gaps_07042026_1520/blueprint.md`).
The live engine silently defaults `execution_symbol` to "CL" and the instrument
registry lacks live-execution metadata, so a mis-generated config (ES01B) would
silently trade the wrong instrument. T1 adds the metadata foundation + fail-fast
startup validation. Zero behavior change for CL configs. Design was audited
(`audit.md`) and APPROVED with conditions (`impact_review.md` C1-C4). The full
field schema, verified per-symbol contract specs, exact error messages, and the
17-item test list live in `audit.md` §4 — the TDD agents MUST read audit.md and
impact_review.md alongside this blueprint; this document governs where they conflict.

## Target Files
- `src/core/instrument_master.py` — extend `Instrument` dataclass + INSTRUMENT_REGISTRY
  (all 8 symbols get: exchange, multiplier, quote_unit_usd, active_months,
  roll_reference, roll_buffer_days, session_hours_ct, bars_per_day_5m, bars_per_day_1h,
  live_vol_index, micro_of). Add micro entries MCL/MES/MNQ/MGC/SIL as first-class
  entries. Fix PA tick 0.05→0.10 ($10 tick value, verified NYMEX spec).
- `src/live_execution/instrument_context.py` — NEW leaf module:
  `resolve_instrument_context(strategy_config) -> InstrumentContext` (execution_symbol,
  brain_symbol, instruments). Hard-RAISE on missing/unknown execution_symbol with the
  exact error messages from audit §4.2. brain_symbol = `micro_of or execution_symbol`.
  Opportunistic experiment_id symbol cross-check (`derive_model_symbol`: token after
  `E2E_` only when it is a registry symbol); HARD enforcement of explicit
  `models.<side>.symbol` field when present.
- `src/live_execution/live_trader.py:276-278` — replace silent
  `strategy_config.get("execution_symbol", "CL")` with resolver call; keep the
  `self._execution_symbol` attribute name/type (49 read-only consumers verified).
- `src/live_execution/cli.py` — resolve+validate instrument immediately after config
  load, BEFORE DataFeed/Execution factories construct anything.
- `configs/strategies/ensemble2_opt.json` — add `"execution_symbol": "CL"` (the only
  config missing it; 19 others verified carrying "CL").
- `tests/test_live_macro_refresh.py` — fixture configs gain execution_symbol (only
  existing test file whose fixtures construct LiveTrader through __init__ with a
  config lacking the field).

## Required Changes (rules the code must satisfy)
1. NO silent defaults: missing execution_symbol, unknown symbol, or missing registry
   field must RAISE with actionable messages (audit §4.2 wording).
2. Registry invariant enforced by test: `tick_value == tick_size * multiplier *
   quote_unit_usd` for every entry; completeness check over all required fields.
3. Reviewer conditions:
   - C1: micro entries inherit parent's `cftc_code`/`volatility_index`; pinned by test.
   - C2: resolver preserves `.upper()` normalization of execution_symbol.
   - C3: after green, re-run the HS14B ledger parity gate (LiveTrader.__init__ gained
     a raise path) — manager runs this, not the coder.
   - C4: include an intended-failure test asserting the shipped
     `configs/strategies/ES01B_Sharpe_E03_07042026.json` FAILS resolution (its
     execution_symbol "CL" contradicts `models.*.experiment_id` `E2E_ES_*`) — this is
     desired behavior until T6 regenerates the config.
4. CL pinning tests: CL registry values byte-pinned (0.01/$10/NYMEX, bars 288/24,
   roll buffer 6); HS14B config resolves unchanged; MCL Two-Stream (brain CL, hands
   MCL) resolves with brain_symbol=="CL".
5. Scope guards: do NOT touch cli.py seed/cache path defaults (T2), ibkr_data_feed
   routing (T2), tick usage in order pricing (T3), macro fetching (T4), watchdog/
   rollover (T5).

## Verification
- Full fast suite green (baseline 814 + new tests).
- Post-green (manager): HS14B ledger parity gate re-run per memory convention
  (`setup --disable-trailing`, 336-bar window; any non-PASS is a T1 regression).
