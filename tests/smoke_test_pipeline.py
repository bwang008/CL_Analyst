"""
Daily Smoke Test Pipeline — System Health Validator with Telegram Alerts.

Validates the live trading system health across four dimensions:
    Stage 1: Database freshness + feature variance (no flatlining)
    Stage 2: Model artifact existence (final_model.pkl, final_model_pure.txt)
    Stage 3: Train-serve parity (playback inference vs OOS ground truth)
    Stage 4: Feature generation latency (<1s per bar)

Sends a Markdown summary to Telegram at the end of each run.

Usage:
    # Local (prints to console only):
    python tests/smoke_test_pipeline.py

    # With Telegram notifications:
    python tests/smoke_test_pipeline.py --telegram

    # Custom strategy config:
    python tests/smoke_test_pipeline.py --config configs/strategies/4h_ensemble_001.json

Crontab (run daily at 08:00 UTC on the VPS):
    0 8 * * * /opt/cl-trader/venv/bin/python /opt/cl-trader/app/tests/smoke_test_pipeline.py --telegram 2>&1 | tee -a /opt/cl-trader/logs/smoke_test.log

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Force UTF-8 stdout — prevents UnicodeEncodeError when conda run re-prints
# emoji output on Windows (cp1252 cannot encode ✅ ❌ etc).
# PYTHONUTF8=1 env var is the preferred external fix; this is the in-process guard.
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add project root to sys.path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv
load_dotenv(_project_root / ".env")

from src.data_paths import get_data_path, get_reports_root
from src.live_execution.live_trader import build_live_features
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy


# ---------------------------------------------------------------------------
# Report Helpers
# ---------------------------------------------------------------------------

_results: list[dict] = []


def _record(stage: str, status: str, detail: str = "") -> None:
    """Record a stage result for the final summary."""
    _results.append({"stage": stage, "status": status, "detail": detail})
    icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
    
    # Gracefully handle Windows console emoji printing issues
    try:
        print(f"  {icon} [{stage}] {status}: {detail}")
    except UnicodeEncodeError:
        ascii_icon = "[OK]" if status == "PASS" else "[X]" if status == "FAIL" else "[!]"
        print(f"  {ascii_icon} [{stage}] {status}: {detail}")


def log_report(msg: str) -> None:
    """Append a line to the persistent HEALTH_REPORT.txt."""
    try:
        reports_dir = get_reports_root()
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / "HEALTH_REPORT.txt"
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass  # non-critical


# ---------------------------------------------------------------------------
# Stage 1: Database Freshness & Feature Variance
# ---------------------------------------------------------------------------

def stage_1_database_integrity(strategy_config_path: Path) -> bool:
    print("\n--- Stage 1: Database & Logging Integrity ---")
    try:
        with open(strategy_config_path, "r") as f:
            config = json.load(f)

        client_id = config.get("live_config", {}).get("client_id", 10)
        db_name = f"live_telemetry_cid{client_id}.db"
        db_path = get_data_path(db_name)

        # Fallback to repo-local
        if not db_path.exists():
            repo_local = _project_root / "data" / db_name
            if repo_local.exists():
                db_path = repo_local

        if not db_path.exists():
            _record("DB_EXISTS", "FAIL", f"Telemetry DB not found: {db_name}")
            return False

        _record("DB_EXISTS", "PASS", str(db_path))

        conn = sqlite3.connect(str(db_path))
        query = (
            "SELECT timestamp, features_json "
            "FROM shadow_log ORDER BY timestamp DESC LIMIT 50"
        )
        df = pd.read_sql(query, conn)
        conn.close()

        if len(df) == 0:
            _record("DB_FRESHNESS", "FAIL", "No entries in shadow_log")
            return False

        # --- Freshness check ---
        latest_ts = df["timestamp"].iloc[0]
        current_time = datetime.now(timezone.utc)
        last_time = pd.to_datetime(latest_ts)
        if last_time.tzinfo is None:
            last_time = last_time.tz_localize("UTC")
        else:
            last_time = last_time.tz_convert("UTC")

        diff_hours = (current_time - last_time).total_seconds() / 3600.0

        if diff_hours > 2.0:
            _record(
                "DB_FRESHNESS", "FAIL",
                f"Latest entry is {diff_hours:.1f}h old (limit: 2h)"
            )
            return False

        _record("DB_FRESHNESS", "PASS", f"Latest entry: {diff_hours:.1f}h ago")

        # --- Feature variance check ---
        parsed_features = []
        for feat_str in df["features_json"]:
            if feat_str:
                parsed_features.append(json.loads(feat_str))

        if not parsed_features:
            _record("FEATURE_VARIANCE", "FAIL", "No features_json data")
            return False

        feat_df = pd.DataFrame(parsed_features)
        target_features = ["MACRO_VIX", "MACRO_DXY", "VOL_PARK_864", "log_ret"]
        flatline_count = 0

        for tf in target_features:
            if tf in feat_df.columns:
                var = feat_df[tf].astype(float).var()
                if pd.isna(var) or var == 0.0:
                    _record("FEATURE_VARIANCE", "FAIL", f"{tf} variance=0 (flatline)")
                    flatline_count += 1

        if flatline_count > 0:
            return False

        _record("FEATURE_VARIANCE", "PASS", "All monitored features have variance > 0")
        return True

    except Exception as e:
        _record("DB_INTEGRITY", "FAIL", str(e))
        return False


# ---------------------------------------------------------------------------
# Stage 2: Model Artifact Existence
# ---------------------------------------------------------------------------

def stage_2_artifact_validation(strategy_config_path: Path) -> bool:
    print("\n--- Stage 2: Model Artifact Validation ---")
    try:
        with open(strategy_config_path, "r") as f:
            config = json.load(f)

        all_ok = True
        for direction in ("long", "short"):
            model_cfg = config["models"][direction]
            model_path = _project_root / model_cfg["model_path"]

            if not model_path.exists():
                _record(
                    f"MODEL_{direction.upper()}", "FAIL",
                    f"final_model.pkl missing: {model_path.name}"
                )
                all_ok = False
            else:
                size_mb = model_path.stat().st_size / (1024 * 1024)
                _record(
                    f"MODEL_{direction.upper()}", "PASS",
                    f"{model_path.name} ({size_mb:.1f} MB)"
                )

            # Check for pure text export (optional but recommended)
            pure_path = model_path.parent / "final_model_pure.txt"
            if pure_path.exists():
                _record(
                    f"PURE_{direction.upper()}", "PASS",
                    "final_model_pure.txt exists"
                )
            else:
                _record(
                    f"PURE_{direction.upper()}", "WARN",
                    "final_model_pure.txt missing (non-critical)"
                )

            # Check OOS predictions
            oos_path = _project_root / model_cfg["predictions_path"]
            if not oos_path.exists():
                _record(
                    f"OOS_{direction.upper()}", "FAIL",
                    f"oos_predictions.csv missing"
                )
                all_ok = False
            else:
                _record(f"OOS_{direction.upper()}", "PASS", "oos_predictions.csv exists")

        # Check warm-start cache
        cache_1h = get_data_path("processed/warm_start_cache_1h.parquet")
        if cache_1h.exists():
            _record("CACHE_1H", "PASS", f"warm_start_cache_1h.parquet exists")
        else:
            _record("CACHE_1H", "WARN", "1h warm-start cache not found (will backfill)")

        return all_ok

    except Exception as e:
        _record("ARTIFACT_CHECK", "FAIL", str(e))
        return False


# ---------------------------------------------------------------------------
# Stage 3: Train-Serve Parity (Playback)
# ---------------------------------------------------------------------------

def stage_3_train_serve_parity(strategy_config_path: Path) -> tuple[bool, list[float]]:
    print("\n--- Stage 3: Train-Serve Parity ---")
    try:
        strategy = ConfigurableStrategy(config_path=str(strategy_config_path))
        feature_names = strategy.feature_names
        learner_buy = strategy._long_learner

        # Find 1h dataset
        parquet_path = get_data_path("processed/cl-1h_bk_HourSet_03.parquet")
        if not parquet_path.exists():
            parquet_path = get_data_path("processed/warm_start_cache_1h.parquet")

        if not parquet_path.exists():
            _record("PARITY_DATA", "FAIL", "No 1h parquet dataset found")
            return False, []

        df = pd.read_parquet(parquet_path)
        if "DateTime" in df.columns:
            df.set_index("DateTime", inplace=True)
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()

        with open(strategy_config_path, "r") as f:
            config = json.load(f)

        oos_path = _project_root / config["models"]["long"]["predictions_path"]
        if not oos_path.exists():
            _record("PARITY_OOS", "FAIL", "OOS predictions CSV missing")
            return False, []

        oos_df = pd.read_csv(oos_path, index_col=0, parse_dates=True)
        oos_df.index = pd.to_datetime(oos_df.index, utc=True)

        target_dates = oos_df[
            (oos_df.index >= "2026-03-01") & (oos_df.index < "2026-04-01")
        ].index
        if len(target_dates) == 0:
            _record("PARITY_DATES", "WARN", "No March 2026 OOS dates — skipping parity")
            return True, []  # non-blocking

        sampled_dates = target_dates[:30]
        max_diff = 0.0
        latencies = []
        diffs = []
        violations = 0

        for bar_time in sampled_dates:
            rolling_df = df[df.index <= bar_time].tail(5000)

            t0 = time.perf_counter()
            live_features = build_live_features(rolling_df, feature_names, bar_size="1h")
            latency = time.perf_counter() - t0
            latencies.append(latency)

            if live_features is None:
                _record("PARITY_FEATURES", "FAIL", f"Feature gen failed at {bar_time}")
                continue

            buy_prob_live = strategy._run_inference(learner_buy, live_features)
            buy_prob_oos = oos_df.loc[bar_time, "prob_Buy"]

            diff = abs(buy_prob_live - buy_prob_oos)
            diffs.append(diff)
            max_diff = max(max_diff, diff)

            if diff > 0.01:
                violations += 1
                flag = "❌"
            else:
                flag = "✅"
                
            try:
                print(f"  {flag} Bar {bar_time}: live={buy_prob_live:.4f} oos={buy_prob_oos:.4f} diff={diff:.4f} latency={latency:.2f}s")
            except UnicodeEncodeError:
                ascii_flag = "X" if flag == "❌" else "OK"
                print(f"  [{ascii_flag}] Bar {bar_time}: live={buy_prob_live:.4f} oos={buy_prob_oos:.4f} diff={diff:.4f} latency={latency:.2f}s")

        if diffs:
            avg_diff = np.mean(diffs)
            if violations > 0:
                _record(
                    "PARITY_DRIFT", "FAIL",
                    f"Avg Parity: {avg_diff:.4f}. {violations} bars exceeded 0.01 diff."
                )
                parity_ok = False
            else:
                _record(
                    "PARITY_CHECK", "PASS",
                    f"Avg Parity: {avg_diff:.4f}. All {len(diffs)} bars within 0.01 diff."
                )
                parity_ok = True
            return parity_ok, latencies
        else:
            _record("PARITY_DRIFT", "FAIL", "No valid inferences completed.")
            return False, latencies

    except Exception as e:
        _record("PARITY", "FAIL", str(e))
        return False, []


# ---------------------------------------------------------------------------
# Stage 4: Latency Check
# ---------------------------------------------------------------------------

def stage_4_latency(latencies: list[float]) -> bool:
    print("\n--- Stage 4: Feature Generation Latency ---")
    try:
        if not latencies:
            _record("LATENCY", "WARN", "No latencies recorded to verify.")
            return True
            
        avg_latency = np.mean(latencies)
        min_latency = np.min(latencies)
        max_latency = np.max(latencies)
        
        msg = f"Mean: {avg_latency:.3f}s | Min: {min_latency:.3f}s | Max: {max_latency:.3f}s"
        
        if avg_latency > 2.0:
            _record("LATENCY", "FAIL", f"{msg} (limit: 2.0s)")
            return False
        elif avg_latency > 1.0:
            _record("LATENCY", "WARN", f"{msg} (slow but acceptable)")
            return True
        else:
            _record("LATENCY", "PASS", msg)
            return True

    except Exception as e:
        _record("LATENCY", "FAIL", str(e))
        return False


# ---------------------------------------------------------------------------
# Telegram Summary Builder
# ---------------------------------------------------------------------------

def _build_telegram_summary(
    stage_results: dict[str, bool],
    config_name: str,
) -> str:
    """Build a Markdown-formatted Telegram message."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_passed = all(stage_results.values())

    header = "✅ *SMOKE TEST PASSED*" if all_passed else "🚨 *SMOKE TEST FAILED*"

    lines = [
        header,
        f"📅 {now}",
        f"📋 Config: `{config_name}`",
        "",
    ]

    stage_names = {
        "database": "Database Integrity",
        "artifacts": "Model Artifacts",
        "parity": "Train-Serve Parity",
        "latency": "Feature Latency",
    }

    for key, passed in stage_results.items():
        icon = "✅" if passed else "❌"
        name = stage_names.get(key, key)
        lines.append(f"{icon} {name}")

    # Add detail lines from _results
    fail_details = [r for r in _results if r["status"] == "FAIL"]
    warn_details = [r for r in _results if r["status"] == "WARN"]

    if fail_details:
        lines.append("")
        lines.append("*Failures:*")
        for r in fail_details[:5]:  # cap at 5 to stay within message limits
            lines.append(f"• `{r['stage']}`: {r['detail'][:80]}")

    if warn_details:
        lines.append("")
        lines.append("*Warnings:*")
        for r in warn_details[:3]:
            lines.append(f"• `{r['stage']}`: {r['detail'][:80]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="CL Analyst Daily Smoke Test")
    parser.add_argument(
        "--config",
        default=str(_project_root / "configs" / "strategies" / "hourly_ensemble_004.json"),
        help="Path to strategy config JSON",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Send results to Telegram (requires TELEGRAM_BOT_TOKEN in .env)",
    )
    args = parser.parse_args()

    strategy_config = Path(args.config)

    print("=" * 60)
    print("  CL ANALYST — DAILY SMOKE TEST PIPELINE")
    print(f"  Config: {strategy_config.name}")
    print(f"  Time:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    parity_ok, latencies = stage_3_train_serve_parity(strategy_config)

    results = {
        "database": stage_1_database_integrity(strategy_config),
        "artifacts": stage_2_artifact_validation(strategy_config),
        "parity": parity_ok,
        "latency": stage_4_latency(latencies),
    }

    # --- Final Summary ---
    print("\n" + "=" * 60)
    print("  SMOKE TEST SUMMARY")
    print("=" * 60)
    for key, passed in results.items():
        icon = "✅" if passed else "❌"
        try:
            print(f"  {icon} {key.upper()}")
        except UnicodeEncodeError:
            ascii_icon = "[OK]" if passed else "[X]"
            print(f"  {ascii_icon} {key.upper()}")

    all_passed = all(results.values())
    verdict = "ALL CHECKS PASSED" if all_passed else "FAILURES DETECTED"
    try:
        print(f"\n  {'✅' if all_passed else '🚨'} VERDICT: {verdict}")
    except UnicodeEncodeError:
        print(f"\n  {'[OK]' if all_passed else '[!]'} VERDICT: {verdict}")
    print("=" * 60)

    log_report(f"SMOKE TEST — {verdict}")

    # --- Telegram Notification ---
    if args.telegram:
        try:
            from src.live_execution.utils.telegram_alert import TelegramAlerter

            tg = TelegramAlerter()
            message = _build_telegram_summary(results, strategy_config.name)
            sent = tg.send(message)
            if sent:
                print("\n📱 Telegram notification sent.")
            else:
                print("\n⚠️  Telegram notification skipped (not configured or failed).")
        except Exception as e:
            print(f"\n⚠️  Telegram import/send failed: {e}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
