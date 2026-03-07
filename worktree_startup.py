"""
Worktree Startup — Initialize / recreate all data files for CL_Analyst.

Run this once after creating a new worktree (or anytime you want to
re-bootstrap the data layer).  It creates the directory structure,
symlinks large shared data files from CL_DATA_ROOT, and validates
that every component (backtest_engine, live_trader, etc.) can find
what it needs.

Usage:
    python worktree_startup.py              # normal run
    python worktree_startup.py --force      # recreate all symlinks
    python worktree_startup.py --copy       # copy files instead of symlinks
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
LOGS_DIR = DATA_DIR / "logs"
MODELS_DIR = PROJECT_ROOT / "models"

# Files to symlink from CL_DATA_ROOT/raw/
RAW_FILES = [
    "cl-5m_bk.csv",
    "cl-5m_bk_old.csv",
    "ng-5m_bk.csv",
    "test.csv",
    "test10k.csv",
    "test10k2.csv",
    "test100k.csv",
    "SPY.csv",
    "SPY_Old.csv",
    "JPM.csv",
]

# Files to symlink from CL_DATA_ROOT/processed/
PROCESSED_FILES = [
    "CL_set_01.csv",
    "CL_set_01.parquet",
    "CL_set_04.parquet",
    "CL_set_05.parquet",
    "CL_set_06.parquet",
    "CL_set_06_shortfix.parquet",
    "cl_continuous_master.parquet",
    "test100k_set_01.csv",
    "test100k_set_01.parquet",
    "test100k_set_02.parquet",
    "test100k_set_03.parquet",
    "test_data_set_03.parquet",
    "warm_start_cache.parquet",
]

# Telemetry databases to create empty if missing
TELEMETRY_DBS = [
    DATA_DIR / "live_telemetry.db",
]

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("worktree_startup")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_data_root() -> Path | None:
    """Read CL_DATA_ROOT from .env or environment."""
    # Check environment first
    root = os.environ.get("CL_DATA_ROOT", "")
    if root:
        return Path(root)

    # Fall back to .env file
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "CL_DATA_ROOT":
                value = value.strip().strip('"').strip("'")
                if value:
                    os.environ["CL_DATA_ROOT"] = value
                    return Path(value)
    return None


def _create_link(source: Path, target: Path, *, force: bool, copy: bool) -> bool:
    """
    Create a symlink (or copy) from *target* pointing to *source*.

    On Windows, uses directory junctions for dirs and hard/symlinks for files.
    Falls back to copy if symlink creation fails (e.g. no admin / dev mode).

    Returns True on success.
    """
    if not source.exists():
        log.warning("  SOURCE MISSING: %s", source)
        return False

    if target.exists() or target.is_symlink():
        if force:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        else:
            log.info("  EXISTS (skip): %s", target.name)
            return True

    target.parent.mkdir(parents=True, exist_ok=True)

    if copy:
        if source.is_dir():
            shutil.copytree(str(source), str(target))
        else:
            shutil.copy2(str(source), str(target))
        log.info("  COPIED: %s", target.name)
        return True

    # Try symlink, fall back to copy
    try:
        if source.is_dir():
            # Windows: use junction for directories (no admin needed)
            if sys.platform == "win32":
                import subprocess
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                    check=True, capture_output=True,
                )
            else:
                target.symlink_to(source)
        else:
            target.symlink_to(source)
        log.info("  LINKED: %s -> %s", target.name, source)
        return True
    except OSError as exc:
        log.warning("  Symlink failed (%s), falling back to copy...", exc)
        if source.is_dir():
            shutil.copytree(str(source), str(target))
        else:
            shutil.copy2(str(source), str(target))
        log.info("  COPIED (fallback): %s", target.name)
        return True


def _create_empty_db(db_path: Path) -> bool:
    """Create an empty SQLite database if it doesn't exist."""
    if db_path.exists():
        log.info("  EXISTS (skip): %s", db_path.name)
        return True
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.close()
        log.info("  CREATED: %s", db_path.name)
        return True
    except Exception as exc:
        log.error("  FAILED to create %s: %s", db_path.name, exc)
        return False


# ---------------------------------------------------------------------------
# Main initialization
# ---------------------------------------------------------------------------

def run(*, force: bool = False, copy: bool = False) -> dict:
    """
    Run the full worktree initialization.

    Returns a summary dict with status counts.
    """
    summary = {"ok": 0, "skip": 0, "fail": 0, "details": []}

    # ── 0. Resolve CL_DATA_ROOT ──────────────────────────────────────────
    data_root = _load_data_root()
    if data_root is None or not data_root.exists():
        log.error(
            "CL_DATA_ROOT is not set or does not exist.\n"
            "  Set CL_DATA_ROOT in your .env file or environment.\n"
            "  Expected: C:\\CL_Analyst_Data"
        )
        summary["fail"] += 1
        summary["details"].append(("CL_DATA_ROOT", "[FAIL] NOT SET"))
        return summary

    log.info("CL_DATA_ROOT = %s", data_root)
    summary["details"].append(("CL_DATA_ROOT", f"[OK] {data_root}"))

    # ── 1. Create directory structure ─────────────────────────────────────
    log.info("\n=== Creating directory structure ===")
    for d in [RAW_DIR, PROCESSED_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
        log.info("  DIR: %s", d.relative_to(PROJECT_ROOT))

    # ── 2. Link raw data files ────────────────────────────────────────────
    log.info("\n=== Linking raw data files ===")
    for fname in RAW_FILES:
        source = data_root / "raw" / fname
        target = RAW_DIR / fname
        ok = _create_link(source, target, force=force, copy=copy)
        status = "[OK]" if ok else "[FAIL]"
        summary["ok" if ok else "fail"] += 1
        summary["details"].append((f"raw/{fname}", status))

    # ── 3. Link processed data files ──────────────────────────────────────
    log.info("\n=== Linking processed data files ===")
    for fname in PROCESSED_FILES:
        source = data_root / "processed" / fname
        target = PROCESSED_DIR / fname
        ok = _create_link(source, target, force=force, copy=copy)
        status = "[OK]" if ok else "[FAIL]"
        summary["ok" if ok else "fail"] += 1
        summary["details"].append((f"processed/{fname}", status))

    # ── 4. Ensure .env exists ─────────────────────────────────────────────
    log.info("\n=== Ensuring .env ===")
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        env_path.write_text(
            f"CL_DATA_ROOT={data_root}\n", encoding="utf-8"
        )
        log.info("  CREATED: .env")
        summary["details"].append((".env", "[OK] created"))
    else:
        log.info("  EXISTS: .env")
        summary["details"].append((".env", "[OK] exists"))
    summary["ok"] += 1

    # ── 5. Create empty telemetry databases ───────────────────────────────
    log.info("\n=== Creating telemetry databases ===")
    for db_path in TELEMETRY_DBS:
        ok = _create_empty_db(db_path)
        status = "[OK]" if ok else "[FAIL]"
        summary["ok" if ok else "fail"] += 1
        summary["details"].append((str(db_path.relative_to(PROJECT_ROOT)), status))

    # ── 6. Validate critical imports ──────────────────────────────────────
    log.info("\n=== Validating imports ===")
    sys.path.insert(0, str(PROJECT_ROOT))

    import_checks = [
        ("src.util", "Core utilities"),
        ("src.data_processor", "DataProcessor"),
        ("src.LGBMLearner", "LGBMLearner"),
        ("src.live_execution.data_manager", "DataManager"),
        ("src.live_execution.strategies.execution_models", "Execution strategies"),
        ("agent.backtest_engine", "BacktestEngine"),
    ]

    for module_name, label in import_checks:
        try:
            __import__(module_name)
            log.info("  [OK] import %s  (%s)", module_name, label)
            summary["ok"] += 1
            summary["details"].append((f"import {module_name}", "[OK]"))
        except Exception as exc:
            log.error("  [FAIL] import %s  FAILED: %s", module_name, exc)
            summary["fail"] += 1
            summary["details"].append((f"import {module_name}", f"[FAIL] {exc}"))

    # ── 7. Validate data files are loadable ───────────────────────────────
    log.info("\n=== Validating data files ===")
    try:
        import pandas as pd

        # Check seed CSV is readable
        seed = RAW_DIR / "cl-5m_bk.csv"
        if seed.exists():
            sample = pd.read_csv(seed, sep=";", header=None, nrows=5)
            assert sample.shape[1] == 7, f"Expected 7 columns, got {sample.shape[1]}"
            log.info("  [OK] Seed CSV readable (%d cols)", sample.shape[1])
            summary["ok"] += 1
            summary["details"].append(("seed CSV readable", "[OK]"))
        else:
            log.warning("  [FAIL] Seed CSV not found (skipped)")
            summary["fail"] += 1
            summary["details"].append(("seed CSV readable", "[FAIL] not found"))

        # Check a processed parquet (read first column only for speed)
        pqt = PROCESSED_DIR / "CL_set_06.parquet"
        if pqt.exists():
            import pyarrow.parquet as _pq
            schema = _pq.read_schema(str(pqt))
            first_col = schema.names[0]
            meta = pd.read_parquet(pqt, engine="pyarrow", columns=[first_col])
            log.info("  [OK] CL_set_06.parquet readable (%d rows, %d cols in schema)", len(meta), len(schema))
            summary["ok"] += 1
            summary["details"].append(("CL_set_06.parquet readable", "[OK]"))
        else:
            log.warning("  [FAIL] CL_set_06.parquet not found (skipped)")
            summary["details"].append(("CL_set_06.parquet readable", "[FAIL] not found"))

    except Exception as exc:
        log.error("  Validation error: %s", exc)
        summary["fail"] += 1

    return summary


def _print_report(summary: dict) -> None:
    """Print a formatted summary report."""
    print("\n" + "=" * 60)
    print("  WORKTREE STARTUP — INITIALIZATION REPORT")
    print("=" * 60)
    print(f"\n  Project Root: {PROJECT_ROOT}")
    print(f"  Total OK:     {summary['ok']}")
    print(f"  Total Fail:   {summary['fail']}")
    print()

    for item, status in summary["details"]:
        print(f"  {status:8s}  {item}")

    print()
    if summary["fail"] == 0:
        print("  [PASS]  All checks passed -- worktree is ready!")
    else:
        print(f"  [WARN]  {summary['fail']} issue(s) found -- see above.")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Initialize a CL_Analyst worktree with all required data files."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Recreate all symlinks even if they already exist."
    )
    parser.add_argument(
        "--copy", action="store_true",
        help="Copy files instead of creating symlinks."
    )
    args = parser.parse_args()

    summary = run(force=args.force, copy=args.copy)
    _print_report(summary)

    sys.exit(1 if summary["fail"] > 0 else 0)
