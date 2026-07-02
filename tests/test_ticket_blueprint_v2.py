"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/batch_post_optimizer.py, agent/generate_ensemble_artifacts.py
Target Class/Function: generate_optimized_report, resolve_exec_data_path, validate_exec_data
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
"""

import json
import os
import re
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared test constants — mirrors the canonical fixture in test_ensemble_eorder.py
# ---------------------------------------------------------------------------
SWEEP = "sweep_hs14b_2x1_6h_canary_20260701-0703"


def _pk(long_metric, short_metric):
    return (
        f"oos_predictions_{SWEEP}_long_{long_metric}"
        f"|oos_predictions_{SWEEP}_short_{short_metric}"
    )


# Canonical order defined by top_pairs.json
CANONICAL = [
    ("logloss", "logloss"),
    ("logloss", "average_precision"),
    ("average_precision", "logloss"),
    ("average_precision", "average_precision"),
]


def _top_pairs_json():
    """Return the canonical top_pairs list-of-dicts."""
    return [
        {
            "target_long": f"oos_predictions_{SWEEP}_long_{lm}",
            "target_short": f"oos_predictions_{SWEEP}_short_{sm}",
        }
        for lm, sm in CANONICAL
    ]


def _build_all_results(order):
    """Build a minimal all_results dict keyed by pair keys in *insertion* order.

    Each entry has status=OK plus enough structure for generate_optimized_report()
    to render a table row without crashing.
    """
    results = {}
    for i, (lm, sm) in enumerate(order):
        pk = _pk(lm, sm)
        results[pk] = {
            "status": "OK",
            "metrics": {
                "trade_count": 100 + i,
                "buy_trades": 50 + i,
                "sell_trades": 50,
                "profit_factor": 1.5,
                "total_pnl": 10000.0 + i * 1000,
            },
            "optuna_info": {
                "holdout_metrics": {
                    "total_pnl": 5000.0 + i * 500,
                    "trade_count": 40 + i,
                    "buy_trades": 20,
                    "sell_trades": 20 + i,
                },
                "baseline_metrics": {
                    "trade_count": 80,
                    "buy_trades": 40,
                    "sell_trades": 40,
                    "profit_factor": 1.2,
                    "total_pnl": 8000.0,
                },
                "long_params": {"entry_threshold": 0.55},
                "short_params": {"entry_threshold": 0.45},
                "best_trial_number": 42,
                "n_trials": 200,
            },
        }
    return results


def _build_experiment_labels(order):
    """Build task_experiment_labels dict matching the order."""
    labels = {}
    for lm, sm in order:
        pk = _pk(lm, sm)
        labels[pk] = {
            "long_label": f"{SWEEP}_long_{lm}",
            "short_label": f"{SWEEP}_short_{sm}",
        }
    return labels


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: Sort Parity (Fix A)
#
# Purpose: Verify that generate_optimized_report() iterates ensembles in
#          the EXACT order returned by _canonical_pair_order(), NOT the
#          independent get_ensemble_sort_key() natural sort.
#
# Expected to FAIL today because generate_optimized_report() uses its own
# get_ensemble_sort_key() instead of delegating to _canonical_pair_order().
# ═══════════════════════════════════════════════════════════════════════════

class TestSortParity:
    """Ensure batch_post_optimizer's report uses _canonical_pair_order()."""

    def test_ensemble_order_matches_canonical_pair_order(self, tmp_path):
        """Report's ensemble numbering must follow top_pairs.json order.

        Steps:
          1. Write a top_pairs.json to tmp_path (the mock batch_dir).
          2. Build all_results in a SCRAMBLED insertion order.
          3. Call generate_optimized_report().
          4. Parse the markdown table to extract ensemble numbers→pair names.
          5. Assert the iteration order matches _canonical_pair_order() output.
        """
        from agent.generate_ensemble_artifacts import _canonical_pair_order
        from agent.batch_post_optimizer import generate_optimized_report

        batch_dir = str(tmp_path)

        # Write canonical top_pairs.json
        tp_path = os.path.join(batch_dir, "top_pairs.json")
        with open(tp_path, "w") as f:
            json.dump(_top_pairs_json(), f)

        # Scrambled insertion order (reverses canonical)
        scrambled = list(reversed(CANONICAL))
        all_results = _build_all_results(scrambled)
        exp_labels = _build_experiment_labels(scrambled)

        progress = {"manifest": "test_manifest.json", "experiments": []}

        md_output = generate_optimized_report(
            batch_dir=batch_dir,
            progress=progress,
            all_results=all_results,
            ohlcv_path="dummy/path.parquet",
            wall_time_seconds=60.0,
            n_trials=200,
            n_workers=1,
            objective_metric="sharpe",
            task_experiment_labels=exp_labels,
            base_cfg=None,
        )

        # Parse ensemble table rows from markdown — look for lines starting
        # with "| <digit>" that form the ensemble table body.
        table_rows = []
        for line in md_output.split("\n"):
            stripped = line.strip()
            if stripped.startswith("|") and not stripped.startswith("|---") and not stripped.startswith("| #"):
                # Split cells; first cell after leading pipe is the ensemble #
                cells = [c.strip() for c in stripped.split("|") if c.strip()]
                if cells and cells[0].isdigit():
                    table_rows.append(cells)

        assert len(table_rows) == len(CANONICAL), (
            f"Expected {len(CANONICAL)} ensemble rows, got {len(table_rows)}"
        )

        # Extract pair keys from the table row content.  The "Long Model"
        # and "Short Model" columns encode the prediction file keys, but
        # the most reliable approach is to compare the iteration order of
        # the keys directly.
        #
        # Build expected order from _canonical_pair_order:
        expected_order = _canonical_pair_order(all_results, batch_dir)
        expected_keys = [k for k, _ in expected_order]

        # The report assigns ensemble # = idx (1-based) in iteration order,
        # so row with # 1 corresponds to expected_keys[0], etc.
        # We verify the "Experiment" column (cell index 1) matches the
        # experiment label that _canonical_pair_order would produce.
        for row_idx, row in enumerate(table_rows):
            expected_pk = expected_keys[row_idx]
            expected_labels = exp_labels[expected_pk]
            expected_exp_col = f"{expected_labels['long_label']} / {expected_labels['short_label']}"
            actual_exp_col = row[1]  # Experiment column
            assert actual_exp_col == expected_exp_col, (
                f"Ensemble #{row_idx + 1}: expected experiment='{expected_exp_col}', "
                f"got '{actual_exp_col}'. "
                f"generate_optimized_report() is not using _canonical_pair_order()."
            )


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: Fatal Error on Missing exec_data (Fix C)
#
# Purpose: Validate that generate_ensemble_artifacts raises a clear FATAL
#          error when no exec_data path can be resolved from the manifest.
#
# Expected to FAIL today because no such validation exists yet.
# ═══════════════════════════════════════════════════════════════════════════

class TestFatalMissingExecData:
    """Startup schema validation: missing exec_data must be a hard error."""

    def test_missing_exec_data_raises_value_error(self):
        """A manifest with no exec_data path must raise ValueError with
        the exact message pattern specified in the blueprint."""
        # Ghost Import — the Coder will create this function
        from agent.generate_ensemble_artifacts import validate_exec_data

        manifest = {
            "defaults": {
                "local_data_path": "data/processed/ES_5min.parquet",
                "strategy_config": "hourly_ensemble_010.json",
                # NOTE: no local_exec_data key
            },
            "baseline": {
                "execution_workflow": {
                    # NOTE: execution_data_path is explicitly null/missing
                }
            },
        }

        with pytest.raises(ValueError, match=r"FATAL: No exec_data path resolved"):
            validate_exec_data(
                manifest=manifest,
                cli_exec_data="",
                symbol="ES",
            )

    def test_null_exec_data_raises_value_error(self):
        """execution_data_path=None must also trigger the FATAL error."""
        from agent.generate_ensemble_artifacts import validate_exec_data

        manifest = {
            "defaults": {
                "local_data_path": "data/processed/ES_5min.parquet",
                "strategy_config": "hourly_ensemble_010.json",
            },
            "baseline": {
                "execution_workflow": {
                    "execution_data_path": None,
                }
            },
        }

        with pytest.raises(ValueError, match=r"FATAL: No exec_data path resolved"):
            validate_exec_data(
                manifest=manifest,
                cli_exec_data="",
                symbol="ES",
            )

    def test_valid_exec_data_does_not_raise(self):
        """A manifest with a valid exec_data path must pass validation."""
        from agent.generate_ensemble_artifacts import validate_exec_data

        manifest = {
            "defaults": {
                "local_data_path": "data/processed/CL_5min.parquet",
                "local_exec_data": "data/processed/CL_5min_raw.parquet",
                "strategy_config": "hourly_ensemble_010.json",
            },
            "baseline": {
                "execution_workflow": {
                    "execution_data_path": "data/processed/CL_5min_raw.parquet",
                }
            },
        }

        # Should NOT raise
        result = validate_exec_data(
            manifest=manifest,
            cli_exec_data="",
            symbol="CL",
        )
        assert result  # Should return the resolved path (truthy)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: GCS→Local Exec Data Resolution (Fix B)
#
# Purpose: Verify that a GCS URI in the manifest's
#          baseline.execution_workflow.execution_data_path is correctly
#          converted to a local path via the same pattern used in
#          gcp/orchestrator.py L92-107.
#
# Expected to FAIL today because generate_ensemble_artifacts.py currently
# only checks defaults.local_exec_data (L201-203) and has no GCS→local
# resolution logic.
# ═══════════════════════════════════════════════════════════════════════════

class TestGCSExecDataResolution:
    """Exec-data fallback chain with GCS→local path conversion."""

    @patch("agent.generate_ensemble_artifacts.os.path.exists")
    def test_gcs_uri_resolved_to_local_path(self, mock_exists):
        """GCS URI gs://bucket/data.parquet → data/processed/data.parquet.

        The resolution logic should:
          1. Extract the filename from the GCS URI
          2. Build local path = data/processed/<filename>
          3. Return the local path when it exists on disk
        """
        # Ghost Import — the Coder will create/modify this function
        from agent.generate_ensemble_artifacts import resolve_exec_data_path

        manifest = {
            "defaults": {
                "local_data_path": "data/processed/ES_5min.parquet",
                # No local_exec_data — forces fallback to baseline key
            },
            "baseline": {
                "execution_workflow": {
                    "execution_data_path": "gs://my-bucket/ES_5min_raw.parquet",
                }
            },
        }

        # The derived local path for gs://my-bucket/ES_5min_raw.parquet
        # should be <data_root>/processed/ES_5min_raw.parquet
        # We mock os.path.exists to return True for the expected local path
        expected_local = str(
            Path("data") / "processed" / "ES_5min_raw.parquet"
        )

        def side_effect(path):
            # Normalize for comparison
            return Path(path).name == "ES_5min_raw.parquet"

        mock_exists.side_effect = side_effect

        result = resolve_exec_data_path(
            manifest=manifest,
            cli_exec_data="",
        )

        # The returned path must end with the expected filename
        assert result is not None, "resolve_exec_data_path returned None"
        assert Path(result).name == "ES_5min_raw.parquet", (
            f"Expected filename 'ES_5min_raw.parquet', got '{Path(result).name}'"
        )

    @patch("agent.generate_ensemble_artifacts.os.path.exists")
    def test_gcs_uri_local_path_not_found_raises_error(self, mock_exists):
        """When the derived local path does NOT exist on disk, raise
        FileNotFoundError with a descriptive message."""
        from agent.generate_ensemble_artifacts import resolve_exec_data_path

        manifest = {
            "defaults": {},
            "baseline": {
                "execution_workflow": {
                    "execution_data_path": "gs://my-bucket/ES_5min_raw.parquet",
                }
            },
        }

        # os.path.exists returns False for the derived local path
        mock_exists.return_value = False

        with pytest.raises(
            FileNotFoundError,
            match=r"Resolved local exec_data path .+ does not exist\. Please download the dataset first\.",
        ):
            resolve_exec_data_path(
                manifest=manifest,
                cli_exec_data="",
            )

    def test_cli_exec_data_takes_precedence_over_manifest(self):
        """If --exec-data is provided via CLI, it should be used directly
        without consulting the manifest."""
        from agent.generate_ensemble_artifacts import resolve_exec_data_path

        manifest = {
            "defaults": {
                "local_exec_data": "data/processed/from_manifest.parquet",
            },
            "baseline": {
                "execution_workflow": {
                    "execution_data_path": "gs://bucket/from_gcs.parquet",
                }
            },
        }

        result = resolve_exec_data_path(
            manifest=manifest,
            cli_exec_data="data/processed/from_cli.parquet",
        )

        assert result == "data/processed/from_cli.parquet", (
            f"CLI exec_data should take precedence; got '{result}'"
        )

    def test_defaults_local_exec_data_used_before_gcs_fallback(self):
        """The fallback chain is: CLI → defaults.local_exec_data →
        baseline.execution_workflow.execution_data_path (GCS convert).
        When defaults.local_exec_data is present, it should be returned
        without touching the GCS path."""
        from agent.generate_ensemble_artifacts import resolve_exec_data_path

        manifest = {
            "defaults": {
                "local_exec_data": "data/processed/CL_5min_raw.parquet",
            },
            "baseline": {
                "execution_workflow": {
                    "execution_data_path": "gs://bucket/CL_5min_raw.parquet",
                }
            },
        }

        result = resolve_exec_data_path(
            manifest=manifest,
            cli_exec_data="",
        )

        assert result == "data/processed/CL_5min_raw.parquet", (
            f"defaults.local_exec_data should be used before GCS fallback; got '{result}'"
        )
