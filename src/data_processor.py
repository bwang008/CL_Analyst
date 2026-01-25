"""
Data Processor Module for CL Futures ML Pipeline.

This module handles ETL (Extract, Transform, Load) for OHLCV data:
- Loading raw semicolon-separated CSV data
- Time feature generation
- Feature generation via AlphaFactory
- Target creation for ML classification
- Normalization and cleanup
- Saving processed data to Parquet/CSV

Multiple dataset configurations are supported:
- set_01: Base feature set with RSI, MACD, SMAs, Volatility, Log Returns
- set_02: (Planned) Alternative feature set

Author: CL Analyst
"""

import os
import json
import numpy as np
import pandas as pd
from pathlib import Path

from src.features.alpha_factory import AlphaFactory


# Dataset version suffix
DATASET_VERSIONS = {
    'set_01': 'AlphaFactory features with cyclical time (Time_Sin, Time_Cos)',
    'set_02': 'AlphaFactory features with raw time (Hour, Minute)',
}


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
                 output_path: str = None,
                 dataset_version: str = "set_01"):
        """
        Initialize the DataProcessor.
        
        Args:
            input_path: Path to the input CSV file (semicolon-separated, no headers)
            output_path: Path for processed output. If None, auto-generates based on input name.
            dataset_version: Version identifier for the dataset (e.g., 'set_01', 'set_02')
        """
        self.input_path = input_path
        self.dataset_version = dataset_version
        
        if output_path is None:
            # Auto-generate output path based on input filename and dataset version
            input_name = Path(input_path).stem
            self.output_path = f"data/processed/{input_name}_{dataset_version}.parquet"
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
    
    def add_time_features_raw(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add raw time features (Hour and Minute) instead of cyclical encoding.
        
        This provides direct hour and minute values which may be useful for
        models that can learn non-linear time relationships on their own.
        
        Args:
            df: DataFrame with DateTime index
            
        Returns:
            pd.DataFrame: DataFrame with Hour and Minute columns added
        """
        print("Adding raw time features (Hour, Minute)...")
        
        df['Hour'] = df.index.hour
        df['Minute'] = df.index.minute
        
        return df
    
    def create_target(self, df: pd.DataFrame, threshold: float = 0.08, 
                      horizon: int = None) -> pd.DataFrame:
        """
        Create target labels for classification and RAW_ columns for evaluation.
        
        Target values (stored as TARGET_Direction):
        - 0: Hold (no significant move)
        - 1: Buy signal (price moves up > threshold)
        - 2: Sell signal (price moves down > threshold)
        
        RAW_ columns (for evaluation, NOT used as features):
        - RAW_Close: Current close price (for visualization)
        - RAW_Future_High: Max high price over horizon (for actual move calculation)
        - RAW_Future_Low: Min low price over horizon (for actual move calculation)
        
        IMPORTANT: This must be called BEFORE normalizing prices, as it requires
        absolute price values to calculate percentage moves.
        
        Args:
            df: DataFrame with High, Low, Close columns (absolute prices)
            threshold: Percentage threshold for significant move (default 0.08 = 8%)
            horizon: Forward-looking window in bars (default: 576 = 48 hours)
            
        Returns:
            pd.DataFrame: DataFrame with TARGET_Direction and RAW_ columns added
        """
        if horizon is None:
            horizon = 2 * self.BARS_PER_DAY  # 576 bars = 48 hours
            
        print(f"Creating target with {threshold*100}% threshold, {horizon} bar ({horizon/self.BARS_PER_DAY:.1f} day) horizon...")
        
        # Calculate future high and low over the horizon
        # We look at the NEXT 'horizon' bars (not including current)
        # Using shift(-horizon) to look forward, then rolling max/min backwards
        future_high = df['High'].iloc[::-1].rolling(window=horizon, min_periods=1).max().iloc[::-1].shift(-1)
        future_low = df['Low'].iloc[::-1].rolling(window=horizon, min_periods=1).min().iloc[::-1].shift(-1)
        
        # Store RAW_ columns for evaluation (these are NOT features - filtered out by get_feature_columns())
        # These allow the evaluator to calculate actual move magnitudes vs predictions
        df['RAW_Close'] = df['Close'].copy()
        df['RAW_Future_High'] = future_high
        df['RAW_Future_Low'] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low for evaluation")
        
        # Calculate percentage moves from current close
        up_move = (future_high - df['Close']) / df['Close']
        down_move = (df['Close'] - future_low) / df['Close']
        
        # Create target labels (will be renamed to TARGET_Direction in cleanup)
        df['TARGET_Direction'] = 0  # Default: Hold
        df.loc[up_move > threshold, 'TARGET_Direction'] = 1   # Buy signal
        df.loc[down_move > threshold, 'TARGET_Direction'] = 2  # Sell signal
        
        # If both conditions are met, prioritize based on which move is larger
        both_signals = (up_move > threshold) & (down_move > threshold)
        df.loc[both_signals & (up_move >= down_move), 'TARGET_Direction'] = 1
        df.loc[both_signals & (down_move > up_move), 'TARGET_Direction'] = 2
        
        # The last 'horizon' rows don't have enough forward data - mark as NaN
        df.loc[df.index[-horizon:], 'TARGET_Direction'] = np.nan
        
        # Print distribution
        target_counts = df['TARGET_Direction'].value_counts(dropna=False)
        print(f"  - TARGET_Direction distribution: {dict(target_counts)}")
        
        return df
    
    def normalize_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize features for ML training.
        
        Transformations:
        - OHLC prices: Convert to log returns (used for regime features, then dropped)
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
        # These are intermediate features used for regime calculations
        for col in ['Open', 'High', 'Low', 'Close']:
            df[f'{col}_Return'] = np.log(df[col] / df[col].shift(1))
        
        # 2. Convert Moving Averages to percent distance from Close (if present)
        if 'SMA_20' in df.columns and df['SMA_20'].notna().any():
            df['SMA_20_Dist'] = (close_orig - df['SMA_20']) / df['SMA_20']

        if 'SMA_30d' in df.columns and df['SMA_30d'].notna().any():
            df['SMA_30d_Dist'] = (close_orig - df['SMA_30d']) / df['SMA_30d']
        
        # 3. Normalize Volume using log1p
        df['Volume_Log'] = np.log1p(df['Volume'])
        
        print("  - OHLC converted to log returns")
        if 'SMA_20_Dist' in df.columns or 'SMA_30d_Dist' in df.columns:
            print("  - SMAs converted to percent distance")
        print("  - Volume log-transformed")
        
        return df
    
    def cleanup(self, df: pd.DataFrame, drop_raw_returns: bool = True) -> pd.DataFrame:
        """
        Clean up the DataFrame by removing raw columns and NaN rows.
        
        Steps:
        1. Drop original raw price columns (Open, High, Low, Close) - but keep RAW_ prefixed versions
        2. Drop original raw MA columns (SMA_20, SMA_30d)
        3. Drop original Volume column
        4. Drop raw return columns (noise for long-horizon predictions)
        5. Drop rows with any NaN values
        6. Convert TARGET_Direction to integer type
        
        Note: RAW_ prefixed columns (RAW_Close, RAW_Future_High, RAW_Future_Low) are 
        preserved for evaluation. They are filtered out by get_feature_columns() 
        and never used as ML features.
        
        Args:
            df: DataFrame after normalization
            drop_raw_returns: If True, drop the raw *_Return columns (default True)
                             These are "noise" for 48-hour predictions but were
                             needed to calculate regime features.
            
        Returns:
            pd.DataFrame: Cleaned DataFrame ready for training
        """
        print("Cleaning up DataFrame...")
        
        rows_before = len(df)
        
        # Columns to drop (raw values that have been normalized)
        # Note: RAW_ prefixed columns are NOT dropped - they are needed for evaluation
        cols_to_drop = ['Open', 'High', 'Low', 'Close', 'Volume', 'SMA_20', 'SMA_30d']
        
        # Also drop raw return columns - they are "ingredients" not "features"
        # We've extracted the signal (volatility, skew, kurtosis); discard the noise
        if drop_raw_returns:
            cols_to_drop.extend(['Open_Return', 'High_Return', 'Low_Return', 'Close_Return'])
        
        cols_existing = [col for col in cols_to_drop if col in df.columns]
        
        if cols_existing:
            df = df.drop(columns=cols_existing)
            print(f"  - Dropped raw columns: {cols_existing}")
        
        # Drop rows with NaN values
        df = df.dropna()
        
        rows_after = len(df)
        print(f"  - Dropped {rows_before - rows_after} rows with NaN values")
        print(f"  - Remaining rows: {rows_after}")
        
        # Convert TARGET_Direction to integer if it exists
        if 'TARGET_Direction' in df.columns:
            df['TARGET_Direction'] = df['TARGET_Direction'].astype(int)
        
        # Verify no NaN values remain
        nan_count = df.isna().sum().sum()
        if nan_count > 0:
            print(f"  WARNING: {nan_count} NaN values still present!")
        else:
            print("  - Verified: No NaN values in final DataFrame")
        
        # Report which columns are features vs RAW/TARGET
        feature_cols = [c for c in df.columns if not c.startswith(('RAW_', 'TARGET_', 'META_'))]
        raw_cols = [c for c in df.columns if c.startswith('RAW_')]
        target_cols = [c for c in df.columns if c.startswith('TARGET_')]
        print(f"  - Feature columns ({len(feature_cols)}): {feature_cols}")
        print(f"  - RAW columns (for eval): {raw_cols}")
        print(f"  - TARGET columns: {target_cols}")
        
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
        Run the complete data processing pipeline based on dataset_version.
        
        Routes to the appropriate processing function:
        - set_01: process_set_01() - Base features
        - set_02: process_set_02() - Alternative features (planned)
        
        Args:
            threshold: Target threshold for significant price move (default 0.08 = 8%)
            horizon: Forward-looking window for target in bars (default: 576 = 48 hours)
            
        Returns:
            pd.DataFrame: Fully processed DataFrame
        """
        if self.dataset_version == "set_01":
            return self.process_set_01(threshold=threshold, horizon=horizon)
        elif self.dataset_version == "set_02":
            return self.process_set_02(threshold=threshold, horizon=horizon)
        else:
            raise ValueError(f"Unknown dataset version: {self.dataset_version}. "
                           f"Available: {list(DATASET_VERSIONS.keys())}")
    
    def process_set_01(self, threshold: float = 0.08, horizon: int = None) -> pd.DataFrame:
        """
        Process data using SET_01 feature configuration.
        
        SET_01 Features:
        - Time: Time_Sin, Time_Cos (cyclical encoding of time of day)
        - AlphaFactory: volatility, liquidity, structure, momentum features
        - Volume: Volume_Log (log-transformed)
        - Target: 0=Hold, 1=Buy, 2=Sell (based on threshold % move in horizon)
        
        Args:
            threshold: Target threshold for significant price move (default 0.08 = 8%)
            horizon: Forward-looking window for target in bars (default: 576 = 48 hours)
            
        Returns:
            pd.DataFrame: Fully processed DataFrame
        """
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print("=" * 60)
        
        # Step 1: Load data
        df = self.load_data()
        
        # Step 2: Add time features
        df = self.add_time_features(df)

        # Step 3: Add AlphaFactory features (windows in bars for 5-min data)
        windows = [
            3 * self.BARS_PER_DAY,
            7 * self.BARS_PER_DAY,
            14 * self.BARS_PER_DAY,
            35 * self.BARS_PER_DAY,
        ]
        df = AlphaFactory(df).add_all_features(windows=windows, include_macro=True)

        # Step 4: Create target (MUST be before normalization)
        df = self.create_target(df, threshold=threshold, horizon=horizon)

        # Step 5: Normalize features (creates *_Return columns as intermediates)
        df = self.normalize_features(df)

        # Step 6: Cleanup (drops raw columns AND raw returns - they're "noise")
        df = self.cleanup(df, drop_raw_returns=True)

        # Step 7: Save
        saved_path = self.save(df)
        
        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        print("=" * 60)
        
        self.df = df
        return df
    
    def process_set_02(self, threshold: float = 0.08, horizon: int = None) -> pd.DataFrame:
        """
        Process data using SET_02 feature configuration.
        
        SET_02 is identical to SET_01 except for time features:
        - Uses raw Hour and Minute instead of cyclical Time_Sin/Time_Cos
        
        SET_02 Features:
        - Time: Hour (0-23), Minute (0-55 in 5-min increments)
        - AlphaFactory: volatility, liquidity, structure, momentum features
        - Volume: Volume_Log (log-transformed)
        - Target: 0=Hold, 1=Buy, 2=Sell (based on threshold % move in horizon)
        
        Args:
            threshold: Target threshold for significant price move (default 0.08 = 8%)
            horizon: Forward-looking window for target in bars (default: 576 = 48 hours)
            
        Returns:
            pd.DataFrame: Fully processed DataFrame
        """
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print("=" * 60)
        
        # Step 1: Load data
        df = self.load_data()
        
        # Step 2: Add RAW time features (Hour, Minute) - differs from set_01
        df = self.add_time_features_raw(df)

        # Step 3: Add AlphaFactory features (windows in bars for 5-min data)
        windows = [
            3 * self.BARS_PER_DAY,
            7 * self.BARS_PER_DAY,
            14 * self.BARS_PER_DAY,
            35 * self.BARS_PER_DAY,
        ]
        df = AlphaFactory(df).add_all_features(windows=windows, include_macro=True)

        # Step 4: Create target (MUST be before normalization)
        df = self.create_target(df, threshold=threshold, horizon=horizon)

        # Step 5: Normalize features (creates *_Return columns as intermediates)
        df = self.normalize_features(df)

        # Step 6: Cleanup (drops raw columns AND raw returns - they're "noise")
        df = self.cleanup(df, drop_raw_returns=True)

        # Step 7: Save
        saved_path = self.save(df)
        
        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        print("=" * 60)
        
        self.df = df
        return df


def main(dataset_version: str = "set_01"):
    """
    Main entry point for running the data processor.
    
    Processes test100k.csv from data/raw/ and outputs to data/processed/.
    
    Args:
        dataset_version: Which dataset configuration to use (default: 'set_01')
    """
    # Check if input file exists in data/raw/, if not try data/
    #input_path = "data/raw/test100k.csv"
    input_path = "data/raw/CL.csv"
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
    processor = DataProcessor(input_path=input_path, dataset_version=dataset_version)
    
    try:
        df = processor.process(threshold=0.08, horizon=576)
        
        # Print summary statistics
        print("\nFeature Summary:")
        print("-" * 40)
        print(df.describe().T)
        
        if 'TARGET_Direction' in df.columns:
            print("\nTARGET_Direction Distribution:")
            print("-" * 40)
            print(df['TARGET_Direction'].value_counts().sort_index())
            
    except Exception as e:
        print(f"Error during processing: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import sys
    # Allow passing dataset version as command line argument
    version = sys.argv[1] if len(sys.argv) > 1 else "set_01"
    main(dataset_version=version)
