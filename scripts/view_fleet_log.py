#!/usr/bin/env python
"""View a fleet log with a blank line inserted between time intervals.

The fleet writes heartbeats from several independent bots into one shared
log, so they arrive one-after-another with no visual break. This is a
READ-TIME viewer: it inserts a single blank line whenever a log line crosses
into a new N-minute wall-clock bucket (default 5 min), grouping each interval
without any change to (or coordination between) the running bots.

Usage:
    python scripts/view_fleet_log.py                     # today's fleet log
    python scripts/view_fleet_log.py path/to/fleet.log   # a specific file
    python scripts/view_fleet_log.py -f                  # follow (tail -f)
    python scripts/view_fleet_log.py -i 1                # 1-minute buckets

Lines that do not start with a "YYYY-MM-DD HH:MM:SS" stamp (blank lines,
tracebacks, etc.) pass through untouched and never move the bucket.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FLEET_LOG_DIR = _PROJECT_ROOT / "reports" / "fleet"
_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def _default_log() -> Path | None:
    """Most recently modified reports/fleet/fleet_*.log, if any."""
    logs = sorted(
        _FLEET_LOG_DIR.glob("fleet_*.log"),
        key=lambda p: p.stat().st_mtime,
    )
    return logs[-1] if logs else None


class _Spacer:
    """Emits a blank line when a line crosses into a new interval bucket."""

    def __init__(self, interval_min: int, out) -> None:
        self._bucket_secs = max(1, interval_min) * 60
        self._last_bucket: int | None = None
        self._out = out

    def feed(self, line: str) -> None:
        m = _TS_RE.match(line)
        if m:
            y, mo, d, h, mi, s = (int(g) for g in m.groups())
            bucket = int(datetime(y, mo, d, h, mi, s).timestamp()) // self._bucket_secs
            if self._last_bucket is not None and bucket != self._last_bucket:
                self._out.write("\n")
            self._last_bucket = bucket
        self._out.write(line if line.endswith("\n") else line + "\n")
        self._out.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logfile", nargs="?", help="log file (default: today's fleet log)")
    parser.add_argument("-i", "--interval", type=int, default=5,
                        help="minutes per group (default 5)")
    parser.add_argument("-f", "--follow", action="store_true",
                        help="keep reading as the log grows (tail -f)")
    args = parser.parse_args(argv)

    path = Path(args.logfile) if args.logfile else _default_log()
    if path is None or not path.exists():
        sys.stderr.write(f"log not found: {path}\n")
        return 1

    spacer = _Spacer(args.interval, sys.stdout)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            spacer.feed(line)
        if not args.follow:
            return 0
        try:
            while True:
                line = fh.readline()
                if line:
                    spacer.feed(line)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
