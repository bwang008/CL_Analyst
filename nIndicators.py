import pandas as pd
import pandas_ta as ta

def generate_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Generates a DataFrame of technical analysis features from an OHLCV DataFrame.

    This function uses the pandas-ta library to calculate various indicators and
    combines them into a single feature set.

    Args:
        df (pd.DataFrame): A DataFrame with columns ['Open', 'High', 'Low', 'Close', 'Volume'].
                           The index should be a DatetimeIndex.

    Returns:
        pd.DataFrame: A new DataFrame containing the calculated indicators.
    """
    # Create a new DataFrame to store the features
    features = pd.DataFrame(index=df.index)

    # --- Use pandas-ta to calculate standard indicators ---
    # The .ta accessor is added to the DataFrame by the pandas_ta library.
    
    # 1. Relative Strength Index (RSI)
    features['RSI'] = df.ta.rsi(length=14)

    # 2. Simple Moving Average (SMA)
    features['SMA_20'] = df.ta.sma(length=20)
    
    # 3. Exponential Moving Average (EMA)
    features['EMA_20'] = df.ta.ema(length=20)

    # --- Calculate Volatility (Standard Deviation of Returns) ---
    # We first calculate the percentage change between prices.
    returns = df['Close'].pct_change()

    # The number of periods depends on the frequency of your data.
    # Based on your screenshot, the data is at a 5-minute frequency.
    # Periods in 1 hour = 12 (60 mins / 5 mins)
    # Periods in 24 hours = 288 (12 * 24)
    # Periods in 5 days = 1440 (288 * 5)
    # Periods in 30 days = 8640 (288 * 30)
    
    # 4. Volatility over the last 24 hours
    features['VOL_24H'] = returns.rolling(window=288).std()

    # 5. Volatility over the last 5 days
    features['VOL_5D'] = returns.rolling(window=1440).std()

    # 6. Volatility over the last 30 days
    features['VOL_30D'] = returns.rolling(window=8640).std()

    # Drop rows with NaN values that are created by the rolling windows
    features.dropna(inplace=True)
    
    return features

def main():
    """
    Example usage of the generate_features function.
    
    This function demonstrates how to load data and generate features.
    You should replace the data loading part with your own method.
    """
    # --- Step 1: Load your data ---
    # This is a placeholder for your data loading logic.
    # For this example, we'll create some sample data.
    # In your case, you would use your `util.get_cl_data()` function.
    print("Loading data...")
    try:
        # Assuming you have a function to load your data like in the screenshot
        # from util import get_cl_data 
        # price_data = get_cl_data()
        
        # For demonstration, let's create a sample DataFrame
        # In a real scenario, you would load your 'test10k.csv' here
        date_rng = pd.date_range(start='2020-01-01', end='2021-01-01', freq='5min')
        data = {
            'Open': np.random.uniform(100, 102, size=len(date_rng)),
            'High': np.random.uniform(102, 104, size=len(date_rng)),
            'Low': np.random.uniform(98, 100, size=len(date_rng)),
            'Close': np.random.uniform(100, 104, size=len(date_rng)),
            'Volume': np.random.randint(1000, 5000, size=len(date_rng))
        }
        price_data = pd.DataFrame(data, index=date_rng)
        print("Sample data created for demonstration.")

    except Exception as e:
        print(f"Could not load data. Error: {e}")
        return

    # --- Step 2: Generate the features ---
    print("\nGenerating features...")
    feature_dataframe = generate_features(price_data)
    
    # --- Step 3: Display the results ---
    print("\nSuccessfully generated features. Here are the first 5 rows:")
    print(feature_dataframe.head())
    
    print("\nAnd the last 5 rows:")
    print(feature_dataframe.tail())


if __name__ == "__main__":
    # To run this script, you need to install pandas-ta:
    # pip install pandas-ta
    import numpy as np # For the sample data generation
    main()
