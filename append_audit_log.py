import datetime
import os

log_file = "c:\\Users\\bwang\\Documents\\GitHub\\CL_Analyst_Development\\tdd_audit_log.md"
timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
log_entry = f"[{timestamp}] | TDD-TESTER | Created failing test test_unbound_local_error_on_zero_position in tests/test_live_trader_bugs.py to reproduce UnboundLocalError when tp_price_live is accessed with current_position=0.\n"

with open(log_file, "a", encoding="utf-8") as f:
    f.write(log_entry)
