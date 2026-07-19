"""
Shared fleet log — one dated file, every process appends, tagged lines.

All fleet processes (the runner + every child bot) append to the SAME
daily file, each line tagged with its source:

    reports/fleet/fleet_20260706.log
    2026-07-06 09:05:01 [INFO] [CL cid=1400] LiveTrader: bar closed ...
    2026-07-06 09:05:02 [INFO] [ES cid=1404] LiveTrader: bar closed ...
    2026-07-06 09:05:10 [WARNING] [FLEET] FleetRunner: child ES01B exited ...

Windows-safe rotation design (deliberate — do NOT "improve" this into a
rename-based rotate): on Windows a file cannot be renamed or deleted while
other processes hold open handles, so the classic single-name +
midnight-rename chop would fail with WinError 32 under a 24/7 fleet.
Instead the DATE IS IN THE FILENAME — at the date boundary each process
simply opens the new day's file (no rename, ever), and the retention sweep
deletes files older than `retention_days`, which by then nobody has open.

Multi-process append caveat: Python logging does not formally guarantee
multi-process writes to one file. Each record here is a single append-mode
write + flush, which is atomic enough on local NTFS that the worst case is
a rare cosmetically interleaved line. Acceptable for an operator log;
anything parity- or money-critical lives in the telemetry DB, not here.
Uncaught-traceback capture stays in the per-instance stderr sinks
(fleet_error_events.py) — crash attribution must never be interleaved.

Stdlib-only at import time (fleet_runner.py rule).
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from src.live_execution.ascii_safe import AsciiFormatter

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_FLEET_LOG_DIR = _PROJECT_ROOT / "reports" / "fleet"

DEFAULT_RETENTION_DAYS = 7

_LINE_FORMAT = "%(asctime)s [%(levelname)s] [{tag}] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_FILENAME_RE = re.compile(r"^fleet_(\d{8})\.log$")


class DailyFleetLogHandler(logging.Handler):
    """Appends tagged records to reports/fleet/fleet_YYYYMMDD.log.

    Rolls by REOPENING the new day's filename (never renaming — see module
    docstring) and sweeps files older than `retention_days` at each roll.
    `now_fn` is injectable for tests. Local time on purpose: this is the
    operator-facing log and the filename should match the operator's day.
    """

    def __init__(self, log_dir=DEFAULT_FLEET_LOG_DIR,
                 retention_days=DEFAULT_RETENTION_DAYS, now_fn=None):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.retention_days = int(retention_days)
        self._now = now_fn or datetime.now
        self._current_date = None   # "YYYYMMDD" of the open stream
        self._stream = None
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def current_path(self):
        return self.log_dir / f"fleet_{self._now():%Y%m%d}.log"

    def _roll_if_needed(self):
        today = f"{self._now():%Y%m%d}"
        if today == self._current_date and self._stream is not None:
            return
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
        self._stream = open(self.log_dir / f"fleet_{today}.log",
                            "a", encoding="utf-8")
        self._current_date = today
        self._sweep_expired(today)

    def _sweep_expired(self, today):
        """Delete fleet_*.log files older than retention_days. Another
        process may win the same unlink race — that is fine."""
        try:
            cutoff = sorted(
                self._recent_dates(today, self.retention_days)
            )[0]
        except (ValueError, IndexError):
            return
        for entry in self.log_dir.glob("fleet_*.log"):
            m = _FILENAME_RE.match(entry.name)
            if m and m.group(1) < cutoff:
                try:
                    os.remove(entry)
                except OSError:
                    pass

    def _recent_dates(self, today, n):
        from datetime import timedelta
        base = datetime.strptime(today, "%Y%m%d")
        return [f"{base - timedelta(days=i):%Y%m%d}" for i in range(n)]

    # ------------------------------------------------------------------
    def emit(self, record):
        # Handler.handle() holds self.lock around emit(): the roll check
        # and the single write+flush are thread-safe within this process;
        # cross-process safety comes from append mode (one write/record).
        try:
            self._roll_if_needed()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def setup_fleet_logging(tag, log_dir=DEFAULT_FLEET_LOG_DIR,
                        level=logging.INFO,
                        retention_days=DEFAULT_RETENTION_DAYS,
                        now_fn=None):
    """Attach the shared daily fleet log to the root logger.

    `tag` identifies the source on every line — children pass
    "<SYMBOL> cid=<client_id>", the runner passes "FLEET". Returns the
    handler (callers keep it only for tests/teardown), or None when
    skipped under pytest.

    Guards (both learned from test-suite pollution on 2026-07-06, when
    fixture ERROR lines appeared in the production log tagged with a
    stale cid):
    - Under pytest, the PRODUCTION log dir is off limits: tests that
      exercise cli.main() must never write reports/fleet/. Tests that
      target the handler itself pass an explicit tmp dir and are exempt.
    - Re-calls REPLACE any previously attached fleet handler instead of
      stacking: a leaked handler would keep logging every later record
      under the old tag.
    """
    if (Path(log_dir) == DEFAULT_FLEET_LOG_DIR
            and "PYTEST_CURRENT_TEST" in os.environ):
        return None

    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, DailyFleetLogHandler):
            root.removeHandler(existing)
            existing.close()

    handler = DailyFleetLogHandler(
        log_dir=log_dir, retention_days=retention_days, now_fn=now_fn,
    )
    handler.setLevel(level)
    handler.setFormatter(
        AsciiFormatter(_LINE_FORMAT.format(tag=tag), datefmt=_DATE_FORMAT)
    )
    root.addHandler(handler)
    return handler
