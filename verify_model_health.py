import numpy as np
import pandas as pd
from sklearn.datasets import load_breast_cancer, make_classification
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
    roc_auc_score,
    log_loss,
)
import lightgbm as lgb

from src.LGBMLearner import LGBMLearner


def _as_label_and_prob(y_pred: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """Return (labels, probs_or_none) for binary outputs."""
    y_pred = np.asarray(y_pred).ravel()
    unique_vals = np.unique(y_pred)
    is_prob = y_pred.dtype.kind in {"f"} and len(unique_vals) > 2
    if is_prob:
        return (y_pred >= 0.5).astype(int), y_pred
    return y_pred.astype(int), None


def _print_metrics(y_true: np.ndarray, y_pred: np.ndarray, label: str) -> None:
    y_true = np.asarray(y_true).ravel()
    y_label, y_prob = _as_label_and_prob(y_pred)

    acc = accuracy_score(y_true, y_label)
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_label, labels=[0, 1], zero_division=0
    )
    cm = confusion_matrix(y_true, y_label, labels=[0, 1])

    print(f"\n   {label} Metrics:")
    print(f"     Accuracy: {acc:.4f}")
    print("     Per-Class Precision/Recall/F1:")
    print(f"       Class 0: P={precision[0]:.4f} R={recall[0]:.4f} F1={f1[0]:.4f} (n={support[0]})")
    print(f"       Class 1: P={precision[1]:.4f} R={recall[1]:.4f} F1={f1[1]:.4f} (n={support[1]})")
    print("     Confusion Matrix (rows=Actual, cols=Predicted):")
    print(f"       [[{cm[0, 0]:5d} {cm[0, 1]:5d}]")
    print(f"        [{cm[1, 0]:5d} {cm[1, 1]:5d}]]")

    if y_prob is not None:
        auc = roc_auc_score(y_true, y_prob)
        ll = log_loss(y_true, y_prob)
        print(f"     AUC: {auc:.4f}")
        print(f"     Log Loss: {ll:.4f}")


def run_calibration_test() -> None:
    print("========================================")
    print("   LGBM MODEL ENGINE DIAGNOSTIC TOOL    ")
    print("========================================")

    # ---------------------------------------------------------
    # TEST 1: The "FizzBuzz" Test (Synthetic Data)
    # ---------------------------------------------------------
    print("\n[TEST 1] Synthetic Data (The 'FizzBuzz' Test)")

    X, y = make_classification(
        n_samples=1000,
        n_features=20,
        n_informative=5,
        n_redundant=2,
        flip_y=0.01,
        random_state=42,
    )

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )

    print("   -> Training Your Wrapper...")
    my_model = LGBMLearner(objective="binary", n_estimators=100, verbose=-1)

    if hasattr(my_model, "add_evidence"):
        my_model.add_evidence(X_train, y_train)
        my_preds = my_model.query(X_test)
    else:
        my_model.fit(X_train, y_train)
        my_preds = my_model.predict(X_test)

    print("   -> Training Raw LightGBM (Control)...")
    raw_model = lgb.LGBMClassifier(random_state=42, verbose=-1)
    raw_model.fit(X_train, y_train)
    raw_preds = raw_model.predict(X_test)

    _print_metrics(y_test, my_preds, "Your Wrapper")
    _print_metrics(y_test, raw_preds, "Control Model")

    my_label, _ = _as_label_and_prob(my_preds)
    my_acc = accuracy_score(y_test, my_label)
    if my_acc < 0.85:
        print("   [FAIL] Your model is failing on easy synthetic data.")
        print("   CHECK: Are you shuffling inputs? Are you handling columns correctly?")
    else:
        print("   [PASS] Your model engine is working correctly.")

    # ---------------------------------------------------------
    # TEST 2: The "Real World" Easy Mode (Breast Cancer)
    # ---------------------------------------------------------
    print("\n[TEST 2] Breast Cancer Dataset (Standard Benchmark)")
    data = load_breast_cancer()
    X = pd.DataFrame(data.data, columns=data.feature_names)
    y = pd.Series(data.target)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )

    if hasattr(my_model, "add_evidence"):
        my_model = LGBMLearner(objective="binary", n_estimators=100, verbose=-1)
        my_model.add_evidence(X_train, y_train)
        my_preds = my_model.query(X_test)
    else:
        my_model = LGBMLearner(objective="binary", n_estimators=100, verbose=-1)
        my_model.fit(X_train, y_train)
        my_preds = my_model.predict(X_test)

    _print_metrics(y_test, my_preds, "Your Wrapper")

    my_label, my_prob = _as_label_and_prob(my_preds)
    if my_prob is not None:
        score = roc_auc_score(y_test, my_prob)
        metric = "AUC"
    else:
        score = accuracy_score(y_test, my_label)
        metric = "Accuracy"

    print(f"\n   Your Model {metric}: {score:.4f}")
    if score > 0.90:
        print("   [PASS] Excellent performance on benchmark.")
        print("   CONCLUSION: Your wrapper is healthy. Any bad results in Trading")
        print("               are due to the Financial Data/Features, not the Model.")
    else:
        print("   [WARN] Performance is lower than expected for this dataset.")


if __name__ == "__main__":
    run_calibration_test()
