import yfinance as yf
import pprint

print([m for m in dir(yf) if not m.startswith('_')])
