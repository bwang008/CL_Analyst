import pandas as pd
import numpy as np
import os
from sklearn.datasets import make_classification

def generate_sklearn_data():
    output_dir = "data"
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "cl-5m_bk_sklearn_clean.parquet")
    
    print("Generating sklearn classification dataset (90,000 samples, 30 features)...")
    # 90,000 samples at 1-hour frequency is >10 years (10 years * 365.25 * 24 = 87660)
    X, y = make_classification(
        n_samples=90000,
        n_features=30,
        n_informative=15,
        n_redundant=5,
        n_classes=2,
        random_state=42
    )
    
    # Create DatetimeIndex (1-hour frequency ending at 2026-04-25)
    end_date = pd.Timestamp("2026-04-25 00:00:00")
    start_date = end_date - pd.Timedelta(hours=90000 - 1)
    date_index = pd.date_range(start=start_date, end=end_date, freq='h')
    
    # Create DataFrame
    df = pd.DataFrame(X, index=date_index, columns=[f"FI_{i:02d}" for i in range(1, 31)])
    
    # Add target columns (LONG = class 1, SHORT = class 0)
    df["TARGET_TRIPLE_2x1_24H_LONG"] = y
    df["TARGET_TRIPLE_2x1_24H_SHORT"] = 1 - y
    
    # Add dummy OHLCV and ATR using a random walk
    print("Synthesizing dummy OHLCV data...")
    returns = np.random.normal(0, 0.001, 90000)
    prices = 100 * np.exp(np.cumsum(returns))
    
    df["Open"] = prices
    df["High"] = prices * 1.002
    df["Low"] = prices * 0.998
    df["Close"] = prices * (1 + np.random.normal(0, 0.0005, 90000))
    df["Volume"] = np.random.randint(100, 10000, 90000)
    
    # Dummy ATR to satisfy volatility filters
    df["VOL_ATR_14"] = prices * 0.005 
    
    # Save to parquet
    print(f"Saving to {output_path}...")
    df.to_parquet(output_path)
    print("Done! Shape:", df.shape)
    print("Data time range:", df.index.min(), "to", df.index.max())

if __name__ == "__main__":
    generate_sklearn_data()
