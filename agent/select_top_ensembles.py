import os
import re
import json
import math
import argparse
import pandas as pd
import sys

def parse_markdown_table(md_path):
    # Parse markdown table to pandas dataframe
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    # Find start of table
    table_lines = []
    in_table = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('|') and 'Long Model' in line:
            in_table = True
            table_lines.append(line)
        elif in_table and line.startswith('|'):
            # Skip separator line
            if set(line.strip('|').replace('-', '').replace(' ', '').replace(':', '')) == set():
                continue
            table_lines.append(line)
            
    if not table_lines:
        return pd.DataFrame()
        
    # Process lines
    rows = []
    headers = [col.strip() for col in table_lines[0].strip('|').split('|')]
    
    for line in table_lines[1:]:
        cols = [col.strip() for col in line.strip('|').split('|')]
        rows.append(cols)
        
    df = pd.DataFrame(rows, columns=headers)
    return df

def main():
    parser = argparse.ArgumentParser(description="Select Top 8 Ensembles from Markdown")
    parser.add_argument("--md-report", required=True, help="Path to batch_ensemble_pre_opt.md")
    parser.add_argument("--output-json", required=True, help="Path to output top_8_ensembles.json")
    parser.add_argument("--top-n", type=int, default=8, help="Number of ensembles to select")
    parser.add_argument("--min-signals", type=int, default=360,
                        help="Minimum signal count floor (frictionless mode). "
                             "Default 360 (~2 signals/week over 3.5 years)")
    args = parser.parse_args()

    if not os.path.exists(args.md_report):
        print(f"Error: {args.md_report} not found.")
        return

    df = parse_markdown_table(args.md_report)
    if df.empty:
        print("Error: Could not parse any data from the markdown table.")
        return

    # Detect format: new frictionless (SNR_24H column) vs legacy (Net PnL / Profit Factor)
    is_frictionless = 'SNR_24H' in df.columns

    if is_frictionless:
        _select_frictionless(df, args)
    else:
        _select_legacy(df, args)


def _select_frictionless(df, args):
    """New frictionless selection path: rank by Peak SNR."""
    snr_cols = ['SNR_6H', 'SNR_12H', 'SNR_24H', 'SNR_48H', 'SNR_72H']

    parsed_data = []
    for _, row in df.iterrows():
        long_model = row['Long Model']
        short_model = row['Short Model']

        try:
            signals = int(row['Signals'])
        except (ValueError, KeyError):
            signals = 0

        try:
            peak_horizon = int(row['Peak Horizon'])
        except (ValueError, KeyError):
            peak_horizon = 0

        snr_values = {}
        for col in snr_cols:
            try:
                snr_values[col] = float(row[col])
            except (ValueError, KeyError):
                snr_values[col] = 0.0

        try:
            ic = float(row['IC'])
        except (ValueError, KeyError):
            ic = 0.0

        peak_snr = max(snr_values.values())

        parsed_data.append({
            'long_prefix': long_model,
            'short_prefix': short_model,
            'signals': signals,
            'peak_snr': peak_snr,
            'peak_horizon': peak_horizon,
            'ic': ic,
        })

    pdf = pd.DataFrame(parsed_data)

    # Filter: minimum signal count and positive peak SNR
    filtered_df = pdf[(pdf['signals'] >= args.min_signals) & (pdf['peak_snr'] > 0)].copy()

    # Rank by Peak SNR descending
    sorted_df = filtered_df.sort_values(by='peak_snr', ascending=False)

    top_n = sorted_df.head(args.top_n)

    if top_n.empty:
        print("CRITICAL: No valid ensembles found!")
        sys.exit(1)

    output_data = []
    for _, row in top_n.iterrows():
        output_data.append({
            "target_long": row['long_prefix'],
            "target_short": row['short_prefix'],
            "signals": int(row['signals']),
            "peak_snr": round(float(row['peak_snr']), 6),
            "peak_horizon": int(row['peak_horizon']),
            "ic": round(float(row['ic']), 6),
            "obj_score": round(float(row['peak_snr']), 6),
        })

    print(f"Selected Top {len(output_data)} ensembles (frictionless mode).")
    for rank, entry in enumerate(output_data, 1):
        print(f"{rank}. Long: {entry['target_long']} | Short: {entry['target_short']} "
              f"| Signals: {entry['signals']} | Peak SNR: {entry['peak_snr']:.6f} "
              f"| Horizon: {entry['peak_horizon']}H | IC: {entry['ic']:.4f}")

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
    with open(args.output_json, 'w') as f:
        json.dump(output_data, f, indent=4)

    print(f"Saved to {args.output_json}")


def _select_legacy(df, args):
    """Legacy selection path: rank by pnl * sqrt(trades)."""
    # Columns from sweep_ensembles.py DataFrame: 'Long Model', 'Short Model', 'Trades', 'Win Rate', 'Profit Factor', 'Net PnL', 'Max DD', 'Tail PnL'
    # Also handle legacy abbreviated names: 'Trds', 'WR%', 'PF'
    # Parse 'Trds' to int, 'Net PnL' to float
    
    parsed_data = []
    for _, row in df.iterrows():
        long_model = row['Long Model']
        short_model = row['Short Model']
        
        try:
            trades_col = 'Trades' if 'Trades' in row.index else 'Trds'
            trades = int(row[trades_col])
        except (ValueError, KeyError):
            trades = 0

        pnl_col = 'Net PnL' if 'Net PnL' in row.index else 'PnL'
        pnl_str = str(row[pnl_col]).replace(',', '').replace('$', '')
        try:
            pnl = float(pnl_str)
        except ValueError:
            pnl = 0.0
            
        parsed_data.append({
            'long_prefix': long_model,
            'short_prefix': short_model,
            'trades': trades,
            'pnl': pnl
        })
        
    pdf = pd.DataFrame(parsed_data)
    
    # 1. Filter: Drop any combinations where Holdout_Trade_Count < 30 or Total_PnL <= 0.
    filtered_df = pdf[(pdf['trades'] >= 30) & (pdf['pnl'] > 0)].copy()
    
    # 2. Score & Sort: Create a new column called Obj_Score calculated as: Total_PnL * math.sqrt(Holdout_Trade_Count).
    filtered_df['obj_score'] = filtered_df['pnl'] * filtered_df['trades'].apply(math.sqrt)
    
    # Sort the remaining models descending by Obj_Score and select the Top 8.
    sorted_df = filtered_df.sort_values(by='obj_score', ascending=False)
    
    top_n = sorted_df.head(args.top_n)
    
    if top_n.empty:
        print("CRITICAL: No valid ensembles found!")
        sys.exit(1)
    
    output_data = []
    for _, row in top_n.iterrows():
        output_data.append({
            "target_long": row['long_prefix'],
            "target_short": row['short_prefix'],
            "trades": row['trades'],
            "pnl": row['pnl'],
            "obj_score": row['obj_score']
        })
        
    print(f"Selected Top {len(output_data)} ensembles.")
    for rank, entry in enumerate(output_data, 1):
        print(f"{rank}. Long: {entry['target_long']} | Short: {entry['target_short']} | Trades: {entry['trades']} | PnL: {entry['pnl']:.2f} | Score: {entry['obj_score']:.2f}")

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
    with open(args.output_json, 'w') as f:
        json.dump(output_data, f, indent=4)
        
    print(f"Saved to {args.output_json}")

if __name__ == "__main__":
    main()
