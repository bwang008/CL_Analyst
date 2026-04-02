from typing import Optional

def walk_forward_folds(
    n_total: int,
    min_train: int = 8640,
    fold_size: int = 8640,
    purge: int = 576,
    lookback_window_years: Optional[int] = None,
    bars_per_year: int = 105120,
) -> list[tuple[int, int, int, int]]:
    """Generate walk-forward expanding-window or rolling-window fold indices."""
    folds = []
    test_start = min_train + purge
    
    while test_start + fold_size <= n_total:
        train_end = test_start - purge
        test_end = test_start + fold_size
        
        if lookback_window_years is None:
            train_start = 0
        else:
            rolling_bars = lookback_window_years * bars_per_year
            train_start = max(0, train_end - rolling_bars)
            
        folds.append((train_start, train_end, test_start, test_end))
        test_start += fold_size
        
    return folds

print("--- Expanding Window (lookback=None) ---")
folds_expand = walk_forward_folds(1500000, lookback_window_years=None)
for i, f in enumerate(folds_expand[:3]):
    print(f"Fold {i}: Train[{f[0]}:{f[1]}] (len={f[1]-f[0]}) | Val[{f[2]}:{f[3]}]")
print(f"...\nFold -1: Train[{folds_expand[-1][0]}:{folds_expand[-1][1]}] (len={folds_expand[-1][1]-folds_expand[-1][0]}) | Val[{folds_expand[-1][2]}:{folds_expand[-1][3]}]")

print("\n--- Rolling Window (lookback=5 years) ---")
folds_roll = walk_forward_folds(1500000, lookback_window_years=5)
for i, f in enumerate(folds_roll[:3]):
    print(f"Fold {i}: Train[{f[0]}:{f[1]}] (len={f[1]-f[0]}) | Val[{f[2]}:{f[3]}]")
print(f"...\nFold -1: Train[{folds_roll[-1][0]}:{folds_roll[-1][1]}] (len={folds_roll[-1][1]-folds_roll[-1][0]}) | Val[{folds_roll[-1][2]}:{folds_roll[-1][3]}]")
