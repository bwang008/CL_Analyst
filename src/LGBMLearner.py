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
        "class_weight": "balanced",
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

    def _focal_loss_obj(self, preds: np.ndarray, train_data: lgb.Dataset) -> tuple[np.ndarray, np.ndarray]:
        """
        Custom Focal Loss objective for both binary and multiclass classification.
        
        Args:
            preds: Raw logits from LightGBM.
                   - Binary: Shape (n_samples,)
                   - Multiclass: Shape (n_samples * n_classes,)
            train_data: LightGBM Dataset with labels
        """
        labels = train_data.get_label().astype(int)
        n_samples = len(labels)
        gamma = self.params.get("focal_gamma", 2.0)
        
        # Binary Case (detected by shape)
        if preds.size == n_samples:
            p = 1.0 / (1.0 + np.exp(-preds))
            # p_t = p if y=1, else 1-p
            p_t = np.where(labels == 1, p, 1 - p)
            
            # Simplified Focal Loss gradient and hessian
            # These are effective approximations for LightGBM
            grad = (p - labels) * ((1 - p_t) ** gamma)
            hess = (p * (1 - p)) * ((1 - p_t) ** gamma)
            return grad, hess
            
        # Multiclass Case
        else:
            n_class = self.params.get("num_class", 3)
            # Reshape preds to (n_samples, n_class)
            preds = preds.reshape(n_samples, n_class)
            
            # Softmax
            exp_p = np.exp(preds - np.max(preds, axis=1, keepdims=True))
            p = exp_p / np.sum(exp_p, axis=1, keepdims=True)
            
            # Indicator matrix for labels
            y_true = np.zeros((n_samples, n_class))
            y_true[np.arange(n_samples), labels] = 1
            
            # Focal weights
            p_t = np.sum(y_true * p, axis=1)
            weights = (1 - p_t) ** gamma
            
            # Gradient and Hessian
            grad = (weights[:, None] * (p - y_true)).reshape(-1)
            # Simple Hessian approximation
            hess = (weights[:, None] * p * (1 - p)).reshape(-1)
            
            return grad, hess

    def add_evidence(self, X: Union[np.ndarray, pd.DataFrame], y: Any) -> None:
        """
        Train the LightGBM model on the provided data.

        Supports custom focal loss if 'use_focal' is in params.
        When validation_fraction > 0 and data is large enough, reserves the
        last fraction of training data as an internal validation set for early
        stopping and convergence tracking.

        After training, sets:
            self.best_iteration_: int  — best boosting round (or n_estimators)
            self.evals_result_: dict | None — train/valid metric history
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

        lgb_params = {k: v for k, v in self.params.items()
                      if k not in ("n_estimators", "validation_fraction", "use_focal",
                                   "focal_gamma")}
        if self.params.get("use_focal", False):
            lgb_params["objective"] = self._focal_loss_obj
            # Remove num_class for binary focal loss — the default of 3
            # conflicts with binary_logloss metric.
            orig_obj = self.params.get("objective", "multiclass")
            if orig_obj != "multiclass":
                lgb_params.pop("num_class", None)
        elif lgb_params.get("objective") == "multiclass":
            if "num_class" not in lgb_params:
                lgb_params["num_class"] = self.params.get("num_class", 3)
        else:
            lgb_params.pop("num_class", None)

        num_boost = int(self.params.get("n_estimators", 100))
        valid_frac = float(self.params.get("validation_fraction", 0.1))

        # --- Build train / optional validation datasets ---
        if valid_frac > 0 and n_rows > 100:
            split = int(n_rows * (1 - valid_frac))
            if isinstance(X_mat, pd.DataFrame):
                X_train_split = X_mat.iloc[:split]
                X_valid_split = X_mat.iloc[split:]
            else:
                X_train_split = X_mat[:split]
                X_valid_split = X_mat[split:]
            y_train_split = y_arr[:split]
            y_valid_split = y_arr[split:]

            train_data = lgb.Dataset(X_train_split, label=y_train_split)
            valid_data = lgb.Dataset(
                X_valid_split, label=y_valid_split, reference=train_data,
            )

            evals_result: dict = {}
            self.model = lgb.train(
                lgb_params,
                train_data,
                num_boost_round=num_boost,
                valid_sets=[train_data, valid_data],
                valid_names=["train", "valid"],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=50, verbose=True),
                    lgb.log_evaluation(period=100),
                    lgb.record_evaluation(evals_result),
                ],
            )
            self.best_iteration_ = self.model.best_iteration
            self.evals_result_ = evals_result

            if self.best_iteration_ == num_boost:
                import logging
                logging.getLogger("LGBMLearner").warning(
                    "Model used all %d rounds — more boosting rounds may help.",
                    num_boost,
                )
        else:
            # Fallback: train without validation (small data or frac=0)
            train_data = lgb.Dataset(X_mat, label=y_arr)
            self.model = lgb.train(
                lgb_params,
                train_data,
                num_boost_round=num_boost,
            )
            self.best_iteration_ = num_boost
            self.evals_result_ = None

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

        if isinstance(self.model, lgb.Booster):
            pred = self.model.predict(X)
        elif hasattr(self.model, "predict_proba"):
            # Sklearn API fallback for legacy .pkl files
            pred = self.model.predict_proba(X)
            # If binary class, align with booster output (1D array of positive probability)
            if pred.ndim == 2 and pred.shape[1] == 2:
                pred = pred[:, 1]
        else:
            # Fallback if predict_proba is not available
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
        import os
        
        # Smart fallback for Model Sanitization protocol
        if filepath.endswith(".pkl"):
            pure_path = filepath.replace(".pkl", "_pure.txt")
            if os.path.exists(pure_path):
                import logging
                logging.getLogger("LGBMLearner").info(
                    "Found %s. Bypassing Joblib for sanitized model.", os.path.basename(pure_path)
                )
                filepath = pure_path

        if filepath.endswith(".txt"):
            self.model = lgb.Booster(model_file=filepath)
            self.feature_names = self.model.feature_name()
            self.n_features_in_ = self.model.num_feature()
            self.params = {}
            return

        data = joblib.load(filepath)
        if isinstance(data, dict) and "model" in data:
            self.model = data["model"]
            self.feature_names = data.get("feature_names")
            self.n_features_in_ = data.get("n_features_in_")
            self.params = data.get("params", {})
        else:
            # Handle native raw LightGBM Booster objects exported by Optuna/Canary pipelines
            self.model = data
            self.feature_names = data.feature_name() if hasattr(data, "feature_name") else None
            self.n_features_in_ = len(self.feature_names) if self.feature_names else getattr(data, "num_feature", lambda: None)()
            self.params = {}

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
