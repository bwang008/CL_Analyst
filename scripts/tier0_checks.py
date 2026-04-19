"""
Tier-0 clone-to-run checks for live_trader readiness.

These checks are intentionally lightweight and do not depend on prior
telemetry or IBKR connectivity. They validate:
  - .env + CL_DATA_ROOT presence
  - Required seed and macro data files
  - Strategy config and model paths
  - Importing the live trader module
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _ok(msg: str) -> None:
    print(f"[OK] {msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tier-0 live_trader checks")
    parser.add_argument(
        "--config",
        default="configs/strategies/hourly_ensemble_004.json",
        help="Strategy config path to validate",
    )
    args = parser.parse_args()

    cl_data_root = os.environ.get("CL_DATA_ROOT", "")
    if not cl_data_root:
        _fail("CL_DATA_ROOT not set. Copy .env.example -> .env and set it.")
    cl_data_root_path = Path(cl_data_root)
    if not cl_data_root_path.exists():
        _fail(f"CL_DATA_ROOT does not exist: {cl_data_root_path}")
    _ok(f"CL_DATA_ROOT={cl_data_root_path}")

    seed_path = cl_data_root_path / "data" / "raw" / "cl-5m_bk.csv"
    if not seed_path.exists():
        _fail(f"Missing seed CSV: {seed_path}")
    _ok("Seed CSV present")

    fred_path = cl_data_root_path / "data" / "raw" / "macro" / "fred_macro_data.csv"
    cot_path = cl_data_root_path / "data" / "raw" / "macro" / "cftc_cot_crude_oil.csv"
    if not fred_path.exists():
        _warn(f"Missing FRED macro CSV: {fred_path}")
    else:
        _ok("FRED macro CSV present")
    if not cot_path.exists():
        _warn(f"Missing COT macro CSV: {cot_path}")
    else:
        _ok("COT macro CSV present")

    config_path = PROJECT_ROOT / args.config
    if not config_path.exists():
        _fail(f"Strategy config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    _ok(f"Loaded config: {config.get('nickname', config_path.name)}")

    for side in ("long", "short"):
        model_path = config.get("models", {}).get(side, {}).get("model_path")
        if not model_path:
            continue
        model_file = PROJECT_ROOT / model_path
        if not model_file.exists():
            _fail(f"Missing model file for {side}: {model_file}")
        _ok(f"Model present ({side})")

    try:
        import src.live_execution.live_trader  # noqa: F401
    except Exception as exc:
        _fail(f"live_trader import failed: {exc}")
    _ok("live_trader imports cleanly")

    print("Tier-0 checks completed.")


if __name__ == "__main__":
    main()
