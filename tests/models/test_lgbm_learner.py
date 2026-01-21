"""
Tests for LGBMLearner model mechanics.

This module tests the MODEL CAPABILITY, not prediction accuracy:
1. Overfitting: Can the model achieve 100% on trivially learnable data?
2. Persistence: Are predictions identical after save/load?
3. Input Guardrails: Does the model error on invalid inputs?

These tests are marked with @pytest.mark.xfail because LGBMLearner is
currently a stub implementation. Once implemented, these tests should pass.

Author: CL Analyst
"""

import numpy as np
import pandas as pd
import pytest
import os
import tempfile

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


# =============================================================================
# OVERFITTING CAPABILITY TESTS
# =============================================================================

class TestOverfittingCapability:
    """
    Tests that verify the model CAN learn perfectly on trivial data.
    
    This is NOT about testing model quality - it's about verifying
    the model mechanics work correctly. A model that can't achieve
    100% accuracy on `y = 1 if x[0] > 0.5 else 0` is broken.
    """
    
    def test_lgbm_overfitting_capability(self, toy_classification_data):
        """
        Model must achieve 100% accuracy on trivially learnable data.
        
        The toy_classification_data fixture provides data where:
        - Target = 1 if Feature[0] > 0.5 else 0
        
        Any tree-based model should achieve perfect accuracy on this.
        
        Expected behavior once implemented:
            - Model trains successfully
            - Predictions on training data are 100% accurate
        """
        from src.LGBMLearner import LGBMLearner
        from sklearn.metrics import accuracy_score
        
        X, y = toy_classification_data
        
        # Initialize and train
        learner = LGBMLearner(
            num_leaves=31,
            learning_rate=0.1,
            n_estimators=100,
            objective='binary',
            num_class=1,
            verbose=-1
        )
        learner.add_evidence(X, y)
        
        # Predict on training data
        predictions = learner.query(X)
        
        # Should achieve perfect accuracy on this trivial problem
        accuracy = accuracy_score(y, predictions)
        assert accuracy == 1.0, \
            f"Model should achieve 100% accuracy on trivial data, got {accuracy:.2%}"
    
    def test_lgbm_multiclass_overfitting(self):
        """
        Model must handle multiclass classification correctly.
        
        Tests the 3-class scenario used in the actual trading strategy
        (Target: 0=Hold, 1=Buy, 2=Sell).
        """
        from src.LGBMLearner import LGBMLearner
        from sklearn.metrics import accuracy_score
        
        np.random.seed(42)
        n_samples = 150
        
        # Create 3-class data with clear decision boundaries
        X = np.random.rand(n_samples, 5)
        y = np.zeros(n_samples, dtype=int)
        y[X[:, 0] > 0.66] = 2  # Sell
        y[(X[:, 0] > 0.33) & (X[:, 0] <= 0.66)] = 1  # Buy
        # y stays 0 for X[:, 0] <= 0.33 (Hold)
        
        learner = LGBMLearner(
            num_leaves=31,
            learning_rate=0.1,
            n_estimators=100,
            objective='multiclass',
            num_class=3,
            verbose=-1
        )
        learner.add_evidence(X, y)
        
        predictions = learner.query(X)
        accuracy = accuracy_score(y, predictions)
        
        # Should achieve very high accuracy on this separable problem
        assert accuracy >= 0.95, \
            f"Model should achieve >= 95% accuracy on separable multiclass, got {accuracy:.2%}"


# =============================================================================
# PERSISTENCE TESTS
# =============================================================================

class TestPersistence:
    """
    Tests that verify model save/load produces identical predictions.
    
    Bitwise identical predictions after reload ensures:
    - Model state is fully serialized
    - No random elements in prediction
    - Production deployment is reproducible
    """
    
    def test_lgbm_persistence_identical_predictions(self, toy_classification_data):
        """
        Predictions must be bitwise identical after save/load.
        
        Sabotage Verification:
            In LGBMLearner.save(), intentionally corrupt the saved model.
            Run this test - it MUST fail. Then revert.
        """
        from src.LGBMLearner import LGBMLearner
        
        X, y = toy_classification_data
        
        # Train model
        learner = LGBMLearner(
            num_leaves=31,
            learning_rate=0.1,
            n_estimators=50,
            objective='binary',
            verbose=-1
        )
        learner.add_evidence(X, y)
        
        # Get predictions before save
        pred_before = learner.query(X)
        
        # Save to temporary file
        with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False) as f:
            filepath = f.name
        
        try:
            learner.save(filepath)
            
            # Load into new learner instance
            loaded_learner = LGBMLearner.__new__(LGBMLearner)
            loaded_learner.load(filepath)
            
            # Get predictions after load
            pred_after = loaded_learner.query(X)
            
            # Predictions must be bitwise identical
            np.testing.assert_array_equal(
                pred_before, pred_after,
                err_msg="Predictions differ after save/load"
            )
        finally:
            # Cleanup
            if os.path.exists(filepath):
                os.remove(filepath)
    
    def test_lgbm_persistence_file_created(self, toy_classification_data):
        """
        save() should create a file at the specified path.
        """
        from src.LGBMLearner import LGBMLearner
        
        X, y = toy_classification_data
        
        learner = LGBMLearner(verbose=-1)
        learner.add_evidence(X, y)
        
        with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False) as f:
            filepath = f.name
        
        # Delete the temp file so we can verify save() creates it
        os.remove(filepath)
        
        try:
            learner.save(filepath)
            assert os.path.exists(filepath), "save() did not create file"
            assert os.path.getsize(filepath) > 0, "Saved file is empty"
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)
    
    def test_lgbm_load_nonexistent_file_raises(self):
        """
        load() should raise an error for nonexistent file.
        """
        from src.LGBMLearner import LGBMLearner
        
        learner = LGBMLearner.__new__(LGBMLearner)
        
        with pytest.raises((FileNotFoundError, IOError, OSError)):
            learner.load("/nonexistent/path/model.pkl")


# =============================================================================
# INPUT GUARDRAIL TESTS
# =============================================================================

class TestInputGuardrails:
    """
    Tests that verify the model properly validates inputs.
    
    A robust model should:
    - Reject mismatched feature counts
    - Error clearly when not trained
    - Handle edge cases gracefully
    """
    
    def test_lgbm_missing_columns_error(self, toy_classification_data):
        """
        Model must raise error if predict() called with wrong feature count.
        
        Sabotage Verification:
            In LGBMLearner.query(), remove the feature count validation.
            Run this test - it MUST fail (or produce garbage predictions).
            Then revert.
        """
        from src.LGBMLearner import LGBMLearner
        
        X, y = toy_classification_data
        n_features = X.shape[1]
        
        # Train model
        learner = LGBMLearner(verbose=-1)
        learner.add_evidence(X, y)
        
        # Try to predict with missing columns
        X_missing = X[:, :-1]  # Remove last column
        
        with pytest.raises(ValueError) as exc_info:
            learner.query(X_missing)
        
        # Error message should mention feature mismatch
        assert "feature" in str(exc_info.value).lower() or \
               "column" in str(exc_info.value).lower() or \
               "shape" in str(exc_info.value).lower() or \
               "mismatch" in str(exc_info.value).lower()
    
    def test_lgbm_extra_columns_error(self, toy_classification_data):
        """
        Model must raise error if predict() called with extra features.
        """
        from src.LGBMLearner import LGBMLearner
        
        X, y = toy_classification_data
        
        learner = LGBMLearner(verbose=-1)
        learner.add_evidence(X, y)
        
        # Try to predict with extra columns
        X_extra = np.hstack([X, np.random.rand(X.shape[0], 2)])
        
        with pytest.raises(ValueError):
            learner.query(X_extra)
    
    def test_lgbm_query_before_train_error(self):
        """
        query() must raise an error when called before add_evidence() or load().
        """
        from src.LGBMLearner import LGBMLearner

        learner = LGBMLearner(verbose=-1)
        with pytest.raises(ValueError):
            learner.query(np.array([[0.5]]))
    
    def test_lgbm_empty_data_error(self):
        """
        Model must raise error when trained with empty data.
        """
        from src.LGBMLearner import LGBMLearner
        
        learner = LGBMLearner(verbose=-1)
        
        X_empty = np.array([]).reshape(0, 5)
        y_empty = np.array([])
        
        with pytest.raises((ValueError, IndexError)):
            learner.add_evidence(X_empty, y_empty)
    
    def test_lgbm_mismatched_xy_shapes_error(self):
        """
        Model must raise error when X and y have different row counts.
        """
        from src.LGBMLearner import LGBMLearner
        
        learner = LGBMLearner(verbose=-1)
        
        X = np.random.rand(100, 5)
        y = np.random.randint(0, 2, 50)  # Different length
        
        with pytest.raises(ValueError):
            learner.add_evidence(X, y)


# =============================================================================
# INTERFACE COMPLIANCE TESTS
# =============================================================================

class TestInterfaceCompliance:
    """
    Tests that verify LGBMLearner follows the expected interface.
    
    The learner should be compatible with other learners in the project
    (RTLearner, BagLearner) for use in ensemble methods.
    """
    
    def test_lgbm_has_add_evidence_method(self):
        """
        LGBMLearner must have add_evidence(x_data, y_data) method.
        """
        from src.LGBMLearner import LGBMLearner
        
        learner = LGBMLearner(verbose=-1)
        
        assert hasattr(learner, 'add_evidence')
        assert callable(getattr(learner, 'add_evidence'))
    
    def test_lgbm_has_query_method(self):
        """
        LGBMLearner must have query(x_data) method.
        """
        from src.LGBMLearner import LGBMLearner
        
        learner = LGBMLearner(verbose=-1)
        
        assert hasattr(learner, 'query')
        assert callable(getattr(learner, 'query'))
    
    def test_lgbm_query_returns_numpy_array(self, toy_classification_data):
        """
        query() must return a numpy array.
        """
        from src.LGBMLearner import LGBMLearner
        
        X, y = toy_classification_data
        
        learner = LGBMLearner(verbose=-1)
        learner.add_evidence(X, y)
        
        predictions = learner.query(X)
        
        assert isinstance(predictions, np.ndarray), \
            f"query() should return numpy array, got {type(predictions)}"
    
    def test_lgbm_query_returns_correct_shape(self, toy_classification_data):
        """
        query() must return predictions with shape (n_samples,).
        """
        from src.LGBMLearner import LGBMLearner
        
        X, y = toy_classification_data
        n_samples = X.shape[0]
        
        learner = LGBMLearner(verbose=-1)
        learner.add_evidence(X, y)
        
        predictions = learner.query(X)
        
        assert predictions.shape == (n_samples,), \
            f"predictions should have shape ({n_samples},), got {predictions.shape}"
    
    def test_lgbm_author_method(self):
        """
        LGBMLearner should have author() method returning identifier.
        """
        from src.LGBMLearner import LGBMLearner
        
        learner = LGBMLearner(verbose=-1)
        
        assert hasattr(learner, 'author')
        author = learner.author()
        assert isinstance(author, str)
        assert len(author) > 0


# =============================================================================
# REPRODUCIBILITY TESTS
# =============================================================================

class TestReproducibility:
    """
    Tests that verify training is reproducible with same random seed.
    """
    
    def test_lgbm_reproducible_with_seed(self, toy_classification_data):
        """
        Two models with same random_state should produce identical predictions.
        """
        from src.LGBMLearner import LGBMLearner
        
        X, y = toy_classification_data
        
        # Train first model
        learner1 = LGBMLearner(random_state=42, verbose=-1)
        learner1.add_evidence(X, y)
        pred1 = learner1.query(X)
        
        # Train second model with same seed
        learner2 = LGBMLearner(random_state=42, verbose=-1)
        learner2.add_evidence(X, y)
        pred2 = learner2.query(X)
        
        np.testing.assert_array_equal(
            pred1, pred2,
            err_msg="Models with same random_state should produce identical predictions"
        )
    
    def test_lgbm_different_seeds_may_differ(self, toy_classification_data):
        """
        Two models with different random_states may produce different predictions.
        
        Note: On trivially learnable data, they might still be identical.
        This test uses more complex data where randomness matters.
        """
        from src.LGBMLearner import LGBMLearner
        
        # Use more complex data where randomness matters
        np.random.seed(42)
        X = np.random.rand(200, 10)
        y = (X.sum(axis=1) > 5).astype(int)
        
        learner1 = LGBMLearner(random_state=1, n_estimators=10, verbose=-1)
        learner1.add_evidence(X, y)
        pred1 = learner1.query(X)
        
        learner2 = LGBMLearner(random_state=999, n_estimators=10, verbose=-1)
        learner2.add_evidence(X, y)
        pred2 = learner2.query(X)
        
        # They CAN be identical by chance, so we just verify the code runs
        # The important thing is both produce valid predictions
        assert pred1.shape == pred2.shape
        assert set(pred1).issubset({0, 1})
        assert set(pred2).issubset({0, 1})
