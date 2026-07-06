# Fleet Expansion Candidates — 2026-07-05 (exploration session)

Goal: 4 symbols running in parallel (user directive). All results below are at TRUE
per-symbol economics (post per-symbol-economics fix, merged e568919), from the fully
repaired pipeline (whole-tree VM deploy, real baselines, pipefail, TTL'd VMs).
Pipeline determinism was validated same-day: two independent E2E NG scout runs
(batch_20260705_1715 vs _1859, seed 42) produced byte-identical results.

## Candidates

| Symbol | Config (full-pipeline artifact) | Honest 6-mo holdout | Status |
|---|---|---|---|
| CL | `configs/strategies/HS14B_Sharpe_E01_06262026.json` | n/a (live-proven) | **LIVE** |
| ES (MES) | `configs/strategies/ES01B_Sharpe_E03_07042026.json` | PF 1.35, +$2.4k MES-sized | **in fleet, dry-run** |
| NG | `batch_20260705_1715_NG_01B_SCOUT_PASS/configs/NG01B_Sharpe_E03_07052026.json` | **+$23,142 / 134 trades** | candidate (primary) |
| NG (alt) | same dir, `NG01B_Sharpe_E02_07052026.json` | +$18,504 / 92 trades | candidate (alt) |
| GC | `batch_20260705_1857/configs/GC_Sharpe_E01_07052026.json` | **+$61,615 / 95 trades** | candidate (primary) |
| GC (alt) | same dir, `GC_Sharpe_E04_07052026.json` | +$52,191 / 191 trades | candidate (alt) |

Also viable: ES01B_Sharpe_E02 (best ES true-cost holdout, PF 1.64) as an A/B against
the E03 already dry-running — but two MES instances = the deferred concurrent-position
conflict question; only one should ever go un-dry until that exists.

## Caveats (apply to every candidate)

1. **Edge class**: all fleet models are volatility-timing + asymmetric-exit harvesters
   (direction-AUC ~0.50-0.56; GC mildest directional tilt, trending UP over years —
   0.60 by 2026). Expect PnL concentration in vol regimes; flat stretches are normal.
2. **GC long-side concentration**: winning GC ensembles trade almost entirely LONG
   (holdout splits 94/1 and 173/18) during a trending-gold window — regime dependence
   is real. GC could execute on MGC (micro, in instrument registry) for the
   observation phase, mirroring the ES→MES pattern.
3. **Holdout ≠ proof**: 92-254 trades over one 6-month window passes the veto gate
   only (bootstrap CI on PF ~1.3 at these counts spans ~1.0). The observation week the
   user planned IS the next validation stage.
4. **Sortino objective picks failed holdout on every symbol** (NG, GC, ZC) — Sharpe
   picks only. Consistent with the earlier Sharpe-over-Sortino decision.
5. **Before restart**: assign unique client_ids spaced >=2 (fleet_runner validates;
   note the historical 1010 WSL/Windows collision risk); coordinate with the
   uncommitted fleet-reconnect fixes workstream (running fleet needs restart; ES01B
   has a known failing predictions-artifact test there).
6. Discards this session: ZC (cost = 32.8% of hourly ATR — structural, parked until
   directional grain features exist), ZS (16.1%, same class). Full ZC analysis:
   `docs/exploration_zc_findings.md`.

## Scout budget: 6 of 10 authorized used
ZC long-horizon (S1), ZC cutoff-2024 (S2), GC #1 (post-opt lost to deploy bug,
training reused nothing — superseded), NG #1 (self-healed via zone failover after the
fix → became source-of-record), GC #2, NG #2 (reproducibility twin). Canary runs not
counted (validation tier).
