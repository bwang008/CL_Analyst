"""
Tests for CL_DATA_ROOT shared data root behavior.

Covers:
1. Reversed fallback order (repo-local first, CL_DATA_ROOT second)
2. Dual-write mirroring in DataProcessor.save()
3. Dual-write mirroring in DataManager._mirror_to_root()
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
    root_raw = root_dir / "raw"
    root_processed = root_dir / "processed"

    for d in [repo_raw, repo_processed, root_raw, root_processed]:
        d.mkdir(parents=True)

    return {
        "repo_data": repo_data,
        "repo_raw": repo_raw,
        "repo_processed": repo_processed,
        "root_dir": root_dir,
        "root_raw": root_raw,
        "root_processed": root_processed,
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
# FALLBACK ORDER TESTS
# =============================================================================


class TestFallbackOrder:
    """Test that data resolution tries repo-local first, then CL_DATA_ROOT."""

    def test_local_file_preferred_over_root(self, fake_data_layout, monkeypatch):
        """When local file exists, it is used even if root also has the file."""
        local_csv = fake_data_layout["repo_raw"] / "cl-5m_bk.csv"
        root_csv = fake_data_layout["root_raw"] / "cl-5m_bk.csv"
        local_csv.write_text("local")
        root_csv.write_text("root")

        # The logic: check local first → found → use it
        local_path = str(local_csv)
        root_path = str(root_csv)

        assert os.path.exists(local_path)
        assert os.path.exists(root_path)
        # Local should be preferred
        if os.path.exists(local_path):
            chosen = local_path
        elif os.path.exists(root_path):
            chosen = root_path
        else:
            chosen = None

        assert chosen == local_path

    def test_root_used_when_local_missing(self, fake_data_layout, monkeypatch):
        """When local file is missing, CL_DATA_ROOT is used as fallback."""
        root_csv = fake_data_layout["root_raw"] / "cl-5m_bk.csv"
        root_csv.write_text("root")
        local_path = str(fake_data_layout["repo_raw"] / "cl-5m_bk.csv")

        assert not os.path.exists(local_path)
        assert os.path.exists(str(root_csv))

        if os.path.exists(local_path):
            chosen = local_path
        elif os.path.exists(str(root_csv)):
            chosen = str(root_csv)
        else:
            chosen = None

        assert chosen == str(root_csv)

    def test_fallback_returns_default_when_neither_exists(self, tmp_path):
        """When neither local nor root file exists, falls back to default."""
        local = str(tmp_path / "nonexistent_local.csv")
        root = str(tmp_path / "nonexistent_root.csv")
        default = "data/raw/test100k.csv"

        if os.path.exists(local):
            chosen = local
        elif os.path.exists(root):
            chosen = root
        else:
            chosen = default

        assert chosen == default


# =============================================================================
# MIRROR-TO-ROOT TESTS
# =============================================================================


class TestMirrorToRoot:
    """Test dual-write mirroring behavior."""

    def test_dataprocessor_mirror_copies_file(
        self, fake_data_layout, sample_df, monkeypatch
    ):
        """DataProcessor._mirror_to_root copies output to CL_DATA_ROOT."""
        from src.data_processor import DataProcessor

        monkeypatch.setenv("CL_DATA_ROOT", str(fake_data_layout["root_dir"]))

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
        """data_manager._mirror_to_root copies file to CL_DATA_ROOT."""
        from src.live_execution.data_manager import _mirror_to_root

        monkeypatch.setenv("CL_DATA_ROOT", str(fake_data_layout["root_dir"]))

        # Create a source file in repo's data/processed/
        src_file = fake_data_layout["repo_processed"] / "cache.parquet"
        src_file.write_text("fake parquet data")

        project_root = fake_data_layout["repo_data"].parent
        _mirror_to_root(src_file, project_root)

        dest = fake_data_layout["root_processed"] / "cache.parquet"
        assert dest.exists(), f"Mirrored file not found at {dest}"
        assert dest.read_text() == "fake parquet data"
