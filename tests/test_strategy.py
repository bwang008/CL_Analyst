from src.live_execution.strategy import TradeSignal

class TestTradeSignal:
    def test_default_action_is_hold(self):
        s = TradeSignal(action='HOLD')
        assert s.action == 'HOLD'
        assert s.signal_label == 'Hold'
        assert s.skip_reason is None

    def test_buy_signal_fields(self):
        s = TradeSignal(
            action='BUY', 
            probability=0.85, 
            confidence_pct=85.0, 
            tp_price=70.0, 
            sl_price=64.0, 
            lots=3, 
            signal_label='Buy'
        )
        assert s.action == 'BUY'
        assert s.lots == 3
        assert s.tp_price == 70.0

    def test_sell_signal_fields(self):
        s = TradeSignal(
            action='SELL', 
            probability=0.75, 
            confidence_pct=75.0, 
            tp_price=60.0, 
            sl_price=70.0, 
            lots=2, 
            signal_label='Sell'
        )
        assert s.action == 'SELL'
        assert s.tp_price < s.sl_price
