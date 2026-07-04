# Validate Ledger Parity Workflow

Ledger-level, **trade-by-trade** reconciliation between the two independent
execution engines:

| Engine | Role |
|--------|------|
| `agent/backtest_engine.py` (BacktestEngine) | reference ledger |
| production `LiveTrader` via `scripts/livetest_engine.py` (harness) | livetest ledger |

This is a **different and heavier layer** than `/validate-parity`. That workflow
runs the fast *offline* suite (config / feature / ATR / execution unit tests +
shadow-log prediction parity) and will report green even when the engines
disagree at the ledger level. This workflow is what actually catches
matching/exit/cooldown divergences (it is how Phenomena A/B/C below were found).

The harness runs in **Parity Mode** (`--predictions-dir`): backtest predictions
are injected so entry SIGNALS are identical by construction — isolating the
matching/exit engine from model inference.

Run this **after** `/validate-parity` is green, and whenever `backtest_engine.py`,
`live_trader.py`, a strategy class, or a cooldown/exit code path changes.

> **Environment:** every command runs in the `trader` conda env:
> `conda run -n trader python ...`. (That env pins pandas 1.5.3 — see Pitfalls.)

---

## Steps

### 1. Setup — build the short subset + patched parity config

// turbo
```bash
conda run -n trader python scripts/ledger_parity_check.py setup \
  --config      reports/batch_runs/batch_20260702_0038_SCOUT_14B_V2/configs/HS14B_Sharpe_E01_07022026.json \
  --data        data/processed/CL_HourSet_14B.parquet \
  --predictions reports/batch_runs/batch_20260702_0038_SCOUT_14B_V2/predictions/HS14B_Sharpe_E01_predictions.csv \
  --long-model  reports/sweep_hs14b_2x1_6h_canary_20260624_2007/registry/canary_output/registry/E2E_CL_HourSet_14B_long_logloss/final_model.pkl \
  --short-model reports/sweep_hs14b_2x1_6h_canary_20260624_2007/registry/canary_output/registry/E2E_CL_HourSet_14B_short_average_precision/final_model.pkl \
  --warmup-bars 2200 --replay-bars 336
```

Writes `reports/_ledger_parity/{livetest_subset.parquet, parity_config.json,
parity_meta.json}` and prints the exact Step-2 and Step-3 commands. `2200` warmup +
`336` replay (~2 weeks of 1h bars) is enough for feature/ATR warmup while keeping
the livetest under ~15 min.

### 2. Run the livetest harness (long-running — run in background)

```bash
conda run -n trader python scripts/livetest_engine.py \
  --config "reports/_ledger_parity/parity_config.json" \
  --data   "reports/_ledger_parity/livetest_subset.parquet" \
  --warmup-bars 2200 \
  --predictions-dir . \
  --output "reports/_ledger_parity/livetest_ledger.csv"
```

Replays 336 bars through the unmodified production `LiveTrader` (~15 min). The
**ledger CSV is the source of truth** — do not parse stdout (see Pitfalls). While
it runs, tail the log for `[OCA] cancelled N resting protective order(s)` (good)
and confirm **no** `[OCA] Failed to cancel ...` warnings (see Pitfalls).

### 3. Reconcile — run the backtest + compare ledgers

// turbo
```bash
conda run -n trader python scripts/ledger_parity_check.py reconcile \
  --work-dir "reports/_ledger_parity" \
  --livetest "reports/_ledger_parity/livetest_ledger.csv"
```

Runs the BacktestEngine over the identical subset, slices the replay window, and
reconciles trade-by-trade. **Exit code 0 = PASS, 1 = FAIL**, so it doubles as a
regression gate. Tolerances (overridable): `--pnl-tolerance 5.0`,
`--fill-tolerance 0.011`.

---

## Exit-reason mapping (expected, NOT a divergence)

The engines label exits differently; the reconciler maps backtest → livetest:

| Backtest | Livetest |
|----------|----------|
| `SL` | `SL_HIT` |
| `TP` | `TP_HIT` |
| `TRAILING_BE` | `SL_HIT` |
| `TIME_BARRIER` | `TIME_BARRIER` |
| `SIGNAL_EXIT` | `SIGNAL_EXIT` |

A matched trade that differs **only** by this mapping with identical PnL is in
parity.

---

## Expected results (clean run)

- **Entry/signal path:** entry-fill delta `$0.0000`; side match on every matched trade.
- **Most trades match to the exact cent** (13/18 in the reference run below).
- **Per-trade PnL delta ≤ $5.00** on all matched trades.
- **Trade counts equal**, no unmatched trades.
- Reconciler prints `PARITY: PASS ✅` and exits `0`.

### Reference baseline — run of 2026-07-02, post-OCA-fix (scout HS14B_Sharpe_E01)

```
trades: backtest=18  livetest=17  matched=16
exact-cent matches: 13/16
side match: True   |  entry_fill delta: $0.0000
```

This run did **not** fully pass — it has three **known-open** divergences. Do
**not** re-flag these as new regressions; a NEW regression is anything *beyond*
this list:

| ID | Divergence | Status |
|----|-----------|--------|
| **A** | Trade-count 18 vs 17 + one-bar-early short / missing follow-on long in the 2026-05-28→29 window | **RESOLVED 2026-07-03** — cooldown double-enforcement fixed (ticket `parity-exit-signal_07022026_1930`, commit `f4f0732`) |
| **B(a)** | Static SL/TP price-basis skew (raw unrounded vs fill+penny-rounded) | **RESOLVED 2026-07-03** — backtest SL/TP now `round(entry_fill ± mult*atr, 2)`; brackets match live to the cent |
| **B(b)** | Same-bar exit precedence inversion (BT checks TIME_BARRIER first; live matching engine fills TP/SL first) — e.g. 2026-05-26 BT `TIME_BARRIER` vs LT `TP_HIT` | **Known open — backlogged** by human decision |
| **C** | Sub-tick trailed-SL residuals | **Known open** — unmeasurable in a 1h harness (see Pitfalls #3); re-measure in a 5m harness |
| **D** | Live exit-reason vocabulary gap: time-barrier/OOB exits reset with `"CLOSED"` → tp_cooldown instead of sl_cooldown | **RESOLVED 2026-07-03 evening** — ticket `exit-fill-routing-cooldown_07032026_0930`, commit `fc89b11` (TIME_BARRIER/CLOSED_OOB reasons; CLOSED-family now SL-flavored) |
| **E** | Fill misrouting: orphaned protective fills processed as ENTRY (brackets around an exit); exits salvaged by OOB; bracket children placed TWICE per entry | **RESOLVED 2026-07-03 evening** — same ticket/commit (`_entry_order_ids` registry + UNRECOGNIZED FILL guard; harness duplicate placement removed). Post-fix log: 0 UNRECOGNIZED, 0 OOB, [OCA] firing, 18/18 single child sets |
| **F** | Exit-bar evaluation semantics (refined 2026-07-03): (1) `on_exit` resets the counter to 0 so the exit-bar evaluate reads 1 vs BT's 0 → same-bar re-entry after TP when tp_cooldown=0; (2) the harness flushes deferred fill callbacks AFTER the bar's evaluation, so the exit-bar evaluate sees a flat sim position with stale counters; (3) live skips evaluation on TIME_BARRIER exit bars while BT evaluates them → one-bar shifts in consecutive-signal gating | **Known open — needs its own ticket**; fix directions in `tickets/exit-fill-routing-cooldown_07032026_0930/tdd_result.md` |

### Reference baseline — run of 2026-07-03 evening, post D/E fix (`fc89b11`), TRAILING DISABLED symmetrically (`trailing_atr_mult=10000` patched into the parity config; livetest ledger is trailing-invariant in the 1h harness)

```
trades: backtest=15  livetest=17  matched=14  (bt_only=1, lt_only=3)
exact-cent matches: 13/14
side match: True (14/14)  |  entry_fill delta: $0.0000
violations: 1 (the B(b) trade, $1500)
unmatched: 05-26 13:00 lt (B(b) cascade × F), 05-28 07:00 lt + 05-28 08:00 bt
           (same trade shifted one bar — F), 06-10 11:00 lt (F)
```

With the ORIGINAL (trailing-on) config the run FAILs on ~9 backtest `TRAILING_BE` trades —
expected, per Pitfall #3; disable trailing symmetrically to use this workflow until the
5m-harness ticket lands.

When B(b) and F are resolved, update this baseline to the new PASS state.

---

## Pitfalls (learned the hard way)

1. **Renamed batch folder breaks `predictions_path`.** Configs may carry a stale
   `predictions_path`. `setup` rewrites it to the exact repo-relative path you
   pass via `--predictions`, and both engines run from the repo root
   (`--predictions-dir .` for the livetest), so the same file resolves for both.
   If you move a batch folder, re-run `setup` — don't hand-edit.

2. **Model pkls must exist locally, but their choice doesn't affect signals.**
   `ConfigurableStrategy` loads a model **at init** to read `feature_names`, even
   in parity mode where inference is bypassed. Point `--long-model` / `--short-model`
   at any locally-available pkls with the **same feature schema** (e.g. the 14B
   canary models). If the sweep VMs are gone and the production pkls aren't local,
   this is the workaround — the injected predictions, not the models, drive signals.

3. **The live trailing stop is INERT in the 1h harness.** `_check_trailing_stop()`
   is bound solely to the 5m callback (`live_trader.py` `_on_bar_update_5m`), and
   the 1h harness wires only `_on_bar_update_1h`. So the live side never trails
   here, while the backtest trails at 1h resolution and production trails at 5m.
   **Trailing-stop parity cannot be validated in a 1h harness** — trailing-dependent
   divergences (e.g. Phenomenon B/C) are expected and belong to a 5m-resolution
   test, not this one.

4. **`[OCA] Failed to cancel ...` warnings = a harness/adapter gap, not a strategy
   bug.** The software-OCA path calls `exec_client.cancel_open_orders(symbol=...)`;
   if a warning fires, the simulated adapter is missing that method and stale
   protective orders will linger and corrupt exits. A clean run logs
   `[OCA] cancelled N resting protective order(s)` and zero failures.

5. **Livetest output is buffered — trust the CSV, not stdout.** When the run is
   piped/backgrounded, stdout may appear empty or truncated until exit. The ledger
   CSV (`--output`) is written on completion and is the source of truth.

6. **Trade-count divergence is legitimate and expected sometimes.** Cooldown/exit
   differences (Phenomenon A) genuinely change how many trades each engine takes.
   The reconciler counts unmatched trades as issues, but cross-check them against
   the Known-Open table before calling a regression.

7. **Aggregate PnL delta is misleading.** Large per-trade divergences can cancel to
   a tiny net (this run: ~$57–188 net masking a $420 single-trade gap). Always read
   the **per-trade** violations, never just the total.

8. **`conda run -n trader` matters (pandas 1.5.3).** In that env `format="mixed"`
   silently produces `NaT` on date parsing (a 2.0+ feature). Stick to the `trader`
   env for every step so datetime handling matches the rest of the pipeline.

9. **Enough warmup.** Too few warmup bars yield different early ATR/feature values
   and spurious early-signal divergences. 2200 is the validated default for 14B.
