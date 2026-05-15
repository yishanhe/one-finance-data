import yfinance as yf

# Test Screen
try:
    print("Testing Screen...")
    s = yf.Screen()
    print("Screen:", s)
except Exception as e:
    print("Screen failed:", e)

# Test EquityQuery
try:
    print("\nTesting EquityQuery...")
    q = yf.EquityQuery('eq', ['region', 'us'])
    print("EquityQuery:", q)
except Exception as e:
    print("EquityQuery failed:", e)
