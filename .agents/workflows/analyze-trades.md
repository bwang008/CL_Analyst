---
name: analyze-trades
description: Re-runnable report of the LIVE fleet's trades and PnL from the shared telemetry DB (fleet_telemetry.db) — per-model trade counts, realized/unrealized PnL (with correct micro-contract multipliers), and the signal/decision distribution (firing rate + model-confidence spread) used to judge whether live behavior matches the backtest and the data streams are healthy. Runs scripts/analyze_live_trades.py, then interprets the result. Default window is since 2026-07-06.
---

# /analyze-trades — Live Fleet Trade & PnL Report

Give the operator a snapshot of what the live fleet (and `fleet_runner.py`'s
child bots) has actually done: **how many trades per model, realized/unrealized
PnL, and — the point of the exercise — whether the trade cadence, long/short
balance, firing rate and model-confidence distribution look like the backtest.**
If a live data stream is bad, the model sees different features, so its
confidence distribution and how often it fires (EXECUTE vs HOLD) drift away from
the backtest *before* the PnL makes it obvious. This workflow surfaces that.

## Step 0 — Run the report
Read-only against the live DB; safe to run anytime, as often as you like.
```bash
conda run -n trader python scripts/analyze_live_trades.py --since 2026-07-06
```
Options:
- `--since 2026-07-06` / `--until 2026-07-08` — trade-entry window (UTC). Default
  `--since` is 2026-07-06; default `--until` is "now" (no upper bound).
- `--db <path>` — defaults to the data root's `fleet_telemetry.db`
  (`C:\CL_Analyst_Data\data\fleet_telemetry.db`).
- `--manifest <path>` — defaults to `configs/fleet/fleet_manifest.json`; this is
  the authority that maps each `client_id` → its **execution** symbol/multiplier.
- `--output reports/live_trade_analysis.md` — the markdown is written here (and
  echoed to the console).

The report prints a Fleet Totals block, a per-model summary table, and a
per-model detail section, plus a data-freshness footer.

## Why this is its own tool (don't reach for the others)
- `scripts/trade_reconciler.py` / `run_reconciliation_audit.py` open the DB in
  **legacy single-bot mode** and RAISE on the fleet DB (user_version 2); they
  diff *one* config's prices against a backtest CSV. Not fleet-wide.
- `src/live_execution/fleet_health.py` reads the fleet DB but only checks
  *health* (naked positions, stale bars, missing fills) — not trades or PnL.

## Critical correctness note — micro multipliers (baked into the script)
Three live models trade **micro** contracts while the DB stores the **brain**
symbol. PnL must use the config's `execution_symbol`, never the DB symbol:

| client_id | config brain (DB `symbol`) | trades (execution_symbol) | $/point |
|--:|---|---|--:|
| 1400 | CL | CL | 1000 |
| 2000 | ES | **MES** | 5 |
| 3000 | NG | NG | 10000 |
| 4000 | GC | **MGC** | 10 |
| 5000 | SI | **SIL** | 1000 |

Valuing ES/GC/SI at the DB brain multiplier would overstate their PnL by
5–10x. The script resolves the multiplier per `client_id` from the manifest's
configs; a `client_id` present in the DB but not the manifest is flagged and
valued with a fallback (possibly wrong for micros).

## Data-quality handling (already in the script — repeat the flags to the user)
- **Two PnL numbers.** The report prints **Clean $** (normal `SL_HIT`/`TP_HIT`/
  time exits — trust this) and **All $** (also includes OOB-recovered exits).
  Lead with the clean number; the gap between them is the contamination risk.
- **Unresolved exit** — a CLOSED row with `exit_price IS NULL`
  (`CLOSED_OOB` / `CLOSED_OOB_UNRECOVERED`): excluded from both PnL numbers.
- **SUSPECT price** — a closed row whose exit is > 25% from its entry (e.g. a GC
  row that recorded an ES-scale `7484` exit against a `4063` entry): excluded and
  flagged as a likely bad print / cross-contaminated row. Do **not** let it into
  the PnL total.
- **OOB-recovered trades** — `close_reason` containing `OOB`: kept out of the
  clean total and counted separately (the `Clean/OOB` column). These are the
  ones the IB-Gateway **master-client-ID login** could have poisoned with other
  clients' executions, so they need a broker cross-check.
- **Contamination cutover** — `--contamination-cutover` (default
  `2026-07-08T13:47:00` UTC, when the Master API requirement was dropped in
  commit 4743489). Entries before it are flagged "verify vs broker". Pass
  `none` to disable, or move it if you stopped the master-ID login at a
  different time. To measure only clean data going forward, run with
  `--since "2026-07-08 14:00"`.
- **Dollars ⇒ trust IBKR, not this DB.** DB PnL is a behavioral cross-check, not
  an accounting ledger — contamination + missing OOB fills mean it can diverge
  from the broker. The account statement is ground truth for money.
- **Entries vs EXECUTE mismatch** — position rows and `trade_ledger` EXECUTE
  counts can differ; both are shown so a divergence is visible.

## How long to run before the report is statistically trustworthy
The script prints this table (derived from `/model-detective`'s heuristics — PnL
is exit-luck until you have many low-concentration trades). At the fleet's
~1.5–2.3 trades/day/model:

| Validate | Sample | ~Live time | Verdict |
|---|---|---|---|
| Probability distribution vs backtest (KS) | ~150–200 decisions/model | **~2 wks** (start now) | data streams good |
| Firing rate / trade cadence | ~30–50 trades | ~3–4 wks | behavior matches backtest |
| Win rate / exit-reason mix | ~100 trades/model | ~2–3 mo | directional/exit behavior |
| PnL / Sharpe | ~200–400 trades/model | ~3–6+ mo | dollar performance (confirmatory) |

**Do not gate on live PnL/Sharpe** (months). The data-stream question is
answerable in ~2–4 weeks; the fastest, highest-signal check is the live
probability distribution vs the backtest's, per model — start it now. The
post-fix clean window (after the contamination cutover) is the real T-zero.

## Step 1 — Interpret (this is the operator's actual question)
After running, write a short read-out. For each model judge:
1. **Trade cadence** — trades/day live vs backtest for the same calendar window.
   Roughly matching count ⇒ the stream is producing the expected number of
   setups. Far fewer/more ⇒ investigate.
2. **Firing rate** (EXECUTE / decisions) — healthy band ≈ **15–70%**. ~0% (model
   went quiet) or ~99% (fires on every bar / always-on) is the fingerprint of a
   bad or stale feed, or a threshold problem. Cross-reference the backtest firing
   rate from `/model-detective` Step 5 if available.
3. **Confidence distribution** — mean/p50/p90 should sit near the backtest's. A
   collapse toward 50 (no separation) or a degenerate spike means the model is
   seeing garbage features → suspect that symbol's data stream.
4. **Long/short balance** — a model that backtested balanced but is live
   all-long (as ES and GC currently are) is worth a note.
5. **PnL** — lead with **Clean $** (SL/TP exits), then All $; net of
   commissions. Treat it as **secondary** to the behavioral signals above for a
   window this short, and reconcile dollars against **IBKR statements** — the DB
   is contaminated by the master-ID leak and missing OOB fills, so a DB↔broker
   gap is expected and not worth chasing.

Then message the operator: the fleet net (realized + unrealized − commissions),
a one-line verdict per model (behavior matches backtest / diverges — and which
data stream to check), and any data-quality flags (suspect prices, unresolved
exits, unmapped client_ids) that need a fix. Point to
`reports/live_trade_analysis.md` for the full detail. Re-run with the same
command to refresh.

## Related
- `/model-detective` — the backtest-side forensic (holdout AUC, firing rate,
  threshold fragility) whose "healthy firing 15–70%" heuristic this report reuses.
- `/diagnose` — data-health / telemetry sanity (feature drift, cache/rollover).
- `configs/fleet/fleet_manifest.json` — the enabled fleet and client_id map.
