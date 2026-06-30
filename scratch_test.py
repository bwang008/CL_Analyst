from unittest.mock import MagicMock

trade = MagicMock()
trade.order.orderId = 10
res = getattr(getattr(trade, "order", None), "orderId", None)
print(res)
