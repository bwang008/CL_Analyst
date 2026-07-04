# Ticket Resolution Blueprint — telegram-spam-tests_07042026_0247
**Ticket Directory:** `.agents/collab/tickets/telegram-spam-tests_07042026_0247/`

## Bug Summary
During test suite execution, the `test_objective_seed_offset.py` file sends live Telegram messages reporting cold-start warnings. This happens because the test invokes the Strategy Optimizer's `run_optimization` method (which internally uses a Telegram notification hook on fallback states) without mocking the `send_telegram` function. Since `_extract_warm_start_params` is mocked to return `None` in the test, it triggers the cold-start fallback branch, firing the warning to the un-mocked Telegram sender.

## Target Files
- `tests/test_objective_seed_offset.py`

## Required Changes
1. **Mock Telegram Sender:** In `tests/test_objective_seed_offset.py`, locate the `_run_optimization_capturing_seeds` function.
2. **Update Patches:** Add a patch for the `send_telegram` function to the `patches` list within that function to prevent actual network transmissions during test execution. It should look like: `patch.object(mod, "send_telegram", return_value=None)` where `mod` is the strategy optimizer module imported in the test.
