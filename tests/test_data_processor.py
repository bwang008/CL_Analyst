"""
Tests for DataProcessor pipeline integration.

This module tests the full data processing pipeline to ensure:
1. Pipeline runs without errors on valid input
2. Output meets data contract requirements
3. Different dataset versions produce expected outputs

Author: CL Analyst
"""

import numpy as np
import pandas as pd
import pytest
import os
import tempfile
from datetime import datetime

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.data_processor import DataProcessor, DATASET_VERSIONS


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def sample_raw_csv(tmp_path):
    """
    Create a sample raw CSV file for testing.

    Format: semicolon-separated, no headers.
    Columns: Date, Time, Open, High, Low, Close, Volume.

    Uses 15,000 rows to satisfy:
    - AlphaFactory largest window: 35×288 = 10,080
    - cleanup() warmup drop: 10,500
    - Target tail NaN: ~288
    """
    n_rows = 15000

    # Generate dates (5-minute intervals)
    start_date = datetime(2024, 1, 1, 0, 0)
    
    rows = []
    current_date = start_date
    base_price = 75.0
    
    np.random.seed(42)
    
    for i in range(n_rows):
        date_str = current_date.strftime('%d/%m/%Y')
        time_str = current_date.strftime('%H:%M')
        
        # Generate OHLCV
        open_price = base_price + np.random.normal(0, 0.1)
        close_price = base_price + np.random.normal(0, 0.1)
        high_price = max(open_price, close_price) + abs(np.random.normal(0, 0.05))
        low_price = min(open_price, close_price) - abs(np.random.normal(0, 0.05))
        volume = np.random.randint(100, 5000)
        
        rows.append(f"{date_str};{time_str};{open_price:.2f};{high_price:.2f};{low_price:.2f};{close_price:.2f};{volume}")
        
        # Increment by 5 minutes
        current_date = current_date + pd.Timedelta(minutes=5)
        
        # Small random walk for price
        base_price += np.random.normal(0, 0.01)
    
    # Write to temp file
    csv_path = tmp_path / "test_data.csv"
    with open(csv_path, 'w') as f:
        f.write('\n'.join(rows))
    
    return str(csv_path)


# =============================================================================
# INITIALIZATION TESTS
# =============================================================================

class TestDataProcessorInit:
    """Tests for DataProcessor initialization."""
    
    def test_init_with_defaults(self, sample_raw_csv):
        """
        DataProcessor initializes with default parameters.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        
        assert processor.input_path == sample_raw_csv
        assert processor.dataset_version == "set_03"
        assert "set_03" in processor.output_path
    
    def test_init_with_custom_version(self, sample_raw_csv):
        """
        DataProcessor accepts custom dataset version.
        """
        processor = DataProcessor(
            input_path=sample_raw_csv,
            dataset_version="set_05"
        )
        
        assert processor.dataset_version == "set_05"
        assert "set_05" in processor.output_path
    
    def test_init_auto_generates_output_path(self, sample_raw_csv):
        """
        Output path is auto-generated based on input name and version.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        
        assert "CL" in processor.output_path
        assert "set_03" in processor.output_path
        assert processor.output_path.endswith('.parquet')


# =============================================================================
# DATA LOADING TESTS
# =============================================================================

class TestDataLoading:
    """Tests for load_data method."""
    
    def test_load_data_returns_dataframe(self, sample_raw_csv):
        """
        load_data returns a pandas DataFrame.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        
        assert isinstance(df, pd.DataFrame)
    
    def test_load_data_has_ohlcv_columns(self, sample_raw_csv):
        """
        Loaded data has expected OHLCV columns.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        
        expected_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"
    
    def test_load_data_has_datetime_index(self, sample_raw_csv):
        """
        Loaded data has DatetimeIndex.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        
        assert isinstance(df.index, pd.DatetimeIndex)
    
    def test_load_data_numeric_types(self, sample_raw_csv):
        """
        OHLCV columns are numeric types.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            assert np.issubdtype(df[col].dtype, np.number), \
                f"Column {col} should be numeric"


# =============================================================================
# TIME FEATURE TESTS
# =============================================================================

class TestTimeFeatures:
    """Tests for time feature generation."""
    
    def test_add_time_features_cyclical(self, sample_raw_csv):
        """
        add_time_features adds Time_Sin and Time_Cos columns.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_time_features(df)
        
        assert 'Time_Sin' in df.columns
        assert 'Time_Cos' in df.columns
    
    def test_time_sin_cos_bounds(self, sample_raw_csv):
        """
        Time_Sin and Time_Cos are within [-1, 1].
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_time_features(df)
        
        assert df['Time_Sin'].min() >= -1
        assert df['Time_Sin'].max() <= 1
        assert df['Time_Cos'].min() >= -1
        assert df['Time_Cos'].max() <= 1
    
    def test_time_features_raw(self, sample_raw_csv):
        """
        add_time_features_raw adds Hour and Minute columns.
        """
        processor = DataProcessor(input_path=sample_raw_csv, dataset_version="set_02")
        df = processor.load_data()
        df = processor.add_time_features_raw(df)
        
        assert 'Hour' in df.columns
        assert 'Minute' in df.columns
        
        assert df['Hour'].min() >= 0
        assert df['Hour'].max() <= 23
        assert df['Minute'].min() >= 0
        assert df['Minute'].max() <= 59


# =============================================================================
# TARGET CREATION TESTS
# =============================================================================

class TestTargetCreation:
    """Tests for triple barrier target label generation."""
    
    def test_add_triple_barrier_target_adds_column(self, sample_raw_csv):
        """
        add_triple_barrier_target adds MULTI column.
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_12H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=144, atr_period=14,
        )
        
        assert 'TARGET_TRIPLE_2x1_12H_MULTI' in df.columns
    
    def test_target_values_valid(self, sample_raw_csv):
        """
        Triple barrier target values are 0, 1, 2, or NaN (at the end).
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_triple_barrier_target(
            df, prefix="TB",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=100, atr_period=14,
        )
        
        # Valid values (excluding NaN)
        valid_targets = df['TB_MULTI'].dropna().unique()
        assert set(valid_targets).issubset({0, 1, 2}), \
            f"Invalid target values: {valid_targets}"
    
    def test_target_has_nan_at_end(self, sample_raw_csv):
        """
        Last max_horizon rows have NaN target (no future data available).
        """
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_triple_barrier_target(
            df, prefix="TB",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=100, atr_period=14,
        )
        
        # Last 100 rows should be NaN
        assert df['TB_MULTI'].iloc[-100:].isna().all()


# =============================================================================
# FULL PIPELINE TESTS
# =============================================================================

class TestFullPipeline:
    """Integration tests for DataProcessor pipeline components.

    Note: Full end-to-end tests calling process() require real market data
    because cleanup() drops the first 10,500 warmup rows and
    normalize_features() produces NaN on synthetic constant-price data.
    These tests exercise the pipeline steps individually.
    """

    def test_alpha_factory_integration(self, sample_raw_csv):
        """AlphaFactory features are added without error."""
        from src.features.alpha_factory import AlphaFactory

        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_time_features(df)
        df = AlphaFactory(df).add_all_features(
            windows=[864, 2016, 4032, 10080],
            include_macro=True,
        )

        # AlphaFactory should add many columns
        assert len(df.columns) > 20
        assert len(df) > 0

    def test_normalize_features(self, sample_raw_csv):
        """normalize_features runs without error."""
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_time_features(df)
        df = processor.normalize_features(df)

        assert 'Volume_Log' in df.columns
        assert len(df) > 0

    def test_triple_barrier_integration(self, sample_raw_csv):
        """Triple barrier target is created successfully."""
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()

        df = processor.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_12H",
            tp_atr_mult=2.0, sl_atr_mult=1.0,
            max_horizon=144, atr_period=14,
        )

        assert 'TARGET_TRIPLE_2x1_12H_MULTI' in df.columns
        # Valid target values
        valid = df['TARGET_TRIPLE_2x1_12H_MULTI'].dropna().unique()
        assert set(valid).issubset({0, 1, 2})

    def test_processed_data_no_raw_ohlcv_after_cleanup(self, sample_raw_csv):
        """cleanup() drops raw OHLCV columns."""
        processor = DataProcessor(input_path=sample_raw_csv)
        df = processor.load_data()
        df = processor.add_time_features(df)
        df = processor.normalize_features(df)
        df = processor.cleanup(df, drop_raw_returns=True, warmup_rows=0, keep_ohlcv=False)

        for col in ['Open', 'High', 'Low', 'Close']:
            assert col not in df.columns, f"Raw column {col} should be dropped"


# =============================================================================
# SAVE/LOAD TESTS
# =============================================================================

class TestSaveLoad:
    """Tests for data persistence."""
    
    def test_save_creates_file(self, sample_raw_csv, tmp_path):
        """
        save() creates output file.
        """
        output_path = str(tmp_path / "output.parquet")
        
        processor = DataProcessor(
            input_path=sample_raw_csv,
            output_path=output_path
        )
        
        df = processor.load_data()
        saved_path = processor.save(df)
        
        assert os.path.exists(saved_path)
    
    def test_saved_data_readable(self, sample_raw_csv, tmp_path):
        """
        Saved parquet file is readable.
        """
        output_path = str(tmp_path / "output.parquet")
        
        processor = DataProcessor(
            input_path=sample_raw_csv,
            output_path=output_path
        )
        
        df = processor.load_data()
        saved_path = processor.save(df)
        
        # Read it back
        loaded_df = pd.read_parquet(saved_path)
        
        assert len(loaded_df) == len(df)
        assert list(loaded_df.columns) == list(df.columns)


# =============================================================================
# INVALID INPUT TESTS
# =============================================================================

class TestInvalidInputs:
    """Tests for error handling with invalid inputs."""
    
    def test_invalid_dataset_version_raises(self, sample_raw_csv):
        """
        Invalid dataset version raises ValueError.
        """
        processor = DataProcessor(
            input_path=sample_raw_csv,
            dataset_version="invalid_version"
        )
        
        with pytest.raises(ValueError) as exc_info:
            processor.process()
        
        assert "unknown dataset version" in str(exc_info.value).lower()
    
    def test_nonexistent_file_raises(self, tmp_path):
        """
        Nonexistent input file raises error on load.
        """
        processor = DataProcessor(
            input_path=str(tmp_path / "nonexistent.csv")
        )
        
        with pytest.raises((FileNotFoundError, ValueError)):
            processor.load_data()


# =============================================================================
# INTEGRATION WITH VERIFIER
# =============================================================================

class TestVerifierIntegration:
    """Tests that processed data passes verification."""
    
    def test_loaded_data_passes_verifier(self, sample_raw_csv):
        """
        Loaded and cleaned data should pass OilDatasetVerifier checks.
        """
        from src.data_verifier import OilDatasetVerifier
        
        processor = DataProcessor(
            input_path=sample_raw_csv,
            dataset_version="set_05"
        )
        
        df = processor.load_data()
        df = processor.add_time_features(df)
        df = processor.normalize_features(df)
        df = processor.cleanup(df, drop_raw_returns=True, warmup_rows=0)
        
        verifier = OilDatasetVerifier(df)
        is_valid = verifier.verify_all()
        
        if not is_valid:
            pytest.fail(f"Processed data failed verification: {verifier.errors}")
