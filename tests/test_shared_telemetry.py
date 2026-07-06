"""
Tests for the SHARED fleet telemetry DB (telemetry.py fleet mode).

One SQLite file, many bots: every write stamped (symbol, client_id),
every read scoped to the owning bot. The dangers these tests pin down:

- timestamp-only UNIQUE + INSERT OR IGNORE silently DROPPED the second
  symbol's rows -> v2 rekeys to (symbol, timestamp) / (client_id, timestamp)
- trade_ids come from per-connection IBKR order ids ("trade_5") and DO
  collide across bots -> unscoped active_positions would let a bot adopt
  or clobber a fleet-mate's position during reconnect recovery
- opening a file in the wrong mode must RAISE, never silently return
  empty scoped results (no silent null defaults)

Legacy (unbound) mode stays byte-identical — tests/test_telemetry.py,
test_shadow_log.py, test_tradebook_logging.py remain authoritative there.
"""

import pytest

from src.live_execution.telemetry import TelemetryDB


TS1 = "2026-07-06T09:00:00"
TS2 = "2026-07-06T10:00:00"


def bot(tmp_path, symbol="CL", client_id=1400):
    return TelemetryDB(str(tmp_path / "fleet_telemetry.db"),
                       symbol=symbol, client_id=client_id)


def log_bar(db, ts=TS1):
    db.log_bar(ts, 62.0, 62.5, 61.5, 62.2, 1000.0)


# =============================================================================
# 1. MODE GUARDS (no silent cross-mode access)
# =============================================================================

class TestModeGuards:

    def test_partial_identity_raises(self, tmp_path):
        with pytest.raises(ValueError):
            TelemetryDB(str(tmp_path / "x.db"), symbol="CL")
        with pytest.raises(ValueError):
            TelemetryDB(str(tmp_path / "x.db"), client_id=1400)

    def test_fleet_mode_on_legacy_file_raises(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        legacy = TelemetryDB(path)  # creates legacy schema
        legacy.close()
        with pytest.raises(ValueError, match="LEGACY"):
            TelemetryDB(path, symbol="CL", client_id=1400)

    def test_legacy_mode_on_fleet_file_raises(self, tmp_path):
        fleet = bot(tmp_path)
        fleet.close()
        with pytest.raises(ValueError, match="FLEET"):
            TelemetryDB(str(tmp_path / "fleet_telemetry.db"))

    def test_busy_timeout_set_for_multi_process_writers(self, tmp_path):
        db = bot(tmp_path)
        timeout = db._get_conn().execute(
            "PRAGMA busy_timeout"
        ).fetchone()[0]
        assert timeout >= 5000


# =============================================================================
# 2. MARKET BARS (shared per symbol, never dropped across symbols)
# =============================================================================

class TestSharedBars:

    def test_same_timestamp_different_symbols_both_kept(self, tmp_path):
        cl = bot(tmp_path, "CL", 1400)
        es = bot(tmp_path, "ES", 1404)
        log_bar(cl)
        log_bar(es)  # same timestamp — the legacy schema DROPPED this row

        assert cl.bar_count() == 1
        assert es.bar_count() == 1
        assert cl.recent_bars(5)[0]["symbol"] == "CL"
        assert es.recent_bars(5)[0]["symbol"] == "ES"

    def test_same_symbol_bots_dedup_to_one_row(self, tmp_path):
        cl_a = bot(tmp_path, "CL", 1400)
        cl_b = bot(tmp_path, "CL", 1402)
        log_bar(cl_a)
        log_bar(cl_b)  # same market data — one row, not two

        assert cl_a.bar_count() == 1
        assert cl_b.bar_count() == 1

    def test_raw_bars_keyed_by_symbol_too(self, tmp_path):
        cl = bot(tmp_path, "CL", 1400)
        es = bot(tmp_path, "ES", 1404)
        cl.log_raw_bar(TS1, 62.0, 62.5, 61.5, 62.2, 1000.0, "202608")
        es.log_raw_bar(TS1, 6200.0, 6250.0, 6150.0, 6220.0, 500.0, "202608")

        assert cl.raw_bar_count() == 1
        assert es.raw_bar_count() == 1


# =============================================================================
# 3. STRATEGY TABLES (scoped by client_id)
# =============================================================================

class TestScopedStrategyTables:

    def test_signals_scoped_per_bot(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "ES", 1404)
        a.log_signal(TS1, "Buy", 61.0, "EXECUTE")
        a.log_signal(TS2, "Hold", 40.0, "HOLD")
        b.log_signal(TS1, "Buy", 70.0, "EXECUTE")

        assert a.signal_count() == 2
        assert b.signal_count() == 1
        assert a.trade_count() == 1
        assert {r["client_id"] for r in a.recent_signals(10)} == {1400}
        summary = a.trade_summary()
        assert summary["total_signals"] == 2
        assert summary["executed_trades"] == 1

    def test_update_fill_ignores_other_bots_colliding_order_id(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "ES", 1404)
        # IBKR order ids are per-connection sequences: both bots have "5".
        a.log_signal(TS1, "Buy", 61.0, "EXECUTE", order_id=5)
        b.log_signal(TS1, "Buy", 70.0, "EXECUTE", order_id=5)

        a.update_fill(order_id=5, fill_price=62.15)

        assert a.recent_signals(1)[0]["fill_price"] == 62.15
        assert b.recent_signals(1)[0]["fill_price"] is None, \
            "bot A's fill must not touch bot B's order 5"

    def test_shadow_log_same_timestamp_both_bots_kept(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "CL", 1402)  # same symbol, different strategy
        a.log_shadow_state(TS1, 62.0, 62.5, 61.5, 62.2, 1000.0,
                           prob_buy=0.61, strategy_name="A")
        b.log_shadow_state(TS1, 62.0, 62.5, 61.5, 62.2, 1000.0,
                           prob_buy=0.44, strategy_name="B")

        assert a.shadow_log_count() == 1
        assert b.shadow_log_count() == 1

    def test_tradebook_scoped_and_symbol_stamped(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "ES", 1404)
        assert a.log_tradebook_event(
            event_id="evt-a1", event_type="ORDER_SUBMITTED",
            event_timestamp_utc=TS1,
        )

        events_a = a.recent_tradebook_events(10)
        assert len(events_a) == 1
        assert events_a[0]["client_id"] == 1400
        assert events_a[0]["symbol"] == "CL", \
            "bound mode must stamp the bot's symbol when caller omits it"
        assert b.recent_tradebook_events(10) == []
        assert b.read_tradebook() == []


# =============================================================================
# 4. ACTIVE POSITIONS (reconnect recovery must never cross bots)
# =============================================================================

class TestPositionIsolation:

    def test_bot_never_sees_fleet_mates_open_position(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "ES", 1404)
        a.open_position(trade_id="trade_5", side="LONG", quantity=1,
                        entry_price=62.0, entry_time=TS1)

        assert a.get_open_position() is not None
        assert b.get_open_position() is None, \
            "reconnect recovery must not adopt another bot's position"

    def test_colliding_trade_ids_do_not_clobber(self, tmp_path):
        # Both bots derive trade_id from their own IBKR order id 5.
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "ES", 1404)
        a.open_position(trade_id="trade_5", side="LONG", quantity=1,
                        entry_price=62.0, entry_time=TS1)
        b.open_position(trade_id="trade_5", side="SHORT", quantity=2,
                        entry_price=6200.0, entry_time=TS1)

        pos_a = a.get_open_position()
        pos_b = b.get_open_position()
        assert pos_a["side"] == "LONG" and pos_a["entry_price"] == 62.0
        assert pos_b["side"] == "SHORT" and pos_b["entry_price"] == 6200.0

    def test_close_and_updates_scoped_to_owner(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "ES", 1404)
        a.open_position(trade_id="trade_5", side="LONG", quantity=1,
                        entry_price=62.0, entry_time=TS1)
        b.open_position(trade_id="trade_5", side="SHORT", quantity=2,
                        entry_price=6200.0, entry_time=TS1)

        a.update_position_brackets("trade_5", tp_price=63.0, sl_price=61.0)
        a.update_position_sl("trade_5", new_sl_price=61.5)
        a.close_position("trade_5", reason="REASON_TP", close_time=TS2,
                         exit_price=63.0)

        assert a.get_open_position() is None
        pos_b = b.get_open_position()
        assert pos_b is not None, "closing A's trade_5 must not close B's"
        assert pos_b["sl_price"] is None, \
            "A's bracket updates must not touch B's row"

    def test_export_trade_ledger_scoped(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        b = bot(tmp_path, "ES", 1404)
        for db, price in ((a, 62.0), (b, 6200.0)):
            db.open_position(trade_id="trade_9", side="LONG", quantity=1,
                             entry_price=price, entry_time=TS1)
            db.close_position("trade_9", reason="REASON_TP", close_time=TS2,
                              exit_price=price + 1)

        ledger_a = a.export_trade_ledger()
        assert len(ledger_a) == 1
        assert ledger_a.iloc[0]["entry_price"] == 62.0

    def test_same_bot_or_replace_still_replaces_own_row(self, tmp_path):
        a = bot(tmp_path, "CL", 1400)
        a.open_position(trade_id="trade_5", side="LONG", quantity=1,
                        entry_price=62.0, entry_time=TS1)
        a.open_position(trade_id="trade_5", side="LONG", quantity=1,
                        entry_price=62.3, entry_time=TS2)  # re-open same id

        pos = a.get_open_position()
        assert pos["entry_price"] == 62.3
