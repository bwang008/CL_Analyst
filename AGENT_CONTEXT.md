# CL_Analyst — Agent Context

> Quick-read bootstrap file for new AG sessions. Read this FIRST for instant project context.

## Project
Crude oil (CL) 5-minute bar ML trading system using LightGBM with focal loss, walk-forward validation, and IBKR live execution.

## Current Best Model
**None established yet.** All existing models (EXP-017 through EXP-033) were trained on datasets with MACRO resample lookahead bias (up to 55 minutes of future data leakage). Their metrics are unreliable.

The prior leaked baseline was ensemble3_3 (EXP-033 LONG + EXP-032 SHORT on set_08), but its $2.66M PnL / 4.01 PF is a hallucination from the data leak.

## Current Priority
**Establish a clean baseline by retraining on set_10** (causally safe dataset, 1.19M rows, 156 features, zero lookahead).

## Key Files
| File | Purpose |
|------|---------|
| `experiment_tracker.json` | Structured registry of all experiments with metrics |
| `research_backlog.json` | Prioritized queue of experiment ideas to try |
| `HANDOFF.md` | Technical details, configs, known bugs |
| `AGENT_LOG.md` | Full chronological history of all work |
| `models/registry/` | Archived model bundles (PKL + metrics + predictions) |
| `configs/strategies/` | Live trading and backtest strategy configs |
| `docs/EXPLORATION_BACKLOG.md` | Exploration ideas (being migrated to research_backlog.json) |

## Datasets
| Dataset | Status | Notes |
|---------|--------|-------|
| set_06, set_07, set_08 | ⛔ Leaked | MACRO resample lookahead + bfill + div-by-zero |
| set_09 | ⚠️ Partial | Fixed MACRO, still has bfill |
| **set_10** | ✅ Clean | Causally safe — use for all new training |
| **HourSet_01** | ✅ Clean | 1H bars for swing trading (101K rows) |

## What To Do Next
1. Read `experiment_tracker.json` for what's been tried
2. Read `research_backlog.json` for prioritized ideas
3. Propose the highest-priority "ready" item
4. Or type `/next` to trigger the experiment proposal workflow
