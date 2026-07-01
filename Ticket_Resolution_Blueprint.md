# Ticket Resolution Blueprint

## Bug Summary
The 4 execution guard tests failed during the full suite run (`task-97`) due to a global logger mutation leaking during test collection. The `agent/strategy_optimizer.py` and `agent/batch_post_optimizer.py` modules contained a global `setLevel(logging.ERROR)` call on the `src.live_execution.execution_guard` logger. When `pytest` collected the tests, these modules were imported, which permanently silenced the logger across the entire test session. Consequently, tests in `test_execution_guard.py` that asserted expected `WARNING` logs failed.

## Target Files
- `agent/strategy_optimizer.py`
- `agent/batch_post_optimizer.py`

## Required Changes
1. Remove the global `logging.getLogger("src.live_execution.execution_guard").setLevel(logging.ERROR)` calls from both files.
2. Relocate these calls inside the respective `if __name__ == "__main__":` blocks of both files. This ensures the logger is only suppressed when the scripts are executed directly, preventing test state pollution.
