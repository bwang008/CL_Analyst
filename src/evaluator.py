"""
Model Evaluator Module for CL Futures ML Pipeline.

This module provides evaluation metrics and analysis for model predictions:
- Classification metrics (accuracy, precision, recall, F1, confusion matrix)
- Actual move magnitude analysis using RAW_ columns
- Fold-by-fold and aggregate reporting
- CSV export of predictions with actual moves

Author: CL Analyst
"""

from __future__ import annotations

import json
import os
import numpy as np
import pandas as pd
from typing import List, Dict, Optional, Any
from datetime import datetime


class ModelEvaluator:
    """
    Evaluator for ML model predictions on CL futures data.
    
    Uses RAW_ columns from the processed DataFrame to compute actual
    price move magnitudes, allowing analysis beyond simple accuracy.
    
    Attributes:
        threshold (float): The target threshold used for labeling (default: 0.08 = 8%)
    """
    
    # Class labels
    HOLD = 0
    BUY = 1
    SELL = 2
    CLASS_NAMES = {0: 'Hold', 1: 'Buy', 2: 'Sell'}
    
    def __init__(self, threshold: float = 0.08):
        """
        Initialize the ModelEvaluator.
        
        Args:
            threshold: The target threshold for significant moves (0.08 = 8%)
        """
        self.threshold = threshold
    
    def evaluate_fold(
        self, 
        y_true: np.ndarray, 
        y_pred: np.ndarray, 
        df_test: pd.DataFrame
    ) -> dict:
        """
        Compute comprehensive metrics for a single fold.
        
        Uses RAW_ columns from df_test to calculate actual move magnitudes,
        enabling analysis of how close predictions were even when wrong.
        
        Args:
            y_true: Actual labels (0=Hold, 1=Buy, 2=Sell)
            y_pred: Predicted labels
            df_test: Test DataFrame containing RAW_Close, RAW_Future_High, RAW_Future_Low
            
        Returns:
            dict: Metrics including accuracy, per-class stats, confusion matrix,
                  and actual move analysis
        """
        y_true = np.asarray(y_true).ravel()
        y_pred = np.asarray(y_pred).ravel()
        
        # Basic classification metrics
        accuracy = (y_true == y_pred).mean()
        
        # Per-class metrics
        classes = [self.HOLD, self.BUY, self.SELL]
        precision = {}
        recall = {}
        f1 = {}
        support = {}
        
        for cls in classes:
            cls_name = self.CLASS_NAMES[cls]
            
            # True positives, false positives, false negatives
            tp = ((y_pred == cls) & (y_true == cls)).sum()
            fp = ((y_pred == cls) & (y_true != cls)).sum()
            fn = ((y_pred != cls) & (y_true == cls)).sum()
            
            # Precision, recall, F1
            precision[cls_name] = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall[cls_name] = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            
            if precision[cls_name] + recall[cls_name] > 0:
                f1[cls_name] = 2 * precision[cls_name] * recall[cls_name] / (precision[cls_name] + recall[cls_name])
            else:
                f1[cls_name] = 0.0
            
            support[cls_name] = (y_true == cls).sum()
        
        # Confusion matrix
        confusion = np.zeros((3, 3), dtype=int)
        for true_cls in classes:
            for pred_cls in classes:
                confusion[true_cls, pred_cls] = ((y_true == true_cls) & (y_pred == pred_cls)).sum()
        
        # Calculate actual moves from RAW columns
        actual_moves = self._calculate_actual_moves(df_test, y_pred)
        
        # Analyze move magnitudes by predicted class
        move_analysis = self._analyze_moves_by_prediction(actual_moves, y_pred, y_true)
        
        return {
            'accuracy': accuracy,
            'precision': precision,
            'recall': recall,
            'f1': f1,
            'support': support,
            'confusion_matrix': confusion,
            'actual_moves': actual_moves,
            'move_analysis': move_analysis,
            'n_samples': len(y_true),
        }
    
    def _calculate_actual_moves(
        self, 
        df_test: pd.DataFrame, 
        y_pred: np.ndarray
    ) -> pd.DataFrame:
        """
        Calculate actual move percentages from RAW columns.
        
        Args:
            df_test: Test DataFrame with RAW_ columns
            y_pred: Predicted labels
            
        Returns:
            DataFrame with columns:
                - DateTime: Index from df_test
                - Predicted: Predicted class (0, 1, 2)
                - Predicted_Label: Human-readable label
                - Actual_Up_Pct: Actual upward move percentage
                - Actual_Down_Pct: Actual downward move percentage
                - Hit_Threshold_Up: Whether up move exceeded threshold
                - Hit_Threshold_Down: Whether down move exceeded threshold
        """
        # Check for required columns
        required_cols = ['RAW_Close', 'RAW_Future_High', 'RAW_Future_Low']
        missing = [c for c in required_cols if c not in df_test.columns]
        if missing:
            raise ValueError(f"Missing required RAW columns: {missing}")
        
        # Calculate actual percentage moves
        actual_up = (df_test['RAW_Future_High'] - df_test['RAW_Close']) / df_test['RAW_Close']
        actual_down = (df_test['RAW_Close'] - df_test['RAW_Future_Low']) / df_test['RAW_Close']
        
        # Build results DataFrame
        results = pd.DataFrame({
            'DateTime': df_test.index,
            'Predicted': y_pred,
            'Predicted_Label': [self.CLASS_NAMES.get(p, 'Unknown') for p in y_pred],
            'TARGET_Direction': df_test['TARGET_Direction'].values if 'TARGET_Direction' in df_test.columns else np.nan,
            'Actual_Up_Pct': actual_up.values,
            'Actual_Down_Pct': actual_down.values,
            'Hit_Threshold_Up': (actual_up >= self.threshold).values,
            'Hit_Threshold_Down': (actual_down >= self.threshold).values,
            'RAW_Close': df_test['RAW_Close'].values,
        })
        results.set_index('DateTime', inplace=True)
        
        return results
    
    def _analyze_moves_by_prediction(
        self, 
        actual_moves: pd.DataFrame,
        y_pred: np.ndarray,
        y_true: np.ndarray
    ) -> dict:
        """
        Analyze actual move magnitudes grouped by predicted class.
        
        This helps understand:
        - When model predicts Buy, how much does price actually move up/down?
        - What's the average move when predictions are correct vs wrong?
        
        Args:
            actual_moves: DataFrame from _calculate_actual_moves
            y_pred: Predicted labels
            y_true: Actual labels
            
        Returns:
            dict: Analysis by predicted class and correctness
        """
        analysis = {}
        
        for cls in [self.HOLD, self.BUY, self.SELL]:
            cls_name = self.CLASS_NAMES[cls]
            mask = y_pred == cls
            
            if mask.sum() == 0:
                analysis[cls_name] = {
                    'count': 0,
                    'mean_up': None,
                    'mean_down': None,
                    'correct_count': 0,
                    'correct_mean_up': None,
                    'correct_mean_down': None,
                }
                continue
            
            cls_moves = actual_moves.iloc[mask]
            correct_mask = (y_pred == cls) & (y_true == cls)
            
            analysis[cls_name] = {
                'count': int(mask.sum()),
                'mean_up': float(cls_moves['Actual_Up_Pct'].mean()),
                'mean_down': float(cls_moves['Actual_Down_Pct'].mean()),
                'median_up': float(cls_moves['Actual_Up_Pct'].median()),
                'median_down': float(cls_moves['Actual_Down_Pct'].median()),
                'std_up': float(cls_moves['Actual_Up_Pct'].std()),
                'std_down': float(cls_moves['Actual_Down_Pct'].std()),
                'hit_threshold_up_pct': float(cls_moves['Hit_Threshold_Up'].mean()),
                'hit_threshold_down_pct': float(cls_moves['Hit_Threshold_Down'].mean()),
                'correct_count': int(correct_mask.sum()),
            }
            
            # Stats for correct predictions only
            if correct_mask.sum() > 0:
                correct_moves = actual_moves.iloc[correct_mask]
                analysis[cls_name]['correct_mean_up'] = float(correct_moves['Actual_Up_Pct'].mean())
                analysis[cls_name]['correct_mean_down'] = float(correct_moves['Actual_Down_Pct'].mean())
            else:
                analysis[cls_name]['correct_mean_up'] = None
                analysis[cls_name]['correct_mean_down'] = None
        
        return analysis
    
    def generate_report(self, fold_results: List[dict]) -> dict:
        """
        Aggregate metrics across all folds.
        
        Args:
            fold_results: List of dicts from evaluate_fold()
            
        Returns:
            dict: Aggregated metrics with mean, std, min, max across folds
        """
        n_folds = len(fold_results)
        
        # Aggregate accuracy
        accuracies = [r['accuracy'] for r in fold_results]
        
        # Aggregate per-class metrics
        class_metrics = {}
        for cls_name in self.CLASS_NAMES.values():
            precisions = [r['precision'][cls_name] for r in fold_results]
            recalls = [r['recall'][cls_name] for r in fold_results]
            f1s = [r['f1'][cls_name] for r in fold_results]
            
            class_metrics[cls_name] = {
                'precision': {'mean': np.mean(precisions), 'std': np.std(precisions)},
                'recall': {'mean': np.mean(recalls), 'std': np.std(recalls)},
                'f1': {'mean': np.mean(f1s), 'std': np.std(f1s)},
            }
        
        # Sum confusion matrices
        total_confusion = np.sum([r['confusion_matrix'] for r in fold_results], axis=0)
        
        # Aggregate move analysis
        move_analysis_agg = {}
        for cls_name in self.CLASS_NAMES.values():
            mean_ups = [r['move_analysis'][cls_name]['mean_up'] 
                       for r in fold_results if r['move_analysis'][cls_name]['mean_up'] is not None]
            mean_downs = [r['move_analysis'][cls_name]['mean_down'] 
                         for r in fold_results if r['move_analysis'][cls_name]['mean_down'] is not None]
            
            move_analysis_agg[cls_name] = {
                'mean_up': np.mean(mean_ups) if mean_ups else None,
                'mean_down': np.mean(mean_downs) if mean_downs else None,
            }
        
        return {
            'n_folds': n_folds,
            'accuracy': {
                'mean': np.mean(accuracies),
                'std': np.std(accuracies),
                'min': np.min(accuracies),
                'max': np.max(accuracies),
                'all': accuracies,
            },
            'class_metrics': class_metrics,
            'total_confusion_matrix': total_confusion.tolist(),
            'move_analysis': move_analysis_agg,
            'threshold': self.threshold,
            'timestamp': datetime.now().isoformat(),
        }
    
    def export_predictions(
        self, 
        fold_results: List[dict], 
        output_path: str,
        include_all_folds: bool = True
    ) -> str:
        """
        Export predictions with actual moves to CSV.
        
        Args:
            fold_results: List of dicts from evaluate_fold()
            output_path: Path for output CSV
            include_all_folds: If True, concatenate all folds; if False, only last fold
            
        Returns:
            str: Path to saved CSV
        """
        if include_all_folds:
            # Concatenate all fold predictions
            all_moves = []
            for i, result in enumerate(fold_results, 1):
                moves = result['actual_moves'].copy()
                moves['Fold'] = i
                all_moves.append(moves)
            combined = pd.concat(all_moves)
        else:
            combined = fold_results[-1]['actual_moves'].copy()
            combined['Fold'] = len(fold_results)
        
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        
        combined.to_csv(output_path)
        print(f"Exported predictions to {output_path}")
        
        return output_path
    
    def save_report(self, report: dict, output_path: str) -> str:
        """
        Save report to JSON file.
        
        Args:
            report: Report dict from generate_report()
            output_path: Path for output JSON
            
        Returns:
            str: Path to saved JSON
        """
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        
        # Convert numpy types for JSON serialization
        def convert(obj):
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, (np.integer, np.int64)):
                return int(obj)
            if isinstance(obj, (np.floating, np.float64)):
                return float(obj)
            if isinstance(obj, dict):
                return {k: convert(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [convert(v) for v in obj]
            return obj
        
        report_serializable = convert(report)
        
        with open(output_path, 'w') as f:
            json.dump(report_serializable, f, indent=2)
        
        print(f"Saved report to {output_path}")
        return output_path
    
    def print_report(self, report: dict) -> None:
        """
        Print a formatted summary of the report.
        
        Args:
            report: Report dict from generate_report()
        """
        print("\n" + "=" * 60)
        print("EVALUATION REPORT")
        print("=" * 60)
        
        print(f"\nFolds evaluated: {report['n_folds']}")
        print(f"Target threshold: {report['threshold']*100:.1f}%")
        
        print(f"\nOverall Accuracy:")
        acc = report['accuracy']
        print(f"  Mean: {acc['mean']:.4f} (+/- {acc['std']:.4f})")
        print(f"  Range: [{acc['min']:.4f}, {acc['max']:.4f}]")
        print(f"  Per-fold: {[f'{a:.4f}' for a in acc['all']]}")
        
        print(f"\nPer-Class Metrics (mean +/- std):")
        for cls_name, metrics in report['class_metrics'].items():
            p = metrics['precision']
            r = metrics['recall']
            f = metrics['f1']
            print(f"  {cls_name}:")
            print(f"    Precision: {p['mean']:.4f} (+/- {p['std']:.4f})")
            print(f"    Recall:    {r['mean']:.4f} (+/- {r['std']:.4f})")
            print(f"    F1:        {f['mean']:.4f} (+/- {f['std']:.4f})")
        
        print(f"\nConfusion Matrix (total across folds):")
        cm = np.array(report['total_confusion_matrix'])
        print("                 Predicted")
        print("              Hold   Buy  Sell")
        for i, cls_name in enumerate(['Hold', 'Buy', 'Sell']):
            print(f"  Actual {cls_name:4s} {cm[i, 0]:5d} {cm[i, 1]:5d} {cm[i, 2]:5d}")
        
        print(f"\nActual Move Analysis (mean across folds):")
        for cls_name, analysis in report['move_analysis'].items():
            up = analysis['mean_up']
            down = analysis['mean_down']
            up_str = f"{up*100:.2f}%" if up is not None else "N/A"
            down_str = f"{down*100:.2f}%" if down is not None else "N/A"
            print(f"  When predicted {cls_name}:")
            print(f"    Avg actual up move:   {up_str}")
            print(f"    Avg actual down move: {down_str}")
        
        print("\n" + "=" * 60)


def quick_evaluate(
    y_true: np.ndarray, 
    y_pred: np.ndarray,
    df_test: pd.DataFrame = None,
    threshold: float = 0.08
) -> dict:
    """
    Quick evaluation of predictions without full fold infrastructure.
    
    Args:
        y_true: Actual labels
        y_pred: Predicted labels
        df_test: Optional test DataFrame with RAW_ columns
        threshold: Target threshold (default: 0.08)
        
    Returns:
        dict: Evaluation metrics
    """
    evaluator = ModelEvaluator(threshold=threshold)
    
    if df_test is not None:
        return evaluator.evaluate_fold(y_true, y_pred, df_test)
    else:
        # Basic evaluation without actual moves
        y_true = np.asarray(y_true).ravel()
        y_pred = np.asarray(y_pred).ravel()
        
        accuracy = (y_true == y_pred).mean()
        
        return {
            'accuracy': accuracy,
            'n_samples': len(y_true),
        }
