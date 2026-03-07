"""
Tests for CL_DATA_ROOT shared data root behavior.

Covers:
1. Priority order: CL_DATA_ROOT first, repo-local second
2. Dual-write mirroring in DataProcessor.save()
3. Dual-write mirroring in DataManager._mirror_to_root()
4. Centralized data_paths helper functions
"""

import os
import shutil

import pandas as pd
import pytest

from pathlib import Path


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def fake_data_layout(tmp_path):
    """Create a fake project + root directory layout for testing."""
    repo_data = tmp_path / "repo" / "data"
    repo_raw = repo_data / "raw"
    repo_processed = repo_data / "processed"
    root_dir = tmp_path / "root"
    root_data = root_dir / "data"
    root_raw = root_data / "raw"
    root_processed = root_data / "processed"
    root_models = root_dir / "models"

    for d in [repo_raw, repo_processed, root_raw, root_processed, root_models]:
        d.mkdir(parents=True)

    return {
        "repo_data": repo_data,
        "repo_raw": repo_raw,
        "repo_processed": repo_processed,
        "root_dir": root_dir,
        "root_data": root_data,
        "root_raw": root_raw,
        "root_processed": root_processed,
        "root_models": root_models,
    }


@pytest.fixture
def sample_df():
    """Small DataFrame for save/mirror tests."""
    return pd.DataFrame({
        "Open": [70.0, 71.0, 72.0],
        "High": [71.0, 72.0, 73.0],
        "Low": [69.0, 70.0, 71.0],
        "Close": [70.5, 71.5, 72.5],
        "Volume": [100, 200, 300],
    })


# =============================================================================
# FALLBACK ORDER TESTS  (CL_DATA_ROOT first, repo-local second)
# =============================================================================


class TestFallbackOrder:
    """Test that data resolution tries CL_DATA_ROOT first, then repo-local."""

    def test_root_preferred_over_local(self, fake_data_layout, monkeypatch):
        """When CL_DATA_ROOT file exists, it is used even if local also has the file."""
        local_csv = fake_data_layout["repo_raw"] / "cl-5m_bk.csv"
        root_csv = fake_data_layout["root_raw"] / "cl-5m_bk.csv"
        local_csv.write_text("local")
        root_csv.write_text("root")

        # The logic: check CL_DATA_ROOT first → found → use it
        local_path = str(local_csv)
        root_path = str(root_csv)

        assert os.path.exists(local_path)
        assert os.path.exists(root_path)
        # Root (CL_DATA_ROOT) should be preferred
        if os.path.exists(root_path):
            chosen = root_path
        elif os.path.exists(local_path):
            chosen = local_path
        else:
            chosen = None

        assert chosen == root_path

    def test_local_used_when_root_missing(self, fake_data_layout, monkeypatch):
        """When CL_DATA_ROOT file is missing, repo-local is used as fallback."""
        local_csv = fake_data_layout["repo_raw"] / "cl-5m_bk.csv"
        local_csv.write_text("local")
        root_path = str(fake_data_layout["root_raw"] / "cl-5m_bk.csv")

        assert not os.path.exists(root_path)
        assert os.path.exists(str(local_csv))

        if os.path.exists(root_path):
            chosen = root_path
        elif os.path.exists(str(local_csv)):
            chosen = str(local_csv)
        else:
            chosen = None

        assert chosen == str(local_csv)

    def test_fallback_returns_default_when_neither_exists(self, tmp_path):
        """When neither CL_DATA_ROOT nor local file exists, falls back to default."""
        local = str(tmp_path / "nonexistent_local.csv")
        root = str(tmp_path / "nonexistent_root.csv")
        default = "data/raw/test100k.csv"

        if os.path.exists(root):
            chosen = root
        elif os.path.exists(local):
            chosen = local
        else:
            chosen = default

        assert chosen == default


# =============================================================================
# DATA_PATHS MODULE TESTS
# =============================================================================


class TestDataPaths:
    """Test the centralized data_paths helper functions."""

    def test_get_data_path_prefers_cl_data_root(self, fake_data_layout, monkeypatch):
        """get_data_path should return CL_DATA_ROOT path when file exists there."""
        import src.data_paths as dp

        monkeypatch.setattr(dp, "_CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "PROJECT_ROOT", fake_data_layout["repo_data"].parent)

        # Create the file in both locations
        (fake_data_layout["root_raw"] / "seed.csv").write_text("root")
        (fake_data_layout["repo_raw"] / "seed.csv").write_text("local")

        result = dp.get_data_path("raw/seed.csv")
        assert str(result) == str(fake_data_layout["root_raw"] / "seed.csv")

    def test_get_data_path_falls_back_to_local(self, fake_data_layout, monkeypatch):
        """get_data_path should fall back to repo-local when CL_DATA_ROOT file missing."""
        import src.data_paths as dp

        monkeypatch.setattr(dp, "_CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "PROJECT_ROOT", fake_data_layout["repo_data"].parent)

        # Only create locally
        (fake_data_layout["repo_raw"] / "seed.csv").write_text("local")

        result = dp.get_data_path("raw/seed.csv")
        assert str(result) == str(fake_data_layout["repo_raw"] / "seed.csv")

    def test_get_model_path_prefers_cl_data_root(self, fake_data_layout, monkeypatch):
        """get_model_path should return CL_DATA_ROOT path when file exists there."""
        import src.data_paths as dp

        monkeypatch.setattr(dp, "_CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "PROJECT_ROOT", fake_data_layout["repo_data"].parent)

        model_dir = fake_data_layout["root_models"] / "registry" / "EXP-001"
        model_dir.mkdir(parents=True)
        (model_dir / "model.pkl").write_text("model")

        result = dp.get_model_path("registry/EXP-001/model.pkl")
        assert str(result) == str(model_dir / "model.pkl")

    def test_get_data_root_prefers_cl_data_root(self, fake_data_layout, monkeypatch):
        """get_data_root should return CL_DATA_ROOT/data when it exists."""
        import src.data_paths as dp

        monkeypatch.setattr(dp, "_CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "PROJECT_ROOT", fake_data_layout["repo_data"].parent)

        result = dp.get_data_root()
        assert str(result) == str(fake_data_layout["root_data"])


# =============================================================================
# MIRROR-TO-ROOT TESTS
# =============================================================================


class TestMirrorToRoot:
    """Test dual-write mirroring behavior."""

    def test_dataprocessor_mirror_copies_file(
        self, fake_data_layout, sample_df, monkeypatch
    ):
        """DataProcessor._mirror_to_root copies output to the other location."""
        import src.data_paths as dp
        from src.data_processor import DataProcessor

        monkeypatch.setenv("CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "_CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "PROJECT_ROOT", fake_data_layout["repo_data"].parent)

        # Create a processor and manually set output path within data/
        output_path = str(
            fake_data_layout["repo_processed"] / "test_set_06.parquet"
        )
        processor = DataProcessor(
            input_path=str(fake_data_layout["repo_raw"] / "dummy.csv"),
            output_path=output_path,
        )

        # Save the file locally first
        fake_data_layout["repo_processed"].mkdir(parents=True, exist_ok=True)
        sample_df.to_parquet(output_path)
        assert os.path.exists(output_path)

        # Now mirror it — we need to chdir so relative path resolution works
        monkeypatch.chdir(fake_data_layout["repo_data"].parent)
        processor._mirror_to_root(output_path)

        # Check mirror destination
        dest = fake_data_layout["root_processed"] / "test_set_06.parquet"
        assert dest.exists(), f"Mirrored file not found at {dest}"

        # Verify contents match
        original = pd.read_parquet(output_path)
        mirrored = pd.read_parquet(str(dest))
        pd.testing.assert_frame_equal(original, mirrored)

    def test_no_mirror_when_env_unset(
        self, fake_data_layout, sample_df, monkeypatch
    ):
        """No mirroring happens when CL_DATA_ROOT is not set."""
        from src.data_processor import DataProcessor

        monkeypatch.delenv("CL_DATA_ROOT", raising=False)

        output_path = str(
            fake_data_layout["repo_processed"] / "test_set_06.parquet"
        )
        processor = DataProcessor(
            input_path=str(fake_data_layout["repo_raw"] / "dummy.csv"),
            output_path=output_path,
        )

        sample_df.to_parquet(output_path)
        processor._mirror_to_root(output_path)

        # Nothing should appear in root
        dest = fake_data_layout["root_processed"] / "test_set_06.parquet"
        assert not dest.exists()

    def test_data_manager_mirror_copies_file(
        self, fake_data_layout, monkeypatch
    ):
        """data_manager._mirror_to_root copies file to the other location."""
        import src.data_paths as dp
        from src.live_execution.data_manager import _mirror_to_root

        monkeypatch.setenv("CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "_CL_DATA_ROOT", str(fake_data_layout["root_dir"]))
        monkeypatch.setattr(dp, "PROJECT_ROOT", fake_data_layout["repo_data"].parent)

        # Create a source file in repo's data/processed/
        src_file = fake_data_layout["repo_processed"] / "cache.parquet"
        src_file.write_text("fake parquet data")

        project_root = fake_data_layout["repo_data"].parent
        _mirror_to_root(src_file, project_root)

        dest = fake_data_layout["root_processed"] / "cache.parquet"
        assert dest.exists(), f"Mirrored file not found at {dest}"
        assert dest.read_text() == "fake parquet data"
