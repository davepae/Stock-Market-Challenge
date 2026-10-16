#!/usr/bin/env python3
"""Portfolio exporter for the Mock Stock Challenge (Flask, port 8081).

Rebuilds the portfolio from the trade log on every request and values it
with prices from a cache that a background thread refreshes every 60 seconds
with one batched yfinance call; request handlers never touch the network.
Peak prices come from PEAKS_JSON (read-only here; trading_cycle.py owns the
file) merged with the current price, and 20-day relative strength vs. SPY is
refreshed by the same thread every REL_STR_CACHE_SECONDS.

Endpoints:
    /metrics    Prometheus text format
    /holdings   JSON list of open positions
    /portfolio  JSON summary: cash, total value, ROI
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify
from prometheus_client import CollectorRegistry, Gauge, generate_latest

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
PRICE_STALE_SECONDS = 120


def get_prices(tickers: list[str]) -> dict[str, float]:
    """Return the last refreshed prices for tickers (cache only, no network I/O).

    The background refresher keeps the cache current; tickers it has never
    fetched are omitted.
    """
    with _price_lock:
        return {t: _price_cache[t][0] for t in tickers if t in _price_cache}


def prices_stale(tickers: list[str]) -> bool:
    """True if any ticker has no cached price or one older than PRICE_STALE_SECONDS."""
    now = time.time()
    with _price_lock:
        return any(t not in _price_cache or now - _price_cache[t][1] > PRICE_STALE_SECONDS
                   for t in tickers)


def _fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch the latest prices for all tickers in one batched yfinance call.

    Tickers with no usable price are skipped; a failed call returns {}.
    """
    import yfinance as yf

    if not tickers:
        return {}
    try:
        data = yf.download(tickers, period="5d", interval="1d", group_by="ticker",
                           auto_adjust=False, progress=False, threads=True)
    except Exception as e:  # network/parse errors from yfinance
        app.logger.warning("Batched price fetch failed for %s: %s", tickers, e)
        return {}
    prices: dict[str, float] = {}
    for ticker in tickers:
        try:
            frame = data[ticker] if ticker in data.columns.get_level_values(0) else data
            close = frame["Close"].dropna()
            if len(close) and close.iloc[-1] > 0:
                prices[ticker] = float(close.iloc[-1])
        except Exception as e:  # missing ticker column, odd frame shape
            app.logger.warning("No price for %s: %s", ticker, e)
    return prices


def get_relative_strengths(tickers: list[str]) -> dict[str, float]:
    """Return cached 20-day relative strength vs. the benchmark (no network I/O)."""
    with _rel_str_lock:
        return {t: _rel_str_cache[t][0] for t in tickers if t in _rel_str_cache}


def _refresh_rel_strengths(tickers: list[str]) -> None:
    """Recompute relative strength for tickers older than REL_STR_CACHE_SECONDS.

    Failed tickers keep their last cached value.
    """
    now = time.time()
    with _rel_str_lock:
        stale = [t for t in tickers
                 if t not in _rel_str_cache or now - _rel_str_cache[t][1] > config.REL_STR_CACHE_SECONDS]
    if not stale:
        return
    try:
        fresh = relative_strengths(stale)
    except Exception as e:  # network/parse errors from yfinance
        app.logger.warning("Relative strength fetch failed for %s: %s", stale, e)
        return
    with _rel_str_lock:
        for ticker, rel_str in fresh.items():
            _rel_str_cache[ticker] = (rel_str, now)


def refresh_caches() -> None:
    """One refresh pass: prices for all open positions, then relative strengths."""
    tickers = sorted(compute_portfolio(load_trades()).positions)
    now = time.time()
    fresh = _fetch_prices(tickers)
    with _price_lock:
        for ticker, price in fresh.items():
            _price_cache[ticker] = (price, now)
    _refresh_rel_strengths(tickers)


def _refresh_loop() -> None:
    while True:
        try:
            refresh_caches()
        except Exception as e:  # never let the refresher die (bad trade log, etc.)
            app.logger.warning("Cache refresh failed: %s", e)
        time.sleep(config.PRICE_CACHE_SECONDS)


def start_refresher() -> threading.Thread:
    """Start the daemon thread that refreshes prices every PRICE_CACHE_SECONDS."""
    thread = threading.Thread(target=_refresh_loop, name="price-refresher", daemon=True)
    thread.start()
    return thread


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
    records = risk.load_peaks()
    holdings = []
    for ticker, pos in sorted(state.positions.items()):
        price = prices.get(ticker)
        stale = price is None
        if stale:
            price = pos.entry_price
        value = pos.shares * price
        levels = risk.stop_levels(ticker, pos.entry_price, peaks.get(ticker, price),
                                  stop_pct=risk.stored_stop_pct(records.get(ticker)))
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


def render_metrics(snap: dict[str, Any], rel_str: dict[str, float], price_stale: bool = False) -> str:
    """Render a snapshot in Prometheus text exposition format.

    Per-holding metrics are labeled ``holding_<metric>{ticker="AAPL"}``.
    ``rel_str`` maps ticker -> relative strength; holdings missing from it
    have no ``holding_rel_strength`` sample. ``price_stale`` feeds the
    ``portfolio_price_stale`` gauge.
    """
    registry = CollectorRegistry()

    def gauge(name: str, help_text: str, labels: tuple[str, ...] = ()) -> Gauge:
        return Gauge(name, help_text, labels, registry=registry)

    gauge("portfolio_value", "Total portfolio value (cash + holdings) in USD").set(snap["total_value"])
    gauge("portfolio_cash", "Uninvested cash in USD").set(snap["cash"])
    gauge("portfolio_roi", "Return on investment in percent").set(snap["roi"])
    gauge("portfolio_holdings_value", "Total value of holdings in USD").set(snap["holdings_value"])
    gauge("portfolio_trade_count", "Number of trades logged").set(snap["trade_count"])
    gauge("portfolio_price_stale",
          f"1 if any holding's cached price is missing or older than {PRICE_STALE_SECONDS}s, else 0"
          ).set(int(price_stale))

    per_holding = (
        ("holding_shares", "Shares held", "shares"),
        ("holding_value", "Current value in USD", "current_value"),
        ("holding_entry_price", "Average entry price", "entry_price"),
        ("holding_current_price", "Current price", "current_price"),
        ("holding_peak_price", "Highest price since entry", "peak_price"),
        ("holding_trailing_stop", "Trailing stop price (0 until activated)", "trailing_stop"),
    )
    for name, help_text, key in per_holding:
        g = gauge(name, help_text, ("ticker",))
        for h in snap["holdings"]:
            g.labels(ticker=h["ticker"]).set(h[key])
    g = gauge("holding_rel_strength",
              f"{config.REL_STR_PERIOD}-day return minus {config.BENCHMARK_TICKER}'s, in points", ("ticker",))
    for h in snap["holdings"]:
        if h["ticker"] in rel_str:
            g.labels(ticker=h["ticker"]).set(rel_str[h["ticker"]])

    try:
        scoring = json.loads(Path(config.SCORING_JSON).read_text())
        held = [s for s in scoring.get("scores", []) if s.get("held")]
    except (OSError, ValueError, AttributeError):
        held = []  # scoring file may not exist yet
    for name, key, help_text in (("ticker_score", "score", "Composite score of held tickers"),
                                 ("ticker_technical_score", "technical_score", "Technical score of held tickers"),
                                 ("ticker_news_score", "news_score", "News sentiment score of held tickers")):
        g = gauge(name, help_text, ("ticker",))
        for s in held:
            if key in s:
                g.labels(ticker=s["ticker"].lower()).set(s[key])
    g = gauge("ticker_reason", "Top reasons for each held ticker's recommendation", ("ticker", "reason"))
    for s in held:
        reason = "; ".join(s.get("reasons", [])[:3])[:100].replace("\n", " ")
        g.labels(ticker=s["ticker"].lower(), reason=reason).set(1)
    return generate_latest(registry).decode()


def _error(message: str, status: int = 500) -> tuple[Response, int]:
    """JSON error response."""
    return jsonify({"error": message}), status


@app.route("/metrics")
def metrics() -> Response | tuple[Response, int]:
    """Prometheus metrics endpoint."""
    try:
        snap = build_snapshot()
        tickers = [h["ticker"] for h in snap["holdings"]]
        body = render_metrics(snap, get_relative_strengths(tickers), prices_stale(tickers))
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


def _serve_json(path: str, empty: dict[str, Any]) -> Response:
    try:
        return jsonify(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return jsonify(empty)


@app.route("/analysis")
def analysis() -> Response:
    """JSON scoring analysis from the last trading cycle."""
    return _serve_json(config.SCORING_JSON, {"timestamp": None, "scores": []})


@app.route("/plans")
def plans() -> Response:
    """JSON trade plans from the last trading cycle."""
    return _serve_json(config.PLANS_JSON, {"timestamp": None, "plans": []})


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
    start_refresher()
    app.run(host="127.0.0.1", port=config.PORTFOLIO_EXPORTER_PORT, threaded=True)
