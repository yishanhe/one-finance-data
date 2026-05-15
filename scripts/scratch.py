import yfinance as yf

# Test Screener
try:
    print("Testing Screener...")
    s = yf.Screener()
    s.set_predefined_body('day_gainers')
    res = s.response
    print("Screener:", res)
except Exception as e:
    print("Screener failed:", e)

# Test Sector
try:
    print("\nTesting Sector...")
    sec = yf.Sector('technology')
    print("Top companies:", sec.top_companies)
    print("Overview:", sec.overview)
except Exception as e:
    print("Sector failed:", e)
