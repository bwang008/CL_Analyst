# Ticket Resolution Blueprint

## Bug Summary
The `NameError: name 'ctx' is not defined` in `_place_bracket_children_on_fill` was caused by an accidental removal of the `ctx` variable assignment (`ctx = self._last_decision_context_by_order_id[order_id]`) in a previous commit, leading to a failure when trying to access `ctx.get("tp_offset")`.

## Target Files
- `src/live_execution/live_trader.py`

## Required Changes
In `src/live_execution/live_trader.py`, locate the `_place_bracket_children_on_fill` method. Right after the block that checks if `order_id` is not in `self._last_decision_context_by_order_id`, and before `tp_offset = ctx.get("tp_offset")`, restore the missing assignment: `ctx = self._last_decision_context_by_order_id[order_id]`.
