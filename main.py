'''
CL Futures ML Pipeline - Main Entry Point

This module serves as the main entry point for:
1. Data Processing: Convert raw OHLCV data to ML-ready features
2. Model Training: Train LightGBM models with walk-forward validation
3. Evaluation: Generate metrics and visualizations

Usage:
    python main.py process [--force]     # Process raw data
    python main.py train [data_path]     # Train and evaluate model
    python main.py --help                # Show help

Author: CL Analyst
'''

import os
import sys
import time
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import re
import src.util as util
import src.indicatorBuilder as ind
from src.data_processor import DataProcessor
from src.LGBMLearner import LGBMLearner
from src.walk_forward import WalkForwardSplitter, walk_forward_validate
from src.evaluator import ModelEvaluator
from src.visualizer import SignalVisualizer

#data = pd.read_csv('data/cl-5m_bk.csv',sep=';',parse_dates=[[0,1]],index_col=0,dayfirst=True)
# Commented out - this code runs on import and causes issues when importing from notebooks
# If needed, move this inside the if __name__ == '__main__' block or use absolute paths
# data = pd.read_csv('data/test.csv',sep=';',header=None,index_col=None)
# cols=['Date','Time','Open','High','Low','Close','Volume']
# data.columns = cols

#Merge the date and time columns to form single DT column and assign it as the index/key


#breakpoint()
#data.columns(cols)


#data.head()

def get_cl_df(cl_test_data="data/raw/test100k.csv"):
    #Get the data
    cl_test_data = util.get_cl_data(cl_test_data)
    features = ind.generate_features(cl_test_data)
    #print(features.head())
    return features


def get_processed_cl_df(
    input_path="data/raw/test100k.csv",
    output_path=None,
    dataset_version="set_03",
    threshold=0.08,
    horizon=576,
    force_reprocess=False,
    keep_ohlcv=True,
):
    """
    Get processed CL data using DataProcessor.
    
    This function checks if processed data already exists. If it does and 
    force_reprocess is False, it loads the existing file. Otherwise, it 
    runs the full processing pipeline.
    
    Args:
        input_path: Path to raw CSV file (default: data/raw/test100k.csv)
        output_path: Path for processed output (auto-generated if None)
        dataset_version: Which dataset configuration to use (default: 'set_01')
        threshold: Target threshold for significant price move (default 0.08 = 8%)
        horizon: Forward-looking window for target in bars (default 576 = 48 hours)
        force_reprocess: If True, reprocess even if output file exists
        
    Returns:
        pd.DataFrame: Processed DataFrame with ML-ready features
    """
    # Create processor instance with dataset version
    processor = DataProcessor(
        input_path=input_path,
        output_path=output_path,
        dataset_version=dataset_version,
        keep_ohlcv=keep_ohlcv,
    )
    
    # Check if processed file already exists
    if os.path.exists(processor.output_path) and not force_reprocess:
        print(f"Loading existing processed data from {processor.output_path}")
        try:
            if processor.output_path.endswith('.parquet'):
                return pd.read_parquet(processor.output_path)
            else:
                return pd.read_csv(processor.output_path, index_col=0, parse_dates=True)
        except Exception as e:
            print(f"Error loading file: {e}. Reprocessing...")
    
    # Run the processing pipeline
    return processor.process(threshold=threshold, horizon=horizon)


def train_and_evaluate(
    data_path: str = "data/processed/CL_set_03.parquet",
    holdout_pct: float = 0.15,
    purge_bars: int = 576,
    min_train_bars: int = 8640,
    fold_size_bars: int = 8640,
    threshold: float = 0.08,
    output_dir: str = "reports",
    model_dir: str = "models",
    model_params: dict = None,
    verbose: bool = True,
    method: str = "walk_forward",
    target_name: str = "TARGET_DIR_8PCT_MULTI",
    balance_mode: str = "weight",
    random_state: int | None = None,
    checkpoint_path: str | None = None,
    train_cutoff_date: str | None = None,
) -> dict:
    """
    Train and evaluate an LightGBM model using walk-forward validation.
    
    This function orchestrates the full ML pipeline:
    1. Load processed data (with features, RAW_, and TARGET_ columns)
    2. Split into gym (85%) and vault (15%) holdout
    3. Run walk-forward validation on gym with expanding window
    4. Evaluate each fold and generate metrics
    5. Final evaluation on vault (untouched until now)
    6. Generate reports and visualizations
    
    Args:
        data_path: Path to processed data file (parquet or CSV)
        holdout_pct: Percentage of data for final holdout (0.15 = 15%)
        purge_bars: Gap between train/test to prevent leakage (576 = 48h)
        min_train_bars: Minimum training set size (8640 = ~30 days)
        fold_size_bars: Size of each test fold (8640 = ~30 days)
        threshold: Target threshold used in data processing (for evaluation)
        output_dir: Directory for reports and visualizations
        model_dir: Directory for saved models
        model_params: Optional dict of LightGBM parameters
        verbose: Whether to print progress
        
    Returns:
        dict: Results containing fold_results, vault_result, and report
    """
    start_time = time.perf_counter()

    print("=" * 60)
    print("TRAIN AND EVALUATE PIPELINE")
    print("=" * 60)
    
    # -------------------------------------------------------------------------
    # Step 1: Load processed data
    # -------------------------------------------------------------------------
    print(f"\n[Step 1] Loading data from {data_path}...")
    
    if not os.path.exists(data_path):
        # Try alternative extensions
        alt_paths = [
            data_path.replace('.parquet', '.csv'),
            data_path.replace('.csv', '.parquet'),
        ]
        for alt in alt_paths:
            if os.path.exists(alt):
                data_path = alt
                break
        else:
            raise FileNotFoundError(f"Data file not found: {data_path}")
    
    if data_path.endswith('.parquet'):
        df = pd.read_parquet(data_path)
    else:
        df = pd.read_csv(data_path, index_col=0, parse_dates=True)
    
    print(f"  Loaded {len(df):,} rows, {len(df.columns)} columns")
    print(f"  Date range: {df.index[0]} to {df.index[-1]}")

    # ---- Apply train cutoff date (OOS support) ----
    full_df = None
    if train_cutoff_date is not None:
        cutoff = pd.Timestamp(train_cutoff_date)
        full_df = df  # keep reference for OOS scoring later
        df = df[df.index < cutoff].copy()
        oos_df = full_df[full_df.index >= cutoff]
        if len(df) == 0:
            raise ValueError(f"No data before cutoff {cutoff.date()}")
        if len(oos_df) == 0:
            raise ValueError(f"No data after cutoff {cutoff.date()}")
        print(f"\n  ** OOS CUTOFF: {cutoff.date()} **")
        print(f"  Training data: {len(df):,} rows ({df.index[0]} to {df.index[-1]})")
        print(f"  OOS holdout:   {len(oos_df):,} rows ({oos_df.index[0]} to {oos_df.index[-1]})")
    
    # Verify required columns
    feature_cols = util.get_feature_columns(df)
    required_raw = ['RAW_Close', 'RAW_Future_High', 'RAW_Future_Low']
    missing_raw = [c for c in required_raw if c not in df.columns]
    if missing_raw:
        raise ValueError(
            f"Missing required RAW columns: {missing_raw}. "
            "Please reprocess data with the updated data_processor.py"
        )
    
    target_col = util.get_target_column(df, target_name=target_name)
    
    print(f"  Feature columns ({len(feature_cols)}): {feature_cols[:5]}...")
    print(f"  RAW columns: {[c for c in df.columns if c.startswith('RAW_')]}")
    print(f"  TARGET columns: {[c for c in df.columns if c.startswith('TARGET_')]}")
    print(f"  Using target: {target_col}")
    
    # -------------------------------------------------------------------------
    # Step 2: Create splitter and split data
    # -------------------------------------------------------------------------
    print(f"\n[Step 2] Setting up training method: {method}...")
    
    if method == "simple":
        splitter = None
        gym_df = None
        vault_df = None
    else:
        splitter = WalkForwardSplitter(
            holdout_pct=holdout_pct,
            purge_bars=purge_bars,
            min_train_bars=min_train_bars,
            fold_size_bars=fold_size_bars,
        )
        gym_df, vault_df = splitter.get_holdout(df)
    
    # -------------------------------------------------------------------------
    # Step 3: Walk-forward validation on gym
    # -------------------------------------------------------------------------
    print(f"\n[Step 3] Training and validation...")
    
    if model_params is None:
        model_params = {
            'n_estimators': 100,
            'learning_rate': 0.1,
            'num_leaves': 31,
            'verbose': -1,
        }

    is_binary_target = target_name.endswith("_LONG") or target_name.endswith("_SHORT")
    if balance_mode == "downsample" and not is_binary_target:
        raise ValueError("downsample balance_mode requires a binary target (_LONG/_SHORT).")
    if is_binary_target:
        model_params["objective"] = "binary"
        model_params["metric"] = "binary_logloss"
        model_params.pop("num_class", None)
    if balance_mode == "downsample":
        model_params["class_weight"] = None
    
    if method == "simple":
        split_idx = int(len(df) * (1 - holdout_pct))
        train_df = df.iloc[:split_idx]
        test_df = df.iloc[split_idx:]

        # Purge gap to prevent leakage
        if purge_bars > 0 and len(test_df) > purge_bars:
            test_df = test_df.iloc[purge_bars:]

        if verbose:
            print(f"  SIMPLE SPLIT: Train {len(train_df):,} bars, Test {len(test_df):,} bars")

        X_train, y_train = util.get_X_y(train_df, target_name=target_name)
        X_test, y_test = util.get_X_y(test_df, target_name=target_name)
        if y_train.isna().any():
            mask = ~y_train.isna()
            X_train = X_train.loc[mask]
            y_train = y_train.loc[mask]
        if y_test.isna().any():
            mask = ~y_test.isna()
            X_test = X_test.loc[mask]
            y_test = y_test.loc[mask]
            test_df = test_df.loc[mask]

        if balance_mode == "downsample":
            X_train, y_train = util.downsample_majority(
                X_train, y_train, random_state=random_state
            )

        model = LGBMLearner(**model_params)
        model.add_evidence(X_train, y_train)
        y_pred = model.query(X_test)

        fold_results = [
            {
                "fold": 1,
                "train_size": len(X_train),
                "test_size": len(X_test),
                "y_true": y_test.values,
                "y_pred": y_pred,
                "df_test": test_df,
                "train_date_range": (train_df.index[0], train_df.index[-1]),
                "test_date_range": (test_df.index[0], test_df.index[-1]),
                "model": model,
            }
        ]
        vault_df = None
    else:
        fold_results, vault_df = walk_forward_validate(
            df=gym_df,
            model_class=LGBMLearner,
            model_params=model_params,
            splitter=splitter,
            verbose=verbose,
            target_name=target_name,
            balance_mode=balance_mode,
            random_state=random_state,
            checkpoint_path=checkpoint_path,
        )
    
    # -------------------------------------------------------------------------
    # Step 3b: Training Diagnostics (B2)
    # -------------------------------------------------------------------------
    if method != "simple" and fold_results:
        best_iters = [
            fr.get("best_iteration")
            for fr in fold_results
            if fr.get("best_iteration") is not None
        ]
        converged = [
            fr.get("converged_early", False) for fr in fold_results
        ]
        n_converged = sum(1 for c in converged if c)
        if best_iters:
            diagnostics = {
                "n_folds": len(fold_results),
                "n_converged_early": n_converged,
                "mean_best_iteration": float(np.mean(best_iters)),
                "per_fold": [
                    {
                        "fold": fr["fold"],
                        "best_iteration": fr.get("best_iteration"),
                        "converged_early": fr.get("converged_early"),
                    }
                    for fr in fold_results
                ],
            }
            diag_path = os.path.join(output_dir, "training_diagnostics.json")
            os.makedirs(output_dir, exist_ok=True)
            with open(diag_path, "w", encoding="utf-8") as f:
                json.dump(diagnostics, f, indent=2)
            if verbose:
                print(
                    f"\n  Training diagnostics: {n_converged}/{len(fold_results)} folds "
                    f"converged early, mean best iteration: {np.mean(best_iters):.0f}"
                )
                print(f"  Saved to {diag_path}")

    # -------------------------------------------------------------------------
    # Step 4: Evaluate folds
    # -------------------------------------------------------------------------
    print(f"\n[Step 4] Evaluating fold results...")
    
    evaluator = ModelEvaluator(threshold=threshold)
    
    evaluated_folds = []
    for fold_result in fold_results:
        eval_result = evaluator.evaluate_fold(
            y_true=fold_result['y_true'],
            y_pred=fold_result['y_pred'],
            df_test=fold_result['df_test'],
        )
        eval_result['fold'] = fold_result['fold']
        eval_result['train_size'] = fold_result['train_size']
        eval_result['test_size'] = fold_result['test_size']
        eval_result['train_date_range'] = fold_result['train_date_range']
        eval_result['test_date_range'] = fold_result['test_date_range']
        evaluated_folds.append(eval_result)
    
    # Generate aggregate report
    report = evaluator.generate_report(evaluated_folds)
    
    # Print report
    if verbose:
        evaluator.print_report(report)
    
    # -------------------------------------------------------------------------
    # Step 5: Final evaluation on vault
    # -------------------------------------------------------------------------
    print(f"\n[Step 5] Final evaluation on vault (holdout set)...")
    
    if method == "simple":
        final_model = fold_results[0]["model"]
        vault_eval = None
        y_vault_pred = None
    else:
        # Train final model on entire gym set
        X_gym, y_gym = util.get_X_y(gym_df, target_name=target_name)
        if y_gym.isna().any():
            mask = ~y_gym.isna()
            X_gym = X_gym.loc[mask]
            y_gym = y_gym.loc[mask]
        if balance_mode == "downsample":
            X_gym, y_gym = util.downsample_majority(
                X_gym, y_gym, random_state=random_state
            )
        final_model = LGBMLearner(**model_params)
        final_model.add_evidence(X_gym, y_gym)

        # Predict on vault
        X_vault, y_vault = util.get_X_y(vault_df, target_name=target_name)
        vault_eval_df = vault_df
        if y_vault.isna().any():
            mask = ~y_vault.isna()
            X_vault = X_vault.loc[mask]
            y_vault = y_vault.loc[mask]
            vault_eval_df = vault_df.loc[mask]
        y_vault_pred = final_model.query(X_vault)

        vault_eval = evaluator.evaluate_fold(
            y_true=y_vault.values,
            y_pred=y_vault_pred,
            df_test=vault_eval_df,
        )

        print(f"\n  Vault Results:")
        print(f"    Accuracy: {vault_eval['accuracy']:.4f}")
        print(f"    Samples: {vault_eval['n_samples']:,}")
    
    # -------------------------------------------------------------------------
    # Step 6: Save results and generate visualizations
    # -------------------------------------------------------------------------
    print(f"\n[Step 6] Saving results and generating visualizations...")
    
    # Create output directories
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(model_dir, exist_ok=True)
    
    # Save report
    report_suffix = "simple" if method == "simple" else "metrics"
    report_path = os.path.join(output_dir, f"{report_suffix}.json")
    evaluator.save_report(report, report_path)
    
    # Save vault report (walk-forward only)
    if vault_eval is not None:
        vault_report_path = os.path.join(output_dir, "vault_metrics.json")
        vault_report = {
            'accuracy': vault_eval['accuracy'],
            'precision': vault_eval['precision'],
            'recall': vault_eval['recall'],
            'f1': vault_eval['f1'],
            'confusion_matrix': vault_eval['confusion_matrix'].tolist(),
            'n_samples': vault_eval['n_samples'],
            'move_analysis': vault_eval['move_analysis'],
        }
        evaluator.save_report(vault_report, vault_report_path)
    
    # Export predictions
    predictions_path = os.path.join(output_dir, f"{report_suffix}_predictions.csv")
    evaluator.export_predictions(evaluated_folds, predictions_path)
    
    # Export vault predictions
    if vault_eval is not None:
        vault_predictions_path = os.path.join(output_dir, "vault_predictions.csv")
        vault_eval['actual_moves'].to_csv(vault_predictions_path)
        print(f"  Saved vault predictions to {vault_predictions_path}")
    
    # Save final model
    model_name = "final_model.pkl" if method != "simple" else "simple_split_model.pkl"
    model_path = os.path.join(model_dir, model_name)
    final_model.save(model_path)
    print(f"  Saved final model to {model_path}")

    # ---- Generate OOS predictions if cutoff was used ----
    oos_predictions_path = None
    if full_df is not None and train_cutoff_date is not None:
        cutoff = pd.Timestamp(train_cutoff_date)
        oos_df = full_df[full_df.index >= cutoff]
        print(f"\n  [OOS] Scoring {len(oos_df):,} post-cutoff rows...")
        X_oos = oos_df[feature_cols]
        raw_pred = final_model.model.predict(X_oos)

        # Convert logits to probabilities (focal loss emits raw logits)
        raw_pred = np.asarray(raw_pred, dtype=float).ravel()
        if np.nanmin(raw_pred) < 0.0 or np.nanmax(raw_pred) > 1.0:
            raw_pred = np.clip(raw_pred, -60, 60)
            oos_probs = 1.0 / (1.0 + np.exp(-raw_pred))
        else:
            oos_probs = raw_pred

        # Determine probability column name from target direction
        if target_name.endswith("_LONG"):
            prob_col = "prob_Buy"
        elif target_name.endswith("_SHORT"):
            prob_col = "prob_Sell"
        else:
            prob_col = "prob_Signal"

        oos_out = pd.DataFrame(index=oos_df.index)
        oos_out[prob_col] = oos_probs
        oos_predictions_path = os.path.join(output_dir, "oos_predictions.csv")
        oos_out.to_csv(oos_predictions_path)

        print(f"  [OOS] Saved {len(oos_out):,} predictions to {oos_predictions_path}")
        print(f"  [OOS] Column: {prob_col}")
        print(f"  [OOS] Mean prob: {oos_probs.mean():.4f}, Std: {oos_probs.std():.4f}")
        for t in [0.50, 0.60, 0.70, 0.80]:
            n = (oos_probs >= t).sum()
            print(f"  [OOS]   >= {t}: {n:>7,} signals ({n/len(oos_probs)*100:.1f}%)")
    
    # Generate visualizations
    visualizer = SignalVisualizer()
    
    # Fold summary plot (walk-forward only)
    if method != "simple":
        fold_summary_path = os.path.join(output_dir, "fold_summary.png")
        visualizer.plot_fold_summary(fold_results, fold_summary_path)
    
    # Signal plot for vault or simple split test
    if method == "simple":
        signals_path = os.path.join(output_dir, "simple_signals.png")
        visualizer.plot_signals(
            fold_results[0]["df_test"],
            fold_results[0]["y_pred"],
            signals_path,
            title="Simple Split: Model Signals",
        )
    else:
        signals_path = os.path.join(output_dir, "vault_signals.png")
        visualizer.plot_signals(
            vault_eval_df,
            y_vault_pred,
            signals_path,
            title="Vault Set: Model Signals",
        )
    
    # Actual moves distribution
    moves_path = os.path.join(
        output_dir,
        "simple_actual_moves_distribution.png" if method == "simple" else "actual_moves_distribution.png",
    )
    moves_source = evaluated_folds[-1]['actual_moves'] if method == "simple" else vault_eval['actual_moves']
    visualizer.plot_actual_moves(moves_source, moves_path)

    # Confusion matrix for vault or simple split
    if method == "simple":
        cm_path = os.path.join(output_dir, "simple_confusion_matrix.png")
        visualizer.plot_confusion_matrix(
            evaluated_folds[-1]["confusion_matrix"],
            output_path=cm_path,
            title="Simple Split Confusion Matrix",
        )
    else:
        cm_path = os.path.join(output_dir, "vault_confusion_matrix.png")
        visualizer.plot_confusion_matrix(
            vault_eval["confusion_matrix"],
            output_path=cm_path,
            title="Vault Confusion Matrix",
        )

    # Feature importance
    if method == "simple":
        importance_path = os.path.join(output_dir, "simple_feature_importance.png")
        feature_names = util.get_feature_columns(df)
        visualizer.plot_feature_importance(final_model, feature_names, importance_path)
    else:
        importances = [
            fr["feature_importance"]
            for fr in fold_results
            if fr.get("feature_importance") is not None
        ]
        feature_names = fold_results[0].get("feature_names") if fold_results else None
        if feature_names is None:
            feature_names = util.get_feature_columns(df)
        if importances:
            mean_importance = np.mean(importances, axis=0)
            pairs = list(zip(feature_names, mean_importance))
            pairs.sort(key=lambda x: x[1], reverse=True)
            importance_df = pd.DataFrame(pairs, columns=["feature", "mean_importance"])
            top_pairs = pairs[:20]

            labels = [p[0] for p in top_pairs][::-1]
            scores = [p[1] for p in top_pairs][::-1]

            safe_target = re.sub(r"[^A-Za-z0-9_]+", "_", target_name)
            safe_balance = re.sub(r"[^A-Za-z0-9_]+", "_", balance_mode)
            importance_path = os.path.join(
                output_dir,
                f"walk_forward_feature_importance_{safe_target}_{safe_balance}.png",
            )
            importance_csv_path = os.path.join(
                output_dir,
                f"walk_forward_feature_importance_{safe_target}_{safe_balance}.csv",
            )

            fig, ax = plt.subplots(figsize=(10, 6))
            ax.barh(labels, scores, color="steelblue")
            ax.set_title(
                f"Walk-Forward Feature Importance: {target_name} ({balance_mode})",
                fontsize=12,
                fontweight="bold",
            )
            ax.set_xlabel("Mean Importance (Gain)")
            ax.grid(True, axis="x", alpha=0.3)
            plt.tight_layout()
            fig.savefig(importance_path, dpi=100, bbox_inches="tight")
            plt.close(fig)
            print(f"Saved walk-forward feature importance to {importance_path}")
            importance_df.to_csv(importance_csv_path, index=False)
            print(f"Saved walk-forward feature importance CSV to {importance_csv_path}")
    
    elapsed_seconds = time.perf_counter() - start_time
    elapsed_minutes = elapsed_seconds / 60

    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"\nOutputs saved to:")
    print(f"  - Reports: {output_dir}/")
    print(f"  - Models: {model_dir}/")
    print(f"  - Wall clock time: {elapsed_minutes:.2f} min ({elapsed_seconds:.1f} sec)")
    
    return {
        'fold_results': evaluated_folds,
        'vault_result': vault_eval,
        'report': report,
        'final_model': final_model,
        'wall_time_seconds': elapsed_seconds,
        'oos_predictions_path': oos_predictions_path,
    }


def log_train_run(
    report_path: str,
    target_name: str,
    method: str,
    data_path: str,
    balance_mode: str,
    results: dict,
):
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    vault_result = results.get("vault_result") or {}
    line = (
        f"{timestamp} | target={target_name} | method={method} | "
        f"balance_mode={balance_mode} | data={data_path} | "
        f"vault_accuracy={vault_result.get('accuracy')}\n"
    )
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(line)

    def _metric_scalar(value):
        if isinstance(value, dict):
            vals = [v for v in value.values() if v is not None]
            return float(np.mean(vals)) if vals else None
        return value

    def _signal_metric(value):
        if isinstance(value, dict):
            return value.get("Buy")
        return None

    batch_path = os.path.join("reports", "batch_results.csv")
    write_header = not os.path.exists(batch_path)
    wall_time_seconds = results.get("wall_time_seconds")
    with open(batch_path, "a", encoding="utf-8") as f:
        if write_header:
            f.write(
                "timestamp,target,method,balance_mode,data_path,"
                "vault_accuracy,vault_precision,vault_recall,vault_f1,"
                "signal_precision,signal_recall,signal_f1,"
                "n_samples,wall_time_seconds\n"
            )
        vault_precision = _metric_scalar(vault_result.get("precision"))
        vault_recall = _metric_scalar(vault_result.get("recall"))
        vault_f1 = _metric_scalar(vault_result.get("f1"))
        signal_precision = _signal_metric(vault_result.get("precision"))
        signal_recall = _signal_metric(vault_result.get("recall"))
        signal_f1 = _signal_metric(vault_result.get("f1"))
        f.write(
            f"{timestamp},{target_name},{method},{balance_mode},{data_path},"
            f"{vault_result.get('accuracy')},"
            f"{vault_precision},"
            f"{vault_recall},"
            f"{vault_f1},"
            f"{signal_precision},"
            f"{signal_recall},"
            f"{signal_f1},"
            f"{vault_result.get('n_samples')},"
            f"{wall_time_seconds}\n"
        )


def print_help():
    """Print usage help."""
    print("""
CL Futures ML Pipeline

Usage:
    python main.py process [--force]           Process raw data to ML-ready features
    python main.py train [data_path]           Train and evaluate model (walk-forward)
    python main.py train [data_path] --method simple  Simple 85/15 split sanity check
    python main.py train [data_path] --target TARGET_SQZ_4PCT_LONG
    python main.py train [data_path] --targets TARGET_SQZ_8PCT_LONG,TARGET_SQZ_4PCT_LONG
    python main.py train --balance_mode downsample
    python main.py --config experiments.json
    python main.py --help                      Show this help message

Commands:
    process     Run data processing pipeline
                --force: Force reprocessing even if output exists
    
    train       Train model with walk-forward validation
                data_path: Path to processed data (default: data/processed/CL_set_03.parquet)
                --target: Target column to train on (default: TARGET_DIR_8PCT_MULTI)
                --targets: Comma-separated targets to train sequentially (logs to reports/train_runs.log)
                --balance_mode: weight (default) or downsample
                --config: JSON file with experiment list

Examples:
    python main.py process                     # Process raw data
    python main.py process --force             # Force reprocess
    python main.py train                       # Train with default data (walk-forward)
    python main.py train data/processed/CL_set_01.csv   # Train with specific file
    python main.py train --method simple       # Simple 85/15 split sanity check
    python main.py train --target TARGET_SQZ_4PCT_LONG
    python main.py train --balance_mode downsample --target TARGET_SQZ_4PCT_LONG
""")


if __name__ == '__main__':
    args = sys.argv[1:]
    
    if not args or args[0] == '--help' or args[0] == '-h':
        print_help()
        sys.exit(0)
    
    command = args[0]
    if command.startswith("--") and "--config" in args:
        command = "train"
    
    if command == 'process':
        # Data processing mode
        force_reprocess = "--force" in args
        
        print("=" * 60)
        print("DATA PROCESSING MODE")
        print("=" * 60)
        
        # Process CL data
        processed_features = get_processed_cl_df(
            input_path="data/raw/CL.csv",
            dataset_version="set_03",
            force_reprocess=force_reprocess
        )
        print("\nProcessed data:")
        print(f"  Total records: {processed_features.shape}")
        print(f"  Columns: {list(processed_features.columns)}")
        print(processed_features.head())
    
    elif command == 'train':
        # Training mode
        method = "walk_forward"
        data_path = "data/processed/CL_set_03.parquet"
        target_name = "TARGET_DIR_8PCT_MULTI"
        target_list = None
        balance_mode = "weight"
        config_path = None

        if "--method" in args:
            method_idx = args.index("--method")
            if method_idx + 1 < len(args):
                method = args[method_idx + 1]
        if "--balance_mode" in args:
            balance_idx = args.index("--balance_mode")
            if balance_idx + 1 < len(args):
                balance_mode = args[balance_idx + 1]
        if "--target" in args:
            target_idx = args.index("--target")
            if target_idx + 1 < len(args):
                target_name = args[target_idx + 1]
        if "--targets" in args:
            targets_idx = args.index("--targets")
            if targets_idx + 1 < len(args):
                raw_targets = args[targets_idx + 1]
                target_list = [t.strip() for t in raw_targets.split(",") if t.strip()]
        if "--config" in args:
            config_idx = args.index("--config")
            if config_idx + 1 < len(args):
                config_path = args[config_idx + 1]

        # First non-flag arg after "train" is treated as data_path
        skip_next = False
        for arg in args[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg == "--method":
                skip_next = True
                continue
            if arg == "--target":
                skip_next = True
                continue
            if arg == "--targets":
                skip_next = True
                continue
            if arg == "--balance_mode":
                skip_next = True
                continue
            if arg == "--config":
                skip_next = True
                continue
            if not arg.startswith("-"):
                data_path = arg
                break

        if config_path:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            if isinstance(config_data, dict):
                experiments = config_data.get("experiments", config_data)
            else:
                experiments = config_data
            for exp in experiments:
                exp_target = exp.get("target")
                exp_targets = exp.get("targets")
                exp_balance = exp.get("balance_mode", balance_mode)
                exp_method = exp.get("method", method)
                exp_data_path = exp.get("data_path", data_path)
                exp_dataset_version = exp.get("dataset_version")
                exp_input_path = exp.get("input_path", "data/raw/CL.csv")
                exp_output_path = exp.get("output_path")
                exp_force_reprocess = exp.get("force_reprocess", False)
                exp_threshold = exp.get("threshold", 0.08)
                exp_horizon = exp.get("horizon", 576)
                if exp_dataset_version:
                    processor = DataProcessor(
                        input_path=exp_input_path,
                        output_path=exp_output_path,
                        dataset_version=exp_dataset_version,
                    )
                    exp_data_path = processor.output_path
                    if exp_force_reprocess or not os.path.exists(exp_data_path):
                        get_processed_cl_df(
                            input_path=exp_input_path,
                            output_path=exp_output_path,
                            dataset_version=exp_dataset_version,
                            threshold=exp_threshold,
                            horizon=exp_horizon,
                            force_reprocess=exp_force_reprocess,
                        )
                exp_target_list = exp_targets or ([exp_target] if exp_target else [target_name])

                for target in exp_target_list:
                    results = train_and_evaluate(
                        data_path=exp_data_path,
                        holdout_pct=exp.get("holdout_pct", 0.15),
                        purge_bars=exp.get("purge_bars", 576),
                        min_train_bars=exp.get("min_train_bars", 8640),
                        fold_size_bars=exp.get("fold_size_bars", 8640),
                        threshold=exp_threshold,
                        output_dir=exp.get("output_dir", "reports"),
                        model_dir=exp.get("model_dir", "models"),
                        verbose=True,
                        method=exp_method,
                        target_name=target,
                        balance_mode=exp_balance,
                        random_state=exp.get("random_state"),
                    )
                    log_train_run(
                        report_path=os.path.join("reports", "train_runs.log"),
                        target_name=target,
                        method=exp_method,
                        balance_mode=exp_balance,
                        data_path=exp_data_path,
                        results=results,
                    )
            sys.exit(0)
        
        if target_list:
            for target in target_list:
                results = train_and_evaluate(
                    data_path=data_path,
                    holdout_pct=0.15,
                    purge_bars=576,      # 48 hours
                    min_train_bars=8640,  # ~30 days
                    fold_size_bars=8640,  # ~30 days per fold
                    threshold=0.08,
                    output_dir="reports",
                    model_dir="models",
                    verbose=True,
                    method=method,
                    target_name=target,
                    balance_mode=balance_mode,
                )
                log_train_run(
                    report_path=os.path.join("reports", "train_runs.log"),
                    target_name=target,
                    method=method,
                    balance_mode=balance_mode,
                    data_path=data_path,
                    results=results,
                )
        else:
            results = train_and_evaluate(
                data_path=data_path,
                holdout_pct=0.15,
                purge_bars=576,      # 48 hours
                min_train_bars=8640,  # ~30 days
                fold_size_bars=8640,  # ~30 days per fold
                threshold=0.08,
                output_dir="reports",
                model_dir="models",
                verbose=True,
                method=method,
                target_name=target_name,
                balance_mode=balance_mode,
            )
            log_train_run(
                report_path=os.path.join("reports", "train_runs.log"),
                target_name=target_name,
                method=method,
                balance_mode=balance_mode,
                data_path=data_path,
                results=results,
            )
    
    else:
        print(f"Unknown command: {command}")
        print("Use --help for usage information.")
        sys.exit(1)
