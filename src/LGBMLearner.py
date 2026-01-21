"""
LightGBM Learner Module for CL Futures ML Pipeline.

This module provides an LGBMLearner class that wraps LightGBM for use in
the trading strategy pipeline. It follows the same interface as RTLearner
and other learners in the project.

Expected Interface:
    - __init__(**kwargs): Initialize with hyperparameters
    - add_evidence(X, y): Train the model
    - query(X): Make predictions
    - save(filepath): Persist model to disk
    - load(filepath): Load model from disk

Author: CL Analyst
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import joblib
import lightgbm as lgb
from typing import Optional, Union, List, Any


class LGBMLearner:
    """
    LightGBM-based learner for classification/regression tasks.

    This learner wraps the LightGBM library to provide gradient boosting
    capabilities for the trading strategy pipeline. It enforces strict
    input validation and column consistency (Paranoid mode).

    Attributes:
        model: The underlying LightGBM Booster (None until trained or loaded).
        params: Dictionary of LightGBM parameters.
        feature_names: Names of features used during training (None if trained on ndarray).
        n_features_in_: Number of features seen during fit.
    """

    _DEFAULTS = {
        "random_state": 42,
        "objective": "multiclass",
        "num_class": 3,
        "n_estimators": 100,
        "learning_rate": 0.1,
        "num_leaves": 31,
        "verbose": -1,
        "min_child_samples": 1,
    }

    def __init__(self, **kwargs: Any) -> None:
        """
        Initialize the LGBMLearner.

        Args:
            **kwargs: LightGBM hyperparameters. Override defaults.
                Common: n_estimators, learning_rate, num_leaves, objective,
                num_class (for multiclass), random_state, verbose.
                min_child_samples=1 by default to allow overfitting on small data.

        Defaults (overridable):
            random_state=42, objective='multiclass', num_class=3,
            n_estimators=100, learning_rate=0.1, num_leaves=31, verbose=-1,
            min_child_samples=1.
        """
        self.params = {**self._DEFAULTS, **kwargs}
        self.model: Optional[lgb.Booster] = None
        self.feature_names: Optional[List[str]] = None
        self.n_features_in_: Optional[int] = None

    def add_evidence(self, X: Union[np.ndarray, pd.DataFrame], y: Any) -> None:
        """
        Train the LightGBM model on the provided data.

        Args:
            X: Feature matrix of shape (n_samples, n_features). ndarray or DataFrame.
            y: Target array of shape (n_samples,). 0/1 for binary; 0,1,2 for multiclass.

        Raises:
            ValueError: If X is empty, or X and y have incompatible shapes.
        """
        y_arr = np.asarray(y)
        if getattr(y_arr, "ndim", 0) > 1:
            y_arr = y_arr.ravel()

        if isinstance(X, pd.DataFrame):
            X_mat = X
            n_rows, n_cols = X.shape[0], X.shape[1]
            self.feature_names = X.columns.tolist()
            self.n_features_in_ = len(self.feature_names)
        else:
            X_mat = np.asarray(X)
            if X_mat.ndim != 2:
                raise ValueError("X must be 2D.")
            n_rows, n_cols = X_mat.shape[0], X_mat.shape[1]
            self.feature_names = None
            self.n_features_in_ = n_cols

        if n_rows == 0:
            raise ValueError("Empty X. Cannot train on empty data.")

        if n_rows != len(y_arr):
            raise ValueError(
                f"X and y shape mismatch: X has {n_rows} rows, y has {len(y_arr)}."
            )

        lgb_params = {k: v for k, v in self.params.items() if k != "n_estimators"}
        if lgb_params.get("objective") == "multiclass":
            # Ensure num_class is present for multiclass
            if "num_class" not in lgb_params:
                lgb_params["num_class"] = self.params.get("num_class", 3)
        else:
            lgb_params.pop("num_class", None)

        train_data = lgb.Dataset(X_mat, label=y_arr)
        num_boost = int(self.params.get("n_estimators", 100))
        self.model = lgb.train(
            lgb_params,
            train_data,
            num_boost_round=num_boost,
        )

    def query(self, X: Union[np.ndarray, pd.DataFrame]) -> np.ndarray:
        """
        Generate predictions using the trained model.

        Args:
            X: Feature matrix. Must have the same columns (name and order) as
                training data if DataFrame; same number of features if ndarray.

        Returns:
            np.ndarray: Predicted classes of shape (n_samples,) for classification,
                or predictions for regression.

        Raises:
            ValueError: If model not trained, or X has wrong columns/shape.
        """
        if self.model is None:
            raise ValueError("Model not trained. Call add_evidence() or load() first.")

        if isinstance(X, pd.DataFrame):
            if self.feature_names is None:
                raise ValueError(
                    "Model was trained on ndarray. Use an ndarray with "
                    f"{self.n_features_in_} features, or retrain with a DataFrame."
                )
            if list(X.columns) != self.feature_names:
                raise ValueError(
                    "Column mismatch: query X must have the same columns (name and "
                    f"order) as training. Expected {self.feature_names}, "
                    f"got {list(X.columns)}."
                )
            n_features = X.shape[1]
        else:
            X_arr = np.asarray(X)
            if X_arr.ndim != 2:
                raise ValueError("X must be 2D.")
            if self.feature_names is not None:
                raise ValueError(
                    "Model was trained on a DataFrame. Use a DataFrame with the same "
                    "columns (name and order) for query."
                )
            n_features = X_arr.shape[1]
            if self.n_features_in_ is None or n_features != self.n_features_in_:
                raise ValueError(
                    f"Feature shape mismatch: expected {self.n_features_in_} features, "
                    f"got {n_features}."
                )

        pred = self.model.predict(X)
        obj = self.params.get("objective", "multiclass")

        if obj == "multiclass":
            return np.argmax(pred, axis=1).astype(np.int64)
        if obj == "binary":
            return (np.asarray(pred).ravel() >= 0.5).astype(np.int64)
        return np.asarray(pred).ravel()

    def save(self, filepath: str) -> None:
        """
        Serialize the model and metadata (column names, params) to disk.

        Args:
            filepath: Path to save the model (e.g. 'model.pkl').

        Raises:
            ValueError: If model has not been trained (optional; currently allowed).
        """
        payload = {
            "model": self.model,
            "feature_names": self.feature_names,
            "n_features_in_": self.n_features_in_,
            "params": self.params,
        }
        joblib.dump(payload, filepath)

    def load(self, filepath: str) -> None:
        """
        Deserialize the model and metadata from disk.

        Works when the instance was created with __new__ (no __init__). Sets
        model, feature_names, n_features_in_, and params on self.

        Args:
            filepath: Path to the saved model.

        Raises:
            FileNotFoundError: If filepath does not exist.
        """
        data = joblib.load(filepath)
        self.model = data["model"]
        self.feature_names = data["feature_names"]
        self.n_features_in_ = data["n_features_in_"]
        self.params = data["params"]

    def author(self) -> str:
        """Return the author's identifier."""
        return "bwang421"

    def get_feature_importance(self) -> Optional[np.ndarray]:
        """
        Get feature importance scores from the trained model.

        Returns:
            np.ndarray: Feature importance scores, or None if not trained.
        """
        if self.model is None:
            return None
        return np.array(self.model.feature_importance(), dtype=float)
