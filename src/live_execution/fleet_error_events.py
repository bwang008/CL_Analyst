"""
Fleet Error Event Writer — crash → structured JSON event for AI triage.

When the FleetRunner detects a dead child, this component captures the
child's traceback (from a per-instance stderr sink file), classifies it
against a maintained collection of known INFRASTRUCTURE error signatures
(IBKR connectivity flaps etc.), deduplicates by normalized-traceback hash,
and drops a structured JSON event into the file-based error queue:

    .agents/collab/error_queue/
    ├── pending/        <- new events land here (this module writes these)
    ├── processing/     <- error_watcher.ps1 moves events here for the agent
    ├── done/           <- completed events (audit trail)
    ├── infra_patterns.json  <- growable collection of infra signatures
    └── audit_log.md    <- append-only log of every fix/resolution

Design constraints honored here:
- Children's stderr is redirected to per-instance FILES, never PIPE — a
  long-running child writing to an unread pipe would fill the OS buffer
  and deadlock the whole fleet.
- This module is stdlib-only at import time (fleet_runner.py rule); the
  optional Telegram alerter is duck-typed and injected by main().
- No silent null defaults: a missing infra_patterns.json RAISES at
  construction (before any child is spawned), it does not degrade quietly.

Protocol details: .agents/collab/error_queue/README.md
Agent workflow:   .agents/skills/fleet-error-monitor/SKILL.md
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("FleetErrorEvents")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_QUEUE_DIR = _PROJECT_ROOT / ".agents" / "collab" / "error_queue"
DEFAULT_STDERR_DIR = _PROJECT_ROOT / "reports" / "fleet_stderr"
DEFAULT_INFRA_PATTERNS_PATH = DEFAULT_QUEUE_DIR / "infra_patterns.json"

QUEUE_SUBDIRS = ("pending", "processing", "done")

# Rotate a stderr sink once it grows past this (children log to stderr).
_STDERR_ROTATE_BYTES = 10 * 1024 * 1024
# How much of the sink tail to scan for the traceback.
_TAIL_READ_BYTES = 64 * 1024
_TRACEBACK_MARKER = "Traceback (most recent call last):"
_MAX_TRACEBACK_LINES = 200
_NO_MARKER_TAIL_LINES = 40

EVENT_SCHEMA_VERSION = 1

# Volatile tokens stripped before hashing so a crash loop produces ONE
# event, not one per restart: memory addresses and datetime-ish strings
# (log timestamps, "no bar since 2026-07-05 10:00" style messages).
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]?[\d:.,]*")
_WS_RE = re.compile(r"\s+")


def load_infra_patterns(path):
    """Load the infra-signature collection. RAISES if missing/invalid
    (project rule: no silent null defaults — the file ships in the repo).

    Returns a list of {"name": str, "regex": str, "notes": str} dicts.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Infra pattern collection not found: {path}. It ships in the "
            f"repo (.agents/collab/error_queue/infra_patterns.json) — "
            f"restore it; refusing to classify blindly."
        )
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if "patterns" not in payload or not isinstance(payload["patterns"], list):
        raise ValueError(
            f"{path} must contain a top-level 'patterns' list."
        )
    for i, entry in enumerate(payload["patterns"]):
        for key in ("name", "regex"):
            if key not in entry:
                raise ValueError(
                    f"{path} pattern #{i} is missing required key "
                    f"'{key}': {entry!r}"
                )
        re.compile(entry["regex"])  # fail fast on a broken regex
    return payload["patterns"]


def classify_traceback(traceback_text, patterns):
    """Match the traceback against the infra collection.

    Returns ("infrastructure", <pattern name>) on first match, else
    ("unknown", None). "unknown" means: needs agent investigation.
    """
    for entry in patterns:
        if re.search(entry["regex"], traceback_text,
                     re.IGNORECASE | re.DOTALL):
            return "infrastructure", entry["name"]
    return "unknown", None


def normalize_traceback(traceback_text):
    """Strip volatile tokens (addresses, datetimes, whitespace runs) so the
    same underlying crash hashes identically across restarts."""
    text = _HEX_ADDR_RE.sub("0xADDR", traceback_text)
    text = _DATETIME_RE.sub("<DT>", text)
    return _WS_RE.sub(" ", text).strip()


def traceback_hash(model_name, traceback_text):
    """Deduplication key: 12 hex chars over model + normalized traceback."""
    normalized = f"{model_name}|{normalize_traceback(traceback_text)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def extract_traceback(stderr_path):
    """Pull the LAST Python traceback block from a stderr sink file.

    Falls back to the last few lines when no traceback marker exists
    (e.g. the child was killed or exited via sys.exit with a log line).
    """
    stderr_path = Path(stderr_path)
    if not stderr_path.exists():
        return "<no stderr captured>"
    try:
        size = stderr_path.stat().st_size
        with open(stderr_path, "rb") as fh:
            if size > _TAIL_READ_BYTES:
                fh.seek(size - _TAIL_READ_BYTES)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError as e:
        return f"<stderr unreadable: {type(e).__name__}: {e}>"
    if not tail.strip():
        return "<no stderr captured>"

    lines = tail.splitlines()
    marker_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(_TRACEBACK_MARKER):
            marker_idx = i
            break
    if marker_idx is None:
        block = lines[-_NO_MARKER_TAIL_LINES:]
        return ("<no Python traceback marker found; last stderr lines:>\n"
                + "\n".join(block))
    return "\n".join(lines[marker_idx:marker_idx + _MAX_TRACEBACK_LINES])


class FleetErrorEventWriter:
    """Writes crash events into the file-based error queue.

    Injected into FleetRunner as `error_writer`; when absent (None) the
    runner behaves exactly as before — the Strict-Locked supervision tests
    exercise that path untouched.
    """

    def __init__(self, queue_dir, stderr_dir, manifest_path,
                 patterns_path, telegram=None):
        self.queue_dir = Path(queue_dir)
        self.stderr_dir = Path(stderr_dir)
        self.manifest_path = str(manifest_path)
        # Raises on missing/broken collection BEFORE any child is spawned.
        self.patterns = load_infra_patterns(patterns_path)
        self.patterns_path = Path(patterns_path)
        self.telegram = telegram  # duck-typed: .send(str) -> bool, or None

        for sub in QUEUE_SUBDIRS:
            (self.queue_dir / sub).mkdir(parents=True, exist_ok=True)
        self.stderr_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # stderr sinks (given to Popen(stderr=...) by FleetRunner._spawn)
    # ------------------------------------------------------------------
    def stderr_path_for(self, instance_name):
        return self.stderr_dir / f"{instance_name}.stderr.log"

    def open_stderr_sink(self, instance_name):
        """Open (append) the per-instance stderr file, rotating if huge.

        Returned handle is passed to Popen as stderr= so the CHILD writes
        directly to the file via the inherited descriptor — the parent
        never reads a pipe, so there is nothing to deadlock.
        """
        path = self.stderr_path_for(instance_name)
        try:
            if path.exists() and path.stat().st_size > _STDERR_ROTATE_BYTES:
                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except OSError:
            log.warning("Could not rotate stderr sink %s", path, exc_info=True)
        return open(path, "ab")

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------
    def _event_path(self, instance_name, tb_hash, subdir):
        return self.queue_dir / subdir / f"{instance_name}_{tb_hash}.json"

    @staticmethod
    def _atomic_write(path, payload):
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)

    def emit_crash_event(self, instance, exit_code, gave_up):
        """Queue a structured crash event (or update the pending duplicate).

        Dedup policy (same model + same normalized traceback):
        - already in pending/    -> update restart_count / gave_up in place
        - already in processing/ -> skip (agent is on it)
        - only in done/          -> NEW event (a recurrence after a "fix"
                                    must re-open investigation)

        Never raises: an error-pipeline failure must not take down the
        supervisor it exists to observe.
        """
        try:
            return self._emit(instance, exit_code, gave_up)
        except Exception:
            log.error(
                "Failed to emit crash event for %s — supervision continues.",
                getattr(instance, "name", "?"), exc_info=True,
            )
            return None

    def _emit(self, instance, exit_code, gave_up):
        stderr_path = self.stderr_path_for(instance.name)
        traceback_text = extract_traceback(stderr_path)
        tb_hash = traceback_hash(instance.name, traceback_text)
        classification, matched = classify_traceback(
            traceback_text, self.patterns
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        processing = self._event_path(instance.name, tb_hash, "processing")
        if processing.exists():
            log.info(
                "Crash event %s already in processing/ — agent is on it; "
                "not re-queueing.", processing.name,
            )
            return None

        pending = self._event_path(instance.name, tb_hash, "pending")
        if pending.exists():
            with open(pending, "r", encoding="utf-8") as fh:
                event = json.load(fh)
            newly_gave_up = gave_up and not event.get("gave_up", False)
            event["restart_count"] = instance.restarts
            event["exit_code"] = exit_code
            event["gave_up"] = gave_up
            event["last_seen"] = now_iso
            event["occurrences"] = event.get("occurrences", 1) + 1
            self._atomic_write(pending, event)
            log.info(
                "Crash event %s deduplicated (occurrence #%d, gave_up=%s).",
                pending.name, event["occurrences"], gave_up,
            )
            if newly_gave_up:
                self._notify(event, updated=True)
            return pending

        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": f"{instance.name}_{tb_hash}",
            "timestamp": now_iso,
            "last_seen": now_iso,
            "occurrences": 1,
            "model_name": instance.name,
            "config_path": str(instance.config_path),
            "client_id": instance.client_id,
            "exit_code": exit_code,
            "restart_count": instance.restarts,
            "gave_up": gave_up,
            "traceback": traceback_text,
            "traceback_hash": tb_hash,
            "classification": classification,
            "matched_infra_pattern": matched,
            "fleet_manifest_path": self.manifest_path,
            "stderr_log_path": str(stderr_path),
        }
        self._atomic_write(pending, event)
        log.warning(
            "Crash event queued: %s (classification=%s%s)",
            pending.name, classification,
            f", pattern={matched}" if matched else "",
        )
        self._notify(event, updated=False)
        return pending

    def _notify(self, event, updated):
        if self.telegram is None:
            return
        headline = ("🛑 restart cap exhausted" if event["gave_up"]
                    else "🚨 Fleet child crashed")
        if event["classification"] == "infrastructure":
            triage = (f"🔌 Classified INFRASTRUCTURE "
                      f"(`{event['matched_infra_pattern']}`) — no ticket, "
                      f"filed for the record.")
        else:
            triage = "🤖 Error event queued for AI triage."
        suffix = " (recurring crash, event updated)" if updated else ""
        self.telegram.send(
            f"{headline}: *{event['model_name']}* "
            f"(exit {event['exit_code']}, "
            f"restarts {event['restart_count']}){suffix}\n"
            f"{triage}\nEvent: `{event['event_id']}`"
        )
