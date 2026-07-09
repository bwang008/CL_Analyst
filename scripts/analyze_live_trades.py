#!/usr/bin/env python
"""Analyze Live Trades — re-runnable fleet trade & PnL report.

Reads the shared fleet telemetry DB (``fleet_telemetry.db``) and produces a
per-model report of trade COUNT, realized/unrealized PnL, and — most
importantly for validating data streams — the SIGNAL/DECISION distribution
(firing rate + model-confidence spread). If a live data stream is corrupt,
the model sees different inputs, so its confidence distribution and how often
it fires (EXECUTE vs HOLD) drift away from the backtest — long before the
dollar PnL makes it obvious. This report surfaces that.

Why this is NOT scripts/trade_reconciler.py: that tool opens the DB in
LEGACY single-bot mode (it raises on the fleet DB, user_version 2) and diffs
one config against a backtest CSV. This is a fleet-wide, read-only summary.

CRITICAL — contract multiplier: three of the live models trade MICRO
contracts (ES→MES ×5, GC→MGC ×10, SI→SIL ×1000) while the telemetry DB
stores the BRAIN symbol (ES/GC/SI, ×50/×100/×5000). PnL must use each
config's ``execution_symbol`` multiplier, NOT the DB symbol column — using
the DB symbol would overstate micro PnL by 5–10x. We resolve the multiplier
per client_id from the fleet manifest's configs.

Usage:
    conda run -n trader python scripts/analyze_live_trades.py
    conda run -n trader python scripts/analyze_live_trades.py --since 2026-07-06
    conda run -n trader python scripts/analyze_live_trades.py \
        --since 2026-07-06 --output reports/live_trade_analysis.md

Read-only: opens the DB with mode=ro and only SELECTs. Safe to run against
the live fleet at any time.

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Windows consoles default to cp1252, which can't encode the report's arrows
# (→) / warning glyphs. Force UTF-8 so the console print matches the file.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and getattr(_stream, "encoding", "").lower() != "utf-8":
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            try:
                setattr(sys, _stream_name, io.TextIOWrapper(
                    _stream.buffer, encoding="utf-8", errors="replace"))
            except Exception:
                pass

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.core.instrument_master import dollars_per_point, get_instrument  # noqa: E402

# A closed trade whose exit is this far (fractionally) from its entry is
# almost certainly a cross-contaminated / bad print (e.g. a GC row that
# recorded an ES-scale 7484 exit against a 4063 entry). Excluded from summed
# PnL and flagged, rather than silently poisoning the total.
SUSPECT_MOVE_FRACTION = 0.25

# The IB Gateway master-client-ID login leaked OTHER clients' executions into
# this fleet's OOB recovery, poisoning some exit prices/trades (commit 4743489,
# 2026-07-08 06:47 -0700 = 13:47 UTC dropped the Master API requirement). Trades
# entered before this instant — especially OOB-recovered ones — must be
# reconciled against the broker before their PnL/count is trusted. Configurable
# / dis'able via --contamination-cutover.
DEFAULT_CONTAMINATION_CUTOVER = "2026-07-08T13:47:00"

DEFAULT_MANIFEST = _PROJECT_ROOT / "configs" / "fleet" / "fleet_manifest.json"
DEFAULT_OUTPUT = _PROJECT_ROOT / "reports" / "live_trade_analysis.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_db_path() -> str:
    """Resolve the fleet DB path; fall back to the known Windows data root."""
    try:
        from src.data_paths import get_data_root
        return str(get_data_root() / "fleet_telemetry.db")
    except Exception:
        return r"C:\CL_Analyst_Data\data\fleet_telemetry.db"


def _parse_ts(value):
    """Parse an ISO-ish telemetry timestamp to datetime, or None."""
    if value is None:
        return None
    s = str(value).replace("T", " ")
    for fmt in (None,):  # fromisoformat first
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _percentile(sorted_vals, pct):
    """Nearest-rank percentile of a pre-sorted list (0..100). None if empty."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _fmt_money(x):
    if x is None:
        return "n/a"
    return f"${x:,.2f}"


def _fmt(x, nd=2):
    if x is None:
        return "n/a"
    return f"{x:.{nd}f}"


# ---------------------------------------------------------------------------
# Manifest → client_id map (nickname + execution_symbol + multiplier)
# ---------------------------------------------------------------------------

def load_client_map(manifest_path: Path) -> dict:
    """Build {client_id: {...}} from the fleet manifest's configs.

    Each entry carries the config nickname, its EXECUTION symbol (the micro
    actually traded), the brain symbol, dollars_per_point for the execution
    symbol, and whether the instance is enabled. This is the authority for
    the PnL multiplier — never the DB's brain-symbol column.
    """
    out = {}
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"WARNING: could not read manifest {manifest_path}: {e}",
              file=sys.stderr)
        return out
    for entry in manifest.get("instances", []):
        cfg_path = _PROJECT_ROOT / entry["config"]
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"WARNING: could not read config {cfg_path}: {e}",
                  file=sys.stderr)
            continue
        live = cfg.get("live_config") or {}
        cid = live.get("client_id")
        if cid is None:
            continue
        exec_sym = cfg.get("execution_symbol")
        info = {
            "nickname": cfg.get("nickname", cfg_path.stem),
            "config": cfg_path.name,
            "execution_symbol": exec_sym,
            "enabled": bool(entry.get("enabled", False)),
            "dpp": None,
            "brain_symbol": None,
            "multiplier_note": None,
        }
        try:
            info["dpp"] = dollars_per_point(exec_sym)
            inst = get_instrument(exec_sym)
            info["brain_symbol"] = inst.micro_of or exec_sym
            if inst.micro_of:
                info["multiplier_note"] = (
                    f"trades micro {exec_sym} (×{inst.multiplier}); "
                    f"brain={inst.micro_of}"
                )
        except Exception as e:  # unknown/None execution_symbol
            info["multiplier_note"] = f"UNKNOWN execution_symbol {exec_sym!r}: {e}"
        out[int(cid)] = info
    return out


# ---------------------------------------------------------------------------
# DB reads (read-only)
# ---------------------------------------------------------------------------

def _query(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def read_fleet(db_path: str):
    """Read positions, ledger decisions, latest bars, and commissions."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        positions = _query(conn, (
            "SELECT client_id, symbol, status, side, quantity, entry_price, "
            "exit_price, close_reason, entry_time, close_time, bars_held "
            "FROM active_positions"))
        ledger = _query(conn, (
            "SELECT client_id, symbol, timestamp, action_taken, confidence_pct "
            "FROM trade_ledger"))
        last_bars = {
            r["symbol"]: r["last_bar"]
            for r in _query(conn, (
                "SELECT symbol, MAX(timestamp) AS last_bar FROM market_bars "
                "GROUP BY symbol"))
        }
        bar_rows = _query(conn, (
            "SELECT client_id, symbol, close, timestamp FROM market_bars "
            "WHERE id IN (SELECT MAX(id) FROM market_bars GROUP BY symbol)"))
        last_close = {r["symbol"]: r["close"] for r in bar_rows}
        # Commissions live on COMMISSION tradebook events (populated by IBKR
        # commission reports). May be empty on older DBs.
        try:
            comm = _query(conn, (
                "SELECT client_id, event_timestamp_utc, commission "
                "FROM tradebook_events WHERE event_type = 'COMMISSION'"))
        except sqlite3.OperationalError:
            comm = []
    finally:
        conn.close()
    return positions, ledger, last_close, last_bars, comm


# ---------------------------------------------------------------------------
# Per-model analysis
# ---------------------------------------------------------------------------

def analyze_model(cid, info, positions, ledger, commissions, last_close,
                  since, until, cutover=None):
    """Compute the per-model summary dict for one client_id."""
    dpp = info.get("dpp") if info else None
    exec_sym = info.get("execution_symbol") if info else None

    def in_window(ts_str):
        ts = _parse_ts(ts_str)
        if ts is None:
            return True  # unparseable — surface it rather than drop it
        if since and ts < since:
            return False
        if until and ts > until:
            return False
        return True

    # --- positions entered in window ---
    pos = [p for p in positions
           if p["client_id"] == cid and in_window(p["entry_time"])]
    closed = [p for p in pos if p["status"] == "CLOSED"]
    open_pos = [p for p in pos if p["status"] == "OPEN"]

    longs = sum(1 for p in pos if str(p["side"]).upper() == "LONG")
    shorts = sum(1 for p in pos if str(p["side"]).upper() == "SHORT")

    exit_reasons = {}
    for p in closed:
        r = p["close_reason"] or "UNKNOWN"
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # --- realized PnL over closed trades ---
    # Two PnL numbers: "clean" = normal SL_HIT/TP_HIT/TIME exits (trustworthy),
    # "all" = also includes OOB-recovered exits, which the master-API leak
    # could have poisoned with cross-client fills. suspect (>25% move) and
    # unresolved (null exit) are excluded from BOTH.
    realized_all = 0.0      # clean + oob, valued
    realized_clean = 0.0    # clean recorded exits only
    wins = losses = 0       # over the CLEAN set (trustworthy win rate)
    valid_closed = 0        # clean + oob, valued
    n_clean = n_oob = 0
    unresolved = []   # null exit price (CLOSED_OOB_UNRECOVERED etc.)
    suspect = []      # implausible exit price (bad print / contamination)
    bars_held_vals = []
    best = worst = None
    for p in closed:
        entry, exit_ = p["entry_price"], p["exit_price"]
        if p.get("bars_held") is not None:
            bars_held_vals.append(p["bars_held"])
        if exit_ is None or entry is None:
            unresolved.append(p)
            continue
        if entry and abs(exit_ - entry) / abs(entry) > SUSPECT_MOVE_FRACTION:
            suspect.append(p)
            continue
        if dpp is None:
            unresolved.append(p)  # can't value without a multiplier
            continue
        sign = 1.0 if str(p["side"]).upper() == "LONG" else -1.0
        qty = p.get("quantity") or 1
        pnl = (exit_ - entry) * sign * dpp * qty
        realized_all += pnl
        valid_closed += 1
        if "OOB" in str(p["close_reason"]).upper():
            n_oob += 1  # contamination-risk exit — kept out of the clean total
        else:
            n_clean += 1
            realized_clean += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        if best is None or pnl > best[0]:
            best = (pnl, p)
        if worst is None or pnl < worst[0]:
            worst = (pnl, p)

    # Win rate over the CLEAN set only — OOB-recovered outcomes aren't a
    # trustworthy win/loss signal.
    win_rate = (wins / n_clean * 100.0) if n_clean else None
    avg_bars = (sum(bars_held_vals) / len(bars_held_vals)
                if bars_held_vals else None)

    # Entries that predate the master-API contamination cutover.
    pre_cutover = 0
    if cutover is not None:
        for p in pos:
            ts = _parse_ts(p["entry_time"])
            if ts is not None and ts < cutover:
                pre_cutover += 1

    # --- unrealized PnL on open positions (mark to last bar close) ---
    unrealized = 0.0
    unrealized_ok = True
    for p in open_pos:
        mark = last_close.get(p["symbol"])
        if mark is None or dpp is None or p["entry_price"] is None:
            unrealized_ok = False
            continue
        sign = 1.0 if str(p["side"]).upper() == "LONG" else -1.0
        qty = p.get("quantity") or 1
        unrealized += (mark - p["entry_price"]) * sign * dpp * qty

    # --- decision / signal distribution (firing behavior) ---
    led = [row for row in ledger
           if row["client_id"] == cid and in_window(row["timestamp"])]
    actions = {}
    for row in led:
        a = row["action_taken"] or "UNKNOWN"
        actions[a] = actions.get(a, 0) + 1
    n_decisions = len(led)
    n_execute = actions.get("EXECUTE", 0)
    firing_rate = (n_execute / n_decisions * 100.0) if n_decisions else None

    conf_all = sorted(row["confidence_pct"] for row in led
                      if row["confidence_pct"] is not None)
    conf_exec = sorted(row["confidence_pct"] for row in led
                       if row["confidence_pct"] is not None
                       and row["action_taken"] == "EXECUTE")

    # --- commissions in window ---
    comm_total = 0.0
    comm_n = 0
    for c in commissions:
        if c["client_id"] != cid:
            continue
        if not in_window(c["event_timestamp_utc"]):
            continue
        if c["commission"] is not None:
            comm_total += c["commission"]
            comm_n += 1

    return {
        "cid": cid,
        "info": info,
        "execution_symbol": exec_sym,
        "dpp": dpp,
        "n_entries": len(pos),
        "n_closed": len(closed),
        "n_open": len(open_pos),
        "longs": longs,
        "shorts": shorts,
        "exit_reasons": exit_reasons,
        "realized": realized_all if valid_closed else 0.0,
        "realized_clean": realized_clean,
        "valid_closed": valid_closed,
        "n_clean": n_clean,
        "n_oob": n_oob,
        "pre_cutover": pre_cutover,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "best": best,
        "worst": worst,
        "avg_bars_held": avg_bars,
        "unresolved": unresolved,
        "suspect": suspect,
        "unrealized": unrealized,
        "unrealized_ok": unrealized_ok,
        "open_pos": open_pos,
        "actions": actions,
        "n_decisions": n_decisions,
        "firing_rate": firing_rate,
        "conf_all": conf_all,
        "conf_exec": conf_exec,
        "comm_total": comm_total,
        "comm_n": comm_n,
    }


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _conf_line(label, vals):
    if not vals:
        return f"    - {label}: (none)"
    mean = sum(vals) / len(vals)
    return (f"    - {label}: n={len(vals)} mean={_fmt(mean,1)} "
            f"p50={_fmt(_percentile(vals,50),1)} "
            f"p90={_fmt(_percentile(vals,90),1)} "
            f"min={_fmt(vals[0],1)} max={_fmt(vals[-1],1)}")


def format_report(models, db_path, since, until, last_bars, unmapped_cids,
                  cutover=None):
    L = []
    # Telemetry timestamps (bars, entries, close_time) are naive UTC — same
    # convention as fleet_health.py. Reference "now" in UTC so freshness ages
    # and the window bound are on the DB's clock (not local, which is behind).
    now = datetime.utcnow()
    L.append("# Live Fleet Trade Analysis")
    L.append("")
    L.append(f"- **Generated:** {now:%Y-%m-%d %H:%M:%S} UTC")
    L.append(f"- **DB:** `{db_path}`")
    L.append(f"- **Window (UTC):** {since:%Y-%m-%d %H:%M} → "
             f"{(until or now):%Y-%m-%d %H:%M}")
    L.append("")

    # Fleet totals
    tot_entries = sum(m["n_entries"] for m in models)
    tot_realized = sum(m["realized"] for m in models)
    tot_clean = sum(m["realized_clean"] for m in models)
    tot_unreal = sum(m["unrealized"] for m in models)
    tot_comm = sum(m["comm_total"] for m in models)
    tot_open = sum(m["n_open"] for m in models)
    tot_oob = sum(m["n_oob"] for m in models)
    tot_pre = sum(m["pre_cutover"] for m in models)
    L.append("## Fleet Totals")
    L.append("")
    L.append(f"- **Models with activity:** {len(models)}")
    L.append(f"- **Total position entries:** {tot_entries} "
             f"({tot_open} still open)")
    L.append(f"- **Realized PnL — CLEAN (SL/TP exits only, trust this):** "
             f"{_fmt_money(tot_clean)}")
    L.append(f"- **Realized PnL — all valued (incl. OOB-recovered):** "
             f"{_fmt_money(tot_realized)}")
    L.append(f"- **Unrealized PnL (open, mark-to-last-bar):** {_fmt_money(tot_unreal)}")
    L.append(f"- **Commissions (from tradebook):** {_fmt_money(tot_comm)}")
    L.append(f"- **Net (clean realized + unrealized − commissions):** "
             f"{_fmt_money(tot_clean + tot_unreal - tot_comm)}")
    if cutover is not None:
        L.append("")
        L.append(f"> ⚠️ **Contamination caveat:** {tot_oob} OOB-recovered "
                 f"trade(s) and {tot_pre} entry(ies) before the master-API fix "
                 f"({cutover:%Y-%m-%d %H:%M} UTC) may carry cross-client fills. "
                 f"**For dollars, IBKR statements are ground truth — not this "
                 f"DB.** The CLEAN figure excludes OOB exits; verify the OOB "
                 f"bucket against the broker before trusting it. Trade *counts* "
                 f"are more robust than DB PnL, but OOB entries could include "
                 f"leaked fills that aren't yours.")
    L.append("")

    # Summary table
    L.append("## Per-Model Summary")
    L.append("")
    L.append("| Model | Trades | Open | Clean/OOB | Win% | Clean $ | All $ | "
             "Unreal $ | Fire% | Flags |")
    L.append("|---|--:|--:|:--:|--:|--:|--:|--:|--:|---|")
    for m in models:
        info = m["info"] or {}
        name = info.get("nickname", f"cid{m['cid']}")
        flags = []
        if m["n_oob"]:
            flags.append(f"{m['n_oob']} OOB")
        if m["suspect"]:
            flags.append(f"{len(m['suspect'])} SUSPECT")
        if m["unresolved"]:
            flags.append(f"{len(m['unresolved'])} unresolved")
        if m["dpp"] is None:
            flags.append("NO-MULT")
        flagstr = "; ".join(flags) if flags else "—"
        L.append(
            f"| {name} | {m['n_entries']} | {m['n_open']} | "
            f"{m['n_clean']}/{m['n_oob']} | {_fmt(m['win_rate'],0)} | "
            f"{_fmt_money(m['realized_clean'])} | {_fmt_money(m['realized'])} | "
            f"{_fmt_money(m['unrealized'])} | {_fmt(m['firing_rate'],0)} | "
            f"{flagstr} |")
    L.append("")

    # Per-model detail
    for m in models:
        info = m["info"] or {}
        name = info.get("nickname", f"cid{m['cid']}")
        L.append("---")
        L.append("")
        L.append(f"### {name}  (client_id {m['cid']})")
        exec_sym = m["execution_symbol"]
        note = info.get("multiplier_note")
        mult_str = (f"execution_symbol=**{exec_sym}** "
                    f"(${_fmt(m['dpp'],2)}/pt)" if m["dpp"] is not None
                    else f"execution_symbol={exec_sym!r} — **MULTIPLIER UNKNOWN**")
        L.append(f"- Instrument: {mult_str}"
                 + (f" — _{note}_" if note else ""))
        L.append(f"- Trades: **{m['n_entries']}** entries "
                 f"({m['n_closed']} closed, {m['n_open']} open) | "
                 f"Long {m['longs']} / Short {m['shorts']} | "
                 f"clean {m['n_clean']} / OOB {m['n_oob']}")
        L.append(f"- Exit reasons: "
                 + (", ".join(f"{k}={v}" for k, v in sorted(m["exit_reasons"].items()))
                    or "—"))
        L.append(f"- Win rate (clean set): {_fmt(m['win_rate'],0)}% "
                 f"({m['wins']}W / {m['losses']}L of {m['n_clean']} clean trades)")
        L.append(f"- Avg hold: {_fmt(m['avg_bars_held'],1)} bars")
        L.append(f"- **Realized PnL — clean (SL/TP):** "
                 f"{_fmt_money(m['realized_clean'])} ({m['n_clean']} trades)")
        L.append(f"- Realized PnL — all valued (incl. OOB): "
                 f"{_fmt_money(m['realized'])} ({m['valid_closed']} trades)")
        if m.get("pre_cutover"):
            L.append(f"- ⚠️  {m['pre_cutover']} entry(ies) predate the master-API "
                     f"fix — contamination-risk, verify vs broker")
        if m["best"]:
            L.append(f"    - best: {_fmt_money(m['best'][0])} "
                     f"({m['best'][1]['side']} {m['best'][1]['close_reason']})")
        if m["worst"]:
            L.append(f"    - worst: {_fmt_money(m['worst'][0])} "
                     f"({m['worst'][1]['side']} {m['worst'][1]['close_reason']})")
        if m["n_open"]:
            mk = "ok" if m["unrealized_ok"] else "PARTIAL (missing marks)"
            L.append(f"- **Unrealized PnL:** {_fmt_money(m['unrealized'])} "
                     f"(mark-to-last-bar, {mk})")
            for p in m["open_pos"]:
                L.append(f"    - OPEN {p['side']} qty {p['quantity']} @ "
                         f"{p['entry_price']} (entered {p['entry_time']})")
        if m["comm_n"]:
            L.append(f"- Commissions: {_fmt_money(m['comm_total'])} "
                     f"({m['comm_n']} events)")
        L.append("")
        # Signal / decision behavior — the data-stream health tell
        L.append("- **Signal / decision distribution** "
                 "(if data streams are healthy this should mirror the backtest "
                 "firing balance):")
        L.append(f"    - firing rate (EXECUTE / decisions): "
                 f"{_fmt(m['firing_rate'],1)}%  "
                 f"[healthy band ≈ 15–70%]")
        L.append("    - actions: "
                 + (", ".join(f"{k}={v}" for k, v in sorted(m["actions"].items()))
                    or "—"))
        L.append(_conf_line("confidence (all decisions)", m["conf_all"]))
        L.append(_conf_line("confidence (at EXECUTE)", m["conf_exec"]))
        # Data-quality detail
        for p in m["suspect"]:
            L.append(f"- ⚠️  SUSPECT exit price (excluded from PnL): "
                     f"{p['side']} entry {p['entry_price']} → exit "
                     f"{p['exit_price']} ({p['close_reason']}) — implausible "
                     f"move, likely a bad print / cross-contaminated row")
        for p in m["unresolved"]:
            L.append(f"- ⚠️  Unresolved exit (excluded from PnL): "
                     f"{p['side']} entry {p['entry_price']} → exit "
                     f"{p['exit_price']} ({p['close_reason']})")
        L.append("")

    # Cross-checks
    if unmapped_cids:
        L.append("---")
        L.append("")
        L.append("## ⚠️ Client IDs in DB not in the fleet manifest")
        L.append("")
        L.append("These traded but no config maps them — PnL used the DB's "
                 "brain-symbol multiplier as a fallback (may be wrong for "
                 "micros). Add them to the manifest or confirm they are stale:")
        for cid in sorted(unmapped_cids):
            L.append(f"- client_id {cid}")
        L.append("")

    # Bar freshness footer
    L.append("---")
    L.append("")
    L.append("## Data freshness (newest bar per symbol)")
    L.append("")
    for sym, ts in sorted(last_bars.items()):
        age = None
        pts = _parse_ts(ts)
        if pts:
            age = (now - pts).total_seconds() / 60.0
        L.append(f"- {sym}: {ts}"
                 + (f"  ({age:.0f} min ago)" if age is not None else ""))
    L.append("")
    L.append("> Interpreting this report: a healthy live stream should keep "
             "each model's **firing rate** and **confidence distribution** in "
             "the same ballpark as its backtest. A model that stops firing, "
             "fires on ~every bar, or whose confidence collapses toward 50 is "
             "the signature of a bad/stale data stream — investigate that "
             "model's feed before trusting its PnL. Compare trade cadence "
             "(trades/day) and long/short balance against the backtest for the "
             "same window.")
    L.append("")
    L.append("## How long until live results are trustworthy "
             "(/model-detective heuristics)")
    L.append("")
    L.append("PnL is the **slowest** signal and the last to trust — over a few "
             "days of 1–3 decisive trades per model it is exit/vol-timing luck, "
             "not edge (a month carried by 1–3 trades means nothing). Validate "
             "in this order; every metric tightens ~√N, so the post-fix clean "
             "window is the real T-zero:")
    L.append("")
    L.append("| Validate | Sample needed | ~Live time | Verdict it earns |")
    L.append("|---|---|---|---|")
    L.append("| Probability distribution vs backtest (KS per model) | "
             "~150–200 decisions/model | **~2 weeks** (start now) | "
             "data streams are good |")
    L.append("| Firing rate / trade cadence | ~30–50 trades, ~400 decisions | "
             "~3–4 weeks | behavior matches backtest |")
    L.append("| Win rate / exit-reason mix | ~100 trades/model | ~2–3 months | "
             "directional/exit behavior |")
    L.append("| PnL / Sharpe | ~200–400 trades/model | ~3–6+ months | "
             "dollar performance (confirmatory only) |")
    L.append("")
    L.append("> At the current ~1.5–2.3 trades/day/model, the data-stream "
             "question you care about is answerable in ~2–4 weeks; do **not** "
             "gate on live PnL/Sharpe (months). The fastest, highest-signal "
             "check — live probability distribution vs the backtest's — is "
             "meaningful within ~2 weeks and can start now.")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Re-runnable fleet trade & PnL report from the live "
                    "telemetry DB (read-only).")
    parser.add_argument("--db", default=None,
                        help="Path to fleet_telemetry.db (default: data root)")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                        help="Fleet manifest (maps client_id → execution "
                             "symbol/multiplier)")
    parser.add_argument("--since", default="2026-07-06",
                        help="Only count trades entered on/after this "
                             "date/datetime (default: 2026-07-06)")
    parser.add_argument("--until", default=None,
                        help="Only count trades entered on/before this "
                             "date/datetime (default: now)")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"Markdown report path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--contamination-cutover",
                        default=DEFAULT_CONTAMINATION_CUTOVER,
                        help="UTC instant the master-API cross-client leak was "
                             "stopped; entries before it are flagged "
                             "contamination-risk (default: "
                             f"{DEFAULT_CONTAMINATION_CUTOVER}). Pass 'none' to "
                             "disable the caveat.")
    args = parser.parse_args(argv)

    db_path = args.db or _default_db_path()
    if not Path(db_path).exists():
        print(f"ERROR: fleet DB not found: {db_path}", file=sys.stderr)
        return 1

    since = _parse_ts(args.since)
    until = _parse_ts(args.until) if args.until else None
    if since is None:
        print(f"ERROR: could not parse --since {args.since!r}", file=sys.stderr)
        return 1
    cutover = None
    if args.contamination_cutover and args.contamination_cutover.lower() != "none":
        cutover = _parse_ts(args.contamination_cutover)
        if cutover is None:
            print(f"ERROR: could not parse --contamination-cutover "
                  f"{args.contamination_cutover!r}", file=sys.stderr)
            return 1

    client_map = load_client_map(Path(args.manifest))
    positions, ledger, last_close, last_bars, commissions = read_fleet(db_path)

    # Every client_id that appears in the DB OR the manifest.
    db_cids = {p["client_id"] for p in positions} | {r["client_id"] for r in ledger}
    all_cids = sorted(db_cids | set(client_map.keys()))
    unmapped = db_cids - set(client_map.keys())

    models = []
    for cid in all_cids:
        info = client_map.get(cid)
        if info is None:
            # Fallback: value with the DB brain symbol (flagged as unmapped).
            sym = next((p["symbol"] for p in positions if p["client_id"] == cid),
                       None)
            info = {"nickname": f"cid{cid} ({sym or '?'})",
                    "execution_symbol": sym, "dpp": None,
                    "multiplier_note": "not in manifest — fallback multiplier"}
            try:
                info["dpp"] = dollars_per_point(sym)
            except Exception:
                pass
        m = analyze_model(cid, info, positions, ledger, commissions,
                          last_close, since, until, cutover)
        # Skip client_ids with zero activity in the window (e.g. an enabled
        # config that hasn't traded yet still gets a line so it's not silently
        # absent).
        if m["n_entries"] == 0 and m["n_decisions"] == 0:
            if info.get("enabled"):
                m["_idle"] = True
            else:
                continue
        models.append(m)

    report = format_report(models, db_path, since, until, last_bars, unmapped,
                           cutover)

    print(report)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\n[saved] {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
