import yfinance as yf

# Test Screener
try:
    print("Testing Screener...")
    res = yf.screener.Screener()
    print("Screener:", res)
except Exception as e:
    print("Screener failed:", e)

# Test EquityQuery
try:
    print("\nTesting EquityQuery...")
    q = yf.EquityQuery(
        "and",
        [yf.EquityQuery("eq", ["region", "us"]), yf.EquityQuery("eq", ["sector", "Technology"])],
    )
    scr = yf.screener.Screener()
    scr.set_body(q)
    print("EquityQuery Response:", scr.response)
except Exception as e:
    print("EquityQuery failed:", e)
