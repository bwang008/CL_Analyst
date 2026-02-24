"""
Out-of-sample regime stress test runner for S_Ultimate.

For each regime window:
1) Train on data strictly before the window start (unseen OOS regime).
2) Predict Buy probabilities on the regime window.
3) Threshold predictions.
4) Run friction-aware backtest.
5) Save per-regime metrics and aggregate summary.

Usage:
    python agent/oos_regime_test.py
"""

import json
import os
import shutil
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import src.util as util
from src.LGBMLearner import LGBMLearner
from agent.backtester import run_backtest


@dataclass
class RegimeWindow:
    name: str
    start: str
    end: str


def _classification_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    tn = int(((y_true == 0) & (y_pred == 0)).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    accuracy = (tp + tn) / len(y_true) if len(y_true) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision_buy": precision,
        "recall_buy": recall,
        "f1_buy": f1,
        "accuracy": accuracy,
    }


def run_oos_regime_tests(
    data_path: str = "data/processed/CL_set_06.parquet",
    target_name: str = "TARGET_TRIPLE_2x1_24H_LONG",
    prob_threshold: float = 0.50,
    commission_per_side: float = 2.50,
    slippage_per_side: float = 0.03,
    contract_multiplier: float = 1000.0,
) -> pd.DataFrame:
    os.makedirs(os.path.join(PROJECT_ROOT, "reports", "oos_regimes"), exist_ok=True)

    # S_Ultimate params from strategy queue / experiment log.
    model_params = {
        "num_leaves": 31,
        "min_child_samples": 166,
        "learning_rate": 0.05242702195760322,
        "feature_fraction": 0.6940065346564026,
        "bagging_fraction": 0.6483459770074159,
        "bagging_freq": 1,
        "reg_alpha": 2.737488884954343,
        "reg_lambda": 7.378557513409711,
        "max_depth": 4,
        "min_gain_to_split": 0.9901794009928347,
        "n_estimators": 1000,
        "objective": "binary",
        "use_focal": True,
        "metric": "binary_logloss",
        "class_weight": None,
    }

    regimes = [
        RegimeWindow(
            name="gfc_aftershock_2009",
            start="2009-01-01",
            end="2009-06-30",
        ),
        RegimeWindow(
            name="covid_crash_2020",
            start="2020-02-01",
            end="2020-06-30",
        ),
    ]

    df = pd.read_parquet(data_path)
    feature_cols = util.get_feature_columns(df)
    target_col = util.get_target_column(df, target_name=target_name)

    print(f"Loaded {len(df):,} rows from {data_path}")
    print(f"Date range: {df.index.min()} to {df.index.max()}")
    print(f"Using target: {target_col}")
    print(f"Using probability threshold: {prob_threshold:.2f}")

    rows = []
    for regime in regimes:
        start_ts = pd.Timestamp(regime.start)
        end_ts = pd.Timestamp(regime.end)

        train_df = df[df.index < start_ts]
        test_df = df[(df.index >= start_ts) & (df.index <= end_ts)]

        print("\n" + "=" * 72)
        print(f"Regime: {regime.name} ({regime.start} to {regime.end})")
        print(f"Train rows: {len(train_df):,} | Test rows: {len(test_df):,}")

        if train_df.empty or test_df.empty:
            print("Skipping regime due to insufficient train/test rows.")
            rows.append(
                {
                    "regime": regime.name,
                    "start": regime.start,
                    "end": regime.end,
                    "status": "skipped",
                    "reason": "insufficient_train_or_test_rows",
                }
            )
            continue

        X_train = train_df[feature_cols]
        y_train = train_df[target_col]
        X_test = test_df[feature_cols]
        y_test = test_df[target_col]

        if y_train.isna().any():
            m = ~y_train.isna()
            X_train = X_train.loc[m]
            y_train = y_train.loc[m]
        if y_test.isna().any():
            m = ~y_test.isna()
            X_test = X_test.loc[m]
            y_test = y_test.loc[m]
            test_df = test_df.loc[m]

        if len(X_train) == 0 or len(X_test) == 0:
            print("Skipping regime due to empty data after NaN filtering.")
            rows.append(
                {
                    "regime": regime.name,
                    "start": regime.start,
                    "end": regime.end,
                    "status": "skipped",
                    "reason": "empty_after_nan_filter",
                }
            )
            continue

        X_train_ds, y_train_ds = util.downsample_majority(X_train, y_train, random_state=42)
        model = LGBMLearner(**model_params)
        model.add_evidence(X_train_ds, y_train_ds)

        buy_probs = np.asarray(model.model.predict(X_test)).ravel()
        pred_buy = (buy_probs >= prob_threshold).astype(int)
        y_true = y_test.astype(int).to_numpy()

        cls_metrics = _classification_metrics(y_true=y_true, y_pred=pred_buy)

        pred_df = pd.DataFrame(index=test_df.index)
        pred_df["prob_Buy"] = buy_probs
        pred_df["Predicted"] = pred_buy
        pred_df["Predicted_Label"] = np.where(pred_buy == 1, "Buy", "Hold")
        pred_path = os.path.join(PROJECT_ROOT, "reports", "oos_regimes", f"{regime.name}_predictions.csv")
        pred_df.to_csv(pred_path)
        print(f"Saved predictions: {pred_path}")

        run_backtest(
            predictions_path=pred_path,
            data_path=data_path,
            prob_threshold=prob_threshold,
            commission_per_side=commission_per_side,
            slippage_per_side=slippage_per_side,
            contract_multiplier=contract_multiplier,
        )

        shared_backtest_path = os.path.join(PROJECT_ROOT, "reports", "backtest_results.csv")
        regime_backtest_path = os.path.join(PROJECT_ROOT, "reports", "oos_regimes", f"{regime.name}_backtest.csv")
        shutil.copyfile(shared_backtest_path, regime_backtest_path)

        trades_df = pd.read_csv(regime_backtest_path)
        if trades_df.empty:
            bt_win_rate = 0.0
            bt_profit_factor = 0.0
            bt_total_pnl = 0.0
            bt_total_trades = 0
        else:
            bt_win_rate = float((trades_df["pnl"] > 0).mean())
            wins = trades_df.loc[trades_df["pnl"] > 0, "pnl"].sum()
            losses = abs(trades_df.loc[trades_df["pnl"] < 0, "pnl"].sum())
            bt_profit_factor = float(wins / losses) if losses > 0 else float("inf")
            bt_total_pnl = float(trades_df["pnl"].sum())
            bt_total_trades = int(len(trades_df))

        row = {
            "regime": regime.name,
            "start": regime.start,
            "end": regime.end,
            "status": "ok",
            "train_rows": int(len(X_train)),
            "test_rows": int(len(X_test)),
            "signals_predicted": int((pred_buy == 1).sum()),
            **cls_metrics,
            "backtest_total_trades": bt_total_trades,
            "backtest_win_rate": bt_win_rate,
            "backtest_profit_factor": bt_profit_factor,
            "backtest_total_net_pnl": bt_total_pnl,
        }
        rows.append(row)

        print(
            f"Regime complete | Precision={cls_metrics['precision_buy']:.2%}, "
            f"WinRate={bt_win_rate:.2%}, PF={bt_profit_factor:.2f}, NetPnL=${bt_total_pnl:,.2f}"
        )

    summary_df = pd.DataFrame(rows)
    summary_csv = os.path.join(PROJECT_ROOT, "reports", "oos_regimes", "oos_regime_summary.csv")
    summary_json = os.path.join(PROJECT_ROOT, "reports", "oos_regimes", "oos_regime_summary.json")
    summary_df.to_csv(summary_csv, index=False)
    with open(summary_json, "w", encoding="utf-8") as f:
        json.dump(summary_df.to_dict(orient="records"), f, indent=2)

    print("\n" + "=" * 72)
    print(f"Saved OOS summary CSV: {summary_csv}")
    print(f"Saved OOS summary JSON: {summary_json}")
    return summary_df


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-path", default="data/processed/CL_set_06.parquet")
    parser.add_argument("--target-name", default="TARGET_TRIPLE_2x1_24H_LONG")
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--commission-per-side", type=float, default=2.50)
    parser.add_argument("--slippage-per-side", type=float, default=0.03)
    parser.add_argument("--contract-multiplier", type=float, default=1000.0)
    args = parser.parse_args()

    run_oos_regime_tests(
        data_path=args.data_path,
        target_name=args.target_name,
        prob_threshold=args.threshold,
        commission_per_side=args.commission_per_side,
        slippage_per_side=args.slippage_per_side,
        contract_multiplier=args.contract_multiplier,
    )
