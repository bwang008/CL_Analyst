# Ticket Resolution Blueprint — per-symbol-economics_07052026_0930
**Ticket Directory:** `.agents/collab/tickets/per-symbol-economics_07052026_0930/`

## Bug Summary
The entire post-training evaluation chain (strategy optimizer → batch post-optimizer →
ensemble artifacts → backtest verification commands) runs at **CL economics for every
symbol**: `contract_multiplier` defaults to 1000 $/pt everywhere and is never resolved
from `src/core/instrument_master.INSTRUMENT_REGISTRY`, and every batch manifest carries
`slippage_per_side: 0.01` (1 CL tick).

Measured impact (ZC HourSet_01A scout, batch_20260704_2215, Ensemble E01):
- Reported: +$603,167, PF 1.30 (mult 1000, slip 0.01/side).
- True ZC econ (mult 50 $/pt, 1-tick 0.25/side slip): **-$15,843, PF 0.88**.
- Half-tick slip: +$5,564, PF 1.05 (breakeven).
Per-symbol distortion of transaction costs relative to gross PnL:
ZC/ZS/ES/NQ ~25x understated, GC 10x understated, SI 2x overstated,
**NG 10x OVERstated** (NG models were unfairly penalized). Only CL is correct.
Because the optimizer's Sortino/Sharpe objective consumed these wrong economics, it
systematically selected high-frequency, thin-edge (sub-spread) configurations for
non-CL symbols. All non-CL ensemble reports/fleet decisions to date are based on
mis-measured PnL.

Severity: HIGH (money path, fleet-wide). Not a recent regression — present since
multi-symbol standup. Design was already flagged as [HIGH] in
`.agents/collab/tickets/global-backtest-pipeline_07042026_0332/gap_analysis.md` §B
("resolve slippage + multiplier from INSTRUMENT_REGISTRY"), with the blast-radius
constraint: **CL behavior must stay byte-identical** (ledger-parity gate baseline
2026-07-04).

## Target Files
- `agent/strategy_optimizer.py`
- `agent/batch_post_optimizer.py`
- `agent/generate_ensemble_artifacts.py`
- `agent/backtest_engine.py` (main() CLI only)
- `src/core/instrument_master.py` (helper only, no registry edits)
- `tests/test_per_symbol_economics.py` (new)

## Required Changes
1. `src/core/instrument_master.py`: add two pure helpers —
   `dollars_per_point(symbol) = multiplier * quote_unit_usd` and
   `default_slippage_points(symbol) = slippage_ticks * tick_size`.
   (CL → 1000.0 / 0.01: identical to today's constants by construction.)
2. `agent/strategy_optimizer.py`: `run_optimization(..., symbol: str | None = None)`.
   When symbol given: resolve `contract_multiplier` via helper and include it in the
   engine overrides at EVERY `BacktestEngine.from_config` call in the optimize paths
   (objective, baseline, best, holdout — both tiered and non-tiered variants);
   when `slippage_per_side is None`, resolve the per-tick default. When symbol is
   None: legacy behavior byte-identical (no multiplier override).
3. `agent/batch_post_optimizer.py`: read `baseline.symbol` from the batch manifest
   (already loaded for `find_ohlcv_path`); add `--symbol` CLI override. FAIL LOUD if
   neither present (house rule: no silent defaults). Thread symbol →
   `run_single_optimization` → `run_optimization`. Print resolved economics at start.
4. `agent/generate_ensemble_artifacts.py`: resolve dollars-per-point from the already
   validated `baseline_symbol`; pass `--contract-multiplier` to the backtest
   subprocess AND embed it in the printed Verification Command. `--slippage-per-side`
   default changes 0.01 → None = resolve per-symbol tick default (CL resolves to 0.01,
   value-preserving); explicit CLI value wins.
5. `agent/backtest_engine.py` main(): `--contract-multiplier` default None → resolve
   from config `execution_symbol` when present, else legacy 1000.0 with a loud
   warning. Explicit CLI value always wins. (Old non-CL configs stamped
   `execution_symbol: "CL"` by the donor bug still reproduce their reports by
   passing the flag explicitly — reproducibility preserved, new artifacts stamped
   correctly by T6.)
6. Tests: helper resolution (CL/ZC/NG/ES), CL byte-identity of resolved values,
   optimizer override plumbed (engine receives 50.0 for ZC), artifacts cmd contains
   `--contract-multiplier`.

## Review notes (inline audit, no fast-track: not a regression, HIGH severity)
- CL parity: every default resolves to today's constants for CL — parity gate safe.
- No optimizer-crutch constraints added (user rule): honest costs only; the existing
  trade floor and min_trades are untouched.
- Manifest slippage values themselves are data, not code: corrected ZC exploration
  manifests are added under `configs/`; other symbols' manifests left for owner
  review (NG owners should EXPECT better-looking models after correction).
