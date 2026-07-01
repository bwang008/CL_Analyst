"""
Seed reproducibility tests (TASK 2 of pipeline fix plan).

Verifies that the LGBM hyperparameter sweep + final model training are
reproducible under a shared `--random-seed`:

  1. Same seed => identical selected hyperparams across two search runs.
  2. Same seed => identical final-model predictions across two train runs.

Fixtures are intentionally tiny so the tests run fast.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Tiny synthetic fixtures
# ---------------------------------------------------------------------------


def _make_synthetic_frame(n_rows: int = 600, n_features: int = 4, seed: int = 0):
    """Build a tiny learnable binary-classification DataFrame with a DatetimeIndex.

    Returns (df, feature_cols, target_col).
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n_rows, freq="5min")
    feature_cols = [f"FEAT_{i}" for i in range(n_features)]
    X = rng.standard_normal((n_rows, n_features))
    # Learnable but noisy signal off the first two features
    logit = 1.3 * X[:, 0] - 0.8 * X[:, 1] + 0.4 * rng.standard_normal(n_rows)
    y = (logit > 0).astype(int)
    data = {c: X[:, i].astype("float32") for i, c in enumerate(feature_cols)}
    target_col = "TARGET_TRIPLE_2x1_24H_LONG"
    data[target_col] = y
    df = pd.DataFrame(data, index=idx)
    return df, feature_cols, target_col


# ---------------------------------------------------------------------------
# Test 1: search reproducibility (identical selected hyperparams)
# ---------------------------------------------------------------------------


def test_search_same_seed_identical_hyperparams():
    """Two runs of make_objective under the same random_seed must select the
    identical best hyperparameters."""
    import optuna

    from agent.optuna_lgbm_search_v2 import make_objective, walk_forward_folds, sample_folds

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    df, feature_cols, target_col = _make_synthetic_frame()
    X = df[feature_cols]
    y = df[target_col]

    # Tiny walk-forward folds
    all_folds = walk_forward_folds(len(X), min_train=200, fold_size=100, purge=10)
    folds = sample_folds(all_folds, max_folds=3, sample_step=1)
    assert folds, "fold generation must produce at least one fold"

    def run_once(seed: int):
        objective = make_objective(
            X=X,
            y=y,
            df_gym=None,
            folds=folds,
            ml_metric="logloss",
            target_name=target_col,
            num_threads=1,
            # n_estimators is suggested in [500, max_n_estimators]; keep the
            # ceiling above the floor. Early stopping keeps trials fast.
            max_n_estimators=600,
            early_stopping_rounds=5,
            random_seed=seed,
        )
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=seed),
        )
        study.optimize(objective, n_trials=8, n_jobs=1)
        return study.best_params

    params_a = run_once(123)
    params_b = run_once(123)

    assert params_a == params_b, (
        f"Same seed must yield identical hyperparams.\nA={params_a}\nB={params_b}"
    )

    # Sanity: a different seed should generally explore different params, proving
    # the seed is load-bearing (not incidental determinism from a fixed env).
    params_c = run_once(456)
    assert params_c != params_a, (
        "Different seeds unexpectedly produced identical hyperparams; the seed "
        "may not be driving the sampler."
    )


# ---------------------------------------------------------------------------
# Test 2: final-model reproducibility (identical predictions)
# ---------------------------------------------------------------------------


def test_final_model_same_seed_identical_predictions(tmp_path):
    """Two final-model training runs under the same random_seed must produce
    identical OOS predictions."""
    import src.util as util
    from gcp.vm_e2e_pipeline import train_final_model, _sigmoid

    df, feature_cols, target_col = _make_synthetic_frame(n_rows=800, seed=7)
    # Introduce class imbalance so downsampling is actually exercised
    df.loc[df.index[:300], target_col] = 0

    df_train = df.iloc[:600]
    df_holdout = df.iloc[600:]

    base_params = {
        "num_leaves": 15,
        "learning_rate": 0.05,
        "max_depth": 4,
        "n_estimators": 60,
        "min_child_samples": 20,
    }

    def train_and_predict(seed: int):
        model = train_final_model(
            df_train=df_train.copy(),
            feature_cols=feature_cols,
            target_col=target_col,
            params=dict(base_params),
            balance_mode="downsample",
            output_path=str(tmp_path / f"model_{seed}.pkl"),
            random_seed=seed,
        )
        raw = model.predict(df_holdout[feature_cols])
        return _sigmoid(raw)

    preds_a = train_and_predict(999)
    preds_b = train_and_predict(999)

    np.testing.assert_array_equal(
        preds_a, preds_b,
        err_msg="Same seed must yield identical final-model predictions.",
    )
