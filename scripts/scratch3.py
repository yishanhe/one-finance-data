import yfinance as yf

print([m for m in dir(yf) if not m.startswith("_")])
