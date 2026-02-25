"""
Quick smoke test: connect to IB Gateway, place a mock CL limit buy at $5.00,
verify order is acknowledged, then cancel. Run manually:

    conda activate trader
    python tests/test_ibkr_live_connection.py
"""

from ib_insync import IB, Future, LimitOrder


def main():
    ib = IB()
    print("Connecting to IB Gateway on port 4002...")
    ib.connect("127.0.0.1", 4002, clientId=99)
    print(f"Connected: {ib.isConnected()}")
    print(f"Account: {ib.managedAccounts()}")

    # Find front-month CL contract
    cl = Future(symbol="CL", exchange="NYMEX", currency="USD")
    details = ib.reqContractDetails(cl)
    print(f"Found {len(details)} CL contract(s)")

    if not details:
        print("ERROR: No CL contracts found!")
        ib.disconnect()
        return

    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    front = details[0].contract
    print(
        f"Front-month: {front.localSymbol} "
        f"(conId={front.conId}, "
        f"expiry={front.lastTradeDateOrContractMonth})"
    )

    # Place a limit buy at $5.00 — will NOT fill
    order = LimitOrder("BUY", 1, 5.00)
    order.tif = "GTC"
    trade = ib.placeOrder(front, order)
    ib.sleep(3)
    print(f"Order placed: orderId={trade.order.orderId}")
    print(f"Order status: {trade.orderStatus.status}")

    # Cancel immediately
    ib.cancelOrder(order)
    ib.sleep(3)
    print(f"Order cancelled. Final status: {trade.orderStatus.status}")

    ib.disconnect()
    print("Disconnected. Test complete!")


if __name__ == "__main__":
    main()
