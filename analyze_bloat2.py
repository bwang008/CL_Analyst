import pandas as pd
from src.util import get_feature_columns
from collections import Counter

def main():
    df = pd.read_parquet('data/cl-1h_bk_HourSet_02.parquet')
    
    with open('feature_audit.txt', 'w') as f:
        f.write(f"DataFrame shape: {df.shape}\n")
        f.write(f"Total columns in parquet: {len(df.columns)}\n\n")
        
        feats = get_feature_columns(df)
        f.write(f"Total features identified by get_feature_columns: {len(feats)}\n\n")
        
        prefixes = Counter()
        for c in df.columns: # count everything just in case
            if '_' in c:
                prefixes[c.split('_')[0]] += 1
            else:
                prefixes['NO_PREFIX'] += 1
                
        f.write("All Column Prefix Summary:\n")
        for k, v in prefixes.most_common():
            f.write(f"  {k}_*: {v}\n")
            
        f.write("\nDetail for top prefixes (checking for bloat):\n")
        for k, v in prefixes.most_common(10):
            samples = [c for c in df.columns if c.startswith(k + '_')]
            f.write(f"-- {k} Examples (total {v}):\n")
            f.write("   " + ", ".join(samples[:10]) + ("..." if len(samples) > 10 else "") + "\n")

if __name__ == '__main__':
    main()
