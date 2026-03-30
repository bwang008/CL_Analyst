import pandas as pd
from src.util import get_feature_columns
from collections import Counter

def main():
    print("Loading dataframe...")
    df = pd.read_parquet('data/cl-1h_bk_HourSet_02.parquet')
    print(f"DataFrame shape: {df.shape}")
    
    feats = get_feature_columns(df)
    print(f"Total features identified by get_feature_columns: {len(feats)}")
    
    # Check for prefix counts
    prefixes = Counter()
    for f in feats:
        if '_' in f:
            prefixes[f.split('_')[0]] += 1
        else:
            prefixes['NO_PREFIX'] += 1
            
    print("\nFeature Prefix Summary:")
    for k, v in prefixes.most_common():
        print(f"  {k}_*: {v}")
        
    print("\nDetailed sample of potential bloat:")
    for k, v in prefixes.most_common(5):
        if v > 20: # arbitrary threshold for bloat
            samples = [f for f in feats if f.startswith(k + '_')]
            print(f"-- {k} Examples (total {v}):")
            print("   " + ", ".join(samples[:10]))
            if len(samples) > 10:
                print("   ...")

if __name__ == '__main__':
    main()
