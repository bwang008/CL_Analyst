---
name: grab-data
description: Download historical futures data from Databento and IBKR, validate parity between sources
---

# /grab-data — Multi-Symbol Futures Data Acquisition

Download historical OHLCV futures data from Databento (training source) and IBKR (live execution source), then cross-reference them with a parity check.

> [!IMPORTANT]
> **Hourly-only ruling (T7, user ruling):** data acquisition in this repo is **HOURLY-ONLY** —
> never acquire 5m data. (Live 5m STREAMING is unaffected: seedless symbols shallow-bootstrap
> their live 5m window from IBKR at startup — no historical 5m purchase involved.)

## Prerequisites

- `DATABENTO_API_KEY` set in `.env`
- IB Gateway running on port 4002 (paper) or 4001 (live)
- `CL_DATA_ROOT` environment variable set (data storage root)

## Supported Symbols

| Symbol | Name | Databento Code | IBKR Exchange |
|--------|------|----------------|---------------|
| CL | Crude Oil | `CL.v.0` | NYMEX |
| HG | Copper | `HG.v.0` | COMEX |
| PA | Palladium | `PA.v.0` | NYMEX |
| GC | Gold | `GC.v.0` | COMEX |
| NG | Natural Gas | `NG.v.0` | NYMEX |
| ES | E-mini S&P 500 | `ES.v.0` | CME |
| NQ | E-mini Nasdaq 100 | `NQ.v.0` | CME |
| ZC | Corn | `ZC.v.0` | CBOT |
| ZS | Soybeans | `ZS.v.0` | CBOT |
| SI | Silver | `SI.v.0` | COMEX |

---

## Step 1: Estimate Databento Cost (Free)

Before spending credits, always estimate first:

```powershell
python -m src.data.databento_data_builder estimate --symbols ES NG --start 2023-06-26 --end 2026-06-25
```

> [!IMPORTANT]
> Databento CME data has a ~1 hour publication lag. The `--end` date must be at least 1 day before today. The canary command handles this automatically.

## Step 2: Databento Canary Download (30 Days)

Run a small test pull to verify data format before committing to a large download:

```powershell
python -m src.data.databento_data_builder canary --symbols ES NG --days 30
```

This will:
1. Submit **one batch per symbol** (separate CSV per symbol)
2. Download to `$CL_DATA_ROOT/data/raw/DataBentoSample/{SYMBOL}/`
3. Validate: columns, prices ÷ 1e9, UTC hourly timestamps, row count

**Output structure:**
```
$CL_DATA_ROOT/data/raw/DataBentoSample/
├── ES/
│   └── GLBX-20260627-XXXXXXXX/
│       ├── glbx-mdp3-YYYYMMDD-YYYYMMDD.ohlcv-1h.csv  (raw Databento)
│       ├── metadata.json
│       └── condition.json
├── NG/
│   └── ...
```

**Review:** Check the canary results. All symbols should show PASS.

## Step 3: Convert to Pipeline Format

After canary passes, convert raw Databento CSVs to the pipeline format:

```powershell
python -m src.data.databento_data_builder convert <path-to-raw.csv> --symbol ES --outdir $env:CL_DATA_ROOT\data\raw\DataBentoSample\ES
```

This produces three adjustment variants:
- `{SYMBOL}_raw.csv` — unadjusted (rollover gaps visible)
- `{SYMBOL}_ratio.csv` — backward ratio-adjusted (multiplicative)
- `{SYMBOL}_panama.csv` — backward additive (Panama Canal)

All in pipeline format: `DD/MM/YYYY;HH:MM;O;H;L;C;V` (semicolon-separated, no headers).

## Step 4: IBKR Sample Download (Free)

Download 30 days of hourly bars from IBKR for cross-reference:

```powershell
python scripts/download_ibkr_multi_history.py --symbols ES NG --port 4002 --days 30
```

This will:
1. Connect to IB Gateway (paper account on port 4002)
2. Qualify `ContFuture` contracts for each symbol
3. Download 30 days of 1-hour bars
4. Save to `$CL_DATA_ROOT/data/raw/ibkr_samples/{SYMBOL}_ibkr_1h.csv`
5. 12-second throttle between symbols (IBKR pacing)

**What this also validates:**
- Market data subscriptions exist for each symbol
- Contract qualification works (confirms exchange routing)
- If a symbol fails here, you're missing that exchange's data package in IBKR

## Step 5: Parity Check (Databento vs IBKR)

Compare the two data sources:

```powershell
python scripts/download_ibkr_multi_history.py --skip-download --parity-check $env:CL_DATA_ROOT\data\raw\DataBentoSample --symbols ES NG
```

This computes per-symbol:
- Number of overlapping hourly bars
- Mean/max absolute Close price error
- Mean percentage Close error
- Pearson correlation coefficient

**Expected:** < 0.5% mean Close divergence (small differences are normal due to different continuous contract roll timing).

> [!WARNING]
> If any symbol shows > 1% Close divergence, investigate before training models on that symbol's Databento data. Roll date differences between Databento and IBKR are the most common cause.

## Step 6: Full Databento Download (3-Year or Full History)

After canary + parity check pass:

```powershell
# 3-year test pull
python scripts/download_futures_data.py submit --symbols ES NG --years 3

# Or full history (earliest available to yesterday)
python scripts/download_futures_data.py submit --symbols ES NG --full
```

Files download to `$CL_DATA_ROOT/data/raw/DataBentoSample/{SYMBOL}/` and ratio-adjusted copies are placed at `$CL_DATA_ROOT/data/raw/{SYMBOL}.csv` for the pipeline.

## Step 7: Verify Pipeline Compatibility

For a NEW symbol, first complete the full instrument registration and its blocking registry gate —
see [build-symbol-pipeline](build-symbol-pipeline.md) Phase 0 (all 17 registry fields + GATE 0).

Load the converted data through the pipeline to confirm feature engineering works:

```python
from src.data_processor import DataProcessor
dp = DataProcessor(input_path="path/to/{SYMBOL}_ratio.csv")
df = dp.load_data()
print(df.shape, df.columns.tolist()[:10])
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Cost estimate | `python -m src.data.databento_data_builder estimate --symbols <SYMS>` |
| Canary (30d) | `python -m src.data.databento_data_builder canary --symbols <SYMS> --days 30` |
| Convert raw | `python -m src.data.databento_data_builder convert <CSV> --symbol <SYM>` |
| IBKR download | `python scripts/download_ibkr_multi_history.py --symbols <SYMS>` |
| Parity check | `python scripts/download_ibkr_multi_history.py --skip-download --parity-check <DIR>` |
| Full download | `python scripts/download_futures_data.py submit --symbols <SYMS> --years 3` |

## Key Files

| File | Purpose |
|------|---------|
| `src/data/databento_data_builder.py` | Core Databento pipeline (download, convert, validate) |
| `scripts/download_futures_data.py` | Convenience CLI for multi-symbol Databento downloads |
| `scripts/download_ibkr_multi_history.py` | IBKR multi-symbol download + parity checker |
| `scripts/download_ibkr_history.py` | Legacy CL-only IBKR downloader |
