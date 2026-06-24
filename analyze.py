import json
import glob
import os
import re
import pandas as pd
import numpy as np

batches = [
    'reports/batch_runs/batch_20260621_1418_HS13A_SCOUT',
    'reports/batch_runs/batch_20260620_0028_HS13B_SCOUT',
    'reports/batch_runs/batch_20260618_1721_HS13A_SCOUT_370'
]

def extract_target_name(key_part):
    # key_part looks like 'HS13A 2x1 12H'
    return key_part.split(' ', 1)[1]

stats = {}

for batch in batches:
    for obj in ['sharpe', 'sortino']:
        fpath = os.path.join(batch, f'optimization_results_{obj}.json')
        if not os.path.exists(fpath): continue
        with open(fpath) as f:
            d = json.load(f)
            for k, v in d.items():
                parts = k.split('|')
                if len(parts) == 3:
                    t_full, side, loss = parts
                    t_name = extract_target_name(t_full)
                    sharpe = v['metrics']['sharpe_ratio']
                    sortino = v['metrics']['sortino_ratio']
                    
                    if t_name not in stats:
                        stats[t_name] = {'long': {'sharpe': [], 'sortino': []}, 'short': {'sharpe': [], 'sortino': []}}
                    
                    stats[t_name][side]['sharpe'].append(sharpe)
                    stats[t_name][side]['sortino'].append(sortino)

agg = []
for t_name, s in stats.items():
    long_sh = np.mean(s['long']['sharpe']) if s['long']['sharpe'] else 0
    long_so = np.mean(s['long']['sortino']) if s['long']['sortino'] else 0
    short_sh = np.mean(s['short']['sharpe']) if s['short']['sharpe'] else 0
    short_so = np.mean(s['short']['sortino']) if s['short']['sortino'] else 0
    agg.append({
        'target': t_name,
        'long_sharpe': long_sh,
        'long_sortino': long_so,
        'short_sharpe': short_sh,
        'short_sortino': short_so
    })

df = pd.DataFrame(agg)
print("--- TOP LONG ---")
print(df.sort_values('long_sharpe', ascending=False).head(8).to_string())
print("\n--- TOP SHORT ---")
print(df.sort_values('short_sortino', ascending=False).head(8).to_string())

# Find prediction files to compute correlations
# We'll map each target to its long/short series by looking at the ensembles.
def get_ensemble_mapping(batch_path):
    # read batch_summary_optimized_ensembles_sharpe.md
    mapping = {}
    fpath = os.path.join(batch_path, 'batch_summary_optimized_ensembles_sharpe.md')
    if not os.path.exists(fpath): return mapping
    with open(fpath) as f:
        lines = f.readlines()
    
    # parse the table
    # | 1 | HS13B 2x1 6H / HS13B 2x1 3H  | LL_LONG (HS13B 2x1 6H) | LL_SHORT (HS13B 2x1 3H) | ...
    for line in lines:
        if line.startswith('|') and 'LONG' in line and 'SHORT' in line:
            parts = [x.strip() for x in line.split('|')]
            if len(parts) > 4 and parts[1].isdigit():
                idx = int(parts[1])
                long_model_col = parts[3]
                short_model_col = parts[4]
                
                # LL_LONG (HS13B 2x1 6H) -> HS13B 2x1 6H
                m1 = re.search(r'\((.*?)\)', long_model_col)
                m2 = re.search(r'\((.*?)\)', short_model_col)
                if m1 and m2:
                    long_target = extract_target_name(m1.group(1))
                    short_target = extract_target_name(m2.group(1))
                    mapping[idx] = {'long': long_target, 'short': short_target}
    return mapping

target_series_long = {}
target_series_short = {}

for batch in batches:
    mapping = get_ensemble_mapping(batch)
    pred_dir = os.path.join(batch, 'predictions')
    if not os.path.exists(pred_dir): continue
    
    csv_files = glob.glob(os.path.join(pred_dir, '*_E*_predictions.csv'))
    for csv_file in csv_files:
        m = re.search(r'E0*(\d+)_predictions\.csv', csv_file)
        if m:
            idx = int(m.group(1))
            if idx in mapping:
                df_pred = pd.read_csv(csv_file)
                long_target = mapping[idx]['long']
                short_target = mapping[idx]['short']
                
                if long_target not in target_series_long:
                    target_series_long[long_target] = df_pred['prob_Buy'].values
                if short_target not in target_series_short:
                    target_series_short[short_target] = df_pred['prob_Sell'].values

print("\n--- Correlations ---")
def print_corr(series_dict, top_targets):
    mat = []
    for t1 in top_targets:
        row = []
        for t2 in top_targets:
            if t1 in series_dict and t2 in series_dict:
                l1 = len(series_dict[t1])
                l2 = len(series_dict[t2])
                m_len = min(l1, l2)
                corr = np.corrcoef(series_dict[t1][:m_len], series_dict[t2][:m_len])[0, 1]
                row.append(corr)
            else:
                row.append(np.nan)
        mat.append(row)
    df_corr = pd.DataFrame(mat, index=top_targets, columns=top_targets)
    print(df_corr)

top_long = df.sort_values('long_sharpe', ascending=False)['target'].head(8).tolist()
top_short = df.sort_values('short_sortino', ascending=False)['target'].head(8).tolist()

print("\nLong Corrs:")
print_corr(target_series_long, top_long)
print("\nShort Corrs:")
print_corr(target_series_short, top_short)
