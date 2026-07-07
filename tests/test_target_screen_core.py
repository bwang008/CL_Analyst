"""Target-screen CORE tests — ticket cloud-target-screen-core_07072026_1223.

Covers the fixed-param, no-Optuna target-screening path:
  * TrainingWorkflowConfig.mode ("optimize" default / "screen" / reject bogus)
  * _screen_one_target  — trains ONE LGBM per target, computes holdout edge metrics
  * run_screen          — split + loop + report, deterministic across seeds
  * write_auc_report    — sorted markdown with every column

The fixture is a tiny synthetic parquet (~800 rows, DatetimeIndex, ~6 numeric
feature cols) with one LEARNABLE target and one PURE-NOISE target so LGBM trains
in a couple of seconds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gcp.vm_e2e_pipeline import (
    SCREEN_LGBM_PARAMS,
    _screen_one_target,
    run_screen,
    write_auc_report,
)
from src.config.schemas import TrainingWorkflowConfig


# ---------------------------------------------------------------------------
# Synthetic fixture
# ---------------------------------------------------------------------------

FEATURE_COLS = [
    "MOM_ret_24",
    "MOM_ret_72",
    "VOL_ATR_14",
    "RSI_14",
    "MACD_hist",
    "TS_slope",
]
LEARNABLE_TARGET = "TARGET_TRIPLE_2x1_6H_LONG"
NOISE_TARGET = "TARGET_TRIPLE_2x1_6H_SHORT"


def _make_synthetic_df(n_rows: int = 800, seed: int = 7) -> pd.DataFrame:
    """Build a tiny screenable dataset.

    - 6 numeric feature columns.
    - LEARNABLE target: a deterministic function of a subset of features so an
      LGBM can recover real edge (holdout AUC materially > 0.5).
    - NOISE target: a coin flip independent of the features (AUC ~ 0.5).
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2018-01-01", periods=n_rows, freq="h")

    X = rng.normal(size=(n_rows, len(FEATURE_COLS)))
    df = pd.DataFrame(X, columns=FEATURE_COLS, index=idx)

    # Learnable signal: linear combo of two features + small noise, thresholded.
    signal = 1.5 * df["MOM_ret_24"] - 1.0 * df["RSI_14"] + 0.3 * rng.normal(size=n_rows)
    df[LEARNABLE_TARGET] = (signal > 0).astype(float)

    # Pure noise: independent coin flip.
    df[NOISE_TARGET] = (rng.normal(size=n_rows) > 0).astype(float)

    return df


@pytest.fixture
def synthetic_screen_df() -> pd.DataFrame:
    return _make_synthetic_df()


@pytest.fixture
def synthetic_parquet(tmp_path, synthetic_screen_df) -> str:
    path = tmp_path / "SYNTH_screen.parquet"
    synthetic_screen_df.to_parquet(path)
    return str(path)


# Split cutoff roughly at 70% for an 800-row/hourly frame starting 2018-01-01.
TRAIN_CUTOFF = "2018-01-24"


DOC_KEYS = {
    "target",
    "direction",
    "auc_train",
    "auc_holdout",
    "pr_auc_holdout",
    "pos_rate_train",
    "pos_rate_holdout",
    "n_train",
    "n_holdout",
    "prob_spread",
    "precision_at_ref",
    "signals_per_yr_at_ref",
}


# ---------------------------------------------------------------------------
# 1. Schema: TrainingWorkflowConfig.mode
# ---------------------------------------------------------------------------

class TestTrainingWorkflowConfigMode:
    def _base_kwargs(self) -> dict:
        # optuna.post_optimizer_holdout_months is a REQUIRED nested field
        # (no silent-null default) — supply a valid block so the model
        # instantiates; the assertions here only concern `mode`.
        return dict(
            train_cutoff_date="2022-01-01",
            target_columns=["TARGET_TRIPLE_2x1_6H_LONG"],
            gcs_base_dir="gs://bucket/run",
            random_seed=42,
            optuna={"post_optimizer_holdout_months": 3},
        )

    def test_mode_defaults_to_optimize(self):
        cfg = TrainingWorkflowConfig(**self._base_kwargs())
        assert cfg.mode == "optimize"

    def test_mode_accepts_screen(self):
        cfg = TrainingWorkflowConfig(mode="screen", **self._base_kwargs())
        assert cfg.mode == "screen"

    def test_mode_rejects_bogus(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TrainingWorkflowConfig(mode="bogus", **self._base_kwargs())


# ---------------------------------------------------------------------------
# 2. SCREEN_LGBM_PARAMS constant
# ---------------------------------------------------------------------------

class TestScreenParams:
    def test_screen_params_is_fixed_dict(self):
        assert isinstance(SCREEN_LGBM_PARAMS, dict)
        # A documented, fixed mid-box config — must carry these knobs.
        for key in ("max_depth", "num_leaves", "learning_rate", "n_estimators"):
            assert key in SCREEN_LGBM_PARAMS

    def test_screen_one_target_does_not_mutate_shared_params(self, synthetic_screen_df):
        """train_final_model .pop()s keys off params; _screen_one_target must
        deepcopy so the module constant is never mutated."""
        before = dict(SCREEN_LGBM_PARAMS)
        df = synthetic_screen_df
        cutoff = pd.Timestamp(TRAIN_CUTOFF)
        df_train = df[df.index < cutoff]
        df_vault = df[df.index >= cutoff]
        _screen_one_target(
            df_train=df_train,
            df_vault=df_vault,
            feature_cols=FEATURE_COLS,
            target_col=LEARNABLE_TARGET,
            direction="long",
            params=SCREEN_LGBM_PARAMS,
            random_seed=42,
        )
        assert dict(SCREEN_LGBM_PARAMS) == before


# ---------------------------------------------------------------------------
# 3. _screen_one_target
# ---------------------------------------------------------------------------

class TestScreenOneTarget:
    def _split(self, df):
        cutoff = pd.Timestamp(TRAIN_CUTOFF)
        return df[df.index < cutoff], df[df.index >= cutoff]

    def test_returns_all_documented_keys(self, synthetic_screen_df):
        df_train, df_vault = self._split(synthetic_screen_df)
        row = _screen_one_target(
            df_train=df_train,
            df_vault=df_vault,
            feature_cols=FEATURE_COLS,
            target_col=LEARNABLE_TARGET,
            direction="long",
            params=SCREEN_LGBM_PARAMS,
            random_seed=42,
        )
        assert DOC_KEYS.issubset(set(row.keys()))
        assert row["target"] == LEARNABLE_TARGET
        assert row["direction"] == "long"

    def test_auc_holdout_in_unit_interval(self, synthetic_screen_df):
        df_train, df_vault = self._split(synthetic_screen_df)
        row = _screen_one_target(
            df_train=df_train,
            df_vault=df_vault,
            feature_cols=FEATURE_COLS,
            target_col=LEARNABLE_TARGET,
            direction="long",
            params=SCREEN_LGBM_PARAMS,
            random_seed=42,
        )
        assert 0.0 <= row["auc_holdout"] <= 1.0

    def test_learnable_beats_noise(self, synthetic_screen_df):
        df_train, df_vault = self._split(synthetic_screen_df)
        learn = _screen_one_target(
            df_train=df_train,
            df_vault=df_vault,
            feature_cols=FEATURE_COLS,
            target_col=LEARNABLE_TARGET,
            direction="long",
            params=SCREEN_LGBM_PARAMS,
            random_seed=42,
        )
        noise = _screen_one_target(
            df_train=df_train,
            df_vault=df_vault,
            feature_cols=FEATURE_COLS,
            target_col=NOISE_TARGET,
            direction="short",
            params=SCREEN_LGBM_PARAMS,
            random_seed=42,
        )
        # Real edge should be materially above the noise floor.
        assert learn["auc_holdout"] > 0.55
        assert learn["auc_holdout"] > noise["auc_holdout"] + 0.05

    def test_degenerate_single_class_label_returns_nan_without_crash(self, synthetic_screen_df):
        """A vault split whose labels are all one class must yield nan AUC, not
        crash (roc_auc_score raises on single-class input)."""
        df_train, df_vault = self._split(synthetic_screen_df)
        df_vault = df_vault.copy()
        # Force all-positive labels in the holdout.
        df_vault[LEARNABLE_TARGET] = 1.0
        row = _screen_one_target(
            df_train=df_train,
            df_vault=df_vault,
            feature_cols=FEATURE_COLS,
            target_col=LEARNABLE_TARGET,
            direction="long",
            params=SCREEN_LGBM_PARAMS,
            random_seed=42,
        )
        assert np.isnan(row["auc_holdout"])


# ---------------------------------------------------------------------------
# 4. run_screen
# ---------------------------------------------------------------------------

class TestRunScreen:
    def test_writes_report_with_both_targets_and_headers(self, synthetic_parquet, tmp_path):
        out_dir = tmp_path / "screen_out"
        rows = run_screen(
            data_path=synthetic_parquet,
            train_cutoff_date=TRAIN_CUTOFF,
            targets=[LEARNABLE_TARGET, NOISE_TARGET],
            symbol="CL",
            output_dir=str(out_dir),
            random_seed=42,
        )
        report = out_dir / "AUC_Model_Report.md"
        assert report.exists()
        text = report.read_text(encoding="utf-8")
        assert LEARNABLE_TARGET in text
        assert NOISE_TARGET in text
        # Column headers present.
        for header in ("AUC train", "AUC holdout", "PR-AUC holdout", "signals/yr", "precision@ref"):
            assert header in text
        assert len(rows) == 2

    def test_rows_sorted_by_auc_holdout_desc(self, synthetic_parquet, tmp_path):
        out_dir = tmp_path / "screen_out"
        rows = run_screen(
            data_path=synthetic_parquet,
            train_cutoff_date=TRAIN_CUTOFF,
            targets=[NOISE_TARGET, LEARNABLE_TARGET],  # deliberately unsorted input
            symbol="CL",
            output_dir=str(out_dir),
            random_seed=42,
        )
        aucs = [r["auc_holdout"] for r in rows]
        assert aucs == sorted(aucs, reverse=True)
        # The learnable target should come first (highest holdout AUC).
        assert rows[0]["target"] == LEARNABLE_TARGET

    def test_deterministic_across_two_runs_same_seed(self, synthetic_parquet, tmp_path):
        rows_a = run_screen(
            data_path=synthetic_parquet,
            train_cutoff_date=TRAIN_CUTOFF,
            targets=[LEARNABLE_TARGET, NOISE_TARGET],
            symbol="CL",
            output_dir=str(tmp_path / "a"),
            random_seed=42,
        )
        rows_b = run_screen(
            data_path=synthetic_parquet,
            train_cutoff_date=TRAIN_CUTOFF,
            targets=[LEARNABLE_TARGET, NOISE_TARGET],
            symbol="CL",
            output_dir=str(tmp_path / "b"),
            random_seed=42,
        )
        by_target_a = {r["target"]: r["auc_holdout"] for r in rows_a}
        by_target_b = {r["target"]: r["auc_holdout"] for r in rows_b}
        assert by_target_a == by_target_b

    def test_direction_inferred_from_suffix(self, synthetic_parquet, tmp_path):
        rows = run_screen(
            data_path=synthetic_parquet,
            train_cutoff_date=TRAIN_CUTOFF,
            targets=[LEARNABLE_TARGET, NOISE_TARGET],
            symbol="CL",
            output_dir=str(tmp_path / "d"),
            random_seed=42,
        )
        dirs = {r["target"]: r["direction"] for r in rows}
        assert dirs[LEARNABLE_TARGET] == "long"
        assert dirs[NOISE_TARGET] == "short"


# ---------------------------------------------------------------------------
# 5. write_auc_report
# ---------------------------------------------------------------------------

class TestWriteAucReport:
    def _rows(self):
        return [
            {
                "target": "TARGET_A_LONG", "direction": "long",
                "auc_train": 0.72, "auc_holdout": 0.51, "pr_auc_holdout": 0.40,
                "pos_rate_train": 0.50, "pos_rate_holdout": 0.48,
                "n_train": 500, "n_holdout": 200, "prob_spread": 0.12,
                "precision_at_ref": 0.55, "signals_per_yr_at_ref": 120.0,
            },
            {
                "target": "TARGET_B_SHORT", "direction": "short",
                "auc_train": 0.80, "auc_holdout": 0.64, "pr_auc_holdout": 0.55,
                "pos_rate_train": 0.50, "pos_rate_holdout": 0.49,
                "n_train": 500, "n_holdout": 200, "prob_spread": 0.30,
                "precision_at_ref": 0.66, "signals_per_yr_at_ref": 120.0,
            },
        ]

    def test_output_sorted_desc_and_has_all_columns(self, tmp_path):
        out = tmp_path / "AUC_Model_Report.md"
        meta = {
            "symbol": "CL", "dataset": "SYNTH", "train_cutoff_date": "2018-01-24",
            "holdout_cutoff_date": None, "params": SCREEN_LGBM_PARAMS, "random_seed": 42,
        }
        write_auc_report(self._rows(), str(out), meta)
        text = out.read_text(encoding="utf-8")

        # All columns present.
        for header in (
            "target", "dir", "AUC train", "AUC holdout", "PR-AUC holdout",
            "pos% train", "pos% holdout", "prob spread", "signals/yr", "precision@ref",
        ):
            assert header in text

        # Sorted by auc_holdout desc: B (0.64) must appear before A (0.51).
        assert text.index("TARGET_B_SHORT") < text.index("TARGET_A_LONG")

    def test_meta_header_present(self, tmp_path):
        out = tmp_path / "AUC_Model_Report.md"
        meta = {
            "symbol": "CL", "dataset": "SYNTH", "train_cutoff_date": "2018-01-24",
            "holdout_cutoff_date": None, "params": SCREEN_LGBM_PARAMS, "random_seed": 42,
        }
        write_auc_report(self._rows(), str(out), meta)
        text = out.read_text(encoding="utf-8")
        assert "CL" in text
        assert "SYNTH" in text
        assert "42" in text  # seed
