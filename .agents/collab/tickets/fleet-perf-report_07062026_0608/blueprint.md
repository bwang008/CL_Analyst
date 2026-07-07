# Ticket Resolution Blueprint — fleet-perf-report_07062026_0608
**Ticket Directory:** `.agents/collab/tickets/fleet-perf-report_07062026_0608/`

## Bug Summary
**Feature Gap (not a bug):** The fleet of 4 live trading models writes comprehensive trade data to a shared SQLite DB (`fleet_telemetry.db`, v2 fleet schema), but NO reporting tool exists to query that data and produce performance summaries. The user needs per-model and aggregate performance tracking for a 1-2 month live evaluation period starting 7/6/2026.

## Target Files
- `scripts/fleet_performance_report.py` **(NEW)**
- `.agents/workflows/fleet-performance-report.md` **(NEW)**

## Required Changes

### 1. `scripts/fleet_performance_report.py` (CREATE — ~350 lines)

A CLI script that generates fleet-wide live trading performance reports.

**CLI interface:**
- `--manifest` (required): Path to `fleet_manifest.json`
- `--since` (optional): ISO date filter on `entry_time` (inclusive)
- `--until` (optional): ISO date filter on `entry_time` (inclusive)
- `--output` (optional): Path for markdown report file (default: `reports/fleet_performance_<timestamp>.md`)
- `--db-path` (optional): Override `fleet_telemetry.db` path (default: `<CL_DATA_ROOT>/data/fleet_telemetry.db`)

**Implementation requirements:**

1. **Manifest Discovery:**
   - Parse `fleet_manifest.json` → for each enabled instance, read the referenced strategy config JSON
   - Extract the identity triple: `(execution_symbol, client_id, nickname)` from each config
   - CRITICAL: Use `execution_symbol` (the actual traded instrument: CL, MES, NG, MGC), NOT the brain/training symbol. GC fleet trades MGC ($10/pt), ES fleet trades MES ($5/pt).

2. **Database Access:**
   - Open `fleet_telemetry.db` via direct `sqlite3.connect(db_path + "?mode=ro", uri=True)` in READ-ONLY mode
   - Do NOT use the `TelemetryDB` class — it enforces single-identity binding which prevents cross-bot queries
   - Query `active_positions WHERE status='CLOSED'` with optional date filtering on `entry_time`

3. **Dollar PnL Computation:**
   - For each closed trade: `gross_pnl_dollars = (exit_price - entry_price) * side_mult * dollars_per_point(execution_symbol) * quantity`
   - Where `side_mult = 1 if side == 'LONG' else -1`
   - Use `dollars_per_point()` from `src.core.instrument_master`

4. **Commission Handling:**
   - For each trade, join with `tradebook_events WHERE event_type='COMMISSION'` matching on `client_id` and the trade's entry/exit order IDs
   - Sum `commission + fees` for net PnL
   - Fallback: If no commission records found, use a conservative estimate or note "commission data unavailable"

5. **Per-Model Metrics (one block per bot):**
   - Trade Count
   - Buy (LONG) / Sell (SHORT) count
   - Win Rate: `wins / total` where win = `net_pnl_dollars > 0`
   - Profit Factor: `gross_profit / abs(gross_loss)`
   - Total Net PnL ($)
   - Max Drawdown ($): peak-to-trough on cumulative net PnL series
   - Avg Trade Duration (bars)
   - Avg Win ($) / Avg Loss ($)
   - Exit Reason Distribution: count + percentage per `close_reason` (TP_HIT, SL_HIT, TRAILING_SL, TIME_BARRIER, CLOSED_OOB)

6. **Aggregate Fleet Metrics:**
   - Same metrics computed across ALL trades from ALL bots combined
   - Per-model contribution to total PnL

7. **Output:**
   - **Console**: Formatted ASCII tables printed to stdout
   - **Markdown file**: Structured `.md` report with headers, tables, per-model sections, and aggregate summary

**Dependencies (all already in project):**
- `pandas`, `sqlite3`, `argparse`, `json`, `pathlib`
- `src.core.instrument_master.dollars_per_point`
- `src.data_paths.get_data_path`

### 2. `.agents/workflows/fleet-performance-report.md` (CREATE — ~50 lines)

Agent workflow for on-demand report generation.

**Requirements:**
- Slash command registration: `/fleet-performance-report`
- Description: "Generate a live fleet trading performance report"
- Step-by-step instructions:
  1. Identify the fleet manifest to report on (default: `configs/fleet/fleet_manifest.json`)
  2. Determine date range (default: last 30 days, or user-specified)
  3. Run the Python script with appropriate arguments
  4. Present the markdown report as an artifact to the user
- Guidance for handling zero-trade scenarios (fleet just started, insufficient data)
- Output artifact location convention
