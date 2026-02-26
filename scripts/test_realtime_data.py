"""Quick test: check if real-time CL data is available after market data upgrade."""
from ib_insync import IB, ContFuture

ib = IB()
print("Connecting to IB Gateway...")
ib.connect("127.0.0.1", 4002, clientId=98)
print(f"Connected: {ib.isConnected()}")
print(f"Account: {ib.managedAccounts()}")

# Use ContFuture (same as live trader)
cl = ContFuture(symbol="CL", exchange="NYMEX", currency="USD")
ib.qualifyContracts(cl)
print(f"\nContract: {cl.localSymbol} (conId={cl.conId})")

# Test 1: Fetch recent historical bars (last 30 min)
print("\n=== Test 1: Historical bars (last 30 min) ===")
bars = ib.reqHistoricalData(
    cl,
    endDateTime="",
    durationStr="1800 S",
    barSizeSetting="5 mins",
    whatToShow="TRADES",
    useRTH=False,
    formatDate=1,
)
print(f"Bars returned: {len(bars)}")
for b in bars:
    print(f"  {b.date}  O={b.open:.2f} H={b.high:.2f} L={b.low:.2f} C={b.close:.2f} V={int(b.volume)}")

# Test 2: Check keepUpToDate subscription
print("\n=== Test 2: keepUpToDate subscription (waiting 20 seconds) ===")
live_bars = ib.reqHistoricalData(
    cl,
    endDateTime="",
    durationStr="60 S",
    barSizeSetting="5 mins",
    whatToShow="TRADES",
    useRTH=False,
    formatDate=1,
    keepUpToDate=True,
)
print(f"Initial bars from subscription: {len(live_bars)}")

received = []
def on_update(bars, has_new_bar):
    received.append(has_new_bar)
    b = bars[-1]
    print(f"  UPDATE: has_new_bar={has_new_bar}, latest={b.date} C={b.close:.2f} V={int(b.volume)}")

live_bars.updateEvent += on_update
ib.sleep(20)

print(f"\nUpdates received in 20s: {len(received)}")
if len(received) > 0:
    print("SUCCESS: Real-time data is flowing!")
else:
    print("WARNING: No updates received. Data subscription may not be active yet.")

ib.cancelHistoricalData(live_bars)
ib.disconnect()
print("\nDone.")
