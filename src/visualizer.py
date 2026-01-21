"""
Visualizer Module for CL Futures ML Pipeline.

This module provides visualization tools for model predictions:
- Price charts with signal markers (Buy=Red, Sell=Green)
- Multi-panel plots with indicators
- Fold-by-fold visualization

Author: CL Analyst
"""

from __future__ import annotations

import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from typing import Optional, List, Tuple
from datetime import datetime


class SignalVisualizer:
    """
    Visualizer for trading signals on price charts.
    
    Creates plots showing:
    - Price line (RAW_Close)
    - Vertical lines for Buy signals (red)
    - Vertical lines for Sell signals (green)
    """
    
    # Signal colors - Red for Buy (going long), Green for Sell (going short)
    BUY_COLOR = 'red'
    SELL_COLOR = 'green'
    HOLD_COLOR = 'gray'
    
    # Class labels
    HOLD = 0
    BUY = 1
    SELL = 2
    
    def __init__(self, figsize: Tuple[int, int] = (16, 8), dpi: int = 100):
        """
        Initialize the SignalVisualizer.
        
        Args:
            figsize: Figure size as (width, height) in inches
            dpi: Dots per inch for saved figures
        """
        self.figsize = figsize
        self.dpi = dpi
    
    def plot_signals(
        self,
        df: pd.DataFrame,
        predictions: np.ndarray,
        output_path: Optional[str] = None,
        title: str = "Model Signals",
        date_range: Optional[Tuple[str, str]] = None,
        show_legend: bool = True,
        max_signals: int = 100,
    ) -> plt.Figure:
        """
        Plot price chart with signal markers.
        
        Args:
            df: DataFrame with RAW_Close column and datetime index
            predictions: Array of predicted labels (0=Hold, 1=Buy, 2=Sell)
            output_path: Path to save figure (optional)
            title: Plot title
            date_range: Optional (start, end) date strings to filter
            show_legend: Whether to show legend
            max_signals: Maximum number of signals to plot (for readability)
            
        Returns:
            matplotlib Figure object
        """
        # Check for required columns
        if 'RAW_Close' not in df.columns:
            raise ValueError("DataFrame must contain 'RAW_Close' column")
        
        # Filter by date range if specified
        plot_df = df.copy()
        if date_range is not None:
            start, end = date_range
            plot_df = plot_df.loc[start:end]
            predictions = predictions[df.index.isin(plot_df.index)]
        
        # Create figure
        fig, ax = plt.subplots(figsize=self.figsize)
        
        # Plot price line
        ax.plot(plot_df.index, plot_df['RAW_Close'], 
                color='blue', linewidth=1, label='Close Price', alpha=0.7)
        
        # Find signal positions
        buy_mask = predictions == self.BUY
        sell_mask = predictions == self.SELL
        
        buy_dates = plot_df.index[buy_mask]
        sell_dates = plot_df.index[sell_mask]
        
        # Limit signals for readability
        if len(buy_dates) > max_signals:
            step = len(buy_dates) // max_signals
            buy_dates = buy_dates[::step]
        if len(sell_dates) > max_signals:
            step = len(sell_dates) // max_signals
            sell_dates = sell_dates[::step]
        
        # Plot buy signals (red vertical lines)
        buy_plotted = False
        for date in buy_dates:
            ax.axvline(x=date, color=self.BUY_COLOR, alpha=0.3, linewidth=0.5,
                      label='Buy Signal' if not buy_plotted else '')
            buy_plotted = True
        
        # Plot sell signals (green vertical lines)
        sell_plotted = False
        for date in sell_dates:
            ax.axvline(x=date, color=self.SELL_COLOR, alpha=0.3, linewidth=0.5,
                      label='Sell Signal' if not sell_plotted else '')
            sell_plotted = True
        
        # Format plot
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('Date', fontsize=12)
        ax.set_ylabel('Price', fontsize=12)
        
        # Format x-axis dates
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        plt.xticks(rotation=45)
        
        # Add grid
        ax.grid(True, alpha=0.3)
        
        # Add legend
        if show_legend:
            ax.legend(loc='upper left')
        
        # Add signal counts annotation
        n_buy = buy_mask.sum()
        n_sell = sell_mask.sum()
        n_hold = (predictions == self.HOLD).sum()
        ax.annotate(
            f'Buy: {n_buy} | Sell: {n_sell} | Hold: {n_hold}',
            xy=(0.99, 0.99), xycoords='axes fraction',
            ha='right', va='top', fontsize=10,
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8)
        )
        
        plt.tight_layout()
        
        # Save if path provided
        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            fig.savefig(output_path, dpi=self.dpi, bbox_inches='tight')
            print(f"Saved signal plot to {output_path}")
        
        return fig
    
    def plot_fold_summary(
        self,
        fold_results: List[dict],
        output_path: Optional[str] = None,
        title: str = "Walk-Forward Validation Results",
    ) -> plt.Figure:
        """
        Create a summary plot showing performance across folds.
        
        Args:
            fold_results: List of fold result dicts (from walk_forward_validate)
            output_path: Path to save figure (optional)
            title: Plot title
            
        Returns:
            matplotlib Figure object
        """
        n_folds = len(fold_results)
        
        # Extract metrics
        accuracies = [(fold['y_pred'] == fold['y_true']).mean() 
                     for fold in fold_results]
        train_sizes = [fold['train_size'] for fold in fold_results]
        test_sizes = [fold['test_size'] for fold in fold_results]
        
        # Create figure with subplots
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        
        # 1. Accuracy by fold
        ax1 = axes[0, 0]
        bars = ax1.bar(range(1, n_folds + 1), accuracies, color='steelblue', alpha=0.7)
        ax1.axhline(np.mean(accuracies), color='red', linestyle='--', 
                   label=f'Mean: {np.mean(accuracies):.4f}')
        ax1.set_xlabel('Fold')
        ax1.set_ylabel('Accuracy')
        ax1.set_title('Accuracy by Fold')
        ax1.set_xticks(range(1, n_folds + 1))
        ax1.legend()
        ax1.set_ylim(0, 1)
        
        # Add value labels on bars
        for bar, acc in zip(bars, accuracies):
            ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.01,
                    f'{acc:.3f}', ha='center', va='bottom', fontsize=9)
        
        # 2. Train/Test sizes by fold
        ax2 = axes[0, 1]
        x = np.arange(n_folds)
        width = 0.35
        ax2.bar(x - width/2, train_sizes, width, label='Train', color='blue', alpha=0.7)
        ax2.bar(x + width/2, test_sizes, width, label='Test', color='orange', alpha=0.7)
        ax2.set_xlabel('Fold')
        ax2.set_ylabel('Number of Samples')
        ax2.set_title('Train/Test Sizes by Fold')
        ax2.set_xticks(x)
        ax2.set_xticklabels(range(1, n_folds + 1))
        ax2.legend()
        
        # 3. Prediction distribution per fold
        ax3 = axes[1, 0]
        buy_counts = [(fold['y_pred'] == self.BUY).sum() for fold in fold_results]
        sell_counts = [(fold['y_pred'] == self.SELL).sum() for fold in fold_results]
        hold_counts = [(fold['y_pred'] == self.HOLD).sum() for fold in fold_results]
        
        x = np.arange(n_folds)
        width = 0.25
        ax3.bar(x - width, hold_counts, width, label='Hold', color='gray', alpha=0.7)
        ax3.bar(x, buy_counts, width, label='Buy', color=self.BUY_COLOR, alpha=0.7)
        ax3.bar(x + width, sell_counts, width, label='Sell', color=self.SELL_COLOR, alpha=0.7)
        ax3.set_xlabel('Fold')
        ax3.set_ylabel('Prediction Count')
        ax3.set_title('Prediction Distribution by Fold')
        ax3.set_xticks(x)
        ax3.set_xticklabels(range(1, n_folds + 1))
        ax3.legend()
        
        # 4. Cumulative accuracy (expanding window effect)
        ax4 = axes[1, 1]
        cumulative_correct = np.cumsum([
            (fold['y_pred'] == fold['y_true']).sum() 
            for fold in fold_results
        ])
        cumulative_total = np.cumsum([fold['test_size'] for fold in fold_results])
        cumulative_accuracy = cumulative_correct / cumulative_total
        
        ax4.plot(range(1, n_folds + 1), cumulative_accuracy, 
                marker='o', color='green', linewidth=2)
        ax4.axhline(cumulative_accuracy[-1], color='red', linestyle='--',
                   label=f'Final: {cumulative_accuracy[-1]:.4f}')
        ax4.set_xlabel('Fold')
        ax4.set_ylabel('Cumulative Accuracy')
        ax4.set_title('Cumulative Accuracy (All Folds)')
        ax4.set_xticks(range(1, n_folds + 1))
        ax4.legend()
        ax4.set_ylim(0, 1)
        
        fig.suptitle(title, fontsize=16, fontweight='bold')
        plt.tight_layout()
        
        # Save if path provided
        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            fig.savefig(output_path, dpi=self.dpi, bbox_inches='tight')
            print(f"Saved fold summary to {output_path}")
        
        return fig
    
    def plot_actual_moves(
        self,
        actual_moves: pd.DataFrame,
        output_path: Optional[str] = None,
        title: str = "Actual Move Distribution by Predicted Class",
    ) -> plt.Figure:
        """
        Plot distribution of actual moves grouped by predicted class.
        
        Args:
            actual_moves: DataFrame from evaluator._calculate_actual_moves()
            output_path: Path to save figure (optional)
            title: Plot title
            
        Returns:
            matplotlib Figure object
        """
        fig, axes = plt.subplots(1, 3, figsize=(15, 5))
        
        for i, (cls, cls_name) in enumerate([(self.HOLD, 'Hold'), 
                                              (self.BUY, 'Buy'), 
                                              (self.SELL, 'Sell')]):
            ax = axes[i]
            mask = actual_moves['Predicted'] == cls
            
            if mask.sum() == 0:
                ax.text(0.5, 0.5, 'No predictions', ha='center', va='center')
                ax.set_title(f'Predicted: {cls_name} (n=0)')
                continue
            
            cls_moves = actual_moves[mask]
            
            # Plot histograms of up and down moves
            ax.hist(cls_moves['Actual_Up_Pct'] * 100, bins=30, alpha=0.5, 
                   label='Up Move', color='green')
            ax.hist(cls_moves['Actual_Down_Pct'] * 100, bins=30, alpha=0.5, 
                   label='Down Move', color='red')
            
            # Add mean lines
            ax.axvline(cls_moves['Actual_Up_Pct'].mean() * 100, 
                      color='green', linestyle='--', linewidth=2)
            ax.axvline(cls_moves['Actual_Down_Pct'].mean() * 100, 
                      color='red', linestyle='--', linewidth=2)
            
            ax.set_xlabel('Move %')
            ax.set_ylabel('Frequency')
            ax.set_title(f'Predicted: {cls_name} (n={mask.sum()})')
            ax.legend()
            ax.grid(True, alpha=0.3)
        
        fig.suptitle(title, fontsize=14, fontweight='bold')
        plt.tight_layout()
        
        # Save if path provided
        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            fig.savefig(output_path, dpi=self.dpi, bbox_inches='tight')
            print(f"Saved actual moves plot to {output_path}")
        
        return fig

    def plot_confusion_matrix(
        self,
        confusion_matrix: np.ndarray,
        output_path: Optional[str] = None,
        title: str = "Confusion Matrix",
        class_labels: Optional[List[str]] = None,
    ) -> plt.Figure:
        """
        Plot a confusion matrix heatmap.

        Args:
            confusion_matrix: 2D array (n_classes x n_classes)
            output_path: Path to save figure (optional)
            title: Plot title
            class_labels: Optional list of class names in order

        Returns:
            matplotlib Figure object
        """
        if class_labels is None:
            class_labels = ["Hold", "Buy", "Sell"]

        fig, ax = plt.subplots(figsize=(6, 5))
        im = ax.imshow(confusion_matrix, cmap="Blues")

        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.set_xlabel("Predicted")
        ax.set_ylabel("Actual")
        ax.set_xticks(np.arange(len(class_labels)))
        ax.set_yticks(np.arange(len(class_labels)))
        ax.set_xticklabels(class_labels)
        ax.set_yticklabels(class_labels)

        # Annotate cells
        for i in range(confusion_matrix.shape[0]):
            for j in range(confusion_matrix.shape[1]):
                ax.text(
                    j,
                    i,
                    str(confusion_matrix[i, j]),
                    ha="center",
                    va="center",
                    color="black",
                    fontsize=10,
                )

        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        plt.tight_layout()

        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            fig.savefig(output_path, dpi=self.dpi, bbox_inches="tight")
            print(f"Saved confusion matrix to {output_path}")

        return fig

    def plot_feature_importance(
        self,
        learner,
        feature_names: Optional[List[str]] = None,
        output_path: Optional[str] = None,
        title: str = "Feature Importance (Gain)",
        max_features: int = 20,
    ) -> plt.Figure:
        """
        Plot feature importance for a trained LightGBM model.

        Args:
            learner: LGBMLearner instance with a trained model
            feature_names: Optional list of feature names
            output_path: Path to save figure (optional)
            title: Plot title
            max_features: Maximum number of features to display

        Returns:
            matplotlib Figure object
        """
        if learner is None or learner.model is None:
            raise ValueError("Learner model is not trained.")

        importances = learner.model.feature_importance(importance_type="gain")
        if feature_names is None:
            feature_names = [f"f{i}" for i in range(len(importances))]

        pairs = list(zip(feature_names, importances))
        pairs.sort(key=lambda x: x[1], reverse=True)
        top_pairs = pairs[:max_features]

        labels = [p[0] for p in top_pairs][::-1]
        scores = [p[1] for p in top_pairs][::-1]

        fig, ax = plt.subplots(figsize=(10, 6))
        ax.barh(labels, scores, color="steelblue")
        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.set_xlabel("Importance (Gain)")
        ax.grid(True, axis="x", alpha=0.3)
        plt.tight_layout()

        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            fig.savefig(output_path, dpi=self.dpi, bbox_inches="tight")
            print(f"Saved feature importance to {output_path}")

        return fig
    
    def plot_signals_with_actual_moves(
        self,
        df: pd.DataFrame,
        predictions: np.ndarray,
        output_path: Optional[str] = None,
        title: str = "Signals with Actual Move Context",
        date_range: Optional[Tuple[str, str]] = None,
        max_signals: int = 50,
    ) -> plt.Figure:
        """
        Plot price chart with signals and annotations showing actual moves.
        
        Args:
            df: DataFrame with RAW_Close, RAW_Future_High, RAW_Future_Low columns
            predictions: Array of predicted labels
            output_path: Path to save figure (optional)
            title: Plot title
            date_range: Optional (start, end) date strings to filter
            max_signals: Maximum number of signals to annotate
            
        Returns:
            matplotlib Figure object
        """
        required_cols = ['RAW_Close', 'RAW_Future_High', 'RAW_Future_Low']
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")
        
        # Filter by date range if specified
        plot_df = df.copy()
        preds = predictions.copy()
        if date_range is not None:
            start, end = date_range
            mask = (df.index >= start) & (df.index <= end)
            plot_df = df.loc[mask]
            preds = predictions[mask]
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(self.figsize[0], self.figsize[1] * 1.5),
                                        gridspec_kw={'height_ratios': [3, 1]})
        
        # Top plot: Price with signals
        ax1.plot(plot_df.index, plot_df['RAW_Close'], 
                color='blue', linewidth=1, label='Close', alpha=0.7)
        
        # Add future high/low bands
        ax1.fill_between(plot_df.index, plot_df['RAW_Close'], plot_df['RAW_Future_High'],
                        alpha=0.1, color='green', label='Future High Range')
        ax1.fill_between(plot_df.index, plot_df['RAW_Future_Low'], plot_df['RAW_Close'],
                        alpha=0.1, color='red', label='Future Low Range')
        
        # Plot signals
        buy_mask = preds == self.BUY
        sell_mask = preds == self.SELL
        
        buy_dates = plot_df.index[buy_mask]
        sell_dates = plot_df.index[sell_mask]
        
        # Limit signals
        if len(buy_dates) > max_signals:
            buy_dates = buy_dates[::len(buy_dates) // max_signals]
        if len(sell_dates) > max_signals:
            sell_dates = sell_dates[::len(sell_dates) // max_signals]
        
        for date in buy_dates:
            ax1.axvline(x=date, color=self.BUY_COLOR, alpha=0.4, linewidth=0.8)
        for date in sell_dates:
            ax1.axvline(x=date, color=self.SELL_COLOR, alpha=0.4, linewidth=0.8)
        
        ax1.set_title(title, fontsize=14, fontweight='bold')
        ax1.set_ylabel('Price')
        ax1.legend(loc='upper left')
        ax1.grid(True, alpha=0.3)
        
        # Bottom plot: Prediction bar
        ax2.bar(plot_df.index, preds - 1, width=1, 
               color=[self.SELL_COLOR if p == 2 else self.BUY_COLOR if p == 1 else self.HOLD_COLOR 
                     for p in preds], alpha=0.7)
        ax2.axhline(0, color='black', linewidth=0.5)
        ax2.set_ylabel('Signal')
        ax2.set_yticks([-1, 0, 1])
        ax2.set_yticklabels(['Sell', 'Hold', 'Buy'])
        ax2.set_xlabel('Date')
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        plt.xticks(rotation=45)
        ax2.grid(True, alpha=0.3)
        
        plt.tight_layout()
        
        # Save if path provided
        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            fig.savefig(output_path, dpi=self.dpi, bbox_inches='tight')
            print(f"Saved signals with context to {output_path}")
        
        return fig


def quick_plot_signals(
    df: pd.DataFrame,
    predictions: np.ndarray,
    output_path: Optional[str] = None,
    title: str = "Model Signals",
) -> plt.Figure:
    """
    Quick convenience function to plot signals.
    
    Args:
        df: DataFrame with RAW_Close column
        predictions: Array of predicted labels
        output_path: Path to save figure (optional)
        title: Plot title
        
    Returns:
        matplotlib Figure object
    """
    viz = SignalVisualizer()
    return viz.plot_signals(df, predictions, output_path, title)
