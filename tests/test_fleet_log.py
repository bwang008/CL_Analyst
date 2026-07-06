"""
Tests for the shared daily fleet log (src/live_execution/fleet_log.py).

Design under test (Windows-safe rotation): the DATE IS IN THE FILENAME —
rolling means opening the new day's file, never renaming (rename of an
open file fails with WinError 32 under a 24/7 multi-process fleet), and
the retention sweep only deletes old-date files nobody has open.
"""

import logging
from datetime import datetime

from src.live_execution.fleet_log import (
    DailyFleetLogHandler,
    setup_fleet_logging,
)


class Clock:
    """Injectable now_fn so tests control the date."""

    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def make_logger(name, handler):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers = [handler]
    return logger


def make_handler(tmp_path, tag, clock, retention_days=7):
    handler = DailyFleetLogHandler(
        log_dir=tmp_path, retention_days=retention_days, now_fn=clock,
    )
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s [%(levelname)s] [{tag}] %(name)s: %(message)s"
    ))
    return handler


class TestDailyFleetLogHandler:

    def test_writes_dated_file_with_source_tag(self, tmp_path):
        clock = Clock(datetime(2026, 7, 6, 9, 5))
        handler = make_handler(tmp_path, "CL cid=1400", clock)
        logger = make_logger("t_tag", handler)

        logger.info("bar closed")
        handler.close()

        path = tmp_path / "fleet_20260706.log"
        assert path.exists(), "log must land in the DATED filename"
        content = path.read_text(encoding="utf-8")
        assert "[CL cid=1400]" in content
        assert "bar closed" in content

    def test_rolls_to_new_dated_file_without_renaming(self, tmp_path):
        clock = Clock(datetime(2026, 7, 6, 23, 59))
        handler = make_handler(tmp_path, "FLEET", clock)
        logger = make_logger("t_roll", handler)

        logger.info("before midnight")
        clock.dt = datetime(2026, 7, 7, 0, 1)
        logger.info("after midnight")
        handler.close()

        day1 = (tmp_path / "fleet_20260706.log").read_text(encoding="utf-8")
        day2 = (tmp_path / "fleet_20260707.log").read_text(encoding="utf-8")
        assert "before midnight" in day1 and "after midnight" not in day1
        assert "after midnight" in day2 and "before midnight" not in day2

    def test_retention_sweep_deletes_only_expired_files(self, tmp_path):
        # 7-day retention on 2026-07-06 keeps 06-30..07-06, drops older.
        (tmp_path / "fleet_20260629.log").write_text("expired")
        (tmp_path / "fleet_20260630.log").write_text("oldest kept")
        (tmp_path / "unrelated.log").write_text("not ours")
        clock = Clock(datetime(2026, 7, 6, 8, 0))
        handler = make_handler(tmp_path, "FLEET", clock, retention_days=7)
        logger = make_logger("t_sweep", handler)

        logger.info("triggers open + sweep")
        handler.close()

        assert not (tmp_path / "fleet_20260629.log").exists()
        assert (tmp_path / "fleet_20260630.log").exists()
        assert (tmp_path / "unrelated.log").exists(), \
            "sweep must only touch fleet_*.log files"

    def test_appends_never_truncates(self, tmp_path):
        path = tmp_path / "fleet_20260706.log"
        path.write_text("earlier process line\n", encoding="utf-8")
        clock = Clock(datetime(2026, 7, 6, 12, 0))
        handler = make_handler(tmp_path, "ES cid=1404", clock)
        logger = make_logger("t_append", handler)

        logger.info("new line")
        handler.close()

        content = path.read_text(encoding="utf-8")
        assert "earlier process line" in content
        assert "new line" in content

    def test_two_writers_interleave_into_one_file(self, tmp_path):
        """Two handlers on the same dir = two fleet processes appending."""
        clock = Clock(datetime(2026, 7, 6, 12, 0))
        h_cl = make_handler(tmp_path, "CL cid=1400", clock)
        h_es = make_handler(tmp_path, "ES cid=1404", clock)
        log_cl = make_logger("t_two_cl", h_cl)
        log_es = make_logger("t_two_es", h_es)

        log_cl.info("CL inference done")
        log_es.info("ES inference done")
        h_cl.close()
        h_es.close()

        content = (tmp_path / "fleet_20260706.log").read_text(encoding="utf-8")
        assert "[CL cid=1400]" in content and "CL inference done" in content
        assert "[ES cid=1404]" in content and "ES inference done" in content


class TestSetupFleetLogging:

    def test_production_dir_refused_under_pytest(self):
        """Tests that reach setup_fleet_logging via cli.main() must NEVER
        write into reports/fleet/ — fixture ERROR lines polluted the real
        operator log on 2026-07-06."""
        from src.live_execution.fleet_log import DEFAULT_FLEET_LOG_DIR
        root = logging.getLogger()
        before = list(root.handlers)

        handler = setup_fleet_logging("CL cid=7",
                                      log_dir=DEFAULT_FLEET_LOG_DIR)

        assert handler is None
        assert root.handlers == before, "nothing may be attached"

    def test_recall_replaces_stale_handler_not_stacks(self, tmp_path):
        """A leaked handler keeps tagging later records with the OLD cid —
        re-calls must swap the handler, not accumulate."""
        root = logging.getLogger()
        h1 = setup_fleet_logging("CL cid=7", log_dir=tmp_path,
                                 now_fn=Clock(datetime(2026, 7, 6, 12, 0)))
        h2 = setup_fleet_logging("ES cid=2000", log_dir=tmp_path,
                                 now_fn=Clock(datetime(2026, 7, 6, 12, 0)))
        try:
            fleet_handlers = [h for h in root.handlers
                              if isinstance(h, DailyFleetLogHandler)]
            assert fleet_handlers == [h2], \
                "exactly one fleet handler (the latest) may be attached"
        finally:
            root.removeHandler(h2)
            h2.close()

    def test_attaches_tagged_handler_to_root(self, tmp_path):
        root = logging.getLogger()
        handler = setup_fleet_logging(
            "GC cid=1408", log_dir=tmp_path,
            now_fn=Clock(datetime(2026, 7, 6, 12, 0)),
        )
        try:
            assert handler in root.handlers
            assert handler.level == logging.INFO
            # Under pytest the root level is WARNING (production sets INFO
            # via live_trader's basicConfig) — give the probe an explicit
            # level so the record reaches root handlers.
            probe = logging.getLogger("SetupProbe")
            probe.setLevel(logging.INFO)
            probe.info("hello fleet")
            content = (tmp_path / "fleet_20260706.log").read_text(
                encoding="utf-8")
            assert "[GC cid=1408]" in content and "hello fleet" in content
        finally:
            root.removeHandler(handler)
            handler.close()
