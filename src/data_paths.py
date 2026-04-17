"""
Centralised path resolution for data and model files.

Priority order:
    1. CL_DATA_ROOT  (shared across worktrees, set in .env)
    2. Repo-local    (PROJECT_ROOT / data  or  PROJECT_ROOT / models)

Every module that needs a data or model path should import helpers from
here instead of computing paths independently.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve project root (parent of src/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env so CL_DATA_ROOT is available even for modules imported early.
load_dotenv(PROJECT_ROOT / ".env")

_CL_DATA_ROOT = os.environ.get("CL_DATA_ROOT", "")


# ---------------------------------------------------------------------------
# Root getters
# ---------------------------------------------------------------------------

def get_data_root() -> Path:
    """Return the primary data root directory.

    Enforces that CL_DATA_ROOT must be set to prevent split-brain
    worktrees creating isolated databases.
    """
    if not _CL_DATA_ROOT:
        raise RuntimeError(
            "CRITICAL: CL_DATA_ROOT is missing from .env or environment variables. "
            "Shared database fallback to project root is disabled to prevent "
            "split-brain data paths between worktrees. Please define CL_DATA_ROOT."
        )
    candidate = Path(_CL_DATA_ROOT) / "data"
    if candidate.is_dir():
        return candidate
    return candidate


def get_models_root() -> Path:
    """Return the primary models root directory."""
    if not _CL_DATA_ROOT:
        raise RuntimeError("CRITICAL: CL_DATA_ROOT is missing. Shared model fallback is disabled.")
    candidate = Path(_CL_DATA_ROOT) / "models"
    if candidate.is_dir():
        return candidate
    return candidate


# ---------------------------------------------------------------------------
# File-level getters (resolve with fallback)
# ---------------------------------------------------------------------------

def get_data_path(relative: str) -> Path:
    """Resolve a path under the data root."""
    if not _CL_DATA_ROOT:
        raise RuntimeError(f"CRITICAL: CL_DATA_ROOT is missing. Cannot resolve shared data path for {relative}.")
    primary = Path(_CL_DATA_ROOT) / "data" / relative
    if primary.exists():
        return primary
    local = PROJECT_ROOT / "data" / relative
    if local.exists():
        return local
    return primary


def get_model_path(relative: str) -> Path:
    """Resolve a path under the models root."""
    if not _CL_DATA_ROOT:
        raise RuntimeError(f"CRITICAL: CL_DATA_ROOT is missing. Cannot resolve shared model path for {relative}.")
    primary = Path(_CL_DATA_ROOT) / "models" / relative
    if primary.exists():
        return primary
    local = PROJECT_ROOT / "models" / relative
    if local.exists():
        return local
    return primary


def get_reports_root() -> Path:
    """Return the primary reports root directory.

    Prefers ``CL_DATA_ROOT/reports`` when CL_DATA_ROOT is set **and** the
    directory exists, otherwise falls back to ``PROJECT_ROOT/reports``.
    """
    if _CL_DATA_ROOT:
        candidate = Path(_CL_DATA_ROOT) / "reports"
        if candidate.is_dir():
            return candidate
    return PROJECT_ROOT / "reports"


def resolve_cli_path(path_str: str) -> str:
    """Resolve a CLI-provided path using CL_DATA_ROOT fallback.

    Designed to be called on argparse values *after* parsing.  Logic:

    1. If *path_str* already points to an existing file/dir, return as-is.
    2. If it starts with ``data/`` or ``data\\``, delegate to
       :func:`get_data_path`.
    3. If it starts with ``models/`` or ``models\\``, delegate to
       :func:`get_model_path`.
    4. If it starts with ``reports/`` or ``reports\\``, try
       ``CL_DATA_ROOT/reports/…`` then ``PROJECT_ROOT/reports/…``.
    5. Otherwise return the original string unchanged.
    """
    p = Path(path_str)
    if p.exists():
        return path_str

    # Normalise to forward-slash for prefix checks
    norm = path_str.replace("\\", "/")

    if norm.startswith("data/"):
        relative = norm[len("data/"):]
        return str(get_data_path(relative))

    if norm.startswith("models/"):
        relative = norm[len("models/"):]
        return str(get_model_path(relative))

    if norm.startswith("reports/"):
        relative = norm[len("reports/"):]
        if _CL_DATA_ROOT:
            candidate = Path(_CL_DATA_ROOT) / "reports" / relative
            if candidate.exists():
                return str(candidate)
        local = PROJECT_ROOT / "reports" / relative
        if local.exists():
            return str(local)
        # Neither exists — prefer shared root when configured
        if _CL_DATA_ROOT:
            return str(Path(_CL_DATA_ROOT) / "reports" / relative)
        return str(local)

    return path_str


# ---------------------------------------------------------------------------
# Mirror helper
# ---------------------------------------------------------------------------

def mirror_file(src_path: Path, relative: str | None = None) -> None:
    """Copy *src_path* to the **other** data location.

    If *relative* is ``None`` the function tries to infer it by checking
    whether *src_path* falls under the shared data root or the repo-local
    data root.

    If CL_DATA_ROOT is set the file came from the shared root, so mirror
    it into the repo-local ``data/`` tree (and vice-versa).  This keeps
    both locations in sync for modules that write outputs.
    """
    import shutil

    if not src_path.exists():
        return
    if not _CL_DATA_ROOT:
        return  # nothing to mirror when CL_DATA_ROOT is unset

    shared_root = Path(_CL_DATA_ROOT) / "data"
    local_root = PROJECT_ROOT / "data"

    # Determine the relative path and the destination root
    if relative is not None:
        # Caller supplied an explicit relative path
        shared_dest = shared_root / relative
        local_dest = local_root / relative
        if str(src_path) == str(shared_dest):
            dest = local_dest
        else:
            dest = shared_dest
    else:
        # Auto-detect which root the source belongs to
        try:
            rel = src_path.relative_to(shared_root)
            dest = local_root / rel
        except ValueError:
            try:
                rel = src_path.relative_to(local_root)
                dest = shared_root / rel
            except ValueError:
                return  # source not under either root; nothing to mirror

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_path), str(dest))
    except OSError:
        pass
