#!/usr/bin/env python3
"""Portfolio exporter for the Mock Stock Challenge (Flask, port 8081).

Rebuilds the portfolio from the trade log on every request and values it
with current prices from yfinance (cached for 60 seconds). Peak prices come
from PEAKS_JSON (read-only here; trading_cycle.py owns the file) merged with
the current price, and 20-day relative strength vs. SPY is cached for
REL_STR_CACHE_SECONDS.

Endpoints:
    /metrics    Prometheus text format
    /holdings   JSON list of open positions
    /portfolio  JSON summary: cash, total value, ROI
"""

from __future__ import annotations

import threading
import time
from typing import Any

from flask import Flask, Response, jsonify

import config
import risk
from investment_model import open_positions
from log_trade import TradeError, compute_portfolio, read_trades
from tech_analysis import relative_strengths

app = Flask(__name__)

_file_lock = threading.Lock()
_price_lock = threading.Lock()
_price_cache: dict[str, tuple[float, float]] = {}  # ticker -> (price, fetched_at)
_rel_str_lock = threading.Lock()
_rel_str_cache: dict[str, tuple[float, float]] = {}  # ticker -> (rel_str, fetched_at)


def get_prices(tickers: list[str]) -> dict[str, float]:
    """Return current prices for tickers, using a 60-second cache.

    Tickers whose price cannot be fetched fall back to the last cached value
    (even if stale); if there is none they are omitted.
    """
    now = time.time()
    with _price_lock:
        stale = [t for t in tickers
                 if t not in _price_cache or now - _price_cache[t][1] > config.PRICE_CACHE_SECONDS]
        if stale:
            for ticker, price in _fetch_prices(stale).items():
                _price_cache[ticker] = (price, now)
        return {t: _price_cache[t][0] for t in tickers if t in _price_cache}


def _fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch the latest prices from yfinance; failures are logged and skipped."""
    import yfinance as yf

    prices: dict[str, float] = {}
    for ticker in tickers:
        try:
            price = yf.Ticker(ticker).fast_info["lastPrice"]
            if price and price > 0:
                prices[ticker] = float(price)
        except Exception as e:  # network/parse errors from yfinance
            app.logger.warning("Price fetch failed for %s: %s", ticker, e)
    return prices


def get_relative_strengths(tickers: list[str]) -> dict[str, float]:
    """Return 20-day relative strength vs. the benchmark, cached like get_prices.

    Uses a REL_STR_CACHE_SECONDS cache; tickers that cannot be computed fall
    back to the last cached value, or are omitted if there is none.
    """
    now = time.time()
    with _rel_str_lock:
        stale = [t for t in tickers
                 if t not in _rel_str_cache or now - _rel_str_cache[t][1] > config.REL_STR_CACHE_SECONDS]
        if stale:
            try:
                fresh = relative_strengths(stale)
            except Exception as e:  # network/parse errors from yfinance
                app.logger.warning("Relative strength fetch failed for %s: %s", stale, e)
                fresh = {}
            for ticker, rel_str in fresh.items():
                _rel_str_cache[ticker] = (rel_str, now)
        return {t: _rel_str_cache[t][0] for t in tickers if t in _rel_str_cache}


def load_trades() -> list[dict[str, str]]:
    """Read the trade log (serialized across threads)."""
    with _file_lock:
        return read_trades(config.TRADES_CSV)


def build_snapshot() -> dict[str, Any]:
    """Compute a valued snapshot of the portfolio.

    If a holding has no available price it is valued at its entry price and
    flagged with ``price_stale``. Peaks and trailing stops follow the same
    rules as trading_cycle.py (risk.stop_levels); the trailing
    stop is 0 until activated.
    """
    trades = load_trades()
    state = compute_portfolio(trades)
    prices = get_prices(sorted(state.positions))
    opened = {t: day for t, (_, _, day) in open_positions(trades).items()}
    peaks = risk.update_peaks({t: (p.entry_price, opened.get(t, "")) for t, p in state.positions.items()},
                              prices, save=False)
    holdings = []
    for ticker, pos in sorted(state.positions.items()):
        price = prices.get(ticker)
        stale = price is None
        if stale:
            price = pos.entry_price
        value = pos.shares * price
        levels = risk.stop_levels(ticker, pos.entry_price, peaks.get(ticker, price))
        holdings.append({
            "ticker": ticker,
            "shares": round(pos.shares, 6),
            "entry_price": round(pos.entry_price, 4),
            "current_price": round(price, 4),
            "current_value": round(value, 2),
            "unrealized_pnl": round(value - pos.cost_basis, 2),
            "unrealized_pnl_pct": round((price / pos.entry_price - 1) * 100, 2) if pos.entry_price else 0.0,
            "price_stale": stale,
            "peak_price": round(levels.peak, 4),
            "trailing_stop": round(levels.trailing_stop or 0.0, 4),
        })
    holdings_value = sum(h["current_value"] for h in holdings)
    total = state.cash + holdings_value
    return {
        "cash": round(state.cash, 2),
        "holdings_value": round(holdings_value, 2),
        "total_value": round(total, 2),
        "roi": round((total - config.INITIAL_CASH) / config.INITIAL_CASH * 100, 4),
        "trade_count": state.trade_count,
        "holdings": holdings,
    }


def render_metrics(snap: dict[str, Any], rel_str: dict[str, float]) -> str:
    """Render a snapshot in Prometheus text exposition format.

    ``rel_str`` maps ticker -> relative strength; holdings missing from it
    have no ``rel_strength`` sample.
    """
    lines: list[str] = []

    def gauge(name: str, value: float, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    gauge("portfolio_value", snap["total_value"], "Total portfolio value (cash + holdings) in USD")
    gauge("portfolio_cash", snap["cash"], "Uninvested cash in USD")
    gauge("portfolio_roi", snap["roi"], "Return on investment in percent")
    gauge("portfolio_holdings_value", snap["holdings_value"], "Total value of holdings in USD")
    gauge("portfolio_trade_count", snap["trade_count"], "Number of trades logged")
    for h in snap["holdings"]:
        t = h["ticker"].lower()
        gauge(f"holding_{t}_shares", h["shares"], f"Shares of {h['ticker']} held")
        gauge(f"holding_{t}_value", h["current_value"], f"Current value of {h['ticker']} in USD")
        gauge(f"holding_{t}_entry_price", h["entry_price"], f"Average entry price of {h['ticker']}")
        gauge(f"holding_{t}_peak_price", h["peak_price"], f"Highest price of {h['ticker']} since entry")
        gauge(f"holding_{t}_trailing_stop", h["trailing_stop"],
              f"Trailing stop price of {h['ticker']} (0 until activated)")
        if h["ticker"] in rel_str:
            gauge(f"holding_{t}_rel_strength", rel_str[h["ticker"]],
                  f"{config.REL_STR_PERIOD}-day return of {h['ticker']} minus {config.BENCHMARK_TICKER}'s, in points")
    return "\n".join(lines) + "\n"


def _error(message: str, status: int = 500) -> tuple[Response, int]:
    """JSON error response."""
    return jsonify({"error": message}), status


@app.route("/metrics")
def metrics() -> Response | tuple[Response, int]:
    """Prometheus metrics endpoint."""
    try:
        snap = build_snapshot()
        body = render_metrics(snap, get_relative_strengths([h["ticker"] for h in snap["holdings"]]))
    except TradeError as e:
        return Response(f"# error: {e}\n", status=500, mimetype="text/plain")
    return Response(body, mimetype="text/plain; version=0.0.4")


@app.route("/holdings")
def holdings() -> Response | tuple[Response, int]:
    """JSON list of open positions."""
    try:
        return jsonify(build_snapshot()["holdings"])
    except TradeError as e:
        return _error(str(e))


@app.route("/portfolio")
def portfolio() -> Response | tuple[Response, int]:
    """JSON summary of the portfolio."""
    try:
        snap = build_snapshot()
    except TradeError as e:
        return _error(str(e))
    return jsonify({k: snap[k] for k in ("cash", "holdings_value", "total_value", "roi", "trade_count")}
                   | {"initial_cash": config.INITIAL_CASH, "positions": len(snap["holdings"])})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORTFOLIO_EXPORTER_PORT, threaded=True)
