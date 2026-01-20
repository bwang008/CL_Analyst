"""
Data Processor Module for CL Futures ML Pipeline.

This module handles ETL (Extract, Transform, Load) for OHLCV data:
- Loading raw semicolon-separated CSV data
- Feature engineering (technical indicators, volatility, time features)
- Target creation for ML classification
- Normalization and cleanup
- Saving processed data to Parquet/CSV

Author: CL Analyst
"""

import os
import numpy as np
import pandas as pd
import pandas_ta as ta
from pathlib import Path


class DataProcessor:
    """
    A class to process raw OHLCV data into ML-ready features.
    
    Attributes:
        input_path (str): Path to the input CSV file
        output_path (str): Path for the output processed file
        bars_per_day (int): Number of 5-minute bars per day (288)
    """
    
    # Constants for 5-minute bar data
    BARS_PER_HOUR = 12
    BARS_PER_DAY = 288
    MINUTES_PER_DAY = 1440
    
    def __init__(self, input_path: str = "data/raw/test100k.csv", 
                 output_path: str = None):
        """
        Initialize the DataProcessor.
        
        Args:
            input_path: Path to the input CSV file (semicolon-separated, no headers)
            output_path: Path for processed output. If None, auto-generates based on input name.
        """
        self.input_path = input_path
        
        if output_path is None:
            # Auto-generate output path based on input filename
            input_name = Path(input_path).stem
            self.output_path = f"data/processed/{input_name}_PROCESSED.parquet"
        else:
            self.output_path = output_path
            
        self.df = None
        
    def load_data(self) -> pd.DataFrame:
        """
        Load raw OHLCV data from CSV file.
        
        The CSV is expected to be semicolon-separated with no headers.
        Columns: Date, Time, Open, High, Low, Close, Volume
        Date format: DD/MM/YYYY, Time format: HH:MM
        
        Returns:
            pd.DataFrame: Loaded data with DateTime index
        """
        print(f"Loading data from {self.input_path}...")
        
        # Try different separators in order of likelihood
        separators = [';', ',', '\t']
        
        for sep in separators:
            try:
                # Read a small sample to test the separator
                sample_df = pd.read_csv(self.input_path, sep=sep, header=None, nrows=5)
                
                # Check if we got the expected 7 columns
                if sample_df.shape[1] == 7:
                    # This separator works, read the full file
                    df = pd.read_csv(self.input_path, sep=sep, header=None, index_col=None)
                    df.columns = ['Date', 'Time', 'Open', 'High', 'Low', 'Close', 'Volume']
                    
                    # Combine Date and Time columns and parse as datetime
                    df['DateTime'] = pd.to_datetime(
                        df['Date'] + ' ' + df['Time'], 
                        format='%d/%m/%Y %H:%M'
                    )
                    
                    # Set as index and drop the separate Date and Time columns
                    df.set_index('DateTime', inplace=True)
                    df.drop(['Date', 'Time'], axis=1, inplace=True)
                    
                    # Ensure numeric types
                    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                    
                    print(f"Successfully loaded {len(df)} rows using separator: '{sep}'")
                    self.df = df
                    return df
                    
            except Exception as e:
                continue  # Try next separator
        
        # If we get here, none of the separators worked
        raise ValueError(f"Could not read {self.input_path} with any of the separators: {separators}. "
                        f"Please check the file format.")
    
    def add_time_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add cyclical time features based on minutes since midnight.
        
        This captures the daily trading cycle pattern using sine/cosine encoding,
        which preserves the cyclical nature (23:55 is close to 00:00).
        
        Args:
            df: DataFrame with DateTime index
            
        Returns:
            pd.DataFrame: DataFrame with Time_Sin and Time_Cos columns added
        """
        print("Adding cyclical time features...")
        
        # Calculate minutes since midnight
        minutes = df.index.hour * 60 + df.index.minute
        
        # Cyclical encoding using sine and cosine
        df['Time_Sin'] = np.sin(2 * np.pi * minutes / self.MINUTES_PER_DAY)
        df['Time_Cos'] = np.cos(2 * np.pi * minutes / self.MINUTES_PER_DAY)
        
        return df
    
    def add_technical_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add technical indicators using pandas_ta.
        
        Indicators added:
        - RSI (14 periods)
        - MACD (12, 26, 9) - returns MACD line, Signal line, and Histogram
        - SMA_20 (20 periods)
        - SMA_30d (8640 periods - 30 days of 5-min bars)
        
        Args:
            df: DataFrame with OHLCV columns
            
        Returns:
            pd.DataFrame: DataFrame with technical indicator columns added
        """
        print("Adding technical indicators...")
        
        data_length = len(df)
        
        # RSI - Relative Strength Index (14 periods)
        rsi_result = df.ta.rsi(length=14)
        if rsi_result is not None:
            df['RSI'] = rsi_result if isinstance(rsi_result, pd.Series) else rsi_result.iloc[:, 0]
        else:
            df['RSI'] = np.nan
            print("  WARNING: RSI could not be calculated (insufficient data)")
        
        # MACD - Moving Average Convergence Divergence
        # Returns DataFrame with columns: MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
        macd = df.ta.macd(fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            df['MACD'] = macd.iloc[:, 0]  # MACD line
            df['MACD_Signal'] = macd.iloc[:, 2]  # Signal line
            df['MACD_Hist'] = macd.iloc[:, 1]  # Histogram
        else:
            df['MACD'] = np.nan
            df['MACD_Signal'] = np.nan
            df['MACD_Hist'] = np.nan
            print("  WARNING: MACD could not be calculated (insufficient data)")
        
        # SMA - Simple Moving Averages
        sma_20_result = df.ta.sma(length=20)
        if sma_20_result is not None:
            df['SMA_20'] = sma_20_result if isinstance(sma_20_result, pd.Series) else sma_20_result.iloc[:, 0]
        else:
            df['SMA_20'] = np.nan
            print("  WARNING: SMA_20 could not be calculated (insufficient data)")
        
        # 30-day SMA (30 days * 288 bars/day = 8640 bars)
        sma_30d_length = 30 * self.BARS_PER_DAY
        if data_length >= sma_30d_length:
            sma_30d_result = df.ta.sma(length=sma_30d_length)
            if sma_30d_result is not None:
                df['SMA_30d'] = sma_30d_result if isinstance(sma_30d_result, pd.Series) else sma_30d_result.iloc[:, 0]
            else:
                df['SMA_30d'] = np.nan
        else:
            df['SMA_30d'] = np.nan
            print(f"  WARNING: SMA_30d requires {sma_30d_length} bars but only {data_length} available")
        
        print(f"  - RSI (14), MACD (12,26,9), SMA_20, SMA_30d ({sma_30d_length} bars)")
        
        return df
    
    def add_volatility_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add volatility features as rolling standard deviation of returns.
        
        Windows:
        - VOL_3D: 3 days (864 bars)
        - VOL_7D: 7 days (2016 bars)
        - VOL_30D: 30 days (8640 bars)
        
        Args:
            df: DataFrame with Close prices
            
        Returns:
            pd.DataFrame: DataFrame with volatility columns added
        """
        print("Adding volatility features...")
        
        data_length = len(df)
        
        # Calculate returns for volatility calculation
        returns = df['Close'].pct_change()
        
        # Volatility windows
        vol_3d_window = 3 * self.BARS_PER_DAY   # 864 bars
        vol_7d_window = 7 * self.BARS_PER_DAY   # 2016 bars
        vol_30d_window = 30 * self.BARS_PER_DAY  # 8640 bars
        
        # Calculate volatility with warnings for insufficient data
        df['VOL_3D'] = returns.rolling(window=vol_3d_window, min_periods=1).std()
        if data_length < vol_3d_window:
            print(f"  WARNING: VOL_3D requires {vol_3d_window} bars but only {data_length} available")
            
        df['VOL_7D'] = returns.rolling(window=vol_7d_window, min_periods=1).std()
        if data_length < vol_7d_window:
            print(f"  WARNING: VOL_7D requires {vol_7d_window} bars but only {data_length} available")
            
        df['VOL_30D'] = returns.rolling(window=vol_30d_window, min_periods=1).std()
        if data_length < vol_30d_window:
            print(f"  WARNING: VOL_30D requires {vol_30d_window} bars but only {data_length} available")
        
        print(f"  - VOL_3D ({vol_3d_window} bars), VOL_7D ({vol_7d_window} bars), VOL_30D ({vol_30d_window} bars)")
        
        return df
    
    def create_target(self, df: pd.DataFrame, threshold: float = 0.08, 
                      horizon: int = None) -> pd.DataFrame:
        """
        Create target labels for classification.
        
        Target values:
        - 0: Hold (no significant move)
        - 1: Buy signal (price moves up > threshold)
        - 2: Sell signal (price moves down > threshold)
        
        IMPORTANT: This must be called BEFORE normalizing prices, as it requires
        absolute price values to calculate percentage moves.
        
        Args:
            df: DataFrame with High, Low, Close columns (absolute prices)
            threshold: Percentage threshold for significant move (default 0.08 = 8%)
            horizon: Forward-looking window in bars (default: 288 = 24 hours)
            
        Returns:
            pd.DataFrame: DataFrame with Target column added
        """
        if horizon is None:
            horizon = self.BARS_PER_DAY  # 288 bars = 24 hours
            
        print(f"Creating target with {threshold*100}% threshold, {horizon} bar ({horizon/self.BARS_PER_DAY:.1f} day) horizon...")
        
        # Calculate future high and low over the horizon
        # We look at the NEXT 'horizon' bars (not including current)
        # Using shift(-horizon) to look forward, then rolling max/min backwards
        future_high = df['High'].iloc[::-1].rolling(window=horizon, min_periods=1).max().iloc[::-1].shift(-1)
        future_low = df['Low'].iloc[::-1].rolling(window=horizon, min_periods=1).min().iloc[::-1].shift(-1)
        
        # Calculate percentage moves from current close
        up_move = (future_high - df['Close']) / df['Close']
        down_move = (df['Close'] - future_low) / df['Close']
        
        # Create target labels
        df['Target'] = 0  # Default: Hold
        df.loc[up_move > threshold, 'Target'] = 1   # Buy signal
        df.loc[down_move > threshold, 'Target'] = 2  # Sell signal
        
        # If both conditions are met, prioritize based on which move is larger
        both_signals = (up_move > threshold) & (down_move > threshold)
        df.loc[both_signals & (up_move >= down_move), 'Target'] = 1
        df.loc[both_signals & (down_move > up_move), 'Target'] = 2
        
        # The last 'horizon' rows don't have enough forward data - mark as NaN
        df.loc[df.index[-horizon:], 'Target'] = np.nan
        
        # Print distribution
        target_counts = df['Target'].value_counts(dropna=False)
        print(f"  - Target distribution: {dict(target_counts)}")
        
        return df
    
    def normalize_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize features for ML training.
        
        Transformations:
        - OHLC prices: Convert to log returns
        - Moving averages: Convert to percent distance from Close
        - Volume: Apply log1p transformation
        
        Args:
            df: DataFrame with all features added
            
        Returns:
            pd.DataFrame: DataFrame with normalized features
        """
        print("Normalizing features...")
        
        # Store original Close for MA calculations before converting
        close_orig = df['Close'].copy()
        
        # 1. Convert OHLC to log returns
        for col in ['Open', 'High', 'Low', 'Close']:
            df[f'{col}_Return'] = np.log(df[col] / df[col].shift(1))
        
        # 2. Convert Moving Averages to percent distance from Close
        # Only compute if the column has valid (non-NaN) values
        if 'SMA_20' in df.columns and df['SMA_20'].notna().any():
            df['SMA_20_Dist'] = (close_orig - df['SMA_20']) / df['SMA_20']
        else:
            df['SMA_20_Dist'] = np.nan
            
        if 'SMA_30d' in df.columns and df['SMA_30d'].notna().any():
            df['SMA_30d_Dist'] = (close_orig - df['SMA_30d']) / df['SMA_30d']
        else:
            df['SMA_30d_Dist'] = np.nan
        
        # 3. Normalize Volume using log1p
        df['Volume_Log'] = np.log1p(df['Volume'])
        
        print("  - OHLC converted to log returns")
        print("  - SMAs converted to percent distance")
        print("  - Volume log-transformed")
        
        return df
    
    def cleanup(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Clean up the DataFrame by removing raw columns and NaN rows.
        
        Steps:
        1. Drop original raw price columns (Open, High, Low, Close)
        2. Drop original raw MA columns (SMA_20, SMA_30d)
        3. Drop original Volume column
        4. Drop rows with any NaN values
        5. Convert Target to integer type
        
        Args:
            df: DataFrame after normalization
            
        Returns:
            pd.DataFrame: Cleaned DataFrame ready for training
        """
        print("Cleaning up DataFrame...")
        
        rows_before = len(df)
        
        # Columns to drop (raw values that have been normalized)
        cols_to_drop = ['Open', 'High', 'Low', 'Close', 'Volume', 'SMA_20', 'SMA_30d']
        cols_existing = [col for col in cols_to_drop if col in df.columns]
        
        if cols_existing:
            df = df.drop(columns=cols_existing)
            print(f"  - Dropped raw columns: {cols_existing}")
        
        # Drop rows with NaN values
        df = df.dropna()
        
        rows_after = len(df)
        print(f"  - Dropped {rows_before - rows_after} rows with NaN values")
        print(f"  - Remaining rows: {rows_after}")
        
        # Convert Target to integer if it exists
        if 'Target' in df.columns:
            df['Target'] = df['Target'].astype(int)
        
        # Verify no NaN values remain
        nan_count = df.isna().sum().sum()
        if nan_count > 0:
            print(f"  WARNING: {nan_count} NaN values still present!")
        else:
            print("  - Verified: No NaN values in final DataFrame")
        
        return df
    
    def save(self, df: pd.DataFrame) -> str:
        """
        Save processed DataFrame to file.
        
        Attempts to save as Parquet (preferred for speed/compression).
        Falls back to CSV if pyarrow/fastparquet is not available.
        
        Args:
            df: Processed DataFrame to save
            
        Returns:
            str: Path to saved file
        """
        # Ensure output directory exists
        output_dir = os.path.dirname(self.output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        
        # Try to save as Parquet first
        if self.output_path.endswith('.parquet'):
            try:
                df.to_parquet(self.output_path)
                print(f"Saved processed data to {self.output_path}")
                return self.output_path
            except ImportError:
                # Parquet not available, fall back to CSV
                csv_path = self.output_path.replace('.parquet', '.csv')
                df.to_csv(csv_path)
                print(f"Parquet not available. Saved as CSV to {csv_path}")
                return csv_path
        else:
            # Save as CSV
            df.to_csv(self.output_path)
            print(f"Saved processed data to {self.output_path}")
            return self.output_path
    
    def process(self, threshold: float = 0.08, horizon: int = None) -> pd.DataFrame:
        """
        Run the complete data processing pipeline.
        
        Pipeline steps:
        1. Load data
        2. Add time features (cyclical encoding)
        3. Add technical indicators (RSI, MACD, SMAs)
        4. Add volatility features
        5. Create target labels (BEFORE normalization)
        6. Normalize features
        7. Cleanup (drop raw columns, remove NaN rows)
        8. Save to file
        
        Args:
            threshold: Target threshold for significant price move (default 0.08 = 8%)
            horizon: Forward-looking window for target in bars (default: 288 = 24 hours)
            
        Returns:
            pd.DataFrame: Fully processed DataFrame
        """
        print("=" * 60)
        print("Starting Data Processing Pipeline")
        print("=" * 60)
        
        # Step 1: Load data
        df = self.load_data()
        
        # Step 2: Add time features
        df = self.add_time_features(df)
        
        # Step 3: Add technical indicators
        df = self.add_technical_indicators(df)
        
        # Step 4: Add volatility features
        df = self.add_volatility_features(df)
        
        # Step 5: Create target (MUST be before normalization)
        df = self.create_target(df, threshold=threshold, horizon=horizon)
        
        # Step 6: Normalize features
        df = self.normalize_features(df)
        
        # Step 7: Cleanup
        df = self.cleanup(df)
        
        # Step 8: Save
        saved_path = self.save(df)
        
        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        print("=" * 60)
        
        self.df = df
        return df


def main():
    """
    Main entry point for running the data processor.
    
    Processes test100k.csv from data/raw/ and outputs to data/processed/.
    """
    # Check if input file exists in data/raw/, if not try data/
    input_path = "data/raw/test100k.csv"
    if not os.path.exists(input_path):
        # Try the main data folder as fallback
        alt_path = "data/test100k.csv"
        if os.path.exists(alt_path):
            print(f"Note: {input_path} not found, using {alt_path}")
            input_path = alt_path
        else:
            print(f"Error: Could not find input file at {input_path} or {alt_path}")
            print("Please ensure test100k.csv is in the data/raw/ folder.")
            return
    
    # Create processor and run pipeline
    processor = DataProcessor(input_path=input_path)
    
    try:
        df = processor.process(threshold=0.08, horizon=288)
        
        # Print summary statistics
        print("\nFeature Summary:")
        print("-" * 40)
        print(df.describe().T)
        
        if 'Target' in df.columns:
            print("\nTarget Distribution:")
            print("-" * 40)
            print(df['Target'].value_counts().sort_index())
            
    except Exception as e:
        print(f"Error during processing: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
