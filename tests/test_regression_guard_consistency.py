import pytest
from unittest.mock import MagicMock
import pandas as pd
import numpy as np

# These will fail initially until the Coder implements them
try:
    from agent.strategy_optimizer import _apply_trade_floor_penalty
except ImportError:
    # Dummy implementation so tests can be loaded and fail properly
    def _apply_trade_floor_penalty(raw_score, trade_count, trade_floor):
        raise NotImplementedError("Coder needs to implement _apply_trade_floor_penalty")

from agent.strategy_optimizer import (
    _compute_objective_score,
    _trade_floor_weight,
    make_objective,
    OBJECTIVE_SCORE_CAP
)

def test_apply_trade_floor_penalty_high_trades():
    # Acceptance Criteria 1: returns raw score unchanged when trade_count >= trade_floor
    raw_score = 3.0
    trade_count = 100
    trade_floor = 50.0
    
    penalized = _apply_trade_floor_penalty(raw_score, trade_count, trade_floor)
    assert penalized == raw_score

def test_apply_trade_floor_penalty_low_trades():
    # Acceptance Criteria 1: strictly smaller value when trade_count << trade_floor and raw_score > 0
    raw_score = 3.0
    trade_count = 5
    trade_floor = 50.0
    
    penalized = _apply_trade_floor_penalty(raw_score, trade_count, trade_floor)
    assert penalized < raw_score
    assert penalized > 0.0

def test_apply_trade_floor_penalty_negative_raw_score():
    # Acceptance Criteria 2: When raw_score <= 0, the penalty is a no-op
    raw_score = -1.5
    trade_count = 5
    trade_floor = 50.0
    
    penalized = _apply_trade_floor_penalty(raw_score, trade_count, trade_floor)
    assert penalized == raw_score

def simulate_guard(baseline_raw, baseline_trades, candidate_raw, candidate_trades, trade_floor):
    baseline_penalized = _apply_trade_floor_penalty(baseline_raw, baseline_trades, trade_floor)
    candidate_penalized = _apply_trade_floor_penalty(candidate_raw, candidate_trades, trade_floor)
    
    old_reverts = candidate_raw <= baseline_raw
    new_reverts = candidate_penalized <= baseline_penalized
    
    return old_reverts, new_reverts

def test_high_trade_baseline_regression_case():
    # Acceptance Criteria 3: HIGH-TRADE BASELINE REGRESSION CASE (must preserve today's behavior)
    trade_floor = 50.0
    
    # Baseline: High trades, decent score
    baseline_raw = 2.5
    baseline_trades = 100
    
    # Candidate: High trades, slightly lower score
    candidate_raw = 2.4
    candidate_trades = 120
    
    old_reverts, new_reverts = simulate_guard(
        baseline_raw, baseline_trades, 
        candidate_raw, candidate_trades, 
        trade_floor
    )
    
    # Both old and new logic should revert because candidate is just strictly worse
    assert old_reverts is True
    assert new_reverts is True

def test_low_trade_baseline_case():
    # Acceptance Criteria 4: LOW-TRADE BASELINE CASE (the behavior change)
    trade_floor = 50.0
    
    # Baseline: Low trades, artificially high score
    baseline_raw = 3.0
    baseline_trades = 5
    
    # Candidate: High trades, slightly lower raw score but genuinely good
    candidate_raw = 2.5
    candidate_trades = 100
    
    old_reverts, new_reverts = simulate_guard(
        baseline_raw, baseline_trades, 
        candidate_raw, candidate_trades, 
        trade_floor
    )
    
    # Old logic compares raw vs raw: 2.5 <= 3.0 -> reverts
    assert old_reverts is True
    
    # New logic compares penalized: 2.5 > (3.0 * tiny_weight) -> DOES NOT revert
    assert new_reverts is False

def create_mock_result(trade_count, num_months=12, mean_pnl=100, std_pnl=10):
    """Small synthetic BacktestResult factory for guard consistency testing."""
    mock_result = MagicMock()
    mock_result.trade_count = trade_count
    mock_result.total_pnl = 1000.0
    mock_result.profit_factor = 2.0
    mock_result.win_rate = 0.5
    mock_result.max_drawdown = -500.0
    mock_result.start_dt = pd.Timestamp("2020-01-01")
    mock_result.end_dt = pd.Timestamp("2021-01-01")
    mock_result.exit_distribution = {}
    
    trades = []
    base_dt = pd.Timestamp("2020-01-01")
    # Make deterministic monthly returns so Sharpe is consistent
    np.random.seed(42)
    # Generate pnls that will result in a positive Sharpe
    pnls = np.random.normal(loc=mean_pnl, scale=std_pnl, size=num_months)
    
    for i in range(num_months):
        dt = base_dt + pd.DateOffset(months=i)
        t = MagicMock()
        t.exit_dt = dt
        t.net_pnl_dollars = pnls[i]
        trades.append(t)
        
    mock_result.trades = trades
    return mock_result

def test_guard_consistency():
    # Acceptance Criteria 5: Guard consistency
    # For any given result, the score the guard uses equals the selection metric formula
    
    trade_floor = 50.0
    # The new signature of _compute_objective_score must take trade_floor to be consistent!
    # If the Coder hasn't updated it yet, this will fail or raise TypeError.
    
    mock_res = create_mock_result(trade_count=10) # low trades
    
    try:
        # Expected new signature: (result, metric, trade_floor)
        guard_score = _compute_objective_score(mock_res, "sharpe", trade_floor)
    except TypeError:
        pytest.fail("_compute_objective_score must be updated to accept trade_floor")
        
    # We must calculate what make_objective's objective() would return.
    # make_objective's objective applies _trade_floor_weight to the raw annualized score
    
    # 1. Compute raw manually to verify
    pnls = [t.net_pnl_dollars for t in mock_res.trades]
    std_pnl = np.std(pnls)
    mean_pnl = np.mean(pnls)
    raw_annualized = (mean_pnl / std_pnl) * np.sqrt(12)
    raw_annualized = min(raw_annualized, OBJECTIVE_SCORE_CAP)
    
    # 2. Compute expected penalized
    expected_penalized = _apply_trade_floor_penalty(raw_annualized, mock_res.trade_count, trade_floor)
    
    assert guard_score == expected_penalized, (
        f"Guard score {guard_score} does not match selection metric {expected_penalized}"
    )

def test_guard_sites_exhibit_penalized_behavior(monkeypatch):
    # Acceptance Criteria 6: Both guard sites exhibit the penalized behavior
    # We can test this by checking that run_optimization passes trade_floor to _compute_objective_score
    
    # We'll mock _compute_objective_score and check if it's called with trade_floor
    # when run_optimization is executed.
    
    from agent.strategy_optimizer import run_optimization
    
    # Mock dependencies heavily
    mock_base_cfg = {"execution_class": "SingleModelStrategy"}
    monkeypatch.setattr("src.live_execution.config_loader.load_strategy_config", lambda x: mock_base_cfg)
    
    mock_preds = pd.DataFrame(index=[pd.Timestamp("2020-01-01"), pd.Timestamp("2021-01-01")])
    monkeypatch.setattr("agent.strategy_optimizer.load_predictions", lambda x: mock_preds)
    monkeypatch.setattr("agent.strategy_optimizer.load_ohlcv_dual", lambda x: (pd.DataFrame(), None))
    monkeypatch.setattr("agent.strategy_optimizer.attach_atr_cache", lambda x: x)
    
    mock_engine_instance = MagicMock()
    mock_result = create_mock_result(100)
    mock_engine_instance.run.return_value = mock_result
    
    monkeypatch.setattr("agent.backtest_engine.BacktestEngine.from_config", lambda *args, **kwargs: mock_engine_instance)
    
    mock_study = MagicMock()
    mock_trial = MagicMock()
    mock_trial.number = 0
    mock_trial.value = 2.0
    mock_trial.params = {}
    mock_study.best_trial = mock_trial
    monkeypatch.setattr("optuna.create_study", lambda *args, **kwargs: mock_study)
    monkeypatch.setattr("agent.strategy_optimizer.send_telegram", lambda x: None)
    
    # Mock TopKTracker to avoid filesystem writes
    monkeypatch.setattr("agent.strategy_optimizer.TopKTracker", MagicMock)
    
    # Mock open for json dump
    monkeypatch.setattr("builtins.open", MagicMock())
    
    compute_spy = MagicMock(return_value=2.0)
    monkeypatch.setattr("agent.strategy_optimizer._compute_objective_score", compute_spy)
    
    run_optimization(
        config_path="dummy.json",
        n_trials=1,
        quiet=True
    )
    
    # Check that _compute_objective_score was called
    assert compute_spy.call_count >= 2 # Baseline and Best
    
    # Check that it was called with trade_floor as the 3rd argument
    # We expect _compute_objective_score(result, objective_metric, trade_floor)
    for call in compute_spy.call_args_list:
        args, kwargs = call
        assert len(args) >= 3 or "trade_floor" in kwargs, "Guard sites must pass trade_floor to _compute_objective_score"
