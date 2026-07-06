"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/fleet_health.py (NEW)
Target Class/Function: scan_log_errors, parse_heartbeats, check_positions,
                       check_bar_freshness, main
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: fleet-health-check_07062026_0640
Phase: RED — module does not exist. User requirement (2026-07-06): the
hourly monitor must verify the fleet is HEALTHY (log errors surfaced, bars
arriving, trades lining up, no hanging orders), not merely that it hasn't
crashed. During today's incident four children sat blind for 17 minutes
with ERROR lines streaming into the log and the monitor saw nothing.

Contract highlights:
  - Read-only: the ONLY file the checker writes is its own state file
    (health_state.json) inside --queue-dir.
  - main() ALWAYS exits 0 (a checker failure must not look like a fleet
    failure); prints HEALTH_OK when clean, else one line per finding:
    "HEALTH_EVENT: <kind> | <who> | <detail>".
  - Per-file byte offsets in the state file make the log scan incremental:
    an ERROR reported on run N is NOT re-reported on run N+1.
  - stdlib-only (sqlite3, json, re, argparse, datetime, pathlib).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.live_execution import fleet_health as fh


# ===========================================================================
# Fixtures
# ===========================================================================

_HB = ("2026-07-06 06:14:39 [INFO] [CL cid=1400] LiveTrader: [CL] "
       "HEARTBEAT: alive | last_bar=0.3h ago (2026-07-06 12:55:00) | "
       "market=OPEN | position=-1 contracts | unr_pnl=$27.63 | "
       "real_pnl=$0.00 | connected=True | subs_lost=True")

_HB_OK = ("2026-07-06 07:14:39 [INFO] [NG cid=3000] LiveTrader: [NG] "
          "HEARTBEAT: alive | last_bar=0.1h ago (2026-07-06 14:10:00) | "
          "market=OPEN | position=FLAT | unr_pnl=$0.00 | real_pnl=$0.00 | "
          "connected=True | subs_lost=False")

_LOG_CLEAN = "\n".join([
    "2026-07-06 06:00:05 [INFO] [CL cid=1400] LiveTrader: [CL] INFERENCE ok",
    _HB_OK,
    "",
])

_LOG_DIRTY = "\n".join([
    "2026-07-06 06:00:05 [INFO] [CL cid=1400] LiveTrader: [CL] INFERENCE ok",
    "2026-07-06 06:00:31 [ERROR] Error 10182, reqId 20: Failed to request "
    "live updates (disconnected).",
    "2026-07-06 06:00:33 [CRITICAL] [NG cid=3000] LiveTrader: something bad",
    "Traceback (most recent call last):",
    '  File "x.py", line 1, in <module>',
    "RuntimeError: boom",
    _HB,
    "",
])


def _make_db(path, *, naked=False, stale_bars=False, null_fill=False):
    """Minimal fleet_telemetry.db with the columns main() reads."""
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            trade_id TEXT, status TEXT, side TEXT, quantity INTEGER,
            entry_price REAL, exit_price REAL, close_reason TEXT,
            tp_order_id INTEGER, sl_order_id INTEGER,
            entry_time TEXT, close_time TEXT);
        CREATE TABLE trade_ledger (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            timestamp TEXT, action_taken TEXT, order_id INTEGER,
            fill_price REAL, created_at TEXT);
        CREATE TABLE market_bars (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            timestamp TEXT);
    """)
    now = datetime.utcnow()
    old = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "INSERT INTO active_positions (symbol, client_id, trade_id, status,"
        " side, quantity, entry_price, tp_order_id, sl_order_id, entry_time)"
        " VALUES ('GC', 4000, 'trade_8', 'OPEN', 'LONG', 1, 4150.1, ?, 10, ?)",
        (None if naked else 9, old),
    )
    con.execute(
        "INSERT INTO trade_ledger (symbol, client_id, timestamp,"
        " action_taken, order_id, fill_price, created_at)"
        " VALUES ('GC', 4000, ?, 'EXECUTE', 8, ?, ?)",
        (old, None if null_fill else 4150.1, old),
    )
    bar_ts = (now - timedelta(minutes=180 if stale_bars else 5))
    con.execute(
        "INSERT INTO market_bars (symbol, client_id, timestamp)"
        " VALUES ('GC', 4000, ?)",
        (bar_ts.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    con.commit()
    con.close()


# ===========================================================================
# scan_log_errors
# ===========================================================================

class TestScanLogErrors:

    def test_reports_error_critical_traceback_lines(self, tmp_path):
        log = tmp_path / "fleet_20260706.log"
        log.write_text(_LOG_DIRTY, encoding="utf-8")
        findings, new_offset = fh.scan_log_errors(log, 0)
        levels = [f["level"] for f in findings]
        assert "ERROR" in levels
        assert "CRITICAL" in levels
        assert "TRACEBACK" in levels
        assert new_offset == log.stat().st_size
        # INFO and heartbeat lines are not findings
        assert all("INFERENCE ok" not in f["line"] for f in findings)

    def test_incremental_offset(self, tmp_path):
        log = tmp_path / "fleet_20260706.log"
        log.write_text(_LOG_CLEAN, encoding="utf-8")
        findings, offset = fh.scan_log_errors(log, 0)
        assert findings == []
        with open(log, "a", encoding="utf-8") as fp:
            fp.write("2026-07-06 08:00:00 [ERROR] fresh problem\n")
        findings2, offset2 = fh.scan_log_errors(log, offset)
        assert len(findings2) == 1
        assert "fresh problem" in findings2[0]["line"]
        assert offset2 > offset

    def test_child_tag_extracted_when_present(self, tmp_path):
        log = tmp_path / "fleet_20260706.log"
        log.write_text(_LOG_DIRTY, encoding="utf-8")
        findings, _ = fh.scan_log_errors(log, 0)
        critical = [f for f in findings if f["level"] == "CRITICAL"][0]
        assert critical["child"] == "NG cid=3000"


# ===========================================================================
# parse_heartbeats
# ===========================================================================

class TestParseHeartbeats:

    def test_latest_heartbeat_per_child(self):
        hbs = fh.parse_heartbeats(_LOG_DIRTY.splitlines()
                                  + _LOG_CLEAN.splitlines())
        assert hbs["CL cid=1400"]["subs_lost"] is True
        assert hbs["CL cid=1400"]["last_bar_age_hours"] == pytest.approx(0.3)
        assert hbs["NG cid=3000"]["subs_lost"] is False

    def test_no_heartbeats(self):
        assert fh.parse_heartbeats(["nothing here"]) == {}


# ===========================================================================
# check_positions
# ===========================================================================

class TestCheckPositions:

    _NOW = datetime(2026, 7, 6, 14, 0, 0)

    def _clean_active(self):
        return {"symbol": "GC", "client_id": 4000, "trade_id": "trade_8",
                "status": "OPEN", "tp_order_id": 9, "sl_order_id": 10,
                "entry_price": 4150.1, "exit_price": None,
                "close_reason": None}

    def _clean_ledger(self):
        return {"symbol": "GC", "client_id": 4000, "order_id": 8,
                "action_taken": "EXECUTE", "fill_price": 4150.1,
                "timestamp": "2026-07-06T13:00:00",
                "created_at": "2026-07-06T13:00:06"}

    def test_clean_rows_no_findings(self):
        assert fh.check_positions(
            [self._clean_active()], [self._clean_ledger()], self._NOW) == []

    def test_unprotected_open_position(self):
        row = self._clean_active()
        row["sl_order_id"] = None
        findings = fh.check_positions([row], [], self._NOW)
        assert [f["kind"] for f in findings] == ["unprotected-position"]

    def test_duplicate_open_position(self):
        a, b = self._clean_active(), self._clean_active()
        b["trade_id"] = "trade_9"
        findings = fh.check_positions([a, b], [], self._NOW)
        assert "duplicate-open-position" in [f["kind"] for f in findings]

    def test_incomplete_close(self):
        row = self._clean_active()
        row["status"] = "CLOSED"
        row["exit_price"] = None
        row["close_reason"] = "SL_HIT"
        findings = fh.check_positions([row], [], self._NOW)
        assert [f["kind"] for f in findings] == ["incomplete-close"]

    def test_missing_fill_price_after_grace(self):
        row = self._clean_ledger()
        row["fill_price"] = None
        row["created_at"] = "2026-07-06T13:00:06"  # 60 min before _NOW
        findings = fh.check_positions([], [row], self._NOW)
        assert [f["kind"] for f in findings] == ["missing-fill-price"]

    def test_missing_fill_price_within_grace_not_flagged(self):
        row = self._clean_ledger()
        row["fill_price"] = None
        row["created_at"] = "2026-07-06T13:55:00"  # 5 min before _NOW
        assert fh.check_positions([], [row], self._NOW) == []


# ===========================================================================
# check_bar_freshness
# ===========================================================================

class TestCheckBarFreshness:

    _NOW = datetime(2026, 7, 6, 14, 0, 0)

    def test_fresh_bars_ok(self):
        rows = [{"symbol": "GC", "client_id": 4000,
                 "last_bar": "2026-07-06 13:55:00"}]
        assert fh.check_bar_freshness(rows, self._NOW) == []

    def test_stale_bars_flagged_with_age(self):
        rows = [{"symbol": "GC", "client_id": 4000,
                 "last_bar": "2026-07-06 12:00:00"}]
        findings = fh.check_bar_freshness(rows, self._NOW,
                                          threshold_minutes=45)
        assert len(findings) == 1
        assert findings[0]["kind"] == "stale-bars"
        assert findings[0]["age_minutes"] == pytest.approx(120.0)


# ===========================================================================
# main() end-to-end
# ===========================================================================

class TestMain:

    def _run(self, tmp_path, capsys, *, log_text, **db_kw):
        log_dir = tmp_path / "fleet"
        log_dir.mkdir(exist_ok=True)
        (log_dir / "fleet_20260706.log").write_text(log_text,
                                                    encoding="utf-8")
        db = tmp_path / "fleet_telemetry.db"
        if not db.exists():
            _make_db(db, **db_kw)
        queue = tmp_path / "error_queue"
        queue.mkdir(exist_ok=True)
        rc = fh.main(["--log-dir", str(log_dir), "--db", str(db),
                      "--queue-dir", str(queue)])
        out = capsys.readouterr().out
        return rc, out, queue

    def test_clean_fleet_health_ok(self, tmp_path, capsys):
        rc, out, _ = self._run(tmp_path, capsys, log_text=_LOG_CLEAN)
        assert rc == 0
        assert "HEALTH_OK" in out
        assert "HEALTH_EVENT" not in out

    def test_dirty_fleet_reports_findings(self, tmp_path, capsys):
        rc, out, _ = self._run(tmp_path, capsys, log_text=_LOG_DIRTY,
                               naked=True, stale_bars=True, null_fill=True)
        assert rc == 0
        assert "HEALTH_OK" not in out
        assert "HEALTH_EVENT: log-error" in out
        assert "HEALTH_EVENT: subs-lost" in out
        assert "HEALTH_EVENT: unprotected-position" in out
        assert "HEALTH_EVENT: stale-bars" in out
        assert "HEALTH_EVENT: missing-fill-price" in out

    def test_log_errors_not_rereported(self, tmp_path, capsys):
        rc1, out1, queue = self._run(tmp_path, capsys, log_text=_LOG_DIRTY)
        assert "HEALTH_EVENT: log-error" in out1
        state = json.loads((queue / "health_state.json")
                           .read_text(encoding="utf-8"))
        assert state["log_offsets"]  # per-file offsets persisted
        rc2, out2, _ = self._run(tmp_path, capsys, log_text=_LOG_DIRTY)
        assert "HEALTH_EVENT: log-error" not in out2

    def test_missing_db_reports_error_but_exits_zero(self, tmp_path, capsys):
        log_dir = tmp_path / "fleet"
        log_dir.mkdir()
        (log_dir / "fleet_20260706.log").write_text(_LOG_CLEAN,
                                                    encoding="utf-8")
        queue = tmp_path / "error_queue"
        queue.mkdir()
        rc = fh.main(["--log-dir", str(log_dir),
                      "--db", str(tmp_path / "nope.db"),
                      "--queue-dir", str(queue)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "HEALTH_CHECK_ERROR" in out
