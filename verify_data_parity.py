import asyncio
import pandas as pd
import yfinance as yf
from ib_insync import IB, Index
from pathlib import Path
import os
import sys

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent))

from src.data_paths import get_data_path

async def fetch_ibkr_data():
    ib = IB()
    try:
        # Use the same port as the user's command: 4002
        await ib.connectAsync('127.0.0.1', 4002, clientId=999)
    except Exception as e:
        print(f"Failed to connect to IBKR on port 4002: {e}")
        return None

    results = {}
    for sym in ["VIX", "OVX"]:
        contract = Index(sym, "CBOE", "USD")
        contract = await ib.qualifyContractsAsync(contract)
        if not contract:
            print(f"Failed to qualify {sym}")
            continue
        contract = contract[0]
        
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="10 D",  # get extra to ensure 5 trading days
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            keepUpToDate=False,
        )
        if bars:
            df = pd.DataFrame([{
                'Date': b.date if type(b.date) is not str else pd.to_datetime(b.date).date(),
                f'{sym}_Live': b.close
            } for b in bars])
            # Filter out today's live incomplete bar if it's there
            today = pd.Timestamp.now("America/New_York").date()
            df = df[df['Date'] != today]
            results[sym] = df.set_index('Date').tail(5)
            
    ib.disconnect()
    
    if len(results) == 2:
        return pd.concat(results.values(), axis=1)
    return pd.DataFrame()

def fetch_yf_data():
    dx_ticker = yf.Ticker("DX-Y.NYB")
    dx_hist = dx_ticker.history(period="10d")
    if dx_hist.empty:
        return pd.DataFrame()
        
    df = pd.DataFrame({
        'Date': dx_hist.index.tz_convert('America/New_York').date,
        'DXY_Live': dx_hist['Close'].values
    })
    
    today = pd.Timestamp.now("America/New_York").date()
    df = df[df['Date'] != today]
    return df.set_index('Date').tail(5)

def get_fred_data():
    fred_path = get_data_path("raw/macro/fred_macro_data.csv")
    df = pd.read_csv(fred_path, parse_dates=['Date'])
    df['Date'] = df['Date'].dt.date
    df = df.set_index('Date')
    df = df.rename(columns={'VIX': 'VIX_FRED', 'OVX': 'OVX_FRED', 'DXY': 'DXY_FRED'})
    return df[['VIX_FRED', 'OVX_FRED', 'DXY_FRED']].dropna()

async def main():
    print("Fetching IBKR data (VIX, OVX)...")
    ibkr_df = await fetch_ibkr_data()
    
    print("Fetching yfinance data (DXY)...")
    yf_df = fetch_yf_data()
    
    print("Loading FRED data...")
    fred_df = get_fred_data()
    
    # Merge live data
    if not ibkr_df.empty and not yf_df.empty:
        live_df = ibkr_df.join(yf_df, how='outer')
    elif not ibkr_df.empty:
        live_df = ibkr_df
    elif not yf_df.empty:
        live_df = yf_df
    else:
        print("Failed to fetch any live data.")
        return

    # Merge FRED with live
    merged = fred_df.join(live_df, how='inner').tail(5)
    
    if merged.empty:
        print("No overlapping dates found between FRED and Live data.")
        print("Live dates:", live_df.index.tolist())
        print("FRED dates:", fred_df.index[-5:].tolist())
        return
        
    print("\n--- Side-by-Side Comparison ---")
    
    for sym in ['VIX', 'OVX', 'DXY']:
        if f'{sym}_Live' in merged.columns and f'{sym}_FRED' in merged.columns:
            comp = merged[[f'{sym}_FRED', f'{sym}_Live']].copy()
            comp['Abs_Err'] = (comp[f'{sym}_Live'] - comp[f'{sym}_FRED']).abs()
            comp['Pct_Err'] = (comp['Abs_Err'] / comp[f'{sym}_FRED']) * 100
            
            print(f"\n{sym} Comparison:")
            print(comp.to_string(float_format=lambda x: f"{x:.4f}"))

if __name__ == "__main__":
    asyncio.run(main())
