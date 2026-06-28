import re

with open('tests/test_rollover.py', 'r') as f:
    text = f.read()

text = text.replace('TestComputeRollDelta', 'TestComputeRollRatio')
text = text.replace('test_empty_cache_returns_none', 'test_empty_cache_returns_none')
text = text.replace('test_correct_delta_computed', 'test_correct_ratio_computed')
text = text.replace('_compute_roll_delta', '_compute_roll_ratio')
text = text.replace('TestBackAdjustCache', 'TestApplyRollToCache')
text = text.replace('_back_adjust_cache', '_apply_roll_to_cache')
text = text.replace('test_ohlc_shifted_by_delta', 'test_ohlc_scaled_by_ratio')
text = text.replace('test_stores_roll_delta', 'test_stores_roll_ratio')
text = text.replace('delta = 2.50', 'ratio = 1.5')
text = text.replace('delta = 1.50', 'ratio = 1.5')
text = text.replace('delta = 2.75', 'ratio = 1.5')
text = text.replace('delta = 0.80', 'ratio = 1.2')
text = text.replace('delta = 0.0', 'ratio = 1.0')
text = text.replace('delta = ', 'ratio = ')
text = text.replace('delta)', 'ratio)')
text = text.replace('dm._roll_delta', 'dm._roll_ratios[-1]')
text = text.replace('cumulative_delta', 'cumulative_ratio')
text = text.replace('roll_delta', 'roll_ratio')
text = text.replace('original_close.iloc[0] + delta', 'original_close.iloc[0] * ratio')
text = text.replace('original_open.iloc[0] + delta', 'original_open.iloc[0] * ratio')
text = text.replace('2.30', '1.8')
text = text.replace('dm.ibkr_manager', 'dm.data_client')
text = text.replace('mock_manager = MagicMock()', 'mock_manager = MagicMock()\n        dm.data_client = mock_manager')

# We'll just replace 'delta' in specific comments as well.
text = text.replace('shifted by delta', 'scaled by ratio')
text = text.replace('zero delta noop', 'ratio of 1 noop')
text = text.replace('zero_delta_noop', 'ratio_one_noop')
text = text.replace('Delta of 0', 'Ratio of 1')
text = text.replace('ratio = 0.0', 'ratio = 1.0')
text = text.replace('0.0)', '1.0)')

with open('tests/test_rollover.py', 'w') as f:
    f.write(text)
