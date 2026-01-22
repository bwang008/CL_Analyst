"""
Shared pytest fixtures for CL_Analyst test suite.

This module provides deterministic test data fixtures that are shared across
all test modules. Fixtures follow the "Data Contracts" philosophy - they
provide known, reproducible data for testing data validity and code correctness.

Fixture Categories:
- synthetic_price_data: Standard OHLCV data for general testing (1k bars)
- sufficient_history_data: 10k bars for VOL_30D (8640) warm-up
- toy_classification_data: Simple learnable data for model mechanics
- Edge cases: flat_market, zero_price, trending, midnight crossing
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta


# =============================================================================
# HELPER: MASSIVE DATA GENERATION (30-DAY WARM-UP)
# =============================================================================

def generate_ohlcv(length=10000, start_price=100.0, trend=0.0, sigma=0.1):
    """
    Generate OHLCV data with enough history for 30-day (8640 bar) warm-up.

    Args:
        length: Number of 5-minute bars
        start_price: Starting price level
        trend: Drift per bar (positive=up, negative=down)
        sigma: Std of random component in price changes

    Returns:
        pd.DataFrame: OHLCV with DateTime index
    """
    np.random.seed(42)
    dates = pd.date_range(start="2024-01-01", periods=length, freq="5min")

    # Random walk or trend
    changes = np.random.normal(0, sigma, size=length) + trend
    close = start_price + np.cumsum(changes)

    # Ensure High/Low physics
    high = close + np.abs(np.random.normal(0, 0.05, size=length))
    low = close - np.abs(np.random.normal(0, 0.05, size=length))
    open_ = close - changes  # open[i] ~ previous close
    volume = np.random.randint(100, 10000, size=length).astype(float)

    return pd.DataFrame({
        'Open': open_, 'High': high, 'Low': low, 'Close': close, 'Volume': volume
    }, index=dates)


# =============================================================================
# SYNTHETIC PRICE DATA FIXTURE
# =============================================================================

@pytest.fixture
def synthetic_price_data():
    """
    Generate deterministic OHLCV DataFrame for indicator testing.
    
    Properties:
    - 1000 rows of 5-minute bars
    - Fixed random seed (42) for reproducibility
    - Valid OHLCV structure: High >= max(Open, Close), Low <= min(Open, Close)
    - DateTime index starting from 2024-01-01 00:00
    - Realistic price movements around base price of 75.0 (typical CL range)
    
    Returns:
        pd.DataFrame: OHLCV DataFrame with DateTime index
    """
    np.random.seed(42)
    n_bars = 1000
    
    # Create datetime index with 5-minute frequency
    start_date = datetime(2024, 1, 1, 0, 0)
    date_index = pd.date_range(start=start_date, periods=n_bars, freq='5min')
    
    # Generate base price using random walk
    base_price = 75.0
    returns = np.random.normal(0, 0.001, n_bars)  # Small returns
    cumulative_returns = np.cumsum(returns)
    close_prices = base_price * (1 + cumulative_returns)
    
    # Generate OHLCV ensuring valid relationships
    # Open is previous close (with small gap)
    open_prices = np.roll(close_prices, 1) * (1 + np.random.normal(0, 0.0002, n_bars))
    open_prices[0] = base_price
    
    # High and Low relative to Open/Close
    bar_range = np.abs(np.random.normal(0.1, 0.05, n_bars))  # Range as percentage
    high_prices = np.maximum(open_prices, close_prices) * (1 + bar_range / 100)
    low_prices = np.minimum(open_prices, close_prices) * (1 - bar_range / 100)
    
    # Volume with some variation
    volume = np.random.randint(100, 10000, n_bars)
    
    df = pd.DataFrame({
        'Open': open_prices,
        'High': high_prices,
        'Low': low_prices,
        'Close': close_prices,
        'Volume': volume
    }, index=date_index)
    
    return df


# =============================================================================
# TOY CLASSIFICATION DATA FIXTURE
# =============================================================================

@pytest.fixture
def toy_classification_data():
    """
    Generate trivially learnable classification data for model mechanics testing.
    
    Properties:
    - 100 samples, 5 features
    - TARGET_Direction is deterministic: 1 if Feature[0] > 0.5 else 0
    - Used to verify model can achieve 100% accuracy (overfitting test)
    - NOT for testing prediction quality, only model capability
    
    Returns:
        tuple: (X, y) where X is features array and y is target array
    """
    np.random.seed(42)
    n_samples = 100
    n_features = 5
    
    X = np.random.rand(n_samples, n_features)
    # TARGET_Direction is deterministic based on first feature
    y = (X[:, 0] > 0.5).astype(int)
    
    return X, y


# =============================================================================
# SUFFICIENT HISTORY (30-DAY WARM-UP)
# =============================================================================

@pytest.fixture
def sufficient_history_data():
    """
    10,000 bars of data. Enough for VOL_30D (8640 bars) to calculate.

    Use for indicatorBuilder tests and any pipeline that needs 30-day windows.
    """
    return generate_ohlcv(length=10000)


@pytest.fixture
def low_vol_history():
    """10,000 bars with low volatility for VOL_24H comparison tests."""
    return generate_ohlcv(length=10000, sigma=0.01)


@pytest.fixture
def high_vol_history():
    """10,000 bars with high volatility for VOL_24H comparison tests."""
    return generate_ohlcv(length=10000, sigma=0.05)


# =============================================================================
# EDGE CASE DATA FIXTURES
# =============================================================================

@pytest.fixture
def flat_market_data():
    """
    All bars have High == Low == Open == Close (10k bars).

    Used by verifier and others. For generate_features, use nearly_flat_market_data
    because RSI is undefined when all deltas are 0 and dropna would remove all rows.
    """
    dates = pd.date_range(start="2024-01-01", periods=10000, freq="5min")
    return pd.DataFrame({
        'Open': 100.0, 'High': 100.0, 'Low': 100.0, 'Close': 100.0, 'Volume': 1000.0
    }, index=dates)


@pytest.fixture
def nearly_flat_market_data():
    """
    Very low variance (~1e-6) so VOL_30D is effectively 0, but RSI is defined.

    generate_features needs some price variation for RSI; perfectly flat yields
    all-NaN RSI and an empty frame after dropna.
    """
    return generate_ohlcv(length=10000, trend=0.0, sigma=1e-6)


@pytest.fixture
def zero_price_data():
    """
    Generate data containing a row with Close = 0.
    
    Tests:
    - Division-by-zero handling in percentage calculations
    - Log return calculations (log(0) is undefined)
    - Should either handle gracefully or raise appropriate error
    
    Returns:
        pd.DataFrame: OHLCV DataFrame with one zero-price row
    """
    np.random.seed(42)
    n_bars = 100
    
    start_date = datetime(2024, 1, 1, 0, 0)
    date_index = pd.date_range(start=start_date, periods=n_bars, freq='5min')
    
    # Normal prices
    close_prices = np.full(n_bars, 75.0)
    # Insert zero at position 50
    close_prices[50] = 0.0
    
    df = pd.DataFrame({
        'Open': close_prices,
        'High': close_prices,
        'Low': close_prices,
        'Close': close_prices,
        'Volume': np.random.randint(100, 1000, n_bars)
    }, index=date_index)
    
    return df


@pytest.fixture
def trending_up_data():
    """
    10,000 bars of strictly increasing prices so RSI saturates near 100.

    Deterministic (no randomness) so every bar is an up-move and RSI > 99.
    """
    np.random.seed(42)
    n_bars = 10000
    dates = pd.date_range(start="2024-01-01", periods=n_bars, freq="5min")
    base_price = 70.0
    increment = 0.02
    close = base_price + np.arange(n_bars) * increment
    open_ = close - increment / 2
    high = close + 0.01
    low = open_ - 0.01
    return pd.DataFrame({
        'Open': open_, 'High': high, 'Low': low, 'Close': close,
        'Volume': np.full(n_bars, 1000.0)
    }, index=dates)


@pytest.fixture
def trending_down_data():
    """
    10,000 bars of strong downtrend so RSI is very low.

    Pure strictly-decreasing can make RSI degenerate (all loss, no gain) in some
    implementations. We use a strong negative trend with tiny noise so RSI < 10.
    """
    return generate_ohlcv(length=10000, trend=-0.08, sigma=0.005)


@pytest.fixture
def midnight_crossing_data():
    """
    Generate data that crosses midnight (23:55 to 00:05).
    
    Tests:
    - Time_Sin/Time_Cos continuity at midnight boundary
    - Time_Sin at 23:55 should be close to Time_Sin at 00:05 (cyclical)
    - The 23:00 to 00:00 wrap-around should not cause discontinuities
    
    Timestamps included:
    - 23:45, 23:50, 23:55 on Day 1
    - 00:00, 00:05, 00:10 on Day 2
    
    Returns:
        pd.DataFrame: OHLCV DataFrame crossing midnight
    """
    np.random.seed(42)
    
    # Create timestamps crossing midnight
    day1 = datetime(2024, 1, 15)
    day2 = datetime(2024, 1, 16)
    
    timestamps = [
        day1.replace(hour=23, minute=45),
        day1.replace(hour=23, minute=50),
        day1.replace(hour=23, minute=55),
        day2.replace(hour=0, minute=0),
        day2.replace(hour=0, minute=5),
        day2.replace(hour=0, minute=10),
    ]
    
    date_index = pd.DatetimeIndex(timestamps)
    n_bars = len(timestamps)
    
    # Normal price data
    close_prices = np.array([75.0, 75.1, 75.05, 75.08, 75.12, 75.10])
    open_prices = np.array([74.95, 75.0, 75.1, 75.05, 75.08, 75.12])
    high_prices = np.maximum(open_prices, close_prices) + 0.05
    low_prices = np.minimum(open_prices, close_prices) - 0.05
    
    df = pd.DataFrame({
        'Open': open_prices,
        'High': high_prices,
        'Low': low_prices,
        'Close': close_prices,
        'Volume': np.random.randint(100, 1000, n_bars)
    }, index=date_index)
    
    return df


# =============================================================================
# PROCESSED DATA FIXTURE (for verifier tests)
# =============================================================================

@pytest.fixture
def sample_processed_data(synthetic_price_data):
    """
    Generate a sample of processed data similar to DataProcessor output.
    
    This fixture creates data that mimics the output format of the
    DataProcessor, useful for testing the OilDatasetVerifier without
    running the full pipeline.
    
    Returns:
        pd.DataFrame: DataFrame with processed features and target
    """
    df = synthetic_price_data.copy()
    n = len(df)
    
    # Add time features (cyclical)
    minutes = df.index.hour * 60 + df.index.minute
    df['Time_Sin'] = np.sin(2 * np.pi * minutes / 1440)
    df['Time_Cos'] = np.cos(2 * np.pi * minutes / 1440)
    
    # Add mock indicators (within valid ranges)
    np.random.seed(42)
    df['RSI'] = np.clip(np.random.normal(50, 15, n), 0, 100)
    df['MACD'] = np.random.normal(0, 0.5, n)
    df['MACD_Signal'] = np.random.normal(0, 0.5, n)
    df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
    
    # Add volatility features (must be non-negative)
    df['VOL_3D'] = np.abs(np.random.normal(0.01, 0.005, n))
    df['VOL_7D'] = np.abs(np.random.normal(0.01, 0.005, n))
    df['VOL_30D'] = np.abs(np.random.normal(0.01, 0.005, n))
    df['Parkinson_Vol_24H'] = np.abs(np.random.normal(0.01, 0.005, n))
    
    # Add other features
    df['Return_Skew_24H'] = np.random.normal(0, 0.5, n)
    df['Return_Kurt_24H'] = np.random.normal(3, 1, n)
    df['SMA_20_Dist'] = np.random.normal(0, 0.02, n)
    df['SMA_30d_Dist'] = np.random.normal(0, 0.02, n)
    df['Volume_Log'] = np.log1p(df['Volume'])
    
    # Add target (0, 1, or 2)
    df['TARGET_Direction'] = np.random.choice([0, 1, 2], n)
    
    # Drop raw OHLCV columns (like DataProcessor does)
    df = df.drop(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    
    return df


@pytest.fixture
def sample_processed_data_with_nan(sample_processed_data):
    """
    Processed data with intentional NaN values for testing verifier catches them.
    """
    df = sample_processed_data.copy()
    df.iloc[10, 0] = np.nan  # Insert NaN
    return df


@pytest.fixture
def sample_processed_data_with_inf(sample_processed_data):
    """
    Processed data with intentional inf values for testing verifier catches them.
    """
    df = sample_processed_data.copy()
    df.iloc[10, 0] = np.inf  # Insert inf
    return df


@pytest.fixture
def sample_processed_data_with_leakage(sample_processed_data):
    """
    Processed data with target leakage (TARGET_Direction perfectly correlates with a feature).
    """
    df = sample_processed_data.copy()
    # Create perfect correlation between target and RSI
    df['RSI'] = df['TARGET_Direction'].astype(float) * 50  # Perfect correlation
    return df
