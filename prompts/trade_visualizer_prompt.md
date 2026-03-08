# Agent Briefing: Interactive Trade Visualizer for CL_Analyst

## Objective

Build a **self-contained, interactive web-based trade visualizer** that lets the user load backtest runs, examine trades on a time-series chart of CL (Crude Oil) 5-minute price data, and visually inspect entry/exit points alongside model signal probabilities. The tool must run locally with a simple `python` command and open in a browser.

## Background & Existing Infrastructure

This is a quantitative trading system for CL futures using LightGBM models. The codebase already has:

### What EXISTS (backend data structures)

| Component | Location | What it does |
|---|---|---|
| [BacktestEngine](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#201-1006) | [agent/backtest_engine.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py) | Bar-by-bar backtester with FSM trade management |
| [TradeRecord](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#79-97) | `agent/backtest_engine.py:80` | Dataclass with: `entry_dt`, `exit_dt`, `entry_price`, `exit_price`, `entry_fill`, `exit_fill`, `side` (+1/-1), `atr_at_entry`, `exit_reason` (TP/SL/TRAILING_BE/TIME_BARRIER), `duration_bars`, `gross_pnl_dollars`, `net_pnl_dollars`, `lots` |
| [BacktestResult](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#99-169) | `agent/backtest_engine.py:100` | Contains `trades: list[TradeRecord]`, `equity_curve: list[float]`, `label`, `start_dt`, `end_dt`. Has computed properties: [total_pnl](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#109-112), [win_rate](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#117-123), [profit_factor](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#124-135), [max_drawdown](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#136-154), [exit_distribution](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#155-169) |
| OOS predictions | [reports/oos_predictions.csv](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/reports/oos_predictions.csv) | CSV with DatetimeIndex + `prob_Buy` or `prob_Sell` column (probability 0-1) |
| Strategy configs | `configs/strategies/*.json` | JSON with `nickname`, `direction`, `entry_threshold`, `tp_atr_mult`, `sl_atr_mult`, `trailing_atr_mult`, etc. |
| Raw OHLCV | [data/raw/cl-5m_bk.csv](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/data/raw/cl-5m_bk.csv) | Semicolon-delimited: `Date;Time;Open;High;Low;Close;Volume` (dd/mm/yyyy format) |
| Processed data | [data/processed/CL_set_06.parquet](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/data/processed/CL_set_06.parquet) | Parquet with feature columns + `RAW_Open`, `RAW_High`, `RAW_Low`, `RAW_Close`, `RAW_Volume` |
| Static visualizer | [src/visualizer.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/src/visualizer.py) | Matplotlib [SignalVisualizer](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/src/visualizer.py#23-546) class — static PNG plots only |
| Experiment runner | [agent/experiment_runner.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/experiment_runner.py) | Logs experiment metadata to `reports/experiment_log.json` |

### What DOES NOT EXIST (gaps to fill)

1. **Trade-level CSV export** — [BacktestEngine](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#201-1006) returns [BacktestResult](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#99-169) in memory but does NOT persist individual [TradeRecord](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#79-97) rows to disk. This is the critical gap.
2. **Run metadata file** — No structured metadata JSON is saved alongside trades that records which predictions file, data file, strategy config, and model were used.
3. **Web-based visualization** — [index.html](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/index.html) exists but is empty. No JavaScript charting. All existing visualization is static matplotlib PNGs.
4. **Data serving** — No HTTP endpoint to serve OHLCV or trade data to a browser frontend.

---

## Implementation Plan

### Phase 1: Data Pipeline (Backend — Python)

#### 1A. Add trade export to [BacktestEngine](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#201-1006)

Add a method to [BacktestResult](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#99-169) in [backtest_engine.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py):

```python
def to_dataframe(self) -> pd.DataFrame:
    """Convert trades list to a DataFrame for export."""
    records = []
    for t in self.trades:
        records.append({
            "entry_dt": t.entry_dt,
            "exit_dt": t.exit_dt,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "entry_fill": t.entry_fill,
            "exit_fill": t.exit_fill,
            "side": t.side,  # +1 long, -1 short
            "atr_at_entry": t.atr_at_entry,
            "exit_reason": t.exit_reason.value,  # "TP", "SL", etc.
            "duration_bars": t.duration_bars,
            "gross_pnl": t.gross_pnl_dollars,
            "net_pnl": t.net_pnl_dollars,
            "commission": t.commission_dollars,
            "lots": t.lots,
        })
    return pd.DataFrame(records)

def export_trades(self, path: str) -> None:
    """Save trades + equity curve to CSV files."""
    self.to_dataframe().to_csv(path, index=False)
    # Also save equity curve
    eq_path = path.replace(".csv", "_equity.csv")
    pd.Series(self.equity_curve, name="equity").to_csv(eq_path)
```

#### 1B. Add run metadata export

Update the [main()](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#1195-1319) function in [backtest_engine.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py) to save a `run_metadata.json` alongside trades:

```json
{
    "run_id": "EXP-025_manatee_20260307",
    "timestamp": "2026-03-07T15:30:00",
    "predictions_file": "reports/oos_predictions.csv",
    "data_file": "data/processed/CL_set_06.parquet",
    "strategy_config": "configs/strategies/manatee.json",
    "strategy_nickname": "Manatee",
    "direction": "LONG",
    "entry_threshold": 0.60,
    "tp_atr_mult": 3.0,
    "sl_atr_mult": 1.5,
    "total_trades": 4530,
    "win_rate": 0.421,
    "profit_factor": 2.97,
    "total_pnl": 631593.48,
    "max_drawdown": -27537.24
}
```

#### 1C. Add `--export-dir` CLI flag

Update the [main()](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#1195-1319) function in [backtest_engine.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py):
- Add `--export-dir` argument (default: `reports/backtest_runs/{run_id}/`)
- Auto-create directory and save: `trades.csv`, `equity_curve.csv`, `run_metadata.json`
- Auto-generate `run_id` from predictions filename + strategy nickname + date

**Output directory structure for a run:**
```
reports/backtest_runs/
├── EXP-025_manatee_20260307/
│   ├── trades.csv           # All TradeRecord fields
│   ├── equity_curve.csv     # Bar-by-bar equity
│   └── run_metadata.json    # Config + aggregate metrics
├── EXP-026_koala_20260307/
│   ├── trades.csv
│   ├── equity_curve.csv
│   └── run_metadata.json
```

---

### Phase 2: Data Server (Backend — Python)

Create [agent/trade_visualizer_server.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/trade_visualizer_server.py) — a lightweight HTTP server (stdlib `http.server` or Flask if available) that:

1. **`GET /api/runs`** — Scan `reports/backtest_runs/*/run_metadata.json` and return a list of available runs with their metadata.

2. **`GET /api/runs/{run_id}/trades`** — Return `trades.csv` contents as JSON array.

3. **`GET /api/runs/{run_id}/equity`** — Return equity curve as JSON array.

4. **`GET /api/ohlcv?start=YYYY-MM-DD&end=YYYY-MM-DD`** — Return OHLCV bars (from raw CSV or processed parquet) as JSON. Must support date range filtering to avoid sending the entire ~870K-row file. Downsample if range > 30 days (aggregate 5-min → 1-hour bars).

5. **`GET /api/runs/{run_id}/signals?start=YYYY-MM-DD&end=YYYY-MM-DD`** — Return prediction probabilities (from the predictions CSV referenced in run metadata) for the given date range. 

6. **`GET /`** — Serve the [index.html](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/index.html) frontend.

> [!IMPORTANT]
> The raw OHLCV CSV ([data/raw/cl-5m_bk.csv](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/data/raw/cl-5m_bk.csv)) is ~360MB with ~870K rows spanning 2008–2026. The server MUST support chunked loading with date range filters. Never load the entire file into the frontend at once.

---

### Phase 3: Frontend (HTML/CSS/JavaScript)

Create a single-page application in [index.html](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/index.html) with the following layout and capabilities:

#### Technology Stack
- **Charting**: [Lightweight Charts](https://github.com/nickolay-nickolov/nickolay-nickolov-lightweight-charts) by TradingView (CDN) — purpose-built for financial time series, supports candlestick charts, markers, overlays, multiple panes
- **UI**: Vanilla HTML/CSS/JS (no build step, no React/Vue)
- **HTTP**: `fetch()` API to the Python data server

#### Layout (3-panel)

```
┌──────────────┬──────────────────────────────────────────┐
│  SIDEBAR     │              MAIN CHART                   │
│              │                                           │
│ ┌──────────┐ │  ┌───── Candlestick Chart ──────────┐    │
│ │ Run List │ │  │  CL 5-min OHLC                   │    │
│ │ --------  │ │  │  ▲ entry markers (green/red)     │    │
│ │ EXP-025  │ │  │  ▼ exit markers (blue/orange)    │    │
│ │ EXP-026  │ │  │  TP/SL level lines                │    │
│ │ ...      │ │  └──────────────────────────────────┘    │
│ └──────────┘ │                                           │
│              │  ┌───── Probability Panel ──────────┐    │
│ ┌──────────┐ │  │  prob_Buy / prob_Sell histogram    │    │
│ │ Run Info │ │  │  (0-1 range, color by threshold)   │    │
│ │ --------  │ │  └──────────────────────────────────┘    │
│ │ WR: 42%  │ │                                           │
│ │ PF: 2.97 │ │  ┌───── Equity Curve Panel ─────────┐    │
│ │ PnL: $631K│ │  │  Cumulative PnL line chart        │    │
│ │ DD: -$27K │ │  └──────────────────────────────────┘    │
│ └──────────┘ │                                           │
│              ├──────────────────────────────────────────┤
│ ┌──────────┐ │           TRADE TABLE                     │
│ │ Filters  │ │  ┌──────────────────────────────────┐    │
│ │ --------  │ │  │ # │ Entry    │ Exit   │Side│PnL │    │
│ │ Win/Loss │ │  │ 1 │ 2022-... │ 2022.. │ B  │+$X │    │
│ │ Side     │ │  │ 2 │ ...      │ ...    │ S  │-$Y │    │
│ │ Exit     │ │  │ ← click to jump to chart →      │    │
│ └──────────┘ │  └──────────────────────────────────┘    │
└──────────────┴──────────────────────────────────────────┘
```

#### UI Components

##### 1. Sidebar: Run Browser
- Fetch `/api/runs` on load → populate a scrollable list of run cards
- Each card shows: run_id, strategy nickname, direction badge (LONG/SHORT), WR, PF, PnL
- Click a card → loads that run's trades, equity, and signals into the main panels
- Highlight the active run

##### 2. Sidebar: Run Info Panel
- Display aggregate metrics for the selected run: total trades, win rate, profit factor, total PnL, max drawdown, exit distribution pie chart

##### 3. Sidebar: Filters
- **Win/Loss toggle**: Show only winning or losing trades (or both)
- **Side filter**: Long only, Short only, All
- **Exit reason checkboxes**: TP, SL, TRAILING_BE, TIME_BARRIER
- Filters apply to both the trade table and the chart markers

##### 4. Main Chart: Candlestick + Markers
- Candlestick chart (OHLC) with pan/zoom using Lightweight Charts
- **Entry markers**: Upward triangle (▲) for long entry (green), downward triangle (▼) for short entry (red) — positioned at the entry bar's close price
- **Exit markers**: Diamond (◆) colored by exit reason:
  - TP = blue
  - SL = orange
  - TRAILING_BE = gray
  - TIME_BARRIER = purple
- **TP/SL lines**: When hovering a trade marker, draw horizontal lines at TP and SL levels (calculated from `entry_price ± atr_at_entry × mult`)
- **Lazy loading**: Fetch OHLCV data for the visible range + buffer. On pan/zoom, fetch new chunks from `/api/ohlcv?start=...&end=...`

##### 5. Sub-panel: Signal Probability
- Line chart (0-1 range) showing `prob_Buy` (green) and/or `prob_Sell` (red) over time
- Horizontal dashed line at the strategy's `entry_threshold` (from run metadata)
- Synced x-axis with the main candlestick chart

##### 6. Sub-panel: Equity Curve
- Line chart of the equity curve from the run
- Shade drawdown periods in light red
- Synced x-axis with the main candlestick chart

##### 7. Trade Table (Bottom Panel)
- Sortable, scrollable table of all trades in the selected run
- Columns: `#`, `Entry Time`, `Exit Time`, `Side`, `Entry Price`, `Exit Price`, `Exit Reason`, `Duration (bars)`, `Net PnL`, `Lots`
- Color-code rows: green for winning trades, red for losing
- **Click-to-jump**: Clicking a trade row scrolls/zooms the chart to center on that trade's entry→exit range
- Keyboard nav: ↑/↓ arrows to cycle through trades

#### Interactions

1. **Click trade row** → Chart zooms to show entry time − 2 hours to exit time + 2 hours. Entry/exit markers and TP/SL lines are highlighted.
2. **Click chart marker** → Highlight the corresponding trade in the table, scroll the table to that row.
3. **Pan/zoom chart** → Lazy-load OHLCV data and probability data for the new visible range.
4. **Hover trade marker** → Tooltip showing: entry/exit prices, PnL, duration, exit reason.

---

## File Inventory

| File | Action | Description |
|---|---|---|
| [agent/backtest_engine.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py) | MODIFY | Add `to_dataframe()`, `export_trades()` to [BacktestResult](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/backtest_engine.py#99-169). Add `--export-dir` to CLI. Add `run_metadata.json` export. |
| `agent/trade_visualizer_server.py` | NEW | Python HTTP server with REST endpoints for runs, trades, OHLCV, signals |
| [index.html](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/index.html) | OVERWRITE | Single-page trade visualizer app |
| `static/visualizer.css` | NEW | Styles for the visualizer (dark theme, responsive panels) |
| `static/visualizer.js` | NEW | Application logic, chart setup, data fetching, trade table |

## Design Constraints

1. **No build step** — The frontend must work by opening [index.html](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/index.html) via the Python server. No npm/webpack/vite.
2. **Dark theme** — Use a dark background (#1a1a2e or similar) with high-contrast text. Financial chart UIs should look like a Bloomberg terminal, not a social media app.
3. **Performance** — The OHLCV data has ~870K rows. Never send more than ~5,000 bars to the frontend at once. Use server-side date filtering and optional downsampling.
4. **Startup** — Single command: `conda run -n trader python agent/trade_visualizer_server.py` → opens browser to `http://localhost:8050`
5. **Data resolution via `CL_DATA_ROOT`** — Use `src/data_paths.py:get_data_path()` for resolving all file paths (supports worktree environments).

## Verification Plan

1. Run backtest with `--export-dir` to generate trade CSVs for both Buy and Sell models
2. Start the data server and verify all API endpoints return correct JSON
3. Open the visualizer and verify:
   - Run list populates with available runs and shows correct metrics on each card
   - Clicking a run loads its trades with correct markers on the chart
   - Click-to-jump from trade table works
   - Signal probability panel shows correct threshold line
   - Chart is lazy-loaded — panning far should fetch new OHLCV data
   - Filters (win/loss, side, exit reason) correctly filter markers and table
