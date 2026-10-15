#!/usr/bin/env python3
"""Trades exporter — exposes recent trades as Prometheus metrics."""
import csv
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from prometheus_client import generate_latest, REGISTRY, Gauge

TRADES_FILE = os.path.expanduser("~/.hermes/cache/stocks/trades.csv")

# Pre-defined metric — we'll set values dynamically
TRADE_METRIC = Gauge(
    "trade_log",
    "Trade log entry",
    ["date", "ticker", "action", "shares", "price", "value"]
)

def read_trades():
    trades = []
    try:
        with open(TRADES_FILE) as f:
            reader = csv.DictReader(f)
            for row in reader:
                date = row["date"].strip()
                ticker = row["ticker"].strip()
                action = row["action"].strip()
                shares = row["shares"].strip()
                price = row["price"].strip()
                value = str(float(shares) * float(price))
                trades.append({
                    "date": date,
                    "ticker": ticker,
                    "action": action,
                    "shares": shares,
                    "price": price,
                    "value": value
                })
    except FileNotFoundError:
        pass
    return trades

class TradesHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            trades = read_trades()
            for t in trades:
                TRADE_METRIC.labels(**t).set(1)
            
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(generate_latest())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Suppress logs

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8082), TradesHandler)
    print("Trades exporter on :8082")
    server.serve_forever()
