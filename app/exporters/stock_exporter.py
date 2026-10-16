#!/usr/bin/env python3
"""
Stock price exporter for Prometheus.
Serves stock prices as Prometheus metrics on port 8080.

Usage:
  python3 stock_exporter.py [ticker1,ticker2,...]
  Defaults to SPY (S&P 500 ETF) if no tickers given.
"""

import os
import sys
import time
import logging
from pathlib import Path

import yfinance as yf
from prometheus_client import start_http_server, Gauge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# Directory for cached data
DATA_DIR = Path(os.environ.get("STOCKS_DATA_DIR", Path.home() / ".hermes/cache/stocks")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Default tickers (S&P 500 ETF as benchmark)
DEFAULT_TICKERS = ["SPY"]

# Prometheus metric: stock_price{ticker="AAPL"} = current price
stock_price = Gauge(
    "stock_price",
    "Current stock price",
    ["ticker"]
)

def fetch_prices(tickers):
    """Fetch current prices from Yahoo Finance."""
    results = {}
    for ticker in tickers:
        try:
            ticker_obj = yf.Ticker(ticker)
            # Get today's price (most recent close or current)
            info = ticker_obj.info
            current = info.get("currentPrice") or info.get("regularMarketPrice")
            if current is None:
                # Try fast info as fallback
                fast = ticker_obj.fast_info
                current = getattr(fast, "last_price", None)
            if current:
                results[ticker] = current
                logging.info(f"{ticker}: ${current}")
            else:
                logging.warning(f"{ticker}: no price found")
        except Exception as e:
            logging.error(f"{ticker}: fetch failed - {e}")
    return results

def main():
    tickers = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_TICKERS
    # Parse comma-separated tickers
    parsed = []
    for t in tickers:
        parsed.extend([x.strip().upper() for x in t.split(",")])
    tickers = parsed

    logging.info(f"Exporting prices for: {tickers}")
    logging.info(f"Serving metrics on http://localhost:8080/metrics")

    # Start Prometheus HTTP server
    start_http_server(8080)

    # Initial fetch
    prices = fetch_prices(tickers)
    for ticker, price in prices.items():
        stock_price.labels(ticker=ticker).set(price)

    # Update every 5 minutes (intraday)
    while True:
        time.sleep(300)
        prices = fetch_prices(tickers)
        for ticker, price in prices.items():
            stock_price.labels(ticker=ticker).set(price)

if __name__ == "__main__":
    main()
