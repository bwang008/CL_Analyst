import json
import os
import pandas as pd
import numpy as np

# 1. Load Performance Data
f_sharpe = 'reports/batch_runs/batch_20260621_1418_HS13A_SCOUT/optimization_results_sharpe.json'
f_sortino = 'reports/batch_runs/batch_20260621_1418_HS13A_SCOUT/optimization_results_sortino.json'

data = []
def process_file(fpath):
    with open(fpath) as f:
        d = json.load(f)
        for k, v in d.items():
            parts = k.split('|')
            if len(parts) == 3:
                target_full, side, loss = parts
                target = target_full.split(' ', 1)[1]
                try:
                    trades = v['optuna_info']['metrics']['trade_count']
                    opt_pnl = v['optuna_info']['metrics']['total_pnl']
                    ho_pnl = v['optuna_info']['holdout_metrics']['total_pnl']
                    data.append({
                        'target': target,
                        'target_col': f"TARGET_TRIPLE_{target.replace(' ', '_')}_{side.upper()}",
                        'side': side,
                        'trades': trades,
                        'opt_pnl': opt_pnl,
                        'ho_pnl': ho_pnl,
                        'total_pnl': opt_pnl + ho_pnl
                    })
                except KeyError:
                    pass

process_file(f_sharpe)
process_file(f_sortino)

df = pd.DataFrame(data)
# Filter positive PnL and sort by trades (primary) and total_pnl (secondary)
df_valid = df[(df['opt_pnl'] > 0) & (df['ho_pnl'] > 0)]
df_valid = df_valid.sort_values(by=['trades', 'total_pnl'], ascending=[False, False]).drop_duplicates(subset=['target_col'])

df_long = df_valid[df_valid['side'] == 'long'].copy()
df_short = df_valid[df_valid['side'] == 'short'].copy()

# 2. Load 14A dataset
parquet_path = 'data/processed/CL_HourSet_14A.parquet'
if not os.path.exists(parquet_path):
    parquet_path = 'data/processed/cl-5m_bk_HourSet_14A.parquet'
    
df_data = pd.read_parquet(parquet_path)

# Jaccard Function
def jaccard(a, b):
    mask = ~(np.isnan(a) | np.isnan(b))
    a_valid = a[mask] > 0
    b_valid = b[mask] > 0
    intersection = np.sum(a_valid & b_valid)
    union = np.sum(a_valid | b_valid)
    if union == 0: return 0.0
    return intersection / union

def select_orthogonal(df_candidates, side):
    selected = []
    dropped = []
    for _, row in df_candidates.iterrows():
        col = row['target_col']
        if col not in df_data.columns:
            continue
        
        arr = df_data[col].values
        
        is_redundant = False
        for s in selected:
            s_arr = df_data[s['target_col']].values
            j_score = jaccard(arr, s_arr)
            if j_score > 0.60:
                is_redundant = True
                dropped.append((row['target'], s['target'], j_score))
                break
        
        if not is_redundant:
            selected.append(row.to_dict())
            if len(selected) == 8:
                break
                
    return selected, dropped

long_selected, long_dropped = select_orthogonal(df_long, 'long')
short_selected, short_dropped = select_orthogonal(df_short, 'short')

print('--- DROPPED DUE TO JACCARD > 0.60 ---')
for l in long_dropped: print(f'LONG: Dropped {l[0]} (correlated {l[2]:.2f} with {l[1]})')
for s in short_dropped: print(f'SHORT: Dropped {s[0]} (correlated {s[2]:.2f} with {s[1]})')

def print_matrix(selected, name):
    print(f'\n--- {name} JACCARD MATRIX ---')
    labels = [s['target'] for s in selected]
    n = len(labels)
    mat = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            a = df_data[selected[i]['target_col']].values
            b = df_data[selected[j]['target_col']].values
            mat[i, j] = jaccard(a, b)
    
    df_mat = pd.DataFrame(mat, index=labels, columns=labels)
    print(df_mat.round(3).to_string())

print_matrix(long_selected, 'LONG')
print_matrix(short_selected, 'SHORT')

# Generate isolated manifest elements
manifest_entries = []
for s in long_selected:
    manifest_entries.append({
        'label': f"HS14A {s['target']} LONG",
        'target_long': s['target_col']
    })
for s in short_selected:
    manifest_entries.append({
        'label': f"HS14A {s['target']} SHORT",
        'target_short': s['target_col']
    })

import sys
with open('configs/sweep_batch_short_hourset14a_scout_cut.json', 'r') as f:
    manifest = json.load(f)

manifest['experiments'] = manifest_entries

with open('configs/sweep_batch_short_hourset14a_scout_cut.json', 'w') as f:
    json.dump(manifest, f, indent=2)

print("\n--- Manifest Updated ---")
