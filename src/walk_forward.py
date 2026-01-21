"""
Walk-Forward Validation Module for CL Futures ML Pipeline.

This module implements time-series cross-validation with:
- Holdout set ("vault") for final evaluation
- Expanding window walk-forward validation
- Purge/embargo gap to prevent label leakage

The walk-forward approach respects the temporal nature of financial data,
ensuring the model never trains on future information.

Author: CL Analyst
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Iterator, Tuple, List, Optional

from .util import get_feature_columns, get_X_y


class WalkForwardSplitter:
    """
    Walk-Forward Cross-Validation with Holdout and Purge.
    
    Implements the "Holdout + Walk-Forward" strategy:
    1. Extract final holdout_pct as untouchable "vault" for final evaluation
    2. Use remaining data as "gym" for walk-forward validation
    3. Apply expanding window with purge gap between train/test
    
    Attributes:
        holdout_pct (float): Percentage of data to hold out (default: 0.15 = 15%)
        purge_bars (int): Number of bars to skip between train and test (default: 576 = 48h)
        min_train_bars (int): Minimum training set size (default: 8640 = ~30 days)
        fold_size_bars (int): Size of each test fold (default: 8640 = ~30 days)
    """
    
    # Constants for 5-minute bar data
    BARS_PER_HOUR = 12
    BARS_PER_DAY = 288
    
    def __init__(
        self,
        holdout_pct: float = 0.15,
        purge_bars: int = 576,  # 48 hours = 2 days
        min_train_bars: int = 8640,  # ~30 days
        fold_size_bars: int = 8640,  # ~30 days per test fold
    ):
        """
        Initialize the WalkForwardSplitter.
        
        Args:
            holdout_pct: Percentage of data to hold out as final test "vault" (0.15 = 15%)
            purge_bars: Gap between train end and test start to prevent label leakage.
                       Should match the prediction horizon (576 bars = 48 hours).
            min_train_bars: Minimum number of bars required for initial training set.
            fold_size_bars: Number of bars in each test fold.
        """
        if not 0 < holdout_pct < 1:
            raise ValueError(f"holdout_pct must be between 0 and 1, got {holdout_pct}")
        if purge_bars < 0:
            raise ValueError(f"purge_bars must be non-negative, got {purge_bars}")
        if min_train_bars <= 0:
            raise ValueError(f"min_train_bars must be positive, got {min_train_bars}")
        if fold_size_bars <= 0:
            raise ValueError(f"fold_size_bars must be positive, got {fold_size_bars}")
            
        self.holdout_pct = holdout_pct
        self.purge_bars = purge_bars
        self.min_train_bars = min_train_bars
        self.fold_size_bars = fold_size_bars
        
    def get_holdout(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Split data into gym (training/validation) and vault (final holdout).
        
        The vault is the final holdout_pct of the data, used only for final
        evaluation after all hyperparameter tuning is complete.
        
        Args:
            df: Full processed DataFrame with features, RAW_, and TARGET_ columns
            
        Returns:
            Tuple of (gym_df, vault_df):
                - gym_df: Data for walk-forward validation (first ~85%)
                - vault_df: Final holdout for production evaluation (last ~15%)
        """
        n_total = len(df)
        n_vault = int(n_total * self.holdout_pct)
        n_gym = n_total - n_vault
        
        if n_gym < self.min_train_bars + self.purge_bars + self.fold_size_bars:
            raise ValueError(
                f"Not enough data for walk-forward validation. "
                f"Gym size ({n_gym}) must be at least "
                f"{self.min_train_bars + self.purge_bars + self.fold_size_bars} bars."
            )
        
        gym_df = df.iloc[:n_gym].copy()
        vault_df = df.iloc[n_gym:].copy()
        
        print(f"Data split:")
        print(f"  - Total: {n_total:,} bars")
        print(f"  - Gym (walk-forward): {len(gym_df):,} bars ({100*(1-self.holdout_pct):.0f}%)")
        print(f"  - Vault (holdout): {len(vault_df):,} bars ({100*self.holdout_pct:.0f}%)")
        print(f"  - Gym date range: {gym_df.index[0]} to {gym_df.index[-1]}")
        print(f"  - Vault date range: {vault_df.index[0]} to {vault_df.index[-1]}")
        
        return gym_df, vault_df
    
    def split(self, df: pd.DataFrame) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
        """
        Generate expanding window train/test splits with purge gap.
        
        Yields indices for each fold:
        - Fold 1: Train on [0 : min_train], Test on [min_train+purge : min_train+purge+fold_size]
        - Fold 2: Train on [0 : min_train+fold_size], Test on [min_train+fold_size+purge : ...]
        - etc.
        
        Args:
            df: DataFrame to split (typically the gym_df from get_holdout)
            
        Yields:
            Tuple of (train_indices, test_indices) as numpy arrays
        """
        n = len(df)
        
        # Calculate number of folds possible
        # After min_train + purge, we need at least fold_size for one fold
        remaining_after_min = n - self.min_train_bars - self.purge_bars
        if remaining_after_min < self.fold_size_bars:
            raise ValueError(
                f"Not enough data for even one fold. "
                f"Need {self.min_train_bars + self.purge_bars + self.fold_size_bars} bars, "
                f"but only have {n}."
            )
        
        fold_num = 0
        train_end = self.min_train_bars
        
        while True:
            test_start = train_end + self.purge_bars
            test_end = test_start + self.fold_size_bars
            
            # Check if we have enough data for this fold
            if test_end > n:
                # Use remaining data as final fold if there's enough
                if test_start < n:
                    test_end = n
                else:
                    break
            
            train_indices = np.arange(0, train_end)
            test_indices = np.arange(test_start, test_end)
            
            fold_num += 1
            print(f"  Fold {fold_num}: Train [0:{train_end}] ({train_end:,} bars), "
                  f"Purge [{train_end}:{test_start}] ({self.purge_bars} bars), "
                  f"Test [{test_start}:{test_end}] ({len(test_indices):,} bars)")
            
            yield train_indices, test_indices
            
            # Expand training window for next fold
            train_end = test_end
            
            # Stop if we've used all data
            if test_end >= n:
                break
    
    def get_n_folds(self, df: pd.DataFrame) -> int:
        """
        Calculate the number of folds for the given DataFrame.
        
        Args:
            df: DataFrame to split
            
        Returns:
            int: Number of folds
        """
        return sum(1 for _ in self.split(df))
    
    def get_fold_data(
        self, 
        df: pd.DataFrame, 
        train_indices: np.ndarray, 
        test_indices: np.ndarray
    ) -> Tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series, pd.DataFrame]:
        """
        Extract features and targets for a fold, ensuring no data leakage.
        
        Uses get_feature_columns() to guarantee RAW_ columns are not included
        in the feature set.
        
        Args:
            df: Full DataFrame with features, RAW_, and TARGET_ columns
            train_indices: Indices for training set
            test_indices: Indices for test set
            
        Returns:
            Tuple of (X_train, y_train, X_test, y_test, df_test):
                - X_train: Training features (DataFrame)
                - y_train: Training targets (Series)
                - X_test: Test features (DataFrame)
                - y_test: Test targets (Series)
                - df_test: Full test DataFrame including RAW_ columns for evaluation
        """
        # Get train/test slices
        df_train = df.iloc[train_indices]
        df_test = df.iloc[test_indices]
        
        # Extract X, y using the safe utility function
        X_train, y_train = get_X_y(df_train)
        X_test, y_test = get_X_y(df_test)
        
        return X_train, y_train, X_test, y_test, df_test
    
    def summary(self, df: pd.DataFrame) -> dict:
        """
        Generate a summary of the walk-forward split configuration.
        
        Args:
            df: DataFrame to analyze
            
        Returns:
            dict: Summary statistics
        """
        n_total = len(df)
        n_vault = int(n_total * self.holdout_pct)
        n_gym = n_total - n_vault
        
        # Count folds (create a temporary gym to count)
        gym_df = df.iloc[:n_gym]
        n_folds = sum(1 for _ in self.split(gym_df))
        
        summary = {
            'total_bars': n_total,
            'gym_bars': n_gym,
            'vault_bars': n_vault,
            'holdout_pct': self.holdout_pct,
            'purge_bars': self.purge_bars,
            'min_train_bars': self.min_train_bars,
            'fold_size_bars': self.fold_size_bars,
            'n_folds': n_folds,
            'date_range': (df.index[0], df.index[-1]),
            'gym_date_range': (gym_df.index[0], gym_df.index[-1]) if len(gym_df) > 0 else None,
        }
        
        return summary


def walk_forward_validate(
    df: pd.DataFrame,
    model_class,
    model_params: dict = None,
    splitter: WalkForwardSplitter = None,
    verbose: bool = True,
) -> List[dict]:
    """
    Run walk-forward validation on a dataset.
    
    This is a convenience function that:
    1. Splits data into gym/vault
    2. Runs walk-forward on gym
    3. Returns results for each fold
    
    Args:
        df: Processed DataFrame with features, RAW_, and TARGET_ columns
        model_class: Model class with add_evidence(X, y) and query(X) methods
        model_params: Parameters to pass to model constructor
        splitter: WalkForwardSplitter instance (creates default if None)
        verbose: Whether to print progress
        
    Returns:
        List of dicts, one per fold, containing:
            - fold: Fold number
            - train_size: Number of training samples
            - test_size: Number of test samples
            - y_true: Actual labels
            - y_pred: Predicted labels
            - df_test: Full test DataFrame for evaluation
    """
    if splitter is None:
        splitter = WalkForwardSplitter()
    
    if model_params is None:
        model_params = {}
    
    # Split into gym and vault
    gym_df, vault_df = splitter.get_holdout(df)
    
    if verbose:
        print(f"\nStarting walk-forward validation...")
        print(f"Purge gap: {splitter.purge_bars} bars ({splitter.purge_bars / splitter.BARS_PER_DAY:.1f} days)")
        print(f"Fold size: {splitter.fold_size_bars} bars ({splitter.fold_size_bars / splitter.BARS_PER_DAY:.1f} days)")
    
    results = []
    
    for fold_num, (train_idx, test_idx) in enumerate(splitter.split(gym_df), 1):
        if verbose:
            print(f"\n--- Fold {fold_num} ---")
        
        # Get fold data
        X_train, y_train, X_test, y_test, df_test = splitter.get_fold_data(
            gym_df, train_idx, test_idx
        )
        
        if verbose:
            print(f"Training on {len(X_train):,} samples, testing on {len(X_test):,} samples")
        
        # Train model
        model = model_class(**model_params)
        model.add_evidence(X_train, y_train)
        
        # Predict
        y_pred = model.query(X_test)
        
        # Store results
        fold_result = {
            'fold': fold_num,
            'train_size': len(X_train),
            'test_size': len(X_test),
            'y_true': y_test.values,
            'y_pred': y_pred,
            'df_test': df_test,
            'train_date_range': (gym_df.index[train_idx[0]], gym_df.index[train_idx[-1]]),
            'test_date_range': (df_test.index[0], df_test.index[-1]),
        }
        results.append(fold_result)
        
        if verbose:
            accuracy = (y_pred == y_test.values).mean()
            print(f"Fold {fold_num} accuracy: {accuracy:.4f}")
    
    if verbose:
        print(f"\nWalk-forward validation complete. {len(results)} folds evaluated.")
    
    return results, vault_df
