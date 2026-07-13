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
import shutil
import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401
from typing import Optional
from pathlib import Path
from datetime import datetime

from src.data_paths import get_data_path, get_data_root, mirror_file
from src.features.alpha_factory import AlphaFactory
from src.features.macro_features import MacroFeatureEngine


# Dataset version suffix
DATASET_VERSIONS = {
    'set_01': 'AlphaFactory features with cyclical time (Time_Sin, Time_Cos)',
    'set_02': 'AlphaFactory features with raw time (Hour, Minute)',
    'set_03': 'Master squeeze targets (4% and 8%) with binary splits',
    'set_04': 'Lower thresholds (2%/3%) with shorter horizons (12h/24h)',
    'set_05': 'Dynamic Triple Barrier targets with ATR-based barriers',
    'set_06': 'Ultimate dataset (set_05 features + targets)',
    'set_07': 'Extended features (set_05 + new clusters + expanded macro + 1D window)',
    'set_08': 'Exhaustion features (set_07 + cumulative return, ATR-normalised exhaustion, distance from high)',
    'set_09': 'MACRO fix (set_08 features with causally-safe bar-level MACRO rolling — no hourly resample lookahead)',
    'set_10': 'Causal cleanup (set_09 + removed bfill lookahead, 26K warmup — 100% causally safe)',
    'set_11': 'Macro-augmented (set_10 + FRED macro regime + CFTC COT positioning features)',
    'set_11c': 'Spread-Adjusted Momentum variant (set_11 + Spread-adj RSI)',
    'set_12': 'Exhaustion Divergence (set_11 cumulative + Slope Div, Peak Offset, Effort-Reward)',
    'HourSet_06': (
        '1-Hour consolidated pipeline (process_hourset_06): stationary indicators '
        '(PPO replacing MACD, normalised LR slope, DMA, Ichimoku) + full target suite '
        '(1.5/2.0/2.5x1 @ 72H/120H, 2x1 @ 3H/6H/12H, 1x0.5 @ 3H/6H/12H, 1x2 @ 3H/6H/12H). '
        'Supersedes HourSet_01/02 — use this for all new experiments.'
    ),
    'HourSet_08': (
        'Precision-driven target refactor (process_hourset_08): '
        'Removes noisy targets, expands asymmetric targets (3x1, 4x1, 5x1) '
        'for 6H, 12H, 24H, and retains core 2x1.'
    ),
    'Hour4Set_01': (
        '4-Hour bar dataset (process_hour4set_01): '
        'Same feature architecture as HourSet_08 with AlphaFactory windows '
        'scaled to 4H bars (6,18,42,84,210). Targets: 2x1,3x1,4x1,5x1 '
        'at 24H,48H,96H horizons (6,12,24 bars). Designed for multi-day swing trading.'
    ),
    'HourSet_09': (
        'Term Structure Shapes (process_hourset_09): '
        'HourSet_08 + 108 TS_* features — Diff, Ratio, and Inversion '
        'transforms comparing fast windows to 840-bar anchor (Protocol A). '
        'Families: VOL_PARK, VOL_YZ, DONCHIAN, LR_SLOPE, VWAP_DIST, CMF, '
        'STOCH_K, EFFICIENCY, CORWIN. Optuna-toggleable via term_structure bucket.'
    ),
    'HourSet_10': (
        'Topology-Aware TS & Pruned Base Features (process_hourset_10): '
        'Orthogonalized 214 feature subset removing collinearity.'
    ),
    'HourSet_11': (
        'Alias for process_hourset_09.'
    ),
    'HourSet_12': (
        'Alias for process_hourset_10.'
    ),
    'HourSet_15B': (
        'Short-horizon dataset (process_hourset_15b): HourSet_14B base features + '
        'short-horizon targets (1x0.5 1H, 2x1 2H, 2x1 1H).'
    ),
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
    
    def __init__(
        self,
        input_path: str = None,
        output_path: str = None,
        dataset_version: str = "set_03",
        keep_ohlcv: bool = True,
        symbol: str = "CL",
        config: "DataWorkflowConfig" = None,
    ):
        """
        Initialize the DataProcessor.
        
        Args:
            input_path: Path to the input CSV file (semicolon-separated, no headers).
                        If None, checks CL_DATA_ROOT env var for shared data,
                        falling back to data/raw/test100k.csv.
            output_path: Path for processed output. If None, auto-generates based on input name.
            dataset_version: Version identifier for the dataset (e.g., 'set_01', 'set_02')
            keep_ohlcv: If True, retain Open/High/Low/Close/Volume/DateTime columns.
        """
        if input_path is None:
            # Resolve from CL_DATA_ROOT first, repo-local fallback
            resolved = get_data_path(f"raw/{symbol}.csv")
            if resolved.exists():
                input_path = str(resolved)
            else:
                input_path = str(get_data_path("raw/test100k.csv"))
        self.input_path = input_path
        self.symbol = symbol
        self.config = config
        self._dataset_version = dataset_version
        self.keep_ohlcv = keep_ohlcv
        self._explicit_output_path = output_path is not None
        if output_path is not None:
            self.output_path = output_path
        else:
            self._update_output_path()

    @property
    def dataset_version(self):
        return self._dataset_version

    @dataset_version.setter
    def dataset_version(self, value):
        self._dataset_version = value
        if not self._explicit_output_path:
            self._update_output_path()

    def _update_output_path(self):
        # Auto-generate output path based on dataset version (decoupled from raw input name)
        self.output_path = str(get_data_root() / "processed" / f"{self.symbol}_{self._dataset_version}.parquet")
            
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

        if not os.path.exists(self.input_path):
            raise FileNotFoundError(
                f"Input data file not found: {self.input_path}\n"
                f"The raw data CSV must exist in one of:\n"
                f"  1. CL_DATA_ROOT env var location: "
                f"{os.environ.get('CL_DATA_ROOT', '(not set)')}\n"
                f"  2. Project-relative path: data/raw/\n"
                f"Set CL_DATA_ROOT or copy the file to fix this."
            )
        
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
    
    def add_time_features(
        self,
        df: pd.DataFrame,
        include_day_of_week: bool = False,
        include_month: bool = False,
    ) -> pd.DataFrame:
        """
        Add cyclical time features based on minutes since midnight.

        This captures the daily trading cycle pattern using sine/cosine encoding,
        which preserves the cyclical nature (23:55 is close to 00:00).

        Args:
            df: DataFrame with DateTime index
            include_day_of_week: If True, also add cyclical day-of-week features
            include_month: If True, also add cyclical month-of-year features
                (Time_Month_Sin/Cos). GATED (default False) so rebuilds of
                pre-03B datasets stay byte-identical — never make this
                unconditional (it would break the rebuild byte-parity
                control gate for every existing dataset).

        Returns:
            pd.DataFrame: DataFrame with Time_Sin and Time_Cos columns added
        """
        print("Adding cyclical time features...")

        # Calculate minutes since midnight
        minutes = df.index.hour * 60 + df.index.minute

        # Cyclical encoding using sine and cosine
        df['Time_Sin'] = np.sin(2 * np.pi * minutes / self.MINUTES_PER_DAY)
        df['Time_Cos'] = np.cos(2 * np.pi * minutes / self.MINUTES_PER_DAY)

        # Day of week (cyclical): 0=Monday ... 4=Friday
        if include_day_of_week:
            day_of_week = df.index.dayofweek
            df['Time_DayOfWeek_Sin'] = np.sin(2 * np.pi * day_of_week / 5)
            df['Time_DayOfWeek_Cos'] = np.cos(2 * np.pi * day_of_week / 5)
            print("  - Added day-of-week cyclical features")

        # Month of year (cyclical): Jan=(sin 0, cos 1) ... phase (month-1)/12
        if include_month:
            month = df.index.month
            df['Time_Month_Sin'] = np.sin(2 * np.pi * (month - 1) / 12)
            df['Time_Month_Cos'] = np.cos(2 * np.pi * (month - 1) / 12)
            print("  - Added month-of-year cyclical features")

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

    def add_squeeze_target(
        self,
        df: pd.DataFrame,
        prefix: str,
        threshold: float,
        horizon: int = 576,
    ) -> pd.DataFrame:
        """
        Creates squeeze targets using the provided prefix:
        - {prefix}_MULTI: 0=Hold, 1=Long, 2=Short
        - {prefix}_LONG: 0/1 binary
        - {prefix}_SHORT: 0/1 binary
        """
        print(f"   [Target] Generating Squeeze Targets ({prefix})...")

        if 'ATR_14' not in df.columns:
            df['ATR_14'] = df.ta.atr(length=14)

        df['vol_metric'] = df['ATR_14'] / df['Close']
        vol_threshold = df['vol_metric'].rolling(window=10000).quantile(0.30)
        is_quiet = df['vol_metric'] < vol_threshold

        future_return = df['Close'].shift(-horizon) / df['Close'] - 1.0

        multi_col = f"{prefix}_MULTI"
        long_col = f"{prefix}_LONG"
        short_col = f"{prefix}_SHORT"

        df[multi_col] = 0
        mask_long = is_quiet & (future_return > threshold)
        df.loc[mask_long, multi_col] = 1

        mask_short = is_quiet & (future_return < -threshold)
        df.loc[mask_short, multi_col] = 2

        df[long_col] = (df[multi_col] == 1).astype('Int64')
        df[short_col] = (df[multi_col] == 2).astype('Int64')
        df[multi_col] = df[multi_col].astype('Int64')

        df[multi_col] = df[multi_col].fillna(0)
        df[long_col] = df[long_col].fillna(0)
        df[short_col] = df[short_col].fillna(0)

        return df

    def add_triple_barrier_target(
        self,
        df: pd.DataFrame,
        prefix: str,
        tp_atr_mult: float = 2.0,
        sl_atr_mult: float = 1.0,
        max_horizon: int = 288,
        atr_period: int = 14,
    ) -> pd.DataFrame:
        """
        Dynamic Triple Barrier Method target.

        Barriers are set using rolling ATR (not fixed percentages):
        - LONG: TP = entry + tp_atr_mult * ATR (price goes UP to profit)
                SL = entry - sl_atr_mult * ATR (price goes DOWN to loss)
        - SHORT: TP = entry - tp_atr_mult * ATR (price goes DOWN to profit)
                 SL = entry + sl_atr_mult * ATR (price goes UP to loss)
        - Vertical: max_horizon bars time limit

        Target values:
        - {prefix}_MULTI: 0=Hold (time expired), 1=Long, 2=Short
        - {prefix}_LONG:  Binary 0/1 (TP hit first from long perspective)
        - {prefix}_SHORT: Binary 0/1 (TP hit first from short perspective)

        Args:
            df: DataFrame with OHLCV columns
            prefix: Target column prefix
            tp_atr_mult: ATR multiplier for take-profit barrier
            sl_atr_mult: ATR multiplier for stop-loss barrier
            max_horizon: Maximum bars to look ahead (vertical barrier)
            atr_period: Period for ATR calculation
        """
        print(
            f"   [Target] Generating Triple Barrier ({prefix}): "
            f"TP={tp_atr_mult}xATR, SL={sl_atr_mult}xATR, horizon={max_horizon}"
        )

        # Compute ATR if not present
        atr_col = f'ATR_{atr_period}'
        if atr_col not in df.columns:
            df[atr_col] = df.ta.atr(length=atr_period)

        close = df['Close'].values
        high_all = df['High'].values
        low_all = df['Low'].values
        atr = df[atr_col].values
        n = len(df)

        # --- Pass 1: LONG labels (TP above entry, SL below) ---
        long_labels = np.zeros(n, dtype=np.float64)
        for i in range(n - 1):
            if np.isnan(atr[i]) or atr[i] <= 0:
                continue
            entry = close[i]
            tp_barrier = entry + tp_atr_mult * atr[i]
            sl_barrier = entry - sl_atr_mult * atr[i]
            end_idx = min(i + max_horizon + 1, n)
            for j in range(i + 1, end_idx):
                if high_all[j] >= tp_barrier:
                    long_labels[i] = 1
                    break
                if low_all[j] <= sl_barrier:
                    break

        # --- Pass 2: SHORT labels (TP below entry, SL above) ---
        short_labels = np.zeros(n, dtype=np.float64)
        for i in range(n - 1):
            if np.isnan(atr[i]) or atr[i] <= 0:
                continue
            entry = close[i]
            tp_barrier = entry - tp_atr_mult * atr[i]  # TP below entry
            sl_barrier = entry + sl_atr_mult * atr[i]  # SL above entry
            end_idx = min(i + max_horizon + 1, n)
            for j in range(i + 1, end_idx):
                if low_all[j] <= tp_barrier:
                    short_labels[i] = 1
                    break
                if high_all[j] >= sl_barrier:
                    break

        # Mark final bars as NaN (insufficient look-ahead)
        long_labels[-max_horizon:] = np.nan
        short_labels[-max_horizon:] = np.nan

        # Build MULTI column: 0=Hold, 1=Long, 2=Short
        # If both fire for the same bar, keep both flagged (MULTI uses Long priority)
        multi_labels = np.where(long_labels == 1, 1.0,
                                np.where(short_labels == 1, 2.0, 0.0))
        multi_labels[-max_horizon:] = np.nan

        multi_col = f"{prefix}_MULTI"
        long_col = f"{prefix}_LONG"
        short_col = f"{prefix}_SHORT"

        df[multi_col] = pd.array(multi_labels, dtype='Int64')
        df[long_col] = pd.array(long_labels, dtype='Int64')
        df[short_col] = pd.array(short_labels, dtype='Int64')

        counts_long = df[long_col].value_counts(dropna=False)
        counts_short = df[short_col].value_counts(dropna=False)
        print(f"  - {long_col} distribution: {dict(counts_long)}")
        print(f"  - {short_col} distribution: {dict(counts_short)}")

        return df

    def add_return_target(
        self,
        df: pd.DataFrame,
        horizons: list[int] | None = None,
    ) -> pd.DataFrame:
        """
        Add continuous future-return target columns for regression.

        Creates TARGET_RET_{horizon} = future return over horizon bars.
        These can be thresholded post-hoc to create classification labels.

        Args:
            df: DataFrame with Close column
            horizons: List of bar counts to compute returns for
        """
        if horizons is None:
            horizons = [144, 288, 576]  # 12h, 24h, 48h

        for h in horizons:
            col = f"TARGET_RET_{h}"
            df[col] = df['Close'].shift(-h) / df['Close'] - 1.0
            print(f"  - Added {col} (mean={df[col].mean():.6f}, std={df[col].std():.6f})")

        return df

    def add_vol_expansion_target(
        self,
        df: pd.DataFrame,
        forward_horizon: int = 288,
        rolling_window: int = 10_080,
        percentile_threshold: float = 0.80,
    ) -> pd.DataFrame:
        """Volatility Expansion target (ported from scripts/generate_vol_target.py).

        For each bar, compute the True Range (max High - min Low) of the next
        `forward_horizon` bars.  If this forward TR exceeds the trailing
        `rolling_window` 80th-percentile, label = 1 (top 20% vol events).

        Args:
            df: DataFrame with High, Low columns and DatetimeIndex.
            forward_horizon: Bars to look ahead (288 = 24H at 5-min).
            rolling_window: Trailing window for percentile (10_080 = 35 days).
            percentile_threshold: Quantile threshold (0.80 = top 20%).
        """
        import numpy as np

        print(f"   [Target] Generating Vol Expansion: "
              f"horizon={forward_horizon}, rolling={rolling_window}, "
              f"top {(1 - percentile_threshold) * 100:.0f}%")

        n = len(df)
        high = df["High"].values
        low = df["Low"].values

        # Forward True Range: max(High[t+1:t+h]) - min(Low[t+1:t+h])
        forward_max_high = (
            pd.Series(high[::-1])
            .rolling(window=forward_horizon, min_periods=1)
            .max()
            .values[::-1]
        )
        forward_min_low = (
            pd.Series(low[::-1])
            .rolling(window=forward_horizon, min_periods=1)
            .min()
            .values[::-1]
        )

        # Shift by 1 to exclude current bar
        forward_tr = np.full(n, np.nan)
        forward_tr[:-1] = forward_max_high[1:] - forward_min_low[1:]
        forward_tr[-forward_horizon:] = np.nan  # insufficient future data

        # Rolling percentile threshold (trailing, causal)
        forward_tr_series = pd.Series(forward_tr, index=df.index)
        rolling_threshold = forward_tr_series.rolling(
            window=rolling_window, min_periods=rolling_window // 2
        ).quantile(percentile_threshold)

        # Label: 1 if forward TR exceeds rolling threshold
        labels = np.where(
            np.isnan(forward_tr) | np.isnan(rolling_threshold.values),
            np.nan,
            np.where(forward_tr > rolling_threshold.values, 1.0, 0.0),
        )
        df["TARGET_VOL_EXPANSION"] = pd.array(labels, dtype="Int64")

        non_nan = df["TARGET_VOL_EXPANSION"].dropna()
        if len(non_nan) > 0:
            pos_rate = (non_nan == 1).sum() / len(non_nan)
            print(f"  - TARGET_VOL_EXPANSION: {len(non_nan):,} valid, "
                  f"positive rate {pos_rate:.1%} (target ~20%)")

        return df
    
    def add_direction_target(
        self,
        df: pd.DataFrame,
        prefix: str,
        threshold: float = 0.08,
        horizon: int = None,
        add_raw: bool = False,
    ) -> pd.DataFrame:
        """
        Create direction targets for classification and RAW_ columns for evaluation.

        Target values (stored as {prefix}_MULTI):
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
            prefix: Target prefix (e.g., TARGET_DIR_8PCT)
            threshold: Percentage threshold for significant move (default 0.08 = 8%)
            horizon: Forward-looking window in bars (default: 576 = 48 hours)
            add_raw: If True, (re)compute RAW_ columns

        Returns:
            pd.DataFrame: DataFrame with {prefix}_MULTI/LONG/SHORT and RAW_ columns added
        """
        if horizon is None:
            horizon = 2 * self.BARS_PER_DAY  # 576 bars = 48 hours

        print(
            f"Creating direction target ({prefix}) with {threshold*100}% threshold, "
            f"{horizon} bar ({horizon/self.BARS_PER_DAY:.1f} day) horizon..."
        )

        if add_raw or "RAW_Future_High" not in df.columns or "RAW_Future_Low" not in df.columns:
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
        up_move = (df['RAW_Future_High'] - df['Close']) / df['Close']
        down_move = (df['Close'] - df['RAW_Future_Low']) / df['Close']

        multi_col = f"{prefix}_MULTI"
        long_col = f"{prefix}_LONG"
        short_col = f"{prefix}_SHORT"

        # Create target labels
        df[multi_col] = 0  # Default: Hold
        df.loc[up_move > threshold, multi_col] = 1   # Buy signal
        df.loc[down_move > threshold, multi_col] = 2  # Sell signal

        # If both conditions are met, prioritize based on which move is larger
        both_signals = (up_move > threshold) & (down_move > threshold)
        df.loc[both_signals & (up_move >= down_move), multi_col] = 1
        df.loc[both_signals & (down_move > up_move), multi_col] = 2

        # The last 'horizon' rows don't have enough forward data - mark as NaN
        df.loc[df.index[-horizon:], multi_col] = np.nan

        df[long_col] = (df[multi_col] == 1).astype("Int64")
        df[short_col] = (df[multi_col] == 2).astype("Int64")
        df.loc[df[multi_col].isna(), [long_col, short_col]] = pd.NA

        # Print distribution
        target_counts = df[multi_col].value_counts(dropna=False)
        print(f"  - {multi_col} distribution: {dict(target_counts)}")

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
    
    def cleanup(
        self,
        df: pd.DataFrame,
        drop_raw_returns: bool = True,
        warmup_rows: int = 10500,
        keep_ohlcv: Optional[bool] = None,
        max_warmup_bars: int = 26_000,
    ) -> pd.DataFrame:
        """
        Clean up the DataFrame by removing raw columns and NaN rows.
        
        Steps:
        1. Drop original raw price columns (Open, High, Low, Close) - but keep RAW_ prefixed versions
        2. Drop original raw MA columns (SMA_20, SMA_30d)
        3. Drop original Volume column
        4. Drop raw return columns (noise for long-horizon predictions)
        5. Drop rows with any NaN values
        6. Convert TARGET_ columns to integer type
        
        Note: RAW_ and EXEC_ prefixed columns are preserved for evaluation/execution. 
        They are filtered out by get_feature_columns() and never used as ML features.
        
        Args:
            df: DataFrame after normalization
            drop_raw_returns: If True, drop the raw *_Return columns (default True)
                             These are "noise" for 48-hour predictions but were
                             needed to calculate regime features.
            keep_ohlcv: If True, keep Open/High/Low/Close/Volume and add DateTime column.
            max_warmup_bars: Maximum warmup rows for the bar frequency.
                            Default 26,000 for 5-min bars. Set lower for
                            coarser timeframes (e.g. 2,200 for 1H bars).
            
        Returns:
            pd.DataFrame: Cleaned DataFrame ready for training
        """
        print("Cleaning up DataFrame...")
        
        rows_before = len(df)
        
        if keep_ohlcv is None:
            keep_ohlcv = self.keep_ohlcv

        if keep_ohlcv and "DateTime" not in df.columns:
            df["DateTime"] = df.index

        # Columns to drop (raw values that have been normalized)
        # Note: RAW_ prefixed columns are NOT dropped - they are needed for evaluation
        cols_to_drop = ['Open', 'High', 'Low', 'Close', 'Volume', 'SMA_20', 'SMA_30d']
        if keep_ohlcv:
            cols_to_drop = [
                c for c in cols_to_drop
                if c not in {'Open', 'High', 'Low', 'Close', 'Volume'}
            ]
        
        # Also drop raw return columns - they are "ingredients" not "features"
        # We've extracted the signal (volatility, skew, kurtosis); discard the noise
        if drop_raw_returns:
            cols_to_drop.extend(['Open_Return', 'High_Return', 'Low_Return', 'Close_Return'])
        
        cols_existing = [col for col in cols_to_drop if col in df.columns]
        
        if cols_existing:
            df = df.drop(columns=cols_existing)
            print(f"  - Dropped raw columns: {cols_existing}")
        
        # --- Causally-safe warmup & NaN handling ---
        # Drop the maximum warmup period required by our longest features.
        # For 5-min bars: MACRO_3M=10,080 + VOL_VOLVOL=20,160 -> 26,000.
        # For 1H bars: longest window ~4,320 -> 2,200 warmup is sufficient.
        effective_warmup = max(warmup_rows, max_warmup_bars)
        if len(df) > effective_warmup:
            df = df.iloc[effective_warmup:].copy()
            print(f"  - Dropped first {effective_warmup} warmup rows")
        else:
            df = df.copy()

        # Forward fill ONLY — never backward fill.
        # bfill() is a lookahead bias: it copies future values into past rows.
        target_cols = [c for c in df.columns if c.startswith('TARGET_')]
        non_target_cols = [c for c in df.columns if c not in target_cols]
        df.loc[:, non_target_cols] = df.loc[:, non_target_cols].ffill()

        # Drop any rows that still contain NaN in non-target columns.
        # If NaNs survived the warmup drop + forward fill, the data is
        # corrupt (e.g., features that are entirely NaN for some reason).
        rows_before_dropna = len(df)
        df = df.dropna(subset=non_target_cols)
        rows_after_dropna = len(df)
        dropped_after_fill = rows_before_dropna - rows_after_dropna
        if dropped_after_fill > 0:
            print(
                f"  WARNING: Dropped {dropped_after_fill} corrupted rows "
                f"that contained NaN after ffill "
                f"({dropped_after_fill / rows_before_dropna:.2%} of remaining data)"
            )

        rows_after = len(df)
        print(f"  - Dropped {rows_before - rows_after} total rows (warmup + NaN)")
        print(f"  - Remaining rows: {rows_after}")
        
        # Convert target columns to nullable integers (preserve NaNs where needed)
        # Skip TARGET_RET_* columns — they are continuous returns for regression
        target_cols = [c for c in df.columns if c.startswith('TARGET_')]
        for col in target_cols:
            if col.startswith('TARGET_RET_'):
                continue  # continuous return targets stay as float
            df[col] = df[col].astype('Int64')
        
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
        saved_path = self.output_path
        if self.output_path.endswith('.parquet'):
            try:
                # Downcast float64 -> float32 to halve memory/disk usage.
                # LightGBM uses float32 internally anyway, so no model impact.
                float64_cols = df.select_dtypes(include=['float64']).columns
                if len(float64_cols) > 0:
                    df[float64_cols] = df[float64_cols].astype(np.float32)
                    print(f"  - Downcast {len(float64_cols)} float64 cols -> float32")
                df.to_parquet(self.output_path)
                print(f"Saved processed data to {self.output_path}")
            except ImportError:
                # Parquet not available, fall back to CSV
                saved_path = self.output_path.replace('.parquet', '.csv')
                df.to_csv(saved_path)
                print(f"Parquet not available. Saved as CSV to {saved_path}")
        else:
            # Save as CSV
            df.to_csv(self.output_path)
            print(f"Saved processed data to {self.output_path}")

        # Mirror to shared data root for worktree access
        self._mirror_to_root(saved_path)
        return saved_path

    def _mirror_to_root(self, saved_path: str) -> None:
        """Mirror the saved file to the other data location."""
        try:
            mirror_file(Path(saved_path))
            print(f"  -> Mirrored {Path(saved_path).name}")
        except (ValueError, OSError) as exc:
            print(f"  -> Mirror failed: {exc}")
    
    def process(self, threshold: float = 0.08, horizon: int = None, **kwargs) -> pd.DataFrame:
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
        elif self.dataset_version == "set_03":
            return self.process_set_03(threshold=threshold, horizon=horizon)
        elif self.dataset_version == "set_04":
            return self.process_set_04(threshold=threshold, horizon=horizon)
        elif self.dataset_version == "set_05":
            return self.process_set_05(threshold=threshold, horizon=horizon)
        elif self.dataset_version == "set_06":
            return self.process_set_06()
        elif self.dataset_version == "set_07":
            return self.process_set_07()
        elif self.dataset_version == "set_08":
            return self.process_set_08()
        elif self.dataset_version == "set_09":
            # set_09 uses the same pipeline as set_08 — the difference is
            # in AlphaFactory.add_macro_context() which now uses bar-level
            # rolling instead of hourly resample (no more lookahead bias).
            return self.process_set_08()
        elif self.dataset_version == "set_10":
            # set_10 = set_09 features + causally-safe cleanup:
            # - No bfill (removed lookahead bias)
            # - 26,000 warmup rows (covers MACRO_3M + VOL_VOLVOL_10080)
            return self.process_set_08()
        elif self.dataset_version == "set_11":
            return self.process_set_11()
        elif self.dataset_version in ("set_11c", "set_11b"):
            return self.process_set_11()
        elif self.dataset_version == "set_12":
            return self.process_set_12()
        elif self.dataset_version == "HourSet_06":
            return self.process_hourset_06()
        elif self.dataset_version == "HourSet_07":
            return self.process_hourset_07()
        elif self.dataset_version == "HourSet_08":
            return self.process_hourset_08()
        elif self.dataset_version == "HourSet_09":
            return self.process_hourset_09()
        elif self.dataset_version == "HourSet_10":
            return self.process_hourset_10()
        elif self.dataset_version == "HourSet_11":
            return self.process_hourset_11(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))
        elif self.dataset_version == "HourSet_12":
            return self.process_hourset_12()
        elif self.dataset_version == "HourSet_13A":
            return self.process_hourset_13a(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))
        elif self.dataset_version == "HourSet_13B":
            return self.process_hourset_13b(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))
        elif self.dataset_version == "HourSet_14A":
            return self.process_hourset_13a(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))
        elif self.dataset_version == "HourSet_14B":
            return self.process_hourset_13b(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))
        elif self.dataset_version == "HourSet_15B":
            return self.process_hourset_15b(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))
        elif self.dataset_version == "Hour4Set_01":
            return self.process_hour4set_01()
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
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)
        
        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [25%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")
        
        # Step 2: Add time features
        df = self.add_time_features(df)
        print(f"  [50%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features (windows in bars for 5-min data)
        windows = [
            3 * self.BARS_PER_DAY,
            7 * self.BARS_PER_DAY,
            14 * self.BARS_PER_DAY,
            35 * self.BARS_PER_DAY,
        ]
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_macro=True,
            log_progress=True,
        )
        print(f"  [75%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Create targets (MUST be before normalization)
        df = self.add_direction_target(
            df,
            prefix="TARGET_DIR_8PCT",
            threshold=threshold,
            horizon=horizon,
            add_raw=True,
        )
        df = self.add_direction_target(
            df,
            prefix="TARGET_DIR_4PCT",
            threshold=0.04,
            horizon=horizon,
        )
        df = self.add_squeeze_target(
            df,
            prefix="TARGET_SQUEEZE",
            threshold=0.04,
            horizon=576,
        )
        df['TARGET_SQUEEZE'] = df['TARGET_SQUEEZE_MULTI']

        # Step 5: Normalize features (creates *_Return columns as intermediates)
        df = self.normalize_features(df)

        # Step 6: Cleanup (drops raw columns AND raw returns - they're "noise")
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 7: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")
        
        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
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
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)
        
        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [25%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")
        
        # Step 2: Add RAW time features (Hour, Minute) - differs from set_01
        df = self.add_time_features_raw(df)
        print(f"  [50%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features (windows in bars for 5-min data)
        windows = [
            3 * self.BARS_PER_DAY,
            7 * self.BARS_PER_DAY,
            14 * self.BARS_PER_DAY,
            35 * self.BARS_PER_DAY,
        ]
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_macro=True,
            log_progress=True,
        )
        print(f"  [75%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Create targets (MUST be before normalization)
        df = self.add_direction_target(
            df,
            prefix="TARGET_DIR_8PCT",
            threshold=threshold,
            horizon=horizon,
            add_raw=True,
        )
        df = self.add_direction_target(
            df,
            prefix="TARGET_DIR_4PCT",
            threshold=0.04,
            horizon=horizon,
        )
        df = self.add_squeeze_target(
            df,
            prefix="TARGET_SQUEEZE",
            threshold=0.04,
            horizon=576,
        )
        df['TARGET_SQUEEZE'] = df['TARGET_SQUEEZE_MULTI']

        # Step 5: Normalize features (creates *_Return columns as intermediates)
        df = self.normalize_features(df)

        # Step 6: Cleanup (drops raw columns AND raw returns - they're "noise")
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 7: Save
        saved_path = self.save(df)

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_set_03(self, threshold: float = 0.08, horizon: int = None) -> pd.DataFrame:
        """
        Process data using SET_03 feature configuration.

        SET_03 Features:
        - Time: Time_Sin, Time_Cos (cyclical encoding of time of day)
        - AlphaFactory: volatility, liquidity, structure, trend, volume-flow
        - Volume: Volume_Log (log-transformed)
        - Targets: Multiple squeeze targets (4% and 8%) with binary splits
        """
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [25%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")

        # Step 2: Add time features
        df = self.add_time_features(df)
        print(f"  [50%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features (windows in bars for 5-min data)
        windows = [
            3 * self.BARS_PER_DAY,
            7 * self.BARS_PER_DAY,
            14 * self.BARS_PER_DAY,
            35 * self.BARS_PER_DAY,
        ]
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_macro=True,
            log_progress=True,
        )
        print(f"  [75%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Create targets (MUST be before normalization)
        df = self.add_direction_target(
            df,
            prefix="TARGET_DIR_8PCT",
            threshold=threshold,
            horizon=horizon,
            add_raw=True,
        )
        df = self.add_direction_target(
            df,
            prefix="TARGET_DIR_4PCT",
            threshold=0.04,
            horizon=horizon,
        )
        df = self.add_squeeze_target(
            df,
            prefix="TARGET_SQZ_8PCT",
            threshold=0.08,
            horizon=576,
        )
        df = self.add_squeeze_target(
            df,
            prefix="TARGET_SQZ_4PCT",
            threshold=0.04,
            horizon=576,
        )

        # Step 5: Normalize features (creates *_Return columns as intermediates)
        df = self.normalize_features(df)

        # Step 6: Cleanup (drops raw columns AND raw returns - they're "noise")
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 7: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_set_04(self, threshold: float = 0.03, horizon: int = None) -> pd.DataFrame:
        """
        Process data using SET_04 feature configuration.

        SET_04: Lower thresholds + shorter horizons for more trainable signal.
        - Time: Time_Sin, Time_Cos (cyclical encoding)
        - AlphaFactory: volatility, liquidity, structure, trend, volume-flow
        - Volume: Volume_Log
        - Targets:
            - DIR_3PCT at 12h and 24h horizons
            - DIR_2PCT at 12h and 24h horizons
            - Continuous return targets at 12h, 24h, 48h
        """
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print(f"  Lower threshold targets (2%/3%) with shorter horizons")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [20%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")

        # Step 2: Add time features
        df = self.add_time_features(df)
        print(f"  [30%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features
        windows = [
            3 * self.BARS_PER_DAY,
            7 * self.BARS_PER_DAY,
            14 * self.BARS_PER_DAY,
            35 * self.BARS_PER_DAY,
        ]
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_macro=True,
            log_progress=True,
        )
        print(f"  [60%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Create targets — lower thresholds, shorter horizons
        # 3% threshold, 12h horizon (144 bars)
        df = self.add_direction_target(
            df, prefix="TARGET_DIR_3PCT_12H",
            threshold=0.03, horizon=144, add_raw=True,
        )
        # 3% threshold, 24h horizon (288 bars)
        df = self.add_direction_target(
            df, prefix="TARGET_DIR_3PCT_24H",
            threshold=0.03, horizon=288,
        )
        # 2% threshold, 12h horizon (144 bars)
        df = self.add_direction_target(
            df, prefix="TARGET_DIR_2PCT_12H",
            threshold=0.02, horizon=144,
        )
        # 2% threshold, 24h horizon (288 bars)
        df = self.add_direction_target(
            df, prefix="TARGET_DIR_2PCT_24H",
            threshold=0.02, horizon=288,
        )

        # Continuous returns for regression approach
        df = self.add_return_target(df, horizons=[144, 288, 576])

        print(f"  [80%] All targets created at {datetime.now().isoformat(timespec='seconds')}")

        # Step 5: Normalize features
        df = self.normalize_features(df)

        # Step 6: Cleanup
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 7: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_set_05(self, threshold: float = 0.08, horizon: int = None) -> pd.DataFrame:
        """
        Process data using SET_05 feature configuration.

        SET_05: Dynamic Triple Barrier targets with ATR-based barriers.
        - Time: Time_Sin, Time_Cos
        - AlphaFactory: volatility, liquidity, structure, trend, volume-flow
        - Volume: Volume_Log
        - Targets:
            - TRIPLE_2x1_12H: TP=2×ATR, SL=1×ATR, max_horizon=12h
            - TRIPLE_2x1_24H: TP=2×ATR, SL=1×ATR, max_horizon=24h
            - TRIPLE_3x1_24H: TP=3×ATR, SL=1×ATR, max_horizon=24h (asymmetric)
        """
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print(f"  Dynamic Triple Barrier targets (ATR-based)")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [20%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")

        # Step 2: Add time features
        df = self.add_time_features(df)
        print(f"  [30%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features
        windows = [
            3 * self.BARS_PER_DAY,
            7 * self.BARS_PER_DAY,
            14 * self.BARS_PER_DAY,
            35 * self.BARS_PER_DAY,
        ]
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_macro=True,
            log_progress=True,
        )
        print(f"  [60%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Add RAW columns for evaluation (required by evaluator)
        raw_horizon = 288  # 24h horizon for actual move analysis
        future_high = df['High'].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
        future_low = df['Low'].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
        df['RAW_Close'] = df['Close'].copy()
        df['RAW_Future_High'] = future_high
        df['RAW_Future_Low'] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low for evaluation")

        # Step 5: Create Triple Barrier targets
        # Balanced: TP=2×ATR, SL=1×ATR, 12h horizon
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_12H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=144, atr_period=14,
        )
        # Balanced: TP=2×ATR, SL=1×ATR, 24h horizon
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_24H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        # Asymmetric: TP=3×ATR, SL=1×ATR, 24h horizon (bigger winners)
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_3x1_24H",
            tp_atr_mult=3.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )

        # Also add continuous returns for comparison
        df = self.add_return_target(df, horizons=[144, 288])

        print(f"  [80%] All targets created at {datetime.now().isoformat(timespec='seconds')}")

        # Step 5: Normalize features
        df = self.normalize_features(df)

        # Step 6: Cleanup — keep ATR columns (useful features)
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 7: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_set_06(self) -> pd.DataFrame:
        """
        Ultimate dataset for final model verification.
        - Strategy 2 features: Microstructure, ROC-Vol, Trends.
        - Triple Barrier targets.
        - RAW columns for backtesting.
        """
        self.dataset_version = "set_06"
        return self.process_set_05()

    def process_set_07(self) -> pd.DataFrame:
        """
        Extended feature set for next-generation models.

        Changes vs set_05/set_06:
        - Adds 1-day (288) bar-level window (A2)
        - Expands macro windows: 1D, 3D, 1W, 2W, 1M, 3M (A1)
        - Enables extended AlphaFactory clusters (A3–A6):
            return distribution, stochastic, Chaikin Money Flow,
            cross-timeframe ratios
        - Adds day-of-week encoding (A7)
        - Triple Barrier targets (same as set_05)
        """
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print(f"  Extended features: 1D window, expanded macro, new clusters")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [15%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")

        # Step 2: Add time features (includes day-of-week for set_07)
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features with extended clusters
        windows = [
            1 * self.BARS_PER_DAY,   # 288 = 1 day (NEW)
            3 * self.BARS_PER_DAY,   # 864 = 3 days
            7 * self.BARS_PER_DAY,   # 2016 = 7 days
            14 * self.BARS_PER_DAY,  # 4032 = 14 days
            35 * self.BARS_PER_DAY,  # 10080 = 35 days
        ]
        macro_windows = {
            "1D": 24, "3D": 72, "1W": 168, "2W": 336,
            "1M": 840, "3M": 2160,
        }
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [60%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Add RAW columns for evaluation
        raw_horizon = 288
        future_high = df['High'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).max().iloc[::-1].shift(-1)
        future_low = df['Low'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).min().iloc[::-1].shift(-1)
        df['RAW_Close'] = df['Close'].copy()
        df['RAW_Future_High'] = future_high
        df['RAW_Future_Low'] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # Step 5: Create Triple Barrier targets (same as set_05)
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_12H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=144, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_24H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_3x1_24H",
            tp_atr_mult=3.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_return_target(df, horizons=[144, 288])
        print(f"  [80%] All targets created at {datetime.now().isoformat(timespec='seconds')}")

        # Step 6: Normalize features
        df = self.normalize_features(df)

        # Step 7: Cleanup
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 8: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_set_08(self) -> pd.DataFrame:
        """
        Exhaustion-aware feature set for next-generation models.

        Changes vs set_07:
        - Adds exhaustion cluster (EXHAUST_CUM_RET, EXHAUST_CUM_ATR,
          EXHAUST_DIST_HIGH) at every bar-level window.
          These help the model detect overextended moves where a
          snap-back is more likely than continuation.
        - Everything else identical to set_07.
        """
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.dataset_version.upper()}")
        print(f"  Exhaustion features: cumulative return, ATR-normalised, distance from high")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [15%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")

        # Step 2: Add time features (includes day-of-week)
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features with extended + exhaustion clusters
        windows = [
            1 * self.BARS_PER_DAY,   # 288 = 1 day
            3 * self.BARS_PER_DAY,   # 864 = 3 days
            7 * self.BARS_PER_DAY,   # 2016 = 7 days
            14 * self.BARS_PER_DAY,  # 4032 = 14 days
            35 * self.BARS_PER_DAY,  # 10080 = 35 days
        ]
        macro_windows = {
            "1D": 24, "3D": 72, "1W": 168, "2W": 336,
            "1M": 840, "3M": 2160,
        }
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,  # includes exhaustion cluster
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [60%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Add RAW columns for evaluation
        raw_horizon = 288
        future_high = df['High'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).max().iloc[::-1].shift(-1)
        future_low = df['Low'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).min().iloc[::-1].shift(-1)
        df['RAW_Close'] = df['Close'].copy()
        df['RAW_Future_High'] = future_high
        df['RAW_Future_Low'] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # Step 5: Create Triple Barrier targets (same as set_05/07)
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_12H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=144, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_24H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_3x1_24H",
            tp_atr_mult=3.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_return_target(df, horizons=[144, 288])
        print(f"  [80%] All targets created at {datetime.now().isoformat(timespec='seconds')}")

        # Step 6: Normalize features
        df = self.normalize_features(df)

        # Step 7: Cleanup
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 8: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    # ------------------------------------------------------------------
    # OHLCV resampling
    # ------------------------------------------------------------------
    @staticmethod
    def resample_to_hourly(df: pd.DataFrame) -> pd.DataFrame:
        """Resample 5-minute OHLCV bars to 1-hour bars.

        Aggregation rules:
            Open  -> first
            High  -> max
            Low   -> min
            Close -> last
            Volume -> sum

        Args:
            df: DataFrame with DatetimeIndex and OHLCV columns.

        Returns:
            DataFrame with 1-hour bars.
        """
        agg = {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
        df_h = df.resample("1h").agg(agg).dropna(subset=["Close"])
        print(f"  Resampled 5-min -> 1H: {len(df)} -> {len(df_h)} bars")
        return df_h

    @staticmethod
    def resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
        """Resample 5-minute OHLCV bars to an arbitrary frequency.

        Uses the same default pandas label/closed convention as
        ``resample_to_hourly`` for consistency with the live trader.

        Aggregation rules:
            Open   -> first
            High   -> max
            Low    -> min
            Close  -> last
            Volume -> sum

        Args:
            df: DataFrame with DatetimeIndex and OHLCV columns.
            freq: Pandas frequency string (e.g., '4h', '1D').

        Returns:
            DataFrame with resampled bars.
        """
        agg = {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
        df_out = df.resample(freq).agg(agg).dropna(subset=["Close"])
        print(f"  Resampled 5-min -> {freq}: {len(df)} -> {len(df_out)} bars")
        return df_out

    # ------------------------------------------------------------------
    # set_11c: set_11 variant with Spread-Adjusted Momentum features
    # ------------------------------------------------------------------
    def process_set_11c(self) -> pd.DataFrame:
        """set_11 variant C for Iteration 4: Spread-Adjusted Momentum."""
        self.dataset_version = "set_11c"
        return self.process_set_11()

    # ------------------------------------------------------------------
    # set_12: Cumulative dataset with Exhaustion Divergence features
    # ------------------------------------------------------------------
    def process_set_12(self) -> pd.DataFrame:
        """Cumulative dataset: set_11 + Exhaustion Divergence cluster.

        Inherits everything from set_11 (AlphaFactory extended clusters,
        FRED macro, CFTC COT, Spread-Adjusted Momentum) and adds:
        - EXHDIV_SLOPE_DIVERGE_*  (price vs RSI slope divergence)
        - EXHDIV_PEAK_OFFSET_*    (temporal peak offset)
        - EXHDIV_EFFORT_REWARD_*  (volume effort vs candle reward)
        """
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - SET_12")
        print(f"  set_11 cumulative + Exhaustion Divergence features")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [15%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")

        # Step 2: Add time features (includes day-of-week)
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features — ALL clusters enabled
        windows = [
            1 * self.BARS_PER_DAY,   # 288 = 1 day
            3 * self.BARS_PER_DAY,   # 864 = 3 days
            7 * self.BARS_PER_DAY,   # 2016 = 7 days
            14 * self.BARS_PER_DAY,  # 4032 = 14 days
            35 * self.BARS_PER_DAY,  # 10080 = 35 days
        ]
        macro_windows = {
            "1D": 24, "3D": 72, "1W": 168, "2W": 336,
            "1M": 840, "3M": 2160,
        }
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_exhaustion_divergence=True,  # NEW for set_12
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Merge external macro features (FRED + COT)
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [60%] {n_macro} external macro features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 5: Add RAW columns for evaluation
        raw_horizon = 288
        future_high = df['High'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).max().iloc[::-1].shift(-1)
        future_low = df['Low'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).min().iloc[::-1].shift(-1)
        df['RAW_Close'] = df['Close'].copy()
        df['RAW_Future_High'] = future_high
        df['RAW_Future_Low'] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # Step 6: Create Triple Barrier targets (cumulative — all horizons)
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_12H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=144, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_24H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_3x1_24H",
            tp_atr_mult=3.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_return_target(df, horizons=[144, 288])
        df = self.add_vol_expansion_target(df)  # TARGET_VOL_EXPANSION
        print(f"  [80%] All targets created at {datetime.now().isoformat(timespec='seconds')}")

        # Step 7: Normalize features
        df = self.normalize_features(df)

        # Step 8: Cleanup
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 9: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    # ------------------------------------------------------------------
    # set_11: set_10 + macro features (FRED + COT)
    # ------------------------------------------------------------------
    def process_set_11(self) -> pd.DataFrame:
        """Macro-augmented 5-min dataset.

        Pipeline:
        Identical to set_10 (via process_set_08) but adds external macro
        features after AlphaFactory:
        - FRED: VIX, OVX, DXY, Yield Curve (changes at 1/3/7/14/35D,
          percentiles at 14/35/60D, VIX/OVX ratio, yield curve sign)
        - CFTC COT: MM/Prod/Spec net positions, OI changes, percentiles

        These are daily/weekly signals forward-filled to every 5-min bar.
        No lookahead bias — each bar sees only already-published data.
        """
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - SET_11")
        print(f"  set_10 + FRED macro + CFTC COT features")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # Step 1: Load data
        df = self.load_data()
        total_rows = len(df)
        print(f"  [15%] Loaded {total_rows} rows at {datetime.now().isoformat(timespec='seconds')}")

        # Step 2: Add time features (includes day-of-week)
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 3: Add AlphaFactory features with extended + exhaustion clusters
        # Same as set_08/set_10
        windows = [
            1 * self.BARS_PER_DAY,   # 288 = 1 day
            3 * self.BARS_PER_DAY,   # 864 = 3 days
            7 * self.BARS_PER_DAY,   # 2016 = 7 days
            14 * self.BARS_PER_DAY,  # 4032 = 14 days
            35 * self.BARS_PER_DAY,  # 10080 = 35 days
        ]
        macro_windows = {
            "1D": 24, "3D": 72, "1W": 168, "2W": 336,
            "1M": 840, "3M": 2160,
        }
        df = AlphaFactory(df).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 4: Merge external macro features (FRED + COT)
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [60%] {n_macro} external macro features added at {datetime.now().isoformat(timespec='seconds')}")

        # Step 5: Add RAW columns for evaluation
        raw_horizon = 288
        future_high = df['High'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).max().iloc[::-1].shift(-1)
        future_low = df['Low'].iloc[::-1].rolling(
            window=raw_horizon, min_periods=1
        ).min().iloc[::-1].shift(-1)
        df['RAW_Close'] = df['Close'].copy()
        df['RAW_Future_High'] = future_high
        df['RAW_Future_Low'] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # Step 6: Create Triple Barrier targets (same as set_08/10)
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_12H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=144, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_24H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_3x1_24H",
            tp_atr_mult=3.0, sl_atr_mult=1.0,
            max_horizon=288, atr_period=14,
        )
        df = self.add_return_target(df, horizons=[144, 288])
        print(f"  [80%] All targets created at {datetime.now().isoformat(timespec='seconds')}")

        # Step 7: Normalize features
        df = self.normalize_features(df)

        # Step 8: Cleanup
        df = self.cleanup(df, drop_raw_returns=True, keep_ohlcv=self.keep_ohlcv)

        # Step 9: Save
        saved_path = self.save(df)
        print(f"  [100%] Saved output at {datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete!")
        print(f"Output: {saved_path}")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # HourSet_06: Consolidated pipeline with stationary indicators
    # ------------------------------------------------------------------
    def process_hourset_06(self) -> pd.DataFrame:
        """Consolidated 1-Hour swing-trading dataset with stationary indicators.

        Single entry-point pipeline — supersedes HourSet_01, HourSet_02,
        and the two-step HourSet_04 -> HourSet_05 build process.
        Invoke via: DataProcessor(dataset_version='HourSet_06').process()


        Features vs HourSet_02
        ----------------------
        - MACD replaced by PPO (Percentage Price Oscillator, dimensionless).
        - TREND_LR_SLOPE normalised by Close ($/bar -> %/bar).
        - TREND_DMA_DIST_25_5  : (Close − SMA25.shift(5)) / SMA25.shift(5).
        - ICHIMOKU_TK_SPREAD, ICHIMOKU_CLOUD_DIST, ICHIMOKU_CLOUD_THICKNESS,
          ICHIMOKU_ABOVE_CLOUD : stationary relational Ichimoku features.

        Full target suite (all consolidated from add_hourly_targets.py)
        ---------------------------------------------------------------
        Swing targets  (tp×sl, horizon):
            1.5x1, 2.0x1, 2.5x1  @  72H and 120H  (HourSet_02 originals)
            2.0x1                 @  3H, 6H, 12H    (short-horizon)
            1.0x0.5               @  3H, 6H, 12H    (tight SL)
            1.0x2                 @  3H, 6H, 12H    (wide SL)
        Continuous return targets: TARGET_RET_72, TARGET_RET_120.
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_03")
        print("  1-Hour bars + stationary indicators + full target suite")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        print(f"  [10%] Loaded {len(df)} 5-min rows")
        df = self.resample_to_hourly(df)
        print(f"  [15%] Resampled to {len(df)} hourly bars at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added")

        # ── Step 3: AlphaFactory — stationary indicators enabled ──────
        # Windows in *bars* (1H data, bars_per_hour=1):
        #   24=1d, 72=3d, 168=1w, 336=2w, 840=5w
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,        # TREND_DMA_DIST_25_5
            include_ichimoku=True,   # ICHIMOKU_* (4 stationary features)
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [55%] {n_macro} external macro features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        future_high = (
            df["High"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .max().iloc[::-1].shift(-1)
        )
        future_low = (
            df["Low"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .min().iloc[::-1].shift(-1)
        )
        df["RAW_Close"] = df["Close"].copy()
        df["RAW_Future_High"] = future_high
        df["RAW_Future_Low"] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Full target suite ─────────────────────────────────
        # 6a: Swing targets — 1.5x/2.0x/2.5x TP, 1x SL @ 72H and 120H
        for tp_mult in [1.5, 2.0, 2.5]:
            tp_label = str(tp_mult).replace(".", "p")
            for horizon_h in [72, 120]:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=1.0,
                    max_horizon=horizon_h,
                    atr_period=14,
                )

        # 6b: Short-horizon targets @ 3H, 6H, 12H
        # (previously built by the separate add_hourly_targets.py script)
        short_horizons = {"3H": 3, "6H": 6, "12H": 12}
        for tag, hours in short_horizons.items():
            # 2x1 ATR (original short-horizon set from HourSet_04)
            df = self.add_triple_barrier_target(
                df,
                prefix=f"TARGET_TRIPLE_2x1_{tag}",
                tp_atr_mult=2.0,
                sl_atr_mult=1.0,
                max_horizon=hours,
                atr_period=14,
            )
            # 1x0.5 ATR (tight SL, from add_hourly_targets.py)
            df = self.add_triple_barrier_target(
                df,
                prefix=f"TARGET_TRIPLE_1x0.5_{tag}",
                tp_atr_mult=1.0,
                sl_atr_mult=0.5,
                max_horizon=hours,
                atr_period=14,
            )
            # 1x2 ATR (wide SL, from add_hourly_targets.py)
            df = self.add_triple_barrier_target(
                df,
                prefix=f"TARGET_TRIPLE_1x2_{tag}",
                tp_atr_mult=1.0,
                sl_atr_mult=2.0,
                max_horizon=hours,
                atr_period=14,
            )

        # 6c: Continuous return targets
        df = self.add_return_target(df, horizons=[72, 120])
        print(f"  [80%] All targets created at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: Cleanup ───────────────────────────────────────────
        # 2,200 warmup rows covers the longest feature window at 1H bars
        # (MACRO_6M = 4,320 bars is the theoretical max, but the first
        #  ~2,200 rows are sufficient to let all features stabilise).
        df = self.cleanup(
            df,
            drop_raw_returns=True,
            warmup_rows=2200,
            max_warmup_bars=2200,
            keep_ohlcv=self.keep_ohlcv,
        )

        # ── Step 9: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print(f"  [100%] Saved output at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete — HourSet_06")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        target_cols = sorted(c for c in df.columns if c.startswith("TARGET_"))
        print(f"Targets: {target_cols}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_hourset_07(self) -> pd.DataFrame:
        """Consolidated 1-Hour swing-trading dataset with stationary indicators.

        Single entry-point pipeline — supersedes HourSet_01, HourSet_02,
        and the two-step HourSet_04 -> HourSet_05 build process.
        Invoke via: DataProcessor(dataset_version='HourSet_07').process()


        Features vs HourSet_02
        ----------------------
        - MACD replaced by PPO (Percentage Price Oscillator, dimensionless).
        - TREND_LR_SLOPE normalised by Close ($/bar -> %/bar).
        - TREND_DMA_DIST_25_5  : (Close − SMA25.shift(5)) / SMA25.shift(5).
        - ICHIMOKU_TK_SPREAD, ICHIMOKU_CLOUD_DIST, ICHIMOKU_CLOUD_THICKNESS,
          ICHIMOKU_ABOVE_CLOUD : stationary relational Ichimoku features.

        Full target suite (all consolidated from add_hourly_targets.py)
        ---------------------------------------------------------------
        Swing targets  (tp×sl, horizon):
            1.5x1, 2.0x1, 2.5x1  @  72H and 120H  (HourSet_02 originals)
            2.0x1                 @  3H, 6H, 12H    (short-horizon)
            1.0x0.5               @  3H, 6H, 12H    (tight SL)
            1.0x2                 @  3H, 6H, 12H    (wide SL)
        Continuous return targets: TARGET_RET_72, TARGET_RET_120.
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_07")
        print("  1-Hour bars + stationary indicators + full target suite")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        print(f"  [10%] Loaded {len(df)} 5-min rows")
        df = self.resample_to_hourly(df)
        print(f"  [15%] Resampled to {len(df)} hourly bars at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added")

        # ── Step 3: AlphaFactory — stationary indicators enabled ──────
        # Windows in *bars* (1H data, bars_per_hour=1):
        #   24=1d, 72=3d, 168=1w, 336=2w, 840=5w
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,        # TREND_DMA_DIST_25_5
            include_ichimoku=True,   # ICHIMOKU_* (4 stationary features)
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [55%] {n_macro} external macro features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        future_high = (
            df["High"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .max().iloc[::-1].shift(-1)
        )
        future_low = (
            df["Low"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .min().iloc[::-1].shift(-1)
        )
        df["RAW_Close"] = df["Close"].copy()
        df["RAW_Future_High"] = future_high
        df["RAW_Future_Low"] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Full target suite ─────────────────────────────────
        # 6a: Swing targets — 1.5x/2.0x/2.5x TP, 1x SL @ 72H and 120H
        for tp_mult in [1.5]:
            tp_label = "1p5"
            for horizon_h in [24]:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=1.0,
                    max_horizon=horizon_h,
                    atr_period=14,
                )

        # 6b: Short-horizon targets @ 3H, 6H, 12H
        # (previously built by the separate add_hourly_targets.py script)
        short_horizons = {"3H": 3, "6H": 6, "12H": 12}
        for tag, hours in short_horizons.items():
            # 2x1 ATR (original short-horizon set from HourSet_04)
            df = self.add_triple_barrier_target(
                df,
                prefix=f"TARGET_TRIPLE_2x1_{tag}",
                tp_atr_mult=2.0,
                sl_atr_mult=1.0,
                max_horizon=hours,
                atr_period=14,
            )
            # 1x0.5 ATR (tight SL, from add_hourly_targets.py)
            df = self.add_triple_barrier_target(
                df,
                prefix=f"TARGET_TRIPLE_1x0.5_{tag}",
                tp_atr_mult=1.0,
                sl_atr_mult=0.5,
                max_horizon=hours,
                atr_period=14,
            )
            # 1x2 ATR (wide SL, from add_hourly_targets.py)
            df = self.add_triple_barrier_target(
                df,
                prefix=f"TARGET_TRIPLE_1x2_{tag}",
                tp_atr_mult=1.0,
                sl_atr_mult=2.0,
                max_horizon=hours,
                atr_period=14,
            )

        # 6c: Continuous return targets
        df = self.add_return_target(df, horizons=[72, 120])
        print(f"  [80%] All targets created at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: Cleanup ───────────────────────────────────────────
        # 2,200 warmup rows covers the longest feature window at 1H bars
        # (MACRO_6M = 4,320 bars is the theoretical max, but the first
        #  ~2,200 rows are sufficient to let all features stabilise).
        df = self.cleanup(
            df,
            drop_raw_returns=True,
            warmup_rows=2200,
            max_warmup_bars=2200,
            keep_ohlcv=self.keep_ohlcv,
        )

        # ── Step 9: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print(f"  [100%] Saved output at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete — HourSet_07")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        target_cols = sorted(c for c in df.columns if c.startswith("TARGET_"))
        print(f"Targets: {target_cols}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_hourset_08(self) -> pd.DataFrame:
        """Precision-driven target refactor for 1-Hour swing-trading dataset.
        
        Removes noisy 1x2 and 1x0.5 targets, expands asymmetric targets (3x1, 4x1, 5x1) 
        for 6H, 12H, and 24H horizons, and retains core 2x1 targets.
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_08")
        print("  1-Hour bars + stationary indicators + precision targets")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        print(f"  [10%] Loaded {len(df)} 5-min rows")
        df = self.resample_to_hourly(df)
        print(f"  [15%] Resampled to {len(df)} hourly bars at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added")

        # ── Step 3: AlphaFactory — stationary indicators enabled ──────
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,
            include_ichimoku=True,
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [55%] {n_macro} external macro features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        future_high = (
            df["High"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .max().iloc[::-1].shift(-1)
        )
        future_low = (
            df["Low"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .min().iloc[::-1].shift(-1)
        )
        df["RAW_Close"] = df["Close"].copy()
        df["RAW_Future_High"] = future_high
        df["RAW_Future_Low"] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Precision Target Suite ────────────────────────────
        # 2x1 core targets + 3x1, 4x1, 5x1 asymmetric targets
        target_multipliers = [2.0, 3.0, 4.0, 5.0]
        target_horizons = [6, 12, 24]
        
        for tp_mult in target_multipliers:
            tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
            for horizon_h in target_horizons:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=1.0,
                    max_horizon=horizon_h,
                    atr_period=14,
                )

        # 6c: Continuous return targets
        df = self.add_return_target(df, horizons=[6, 12, 24, 72, 120])
        print(f"  [80%] All targets created at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: Cleanup ───────────────────────────────────────────
        df = self.cleanup(
            df,
            drop_raw_returns=True,
            warmup_rows=2200,
            max_warmup_bars=2200,
            keep_ohlcv=self.keep_ohlcv,
        )

        # ── Step 9: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print(f"  [100%] Saved output at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete — HourSet_08")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        target_cols = sorted(c for c in df.columns if c.startswith("TARGET_"))
        print(f"Targets: {target_cols}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def _load_and_resample_exec(self, exec_ohlcv_path: str) -> pd.DataFrame:
        """Helper to load and resample raw execution data."""
        orig_path = self.input_path
        self.input_path = exec_ohlcv_path
        try:
            raw_df = self.load_data()
            raw_1h = self.resample_to_hourly(raw_df)
            return raw_1h
        finally:
            self.input_path = orig_path

    def process_hourset_09(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """HourSet_08 + Term Structure Shape features (Diff/Ratio/Inversion).

        Identical pipeline to HourSet_08 with `include_term_structure=True`
        to add ~108 TS_* features capturing cross-window velocity, magnitude,
        and regime shift signals via Protocol A (Anchor to 840-bar window).

        New features (all Optuna-toggleable via "term_structure" bucket):
            TS_{FAMILY}_DIFF_{fast}v{slow}    — absolute spread
            TS_{FAMILY}_RATIO_{fast}v{slow}   — relative magnitude
            TS_{FAMILY}_INVERT_{fast}v{slow}  — boolean regime shift

        Families: VOL_PARK, VOL_YZ, DONCHIAN, LR_SLOPE, VWAP_DIST, CMF,
                  STOCH_K, EFFICIENCY, CORWIN
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_09")
        print("  1-Hour bars + stationary indicators + term structure shapes")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        print(f"  [10%] Loaded {len(df)} 5-min rows")
        df = self.resample_to_hourly(df)
        print(f"  [15%] Resampled to {len(df)} hourly bars at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added")

        # ── Step 3: AlphaFactory — stationary indicators + TS shapes ──
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,
            include_ichimoku=True,
            include_term_structure=True,
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [55%] {n_macro} external macro features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        
        if exec_ohlcv_path:
            print(f"  - Loading raw execution data from {exec_ohlcv_path}...")
            raw_1h = self._load_and_resample_exec(exec_ohlcv_path)
            raw_1h = raw_1h.reindex(df.index)
            
            df["EXEC_Open"] = raw_1h["Open"]
            df["EXEC_High"] = raw_1h["High"]
            df["EXEC_Low"] = raw_1h["Low"]
            df["EXEC_Close"] = raw_1h["Close"]
            
            exec_tr = np.maximum(
                raw_1h["High"] - raw_1h["Low"],
                np.maximum(
                    (raw_1h["High"] - raw_1h["Close"].shift(1)).abs(),
                    (raw_1h["Low"] - raw_1h["Close"].shift(1)).abs(),
                ),
            )
            df["EXEC_ATR_14"] = exec_tr.rolling(14).mean()
            
            future_high = raw_1h["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = raw_1h["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            
            df["RAW_Close"] = raw_1h["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added EXEC_* columns and updated RAW_ columns from raw data")
        else:
            future_high = (
                df["High"].iloc[::-1]
                .rolling(window=raw_horizon, min_periods=1)
                .max().iloc[::-1].shift(-1)
            )
            future_low = (
                df["Low"].iloc[::-1]
                .rolling(window=raw_horizon, min_periods=1)
                .min().iloc[::-1].shift(-1)
            )
            df["RAW_Open"] = df["Open"].copy()
            df["RAW_High"] = df["High"].copy()
            df["RAW_Low"] = df["Low"].copy()
            df["RAW_Close"] = df["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added RAW_Open, RAW_High, RAW_Low, RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Precision Target Suite ────────────────────────────
        target_multipliers = [2.0, 3.0, 4.0, 5.0]
        target_horizons = [6, 12, 24]

        for tp_mult in target_multipliers:
            tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
            for horizon_h in target_horizons:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=1.0,
                    max_horizon=horizon_h,
                    atr_period=14,
                )

        # 6c: Continuous return targets
        df = self.add_return_target(df, horizons=[6, 12, 24, 72, 120])
        print(f"  [80%] All targets created at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: Cleanup ───────────────────────────────────────────
        df = self.cleanup(
            df,
            drop_raw_returns=True,
            warmup_rows=2200,
            max_warmup_bars=2200,
            keep_ohlcv=self.keep_ohlcv,
        )

        # ── Step 9: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print(f"  [100%] Saved output at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete — HourSet_09")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        ts_cols = sorted(c for c in df.columns if c.startswith("TS_"))
        target_cols = sorted(c for c in df.columns if c.startswith("TARGET_"))
        print(f"TS features: {len(ts_cols)}")
        print(f"Targets: {target_cols}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_hourset_10(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """HourSet_10: Superset feature generation with rigid filtering.
        
        Applies AlphaFactory with include_term_structure=True to generate 
        a superset of all legacy + new (LOG_RATIO, ZSCORE) features. 
        Then filters out mathematically redundant clones (VIF=inf, rho>0.95) 
        and poor distributions to leave exactly ~214 high-density features.
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_10")
        print("  1-Hour bars + topology-aware TS + pruned base features")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        df = self.resample_to_hourly(df)

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)

        # ── Step 3: AlphaFactory ──────────────────────────────────────
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,
            include_ichimoku=True,
            include_term_structure=True,
            macro_windows=macro_windows,
            log_progress=True,
        )

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        
        if exec_ohlcv_path:
            print(f"  - Loading raw execution data from {exec_ohlcv_path}...")
            raw_1h = self._load_and_resample_exec(exec_ohlcv_path)
            raw_1h = raw_1h.reindex(df.index)
            
            df["EXEC_Open"] = raw_1h["Open"]
            df["EXEC_High"] = raw_1h["High"]
            df["EXEC_Low"] = raw_1h["Low"]
            df["EXEC_Close"] = raw_1h["Close"]
            
            exec_tr = np.maximum(
                raw_1h["High"] - raw_1h["Low"],
                np.maximum(
                    (raw_1h["High"] - raw_1h["Close"].shift(1)).abs(),
                    (raw_1h["Low"] - raw_1h["Close"].shift(1)).abs(),
                ),
            )
            df["EXEC_ATR_14"] = exec_tr.rolling(14).mean()
            
            future_high = raw_1h["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = raw_1h["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            
            df["RAW_Close"] = raw_1h["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added EXEC_* columns and updated RAW_ columns from raw data")
        else:
            future_high = df["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = df["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            df["RAW_Close"] = df["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Precision Target Suite ────────────────────────────
        target_multipliers = [2.0, 3.0, 4.0, 5.0]
        target_horizons = [6, 12, 24]
        for tp_mult in target_multipliers:
            tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
            for horizon_h in target_horizons:
                df = self.add_triple_barrier_target(
                    df, prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult, sl_atr_mult=1.0, max_horizon=horizon_h, atr_period=14
                )
        df = self.add_return_target(df, horizons=[6, 12, 24, 72, 120])

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: HourSet_10 Pruning (Remove Collinearity) ──────────
        cols_to_drop = []
        for col in df.columns:
            # 1. Base Feature Pruning
            if col.startswith("VOLFLOW_DIVERGENCE_"): cols_to_drop.append(col)
            elif col.startswith("MACRO_POS_"): cols_to_drop.append(col)
            elif col.startswith("MOM_STOCH_K_") or col.startswith("MOM_STOCH_D_"): cols_to_drop.append(col)
            elif col.startswith("VOL_PARK_") or col.startswith("VOL_RS_"): cols_to_drop.append(col)
            elif col == "DIST_ZSCORE_72": cols_to_drop.append(col)
            elif col.startswith("TREND_LR_SLOPE_") and col not in ("TREND_LR_SLOPE_24", "TREND_LR_SLOPE_72"): cols_to_drop.append(col)
            elif col.startswith("LIQ_CORWIN_") and col not in ("LIQ_CORWIN_24", "LIQ_CORWIN_72"): cols_to_drop.append(col)
            
            # 2. Term Structure Pruning
            elif col.startswith("TS_"):
                if "STOCH_K" in col or "VOL_PARK" in col: cols_to_drop.append(col)
                elif "VOL_YZ" in col or "CORWIN" in col:
                    if "DIFF" in col or "INVERT" in col: cols_to_drop.append(col)
                    elif "RATIO" in col and "LOG_RATIO" not in col: cols_to_drop.append(col)
                elif "DONCHIAN" in col or "EFFICIENCY" in col:
                    if "RATIO" in col or "INVERT" in col: cols_to_drop.append(col)
                elif "LR_SLOPE" in col or "VWAP_DIST" in col or "CMF" in col:
                    if "SIGN_AGREE" in col: cols_to_drop.append(col)

        cols_to_drop = list(set(cols_to_drop))
        print(f"  [85%] Pruning {len(cols_to_drop)} redundant features for HourSet_10...")
        df.drop(columns=cols_to_drop, inplace=True, errors='ignore')

        # ── Step 9: Cleanup ───────────────────────────────────────────
        df = self.cleanup(
            df, drop_raw_returns=True, warmup_rows=2200, max_warmup_bars=2200, keep_ohlcv=self.keep_ohlcv
        )

        # ── Step 10: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print("=" * 60)
        print("Processing Complete — HourSet_10")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        print("=" * 60)

        self.df = df
        return df

    def process_hourset_11(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """Alias for process_hourset_09 logic."""
        return self.process_hourset_09(exec_ohlcv_path=exec_ohlcv_path)

    def process_hourset_12(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """Alias for process_hourset_10 logic."""
        return self.process_hourset_10(exec_ohlcv_path=exec_ohlcv_path)

    def process_hourset_13a(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """HourSet_08 + Term Structure Shape features (Diff/Ratio/Inversion).

        Identical pipeline to HourSet_08 with `include_term_structure=True`
        to add ~108 TS_* features capturing cross-window velocity, magnitude,
        and regime shift signals via Protocol A (Anchor to 840-bar window).

        New features (all Optuna-toggleable via "term_structure" bucket):
            TS_{FAMILY}_DIFF_{fast}v{slow}    — absolute spread
            TS_{FAMILY}_RATIO_{fast}v{slow}   — relative magnitude
            TS_{FAMILY}_INVERT_{fast}v{slow}  — boolean regime shift

        Families: VOL_PARK, VOL_YZ, DONCHIAN, LR_SLOPE, VWAP_DIST, CMF,
                  STOCH_K, EFFICIENCY, CORWIN
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_13A")
        print("  1-Hour bars + stationary indicators + term structure shapes")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        print(f"  [10%] Loaded {len(df)} 5-min rows")
        df = self.resample_to_hourly(df)
        print(f"  [15%] Resampled to {len(df)} hourly bars at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added")

        # ── Step 3: AlphaFactory — stationary indicators + TS shapes ──
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,
            include_ichimoku=True,
            include_term_structure=True,
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df, check_staleness=False)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [55%] {n_macro} external macro features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        
        if exec_ohlcv_path:
            print(f"  - Loading raw execution data from {exec_ohlcv_path}...")
            raw_1h = self._load_and_resample_exec(exec_ohlcv_path)
            raw_1h = raw_1h.reindex(df.index)
            
            df["EXEC_Open"] = raw_1h["Open"]
            df["EXEC_High"] = raw_1h["High"]
            df["EXEC_Low"] = raw_1h["Low"]
            df["EXEC_Close"] = raw_1h["Close"]
            
            exec_tr = np.maximum(
                raw_1h["High"] - raw_1h["Low"],
                np.maximum(
                    (raw_1h["High"] - raw_1h["Close"].shift(1)).abs(),
                    (raw_1h["Low"] - raw_1h["Close"].shift(1)).abs(),
                ),
            )
            df["EXEC_ATR_14"] = exec_tr.rolling(14).mean()
            
            future_high = raw_1h["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = raw_1h["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            
            df["RAW_Close"] = raw_1h["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added EXEC_* columns and updated RAW_ columns from raw data")
        else:
            future_high = (
                df["High"].iloc[::-1]
                .rolling(window=raw_horizon, min_periods=1)
                .max().iloc[::-1].shift(-1)
            )
            future_low = (
                df["Low"].iloc[::-1]
                .rolling(window=raw_horizon, min_periods=1)
                .min().iloc[::-1].shift(-1)
            )
            df["RAW_Open"] = df["Open"].copy()
            df["RAW_High"] = df["High"].copy()
            df["RAW_Low"] = df["Low"].copy()
            df["RAW_Close"] = df["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added RAW_Open, RAW_High, RAW_Low, RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Precision Target Suite ────────────────────────────
        target_multipliers = [2.0, 3.0, 4.0, 5.0]
        target_horizons = [6, 12, 24]

        for tp_mult in target_multipliers:
            tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
            for horizon_h in target_horizons:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=1.0,
                    max_horizon=horizon_h,
                    atr_period=14,
                )


        # --- NEW TARGETS (13A/13B) ---
        # 3H horizon: 2x1
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_3H",
            tp_atr_mult=2.0, sl_atr_mult=1.0, max_horizon=3, atr_period=14
        )
        
        # 36H and 48H horizons
        long_configs = [
            (4.0, 1.0, "4x1"),
            (5.0, 1.0, "5x1"),
            (6.0, 2.0, "6x2"),
            (8.0, 2.0, "8x2")
        ]
        for tp_mult, sl_mult, tag in long_configs:
            for horizon_h in [36, 48]:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tag}_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=sl_mult,
                    max_horizon=horizon_h,
                    atr_period=14,
                )
        # 6c: Continuous return targets
        df = self.add_return_target(df, horizons=[6, 12, 24, 72, 120])
        print(f"  [80%] All targets created at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: Cleanup ───────────────────────────────────────────
        df = self.cleanup(
            df,
            drop_raw_returns=True,
            warmup_rows=2200,
            max_warmup_bars=2200,
            keep_ohlcv=self.keep_ohlcv,
        )

        # ── Step 9: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print(f"  [100%] Saved output at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete — HourSet_13A")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        ts_cols = sorted(c for c in df.columns if c.startswith("TS_"))
        target_cols = sorted(c for c in df.columns if c.startswith("TARGET_"))
        print(f"TS features: {len(ts_cols)}")
        print(f"Targets: {target_cols}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df

    def process_hourset_13b(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """HourSet_13B: Superset feature generation with rigid filtering.
        
        Applies AlphaFactory with include_term_structure=True to generate 
        a superset of all legacy + new (LOG_RATIO, ZSCORE) features. 
        Then filters out mathematically redundant clones (VIF=inf, rho>0.95) 
        and poor distributions to leave exactly ~214 high-density features.
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_13B")
        print("  1-Hour bars + topology-aware TS + pruned base features")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        df = self.resample_to_hourly(df)

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)

        # ── Step 3: AlphaFactory ──────────────────────────────────────
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,
            include_ichimoku=True,
            include_term_structure=True,
            macro_windows=macro_windows,
            log_progress=True,
        )

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df, check_staleness=False)

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        
        if exec_ohlcv_path:
            print(f"  - Loading raw execution data from {exec_ohlcv_path}...")
            raw_1h = self._load_and_resample_exec(exec_ohlcv_path)
            raw_1h = raw_1h.reindex(df.index)
            
            df["EXEC_Open"] = raw_1h["Open"]
            df["EXEC_High"] = raw_1h["High"]
            df["EXEC_Low"] = raw_1h["Low"]
            df["EXEC_Close"] = raw_1h["Close"]
            
            exec_tr = np.maximum(
                raw_1h["High"] - raw_1h["Low"],
                np.maximum(
                    (raw_1h["High"] - raw_1h["Close"].shift(1)).abs(),
                    (raw_1h["Low"] - raw_1h["Close"].shift(1)).abs(),
                ),
            )
            df["EXEC_ATR_14"] = exec_tr.rolling(14).mean()
            
            future_high = raw_1h["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = raw_1h["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            
            df["RAW_Close"] = raw_1h["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added EXEC_* columns and updated RAW_ columns from raw data")
        else:
            future_high = df["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = df["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            df["RAW_Close"] = df["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Precision Target Suite ────────────────────────────
        target_multipliers = [2.0, 3.0, 4.0, 5.0]
        target_horizons = [6, 12, 24]
        for tp_mult in target_multipliers:
            tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
            for horizon_h in target_horizons:
                df = self.add_triple_barrier_target(
                    df, prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult, sl_atr_mult=1.0, max_horizon=horizon_h, atr_period=14
                )

        # --- NEW TARGETS (13A/13B) ---
        # 3H horizon: 2x1
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_3H",
            tp_atr_mult=2.0, sl_atr_mult=1.0, max_horizon=3, atr_period=14
        )
        
        # 36H and 48H horizons
        long_configs = [
            (4.0, 1.0, "4x1"),
            (5.0, 1.0, "5x1"),
            (6.0, 2.0, "6x2"),
            (8.0, 2.0, "8x2")
        ]
        for tp_mult, sl_mult, tag in long_configs:
            for horizon_h in [36, 48]:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tag}_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=sl_mult,
                    max_horizon=horizon_h,
                    atr_period=14,
                )
        df = self.add_return_target(df, horizons=[6, 12, 24, 72, 120])

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: HourSet_13B Pruning (Remove Collinearity) ──────────
        cols_to_drop = []
        for col in df.columns:
            # 1. Base Feature Pruning
            if col.startswith("VOLFLOW_DIVERGENCE_"): cols_to_drop.append(col)
            elif col.startswith("MACRO_POS_"): cols_to_drop.append(col)
            elif col.startswith("MOM_STOCH_K_") or col.startswith("MOM_STOCH_D_"): cols_to_drop.append(col)
            elif col.startswith("VOL_PARK_") or col.startswith("VOL_RS_"): cols_to_drop.append(col)
            elif col == "DIST_ZSCORE_72": cols_to_drop.append(col)
            elif col.startswith("TREND_LR_SLOPE_") and col not in ("TREND_LR_SLOPE_24", "TREND_LR_SLOPE_72"): cols_to_drop.append(col)
            elif col.startswith("LIQ_CORWIN_") and col not in ("LIQ_CORWIN_24", "LIQ_CORWIN_72"): cols_to_drop.append(col)
            
            # 2. Term Structure Pruning
            elif col.startswith("TS_"):
                if "STOCH_K" in col or "VOL_PARK" in col: cols_to_drop.append(col)
                elif "VOL_YZ" in col or "CORWIN" in col:
                    if "DIFF" in col or "INVERT" in col: cols_to_drop.append(col)
                    elif "RATIO" in col and "LOG_RATIO" not in col: cols_to_drop.append(col)
                elif "DONCHIAN" in col or "EFFICIENCY" in col:
                    if "RATIO" in col or "INVERT" in col: cols_to_drop.append(col)
                elif "LR_SLOPE" in col or "VWAP_DIST" in col or "CMF" in col:
                    if "SIGN_AGREE" in col: cols_to_drop.append(col)

        cols_to_drop = list(set(cols_to_drop))
        print(f"  [85%] Pruning {len(cols_to_drop)} redundant features for HourSet_13B...")
        df.drop(columns=cols_to_drop, inplace=True, errors='ignore')

        # ── Step 9: Cleanup ───────────────────────────────────────────
        df = self.cleanup(
            df, drop_raw_returns=True, warmup_rows=2200, max_warmup_bars=2200, keep_ohlcv=self.keep_ohlcv
        )

        # ── Step 10: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print("=" * 60)
        print("Processing Complete — HourSet_13B")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        print("=" * 60)

        self.df = df
        return df

    def process_hourset_15b(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """HourSet_15B: Superset feature generation with rigid filtering.
        
        Applies AlphaFactory with include_term_structure=True to generate 
        a superset of all legacy + new (LOG_RATIO, ZSCORE) features. 
        Then filters out mathematically redundant clones (VIF=inf, rho>0.95) 
        and poor distributions to leave exactly ~214 high-density features.
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOURSET_15B")
        print("  1-Hour bars + topology-aware TS + pruned base features")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 1H ───────────────
        df = self.load_data()
        df = self.resample_to_hourly(df)

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)

        # ── Step 3: AlphaFactory ──────────────────────────────────────
        windows = [24, 72, 168, 336, 840]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=1).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,
            include_ichimoku=True,
            include_term_structure=True,
            macro_windows=macro_windows,
            log_progress=True,
        )

        # ── Step 4: External macro features (FRED + COT) ─────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df, check_staleness=False)

        # ── Step 5: RAW columns for evaluation (120H forward window) ──
        raw_horizon = 120
        
        if exec_ohlcv_path:
            print(f"  - Loading raw execution data from {exec_ohlcv_path}...")
            raw_1h = self._load_and_resample_exec(exec_ohlcv_path)
            raw_1h = raw_1h.reindex(df.index)
            
            df["EXEC_Open"] = raw_1h["Open"]
            df["EXEC_High"] = raw_1h["High"]
            df["EXEC_Low"] = raw_1h["Low"]
            df["EXEC_Close"] = raw_1h["Close"]
            
            exec_tr = np.maximum(
                raw_1h["High"] - raw_1h["Low"],
                np.maximum(
                    (raw_1h["High"] - raw_1h["Close"].shift(1)).abs(),
                    (raw_1h["Low"] - raw_1h["Close"].shift(1)).abs(),
                ),
            )
            df["EXEC_ATR_14"] = exec_tr.rolling(14).mean()
            
            future_high = raw_1h["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = raw_1h["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            
            df["RAW_Close"] = raw_1h["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added EXEC_* columns and updated RAW_ columns from raw data")
        else:
            future_high = df["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = df["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            df["RAW_Close"] = df["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Precision Target Suite ────────────────────────────
        target_multipliers = [2.0, 3.0, 4.0, 5.0]
        target_horizons = [6, 12, 24]
        for tp_mult in target_multipliers:
            tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
            for horizon_h in target_horizons:
                df = self.add_triple_barrier_target(
                    df, prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                    tp_atr_mult=tp_mult, sl_atr_mult=1.0, max_horizon=horizon_h, atr_period=14
                )

        # --- NEW TARGETS (13A/13B) ---
        # 3H horizon: 2x1
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_3H",
            tp_atr_mult=2.0, sl_atr_mult=1.0, max_horizon=3, atr_period=14
        )
        
        # 36H and 48H horizons
        long_configs = [
            (4.0, 1.0, "4x1"),
            (5.0, 1.0, "5x1"),
            (6.0, 2.0, "6x2"),
            (8.0, 2.0, "8x2")
        ]
        for tp_mult, sl_mult, tag in long_configs:
            for horizon_h in [36, 48]:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tag}_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=sl_mult,
                    max_horizon=horizon_h,
                    atr_period=14,
                )
        df = self.add_return_target(df, horizons=[6, 12, 24, 72, 120])

        # --- NEW TARGETS (15B short-horizon) ---
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_1x0p5_1H",
            tp_atr_mult=1.0, sl_atr_mult=0.5, max_horizon=1, atr_period=14
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_2H",
            tp_atr_mult=2.0, sl_atr_mult=1.0, max_horizon=2, atr_period=14
        )
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_1H",
            tp_atr_mult=2.0, sl_atr_mult=1.0, max_horizon=1, atr_period=14
        )

        # ── Step 7: Normalize features ────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: HourSet_15B Pruning (Remove Collinearity) ──────────
        cols_to_drop = []
        for col in df.columns:
            # 1. Base Feature Pruning
            if col.startswith("VOLFLOW_DIVERGENCE_"): cols_to_drop.append(col)
            elif col.startswith("MACRO_POS_"): cols_to_drop.append(col)
            elif col.startswith("MOM_STOCH_K_") or col.startswith("MOM_STOCH_D_"): cols_to_drop.append(col)
            elif col.startswith("VOL_PARK_") or col.startswith("VOL_RS_"): cols_to_drop.append(col)
            elif col == "DIST_ZSCORE_72": cols_to_drop.append(col)
            elif col.startswith("TREND_LR_SLOPE_") and col not in ("TREND_LR_SLOPE_24", "TREND_LR_SLOPE_72"): cols_to_drop.append(col)
            elif col.startswith("LIQ_CORWIN_") and col not in ("LIQ_CORWIN_24", "LIQ_CORWIN_72"): cols_to_drop.append(col)
            
            # 2. Term Structure Pruning
            elif col.startswith("TS_"):
                if "STOCH_K" in col or "VOL_PARK" in col: cols_to_drop.append(col)
                elif "VOL_YZ" in col or "CORWIN" in col:
                    if "DIFF" in col or "INVERT" in col: cols_to_drop.append(col)
                    elif "RATIO" in col and "LOG_RATIO" not in col: cols_to_drop.append(col)
                elif "DONCHIAN" in col or "EFFICIENCY" in col:
                    if "RATIO" in col or "INVERT" in col: cols_to_drop.append(col)
                elif "LR_SLOPE" in col or "VWAP_DIST" in col or "CMF" in col:
                    if "SIGN_AGREE" in col: cols_to_drop.append(col)

        cols_to_drop = list(set(cols_to_drop))
        print(f"  [85%] Pruning {len(cols_to_drop)} redundant features for HourSet_15B...")
        df.drop(columns=cols_to_drop, inplace=True, errors='ignore')

        # ── Step 9: Cleanup ───────────────────────────────────────────
        df = self.cleanup(
            df, drop_raw_returns=True, warmup_rows=2200, max_warmup_bars=2200, keep_ohlcv=self.keep_ohlcv
        )

        # ── Step 10: Save ──────────────────────────────────────────────
        saved_path = self.save(df)
        print("=" * 60)
        print("Processing Complete — HourSet_15B")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        print("=" * 60)

        self.df = df
        return df

    def process_hour4set_01(self) -> pd.DataFrame:
        """4-Hour bar dataset for multi-day swing trading.

        Same feature architecture as HourSet_08 with all AlphaFactory windows
        scaled from 1H to 4H bar resolution.  Targets use 24H/48H/96H horizons
        (6/12/24 bars at 4H) — the same bar-count structure as HourSet_08 but
        representing longer calendar time appropriate for 4H swing strategies.

        Pipeline
        --------
        1. Load 5-min raw data
        2. Resample to 4H bars via ``resample_ohlcv('4h')``
        3. Add cyclical time + day-of-week features
        4. AlphaFactory: windows = [6, 18, 42, 84, 210] bars
           (1d, 3d, 1w, 2w, 5w in calendar time)
        5. External macro features (FRED + COT)
        6. RAW columns (30-bar = 120H forward window)
        7. Triple Barrier targets: 2x1, 3x1, 4x1, 5x1 @ 6, 12, 24 bars
        8. Continuous return targets at 6, 12, 24 bars
        9. Normalize + cleanup (600 warmup) + save
        """
        start_time = datetime.now()
        print("=" * 60)
        print("Starting Data Processing Pipeline - HOUR4SET_01")
        print("  4-Hour bars + stationary indicators + precision targets")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load 5-min data and resample to 4H ────────────────
        df = self.load_data()
        print(f"  [10%] Loaded {len(df)} 5-min rows")
        df = self.resample_ohlcv(df, "4h")
        print(f"  [15%] Resampled to {len(df)} 4-hour bars at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 2: Time features ──────────────────────────────────────
        df = self.add_time_features(df, include_day_of_week=True)
        print(f"  [20%] Time features added")

        # ── Step 3: AlphaFactory — windows scaled to 4H bars ──────────
        # Calendar equivalents: 1d=6, 3d=18, 1w=42, 2w=84, 5w=210
        windows = [6, 18, 42, 84, 210]
        macro_windows = {
            "1W": 168, "2W": 336, "1M": 840,
            "3M": 2160, "6M": 4320,
        }
        df = AlphaFactory(df, bars_per_hour=0.25).add_all_features(
            windows=windows,
            include_momentum=True,
            include_macro=True,
            include_extended=True,
            include_dma=True,
            include_ichimoku=True,
            macro_windows=macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4: External macro features (FRED + COT) ──────────────
        macro_engine = MacroFeatureEngine()
        df = macro_engine.merge_all(df)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [55%] {n_macro} external macro features added at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 5: RAW columns for evaluation (30 bars = 120H) ───────
        raw_horizon = 30  # 30 × 4H = 120H = 5 trading days
        future_high = (
            df["High"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .max().iloc[::-1].shift(-1)
        )
        future_low = (
            df["Low"].iloc[::-1]
            .rolling(window=raw_horizon, min_periods=1)
            .min().iloc[::-1].shift(-1)
        )
        df["RAW_Close"] = df["Close"].copy()
        df["RAW_Future_High"] = future_high
        df["RAW_Future_Low"] = future_low
        print("  - Added RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Precision Target Suite ─────────────────────────────
        # Horizons in 4H bars: 6=24H (1d), 12=48H (2d), 24=96H (4d)
        # Same bar-count structure as HourSet_08 but longer calendar time
        target_multipliers = [2.0, 3.0, 4.0, 5.0]
        target_horizons_4h = {"24H": 6, "48H": 12, "96H": 24}

        for tp_mult in target_multipliers:
            tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
            for tag, horizon_bars in target_horizons_4h.items():
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tp_label}x1_{tag}",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=1.0,
                    max_horizon=horizon_bars,
                    atr_period=14,
                )

        # Continuous return targets
        df = self.add_return_target(df, horizons=[6, 12, 24])
        print(f"  [80%] All targets created at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        # ── Step 7: Normalize features ─────────────────────────────────
        df = self.normalize_features(df)

        # ── Step 8: Cleanup ────────────────────────────────────────────
        # 600 warmup rows at 4H ≈ 100 days; covers longest macro window
        # (6M = 4320 hours × 0.25 bars/hr = 1080 bars, but first ~600
        #  rows are sufficient for feature stabilisation after ffill)
        df = self.cleanup(
            df,
            drop_raw_returns=True,
            warmup_rows=600,
            max_warmup_bars=600,
            keep_ohlcv=self.keep_ohlcv,
        )

        # ── Step 9: Save ───────────────────────────────────────────────
        saved_path = self.save(df)
        print(f"  [100%] Saved output at "
              f"{datetime.now().isoformat(timespec='seconds')}")

        print("=" * 60)
        print("Processing Complete — Hour4Set_01")
        print(f"Output : {saved_path}")
        print(f"Shape  : {df.shape}")
        target_cols = sorted(c for c in df.columns if c.startswith("TARGET_"))
        print(f"Targets: {target_cols}")
        duration = datetime.now() - start_time
        print(f"Wall time: {str(duration).split('.')[0]}")
        print("=" * 60)

        self.df = df
        return df


    def process_from_config(self, exec_ohlcv_path: Optional[str] = None) -> pd.DataFrame:
        """Process data dynamically based on the injected DataWorkflowConfig."""
        if not self.config:
            raise ValueError("DataWorkflowConfig must be provided to DataProcessor to use process_from_config.")
        
        cfg = self.config
        
        start_time = datetime.now()
        print("=" * 60)
        print(f"Starting Data Processing Pipeline - {self.symbol} {self._dataset_version}")
        print(f"  Config-driven features & targets")
        print(f"Started at: {start_time.isoformat(timespec='seconds')}")
        print("=" * 60)

        # ── Step 1: Load data and resample ───────────────
        df = self.load_data()
        print(f"  [10%] Loaded {len(df)} 5-min rows")
        if cfg.resolution == "1h":
            df = self.resample_to_hourly(df)
            print(f"  [15%] Resampled to {len(df)} hourly bars at {datetime.now().isoformat(timespec='seconds')}")
            bars_per_hour = 1
        elif cfg.resolution == "4h":
            df = self.resample_to_4hourly(df)
            bars_per_hour = 1/4
        else:
            bars_per_hour = 12 # assuming 5-min

        # ── Step 2: Time features ─────────────────────────────────────
        df = self.add_time_features(
            df,
            include_day_of_week=True,
            include_month=cfg.features.include_month_encoding,
        )
        print(f"  [20%] Time features added")

        # ── Step 3: AlphaFactory ──────────────────────────────────────
        df = AlphaFactory(df, bars_per_hour=bars_per_hour).add_all_features(
            windows=cfg.features.windows,
            include_momentum=cfg.features.include_momentum,
            include_macro=cfg.features.include_macro,
            include_extended=cfg.features.include_extended,
            include_dma=cfg.features.include_dma,
            include_ichimoku=cfg.features.include_ichimoku,
            include_term_structure=cfg.features.include_term_structure,
            macro_windows=cfg.features.macro_windows,
            log_progress=True,
        )
        print(f"  [50%] AlphaFactory features added at {datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4: External macro features ───────────────────────────
        from src.core.instrument_master import get_instrument
        macro_engine = MacroFeatureEngine(instrument=get_instrument(self.symbol))
        df = macro_engine.merge_all(df, check_staleness=False)
        n_macro = len(macro_engine.get_feature_names())
        print(f"  [55%] {n_macro} external macro features added at {datetime.now().isoformat(timespec='seconds')}")

        # ── Step 4.5: Futures-curve calendar-spread features (gated) ──
        if cfg.features.include_curve_spread:
            from src.features.curve_features import CurveFeatureEngine
            curve_engine = CurveFeatureEngine(
                front_leg_csv=cfg.features.curve_front_leg_csv,
                second_leg_csv=cfg.features.curve_second_leg_csv,
                seasonal_bucket=cfg.features.curve_seasonal_bucket,
                seasonal_min_prior_years=cfg.features.curve_seasonal_min_prior_years,
                seasonal_pctl=cfg.features.curve_seasonal_pctl,
            )
            df = curve_engine.merge_curve(df, max_leading_nan_bars=2200)
            print(f"  [57%] Curve calendar-spread features added at {datetime.now().isoformat(timespec='seconds')}")

        # ── Step 5: RAW columns ───────────────────────────────────────
        raw_horizon = cfg.targets.raw_horizon
        
        if exec_ohlcv_path:
            print(f"  - Loading raw execution data from {exec_ohlcv_path}...")
            raw_resampled = self._load_and_resample_exec(exec_ohlcv_path)
            raw_resampled = raw_resampled.reindex(df.index)
            
            df["EXEC_Open"] = raw_resampled["Open"]
            df["EXEC_High"] = raw_resampled["High"]
            df["EXEC_Low"] = raw_resampled["Low"]
            df["EXEC_Close"] = raw_resampled["Close"]
            
            exec_tr = np.maximum(
                raw_resampled["High"] - raw_resampled["Low"],
                np.maximum(
                    (raw_resampled["High"] - raw_resampled["Close"].shift(1)).abs(),
                    (raw_resampled["Low"] - raw_resampled["Close"].shift(1)).abs(),
                ),
            )
            df["EXEC_ATR_14"] = exec_tr.rolling(14).mean()
            
            future_high = raw_resampled["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = raw_resampled["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            
            df["RAW_Close"] = raw_resampled["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added EXEC_* columns and updated RAW_ columns from raw data")
        else:
            future_high = df["High"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).max().iloc[::-1].shift(-1)
            future_low = df["Low"].iloc[::-1].rolling(window=raw_horizon, min_periods=1).min().iloc[::-1].shift(-1)
            df["RAW_Open"] = df["Open"].copy()
            df["RAW_High"] = df["High"].copy()
            df["RAW_Low"] = df["Low"].copy()
            df["RAW_Close"] = df["Close"].copy()
            df["RAW_Future_High"] = future_high
            df["RAW_Future_Low"] = future_low
            print("  - Added RAW_Open, RAW_High, RAW_Low, RAW_Close, RAW_Future_High, RAW_Future_Low")

        # ── Step 6: Config-driven Target Suite ────────────────────────
        for tdef in cfg.targets.definitions:
            if tdef.type == "triple_barrier_grid":
                for tp_mult in tdef.tp_multipliers:
                    tp_label = str(int(tp_mult)) if tp_mult.is_integer() else str(tp_mult).replace(".", "p")
                    for horizon_h in tdef.horizons:
                        df = self.add_triple_barrier_target(
                            df,
                            prefix=f"TARGET_TRIPLE_{tp_label}x1_{horizon_h}H",
                            tp_atr_mult=tp_mult,
                            sl_atr_mult=tdef.sl_multiplier,
                            max_horizon=horizon_h,
                            atr_period=cfg.targets.atr_period,
                        )
            elif tdef.type == "triple_barrier":
                tp_label = str(int(tdef.tp_multiplier)) if tdef.tp_multiplier.is_integer() else str(tdef.tp_multiplier).replace(".", "p")
                sl_label = str(int(tdef.sl_multiplier)) if tdef.sl_multiplier.is_integer() else str(tdef.sl_multiplier).replace(".", "p")
                suffix = "1" if tdef.sl_multiplier == 1.0 else sl_label
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tp_label}x{suffix}_{tdef.horizon}H",
                    tp_atr_mult=tdef.tp_multiplier,
                    sl_atr_mult=tdef.sl_multiplier,
                    max_horizon=tdef.horizon,
                    atr_period=cfg.targets.atr_period,
                )
            elif tdef.type == "continuous_return":
                df = self.add_return_target(df, horizons=tdef.horizons)
        print(f"  [70%] Targets added at {datetime.now().isoformat(timespec='seconds')}")

        # ── Step 7: Final processing ──────────────────────────────────
        df = self.normalize_features(df)
        print(f"  [85%] Features normalized")
        
        # ── Step 7.5: JSON Feature Pruning ────────────────────────────
        if cfg.features.drop_features:
            cols_to_drop = set()
            for pattern in cfg.features.drop_features:
                if pattern.endswith("*"):
                    prefix = pattern[:-1]
                    cols_to_drop.update([c for c in df.columns if c.startswith(prefix)])
                else:
                    if pattern in df.columns:
                        cols_to_drop.add(pattern)
            
            # Ensure we don't drop target or OHLCV columns by accident if wildcard was too broad
            safe_to_drop = [c for c in cols_to_drop if c in df.columns and not c.startswith("TARGET_") and c not in ["Open", "High", "Low", "Close", "Volume", "DateTime"]]
            df = df.drop(columns=safe_to_drop)
            print(f"  [-] Dropped {len(safe_to_drop)} features via JSON config rules")

        # Configs can specify warmup, but defaults are fine for now
        warmup = 2200 if cfg.resolution == "1h" else 600
        df = self.cleanup(df, warmup_rows=warmup, max_warmup_bars=warmup)
        print(f"  [95%] Cleanup complete, dropped first {warmup} rows")

        self.df = df
        print(f"  [100%] Process complete at {datetime.now().isoformat(timespec='seconds')}")
        print(f"  Final DataFrame shape: {self.df.shape}")
        
        self.save(self.df)
        return self.df


def main():
    """
    Main entry point for running the data processor.
    
    Processes CL.csv from C:/CL_Analyst_Data/data/raw/ and outputs to data/processed/.
    """
    import argparse
    parser = argparse.ArgumentParser(description="Data Processor")
    parser.add_argument("dataset_version", nargs="?", default="set_03", help="Dataset version to build (default: set_03)")
    parser.add_argument("--exec-data", default=None, help="Path to raw execution data for dual-data sets")
    parser.add_argument("--input", default="C:/CL_Analyst_Data/data/raw/CL.csv", help="Path to base data CSV")
    args = parser.parse_args()
    
    input_path = args.input
    if not os.path.exists(input_path):
        alt_path = "data/test100k.csv"
        if os.path.exists(alt_path):
            print(f"Note: {input_path} not found, using {alt_path}")
            input_path = alt_path
        else:
            print(f"Error: Could not find input file at {input_path} or {alt_path}")
            return
    
    processor = DataProcessor(input_path=input_path, dataset_version=args.dataset_version)
    
    try:
        df = processor.process(threshold=0.08, horizon=576, exec_ohlcv_path=args.exec_data)
        
        print("\nFeature Summary:")
        print("-" * 40)
        print(df.describe().T)
        
        if 'TARGET_DIR_8PCT_MULTI' in df.columns:
            print("\nTARGET_DIR_8PCT_MULTI Distribution:")
            print("-" * 40)
            print(df['TARGET_DIR_8PCT_MULTI'].value_counts().sort_index())
            
    except Exception as e:
        print(f"Error during processing: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
