"""
Fleet Health Check — hourly, read-only "is the bot actually healthy?" sweep.

Ticket fleet-health-check_07062026_0640. 2026-07-06 lesson: the error queue
only captured process CRASHES, so four children sat alive-but-blind with
ERROR lines streaming into the fleet log and the hourly monitor saw nothing.
The monitor's job is "the fleet is healthy", not "the fleet hasn't died".

Checks (all read-only; the ONLY write is this module's own state file
``health_state.json`` inside --queue-dir):

1. Log scan   — new [ERROR]/[CRITICAL]/Traceback lines in every
                ``fleet_*.log`` since the last run (per-file byte offsets
                persisted in the state file, so nothing is re-reported).
2. Heartbeats — latest HEARTBEAT per child in the newly read log text;
                ``subs_lost=True`` is a finding.
3. Positions  — from fleet_telemetry.db: OPEN rows missing TP/SL order ids
                (naked position!), duplicate OPEN rows per (symbol,
                client_id), CLOSED rows missing exit_price/close_reason
                (closes within 48h only — adjudicated old rows stop
                nagging), and EXECUTE ledger rows still missing fill_price
                after a 10-minute grace AND provably filled per tradebook/
                position evidence (update_fill regression signal; NULL is
                the legitimate permanent state of never-filled entries).
4. Bars       — max(market_bars.timestamp) per (symbol, client_id) older
                than --stale-bars-min (default 45). Market-closed judgment
                belongs to the AGENT reading the report, not this script.

Output contract (consumed by the fleet-error-monitor hourly protocol):
    HEALTH_OK (...)                          when nothing was found
    HEALTH_EVENT: <kind> | <who> | <detail>  one line per finding
    HEALTH_CHECK_ERROR: <detail>             checker-internal problem
Always exits 0 — a checker failure must not look like a fleet failure.

Stdlib-only (fleet_runner.py rule for supervisory tooling).
"""

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG_DIR = _PROJECT_ROOT / "reports" / "fleet"
DEFAULT_QUEUE_DIR = _PROJECT_ROOT / ".agents" / "collab" / "error_queue"

STATE_FILENAME = "health_state.json"
FILL_PRICE_GRACE_MINUTES = 10
DEFAULT_STALE_BARS_MINUTES = 45
INCOMPLETE_CLOSE_WINDOW_HOURS = 48

# "2026-07-06 06:00:31 [ERROR] ..." — level token after the timestamp.
_LEVEL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.*?\[(ERROR|CRITICAL)\]"
)
# Child tag as written by the shared fleet log: "[NG cid=3000]".
_CHILD_RE = re.compile(r"\[([A-Z]{1,6} cid=\d+)\]")
_TRACEBACK_MARKER = "Traceback (most recent call last):"

_HEARTBEAT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
    r"\[(?P<child>[A-Z]{1,6} cid=\d+)\].*?HEARTBEAT: alive \| "
    r"last_bar=(?P<age>[\d.]+)h ago.*?"
    r"position=(?P<position>[^|]+?)\s*\|.*?"
    r"subs_lost=(?P<subs>True|False)"
)


# ---------------------------------------------------------------------------
# 1. Log scan
# ---------------------------------------------------------------------------

def scan_log_errors(log_path, offset):
    """Scan a fleet log from a byte offset for ERROR/CRITICAL/Traceback lines.

    Returns (findings, new_offset). A trailing partial line (no newline yet
    — the fleet is mid-write) is left for the next run.
    """
    log_path = Path(log_path)
    size = log_path.stat().st_size
    if offset >= size:
        return [], offset
    with open(log_path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk.endswith(b"\n"):
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return [], offset  # single partial line — wait for next run
        chunk = chunk[:last_nl + 1]
    text = chunk.decode("utf-8", errors="replace")

    findings = []
    for line in text.splitlines():
        level = None
        m = _LEVEL_RE.match(line)
        if m:
            level = m.group(1)
        elif line.startswith(_TRACEBACK_MARKER):
            level = "TRACEBACK"
        if level is None:
            continue
        child_m = _CHILD_RE.search(line)
        findings.append({
            "kind": "log-error",
            "level": level,
            "line": line.strip(),
            "child": child_m.group(1) if child_m else None,
        })
    return findings, offset + len(chunk)


# ---------------------------------------------------------------------------
# 2. Heartbeats
# ---------------------------------------------------------------------------

def parse_heartbeats(lines):
    """Latest HEARTBEAT per child tag from an iterable of log lines."""
    beats = {}
    for line in lines:
        m = _HEARTBEAT_RE.match(line)
        if not m:
            continue
        beats[m.group("child")] = {
            "timestamp": m.group("ts"),
            "last_bar_age_hours": float(m.group("age")),
            "position": m.group("position").strip(),
            "subs_lost": m.group("subs") == "True",
        }
    return beats


# ---------------------------------------------------------------------------
# 3. Positions / orders
# ---------------------------------------------------------------------------

def _parse_ts(value):
    return datetime.fromisoformat(str(value).replace("T", " "))


def check_positions(active_rows, ledger_rows, now, *, fill_evidence=None):
    """DB-internal trade/order consistency findings.

    fill_evidence (D3, oob-entry-state-recovery): optional collection of
    order ids PROVABLY filled (tradebook EXECUTION_FILL order_ids union
    active_positions.entry_order_id values). With evidence, a NULL-fill
    EXECUTE row flags only when its order provably filled — a true
    update_fill regression. NULL fill_price is otherwise the legitimate
    permanent state of a never-filled/TTL-cancelled entry. None (the
    default) means evidence UNAVAILABLE -> legacy conservative time-based
    over-flagging (A6: over-flag, never silently skip).
    """
    findings = []
    open_seen = {}
    close_recency = timedelta(hours=INCOMPLETE_CLOSE_WINDOW_HOURS)
    for row in active_rows:
        who = f"{row['symbol']}/{row['client_id']}"
        if row["status"] == "OPEN":
            if not row.get("tp_order_id") or not row.get("sl_order_id"):
                findings.append({
                    "kind": "unprotected-position", "who": who,
                    "detail": (
                        f"{row['trade_id']} is OPEN with tp_order_id="
                        f"{row.get('tp_order_id')} sl_order_id="
                        f"{row.get('sl_order_id')} — NAKED position, "
                        f"verify brackets on the broker immediately"
                    ),
                })
            key = (row["symbol"], row["client_id"])
            open_seen.setdefault(key, []).append(row["trade_id"])
        elif row["status"] == "CLOSED":
            if row.get("exit_price") is None or not row.get("close_reason"):
                # D3: adjudicated old rows must stop nagging hourly — scope
                # to recent closes. NULL/unparseable close_time still flags
                # ("surface it" precedent).
                recent = True
                close_time = row.get("close_time")
                if close_time is not None:
                    try:
                        recent = (now - _parse_ts(close_time)) <= close_recency
                    except (TypeError, ValueError):
                        recent = True
                if recent:
                    findings.append({
                        "kind": "incomplete-close", "who": who,
                        "detail": (
                            f"{row['trade_id']} is CLOSED with exit_price="
                            f"{row.get('exit_price')} close_reason="
                            f"{row.get('close_reason')}"
                        ),
                    })
    for (symbol, client_id), trade_ids in open_seen.items():
        if len(trade_ids) > 1:
            findings.append({
                "kind": "duplicate-open-position",
                "who": f"{symbol}/{client_id}",
                "detail": f"{len(trade_ids)} OPEN rows: {', '.join(trade_ids)}",
            })
    grace = timedelta(minutes=FILL_PRICE_GRACE_MINUTES)
    # str/int-robust: ledger order_ids are ints, tradebook rows may carry
    # str(orderId) from broker events.
    evidence_ids = (
        None if fill_evidence is None
        else {str(oid) for oid in fill_evidence}
    )
    for row in ledger_rows:
        if row.get("action_taken") != "EXECUTE":
            continue
        if row.get("fill_price") is not None:
            continue
        if (evidence_ids is not None
                and str(row.get("order_id")) not in evidence_ids):
            continue  # no fill proven — a never-filled entry, not a bug
        created = row.get("created_at") or row.get("timestamp")
        try:
            age_ok = (now - _parse_ts(created)) > grace
        except (TypeError, ValueError):
            age_ok = True  # unparseable timestamp — surface it
        if age_ok:
            findings.append({
                "kind": "missing-fill-price",
                "who": f"{row['symbol']}/{row['client_id']}",
                "detail": (
                    f"EXECUTE order_id={row.get('order_id')} created "
                    f"{created} still has NULL fill_price (update_fill "
                    f"not stamping?)"
                ),
            })
    return findings


# ---------------------------------------------------------------------------
# 4. Bar freshness
# ---------------------------------------------------------------------------

def check_bar_freshness(bar_rows, now, threshold_minutes=DEFAULT_STALE_BARS_MINUTES):
    """Flag (symbol, client_id) whose newest market bar is too old."""
    findings = []
    for row in bar_rows:
        try:
            age_minutes = (now - _parse_ts(row["last_bar"])).total_seconds() / 60
        except (TypeError, ValueError):
            age_minutes = float("inf")
        if age_minutes > threshold_minutes:
            findings.append({
                "kind": "stale-bars",
                "who": f"{row['symbol']}/{row['client_id']}",
                "age_minutes": age_minutes,
                "detail": (
                    f"newest market bar {row.get('last_bar')} is "
                    f"{age_minutes:.0f} min old (threshold "
                    f"{threshold_minutes}) — if the market is open, the "
                    f"child is blind"
                ),
            })
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _load_state(state_path):
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"log_offsets": {}}


def _save_state(state_path, state):
    tmp = state_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    tmp.replace(state_path)


def _default_db_path():
    from src.data_paths import get_data_root
    return get_data_root() / "fleet_telemetry.db"


def _query_dicts(conn, sql):
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only fleet health check (hourly monitor step 2).")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--db", default=None)
    parser.add_argument("--queue-dir", default=str(DEFAULT_QUEUE_DIR))
    parser.add_argument("--stale-bars-min", type=int,
                        default=DEFAULT_STALE_BARS_MINUTES)
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC
    findings = []
    checked = []
    state_path = Path(args.queue_dir) / STATE_FILENAME
    state = _load_state(state_path)

    # 1 + 2 — log scan and heartbeats over the newly read text.
    try:
        offsets = state.setdefault("log_offsets", {})
        new_lines = []
        for log_path in sorted(Path(args.log_dir).glob("fleet_*.log")):
            offset = int(offsets.get(log_path.name, 0))
            size = log_path.stat().st_size
            if size < offset:
                offset = 0  # file truncated/recreated — rescan
            log_findings, new_offset = scan_log_errors(log_path, offset)
            findings.extend(log_findings)
            with open(log_path, "rb") as fh:
                fh.seek(offset)
                new_lines.extend(
                    fh.read(new_offset - offset)
                    .decode("utf-8", errors="replace").splitlines()
                )
            offsets[log_path.name] = new_offset
        checked.append("log")
        for child, beat in parse_heartbeats(new_lines).items():
            if beat["subs_lost"]:
                findings.append({
                    "kind": "subs-lost", "who": child,
                    "detail": (
                        f"heartbeat {beat['timestamp']} reports "
                        f"subs_lost=True (last_bar "
                        f"{beat['last_bar_age_hours']}h ago, position "
                        f"{beat['position']}) — child alive but blind"
                    ),
                })
        checked.append("heartbeats")
    except Exception as exc:  # checker bug must not look like fleet failure
        print(f"HEALTH_CHECK_ERROR: log scan failed: {exc!r}")

    # 3 + 4 — telemetry DB checks (read-only connection).
    try:
        db_path = Path(args.db) if args.db else _default_db_path()
    except Exception as exc:
        # src.data_paths pulls in dotenv, which the global interpreter may
        # lack — a checker environment problem must not crash the check
        # (run via `conda run -n trader` or pass --db explicitly).
        print(f"HEALTH_CHECK_ERROR: cannot resolve default db path: {exc!r}")
        db_path = None
    if db_path is None:
        pass
    elif not db_path.exists():
        print(f"HEALTH_CHECK_ERROR: telemetry db not found: {db_path}")
    else:
        try:
            conn = sqlite3.connect(
                f"file:{db_path.as_posix()}?mode=ro", uri=True)
            try:
                try:
                    active = _query_dicts(conn, (
                        "SELECT symbol, client_id, trade_id, status,"
                        " tp_order_id, sl_order_id, entry_price, exit_price,"
                        " close_reason, entry_order_id, close_time"
                        " FROM active_positions"))
                except sqlite3.OperationalError:
                    # Legacy schema without entry_order_id — the positions
                    # check must keep running; fill evidence degrades to
                    # None below (A6: over-flag, never silently skip).
                    active = _query_dicts(conn, (
                        "SELECT symbol, client_id, trade_id, status,"
                        " tp_order_id, sl_order_id, entry_price, exit_price,"
                        " close_reason, close_time FROM active_positions"))
                ledger = _query_dicts(conn, (
                    "SELECT symbol, client_id, timestamp, action_taken,"
                    " order_id, fill_price, created_at FROM trade_ledger"
                    " WHERE action_taken = 'EXECUTE'"))
                bars = _query_dicts(conn, (
                    "SELECT symbol, client_id, MAX(timestamp) AS last_bar"
                    " FROM market_bars GROUP BY symbol, client_id"))
                # Fill evidence (D3): order ids provably filled — tradebook
                # EXECUTION_FILL rows union position entry_order_ids.
                # ORDER_SUBMITTED rows are deliberately NOT evidence.
                try:
                    fill_rows = _query_dicts(conn, (
                        "SELECT DISTINCT order_id FROM tradebook_events"
                        " WHERE event_type = 'EXECUTION_FILL'"
                        " AND order_id IS NOT NULL"))
                    fill_evidence = {r["order_id"] for r in fill_rows}
                    fill_evidence.update(
                        row["entry_order_id"] for row in active
                        if row.get("entry_order_id") is not None)
                except sqlite3.OperationalError:
                    fill_evidence = None  # legacy schema — no evidence source
            finally:
                conn.close()
            # A6: fill_evidence is ALWAYS passed explicitly here — the
            # parameter default exists only for callers without evidence.
            findings.extend(check_positions(
                active, ledger, now, fill_evidence=fill_evidence))
            checked.append("positions")
            findings.extend(check_bar_freshness(
                bars, now, threshold_minutes=args.stale_bars_min))
            checked.append("bars")
        except Exception as exc:
            print(f"HEALTH_CHECK_ERROR: db checks failed: {exc!r}")

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _save_state(state_path, state)
    except OSError as exc:
        print(f"HEALTH_CHECK_ERROR: could not persist state: {exc!r}")

    if findings:
        for f in findings:
            who = f.get("who") or f.get("child") or "fleet"
            detail = f.get("detail") or f.get("line") or ""
            print(f"HEALTH_EVENT: {f['kind']} | {who} | {detail}")
        print(f"HEALTH_SUMMARY: {len(findings)} finding(s) "
              f"(checked: {', '.join(checked)})")
    else:
        print(f"HEALTH_OK (checked: {', '.join(checked)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
