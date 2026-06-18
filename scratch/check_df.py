import pandas as pd
p1 = pd.read_csv('reports/sweep_hs11_3x1_24h_20260615_0622/registry/canary_output/extended_predictions_long_ap.csv', index_col=0, parse_dates=True)
p2 = pd.read_csv('reports/sweep_hs11_3x1_6h_20260615_0622/registry/canary_output/extended_predictions_short_ll.csv', index_col=0, parse_dates=True)

for ts in ["2026-04-30 04:00:00", "2026-04-30 05:00:00", "2026-04-30 06:00:00", "2026-05-04 00:00:00"]:
    print(ts)
    if ts in p1.index:
        print("Long:", p1.loc[ts].to_dict())
    else:
        print("Long: missing")
    if ts in p2.index:
        print("Short:", p2.loc[ts].to_dict())
    else:
        print("Short: missing")
    print("---")
