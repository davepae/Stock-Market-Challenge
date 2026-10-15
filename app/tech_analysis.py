#!/usr/bin/env python3
"""Technical analysis for the Mock Stock Challenge universe.

For each ticker, computes from the last 30 trading sessions of daily data:
  - current price
  - 20-period simple moving average
  - 14-period RSI (Wilder smoothing)
  - price vs. MA (percent)
  - momentum score from 1 (weak) to 10 (strong)
  - 20-day relative strength vs. SPY (ticker return minus SPY return, in
    percentage points; SPY is always downloaded as the benchmark)
  - daily volatility and consecutive closes below the 20MA

Usage:
    tech_analysis.py                 # all STOCK_UNIVERSE tickers, table output
    tech_analysis.py AAPL NVDA       # specific tickers
    tech_analysis.py --json          # JSON output
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

import config


@dataclass
class Analysis:
    """Technical indicators for a single ticker."""

    ticker: str
    price: float
    ma20: float
    rsi: float
    price_vs_ma_pct: float
    roc5_pct: float
    momentum: int
    rel_str: float | None = None    # REL_STR_PERIOD return minus benchmark's, in points
    volatility_pct: float = 0.0     # stdev of daily returns over MA_PERIOD sessions
    days_below_ma: int = 0          # consecutive closes (ending today) below their 20MA


def compute_rsi(closes: list[float], period: int = config.RSI_PERIOD) -> float:
    """Compute RSI using Wilder's smoothing. Requires at least period+1 closes."""
    if len(closes) < period + 1:
        raise ValueError(f"need {period + 1} closes for RSI, got {len(closes)}")
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp value into [low, high]."""
    return max(low, min(high, value))


def momentum_score(price_vs_ma_pct: float, rsi: float, roc5_pct: float,
                   ma_slope_pct: float) -> int:
    """Combine trend and momentum signals into a 1-10 score (5 is neutral).

    Components (each clamped):
      - 5-day rate of change:  +/-4% maps to +/-2
      - price vs 20MA:         +/-5% maps to +/-2
      - RSI distance from 50:  +/-15 maps to +/-1.5
      - 20MA slope (5 bars):   +/-1% maps to +/-1
    """
    raw = (_clamp(roc5_pct / 2.0, -2, 2)
           + _clamp(price_vs_ma_pct / 2.5, -2, 2)
           + _clamp((rsi - 50.0) / 10.0, -1.5, 1.5)
           + _clamp(ma_slope_pct, -1, 1))
    return int(_clamp(math.floor(5.0 + raw * 0.75 + 0.5), 1, 10))


def period_return(closes: list[float], period: int = config.REL_STR_PERIOD) -> float:
    """Percent return over the last `period` sessions."""
    if len(closes) < period + 1:
        raise ValueError(f"need {period + 1} closes for a {period}-day return, got {len(closes)}")
    return (closes[-1] / closes[-period - 1] - 1) * 100.0


def relative_strength(closes: list[float], benchmark: list[float],
                      period: int = config.REL_STR_PERIOD) -> float:
    """Return over `period` sessions minus the benchmark's, in percentage points."""
    return period_return(closes, period) - period_return(benchmark, period)


def daily_volatility(closes: list[float], period: int = config.MA_PERIOD) -> float:
    """Standard deviation of the last `period` daily returns, in percent."""
    window = closes[-period - 1:]
    returns = [(b / a - 1) * 100.0 for a, b in zip(window, window[1:])]
    return statistics.stdev(returns) if len(returns) >= 2 else 0.0


def consecutive_closes_below_ma(closes: list[float], period: int = config.MA_PERIOD) -> int:
    """Number of most recent closes in a row below their own `period` moving average."""
    count = 0
    for end in range(len(closes), period - 1, -1):
        window = closes[end - period:end]
        if window[-1] >= sum(window) / period:
            break
        count += 1
    return count


def analyze_closes(ticker: str, closes: list[float],
                   benchmark: list[float] | None = None) -> Analysis:
    """Compute all indicators from a list of daily closes (oldest first).

    ``benchmark`` holds the benchmark's closes over the same sessions; without
    it (or with too little history) ``rel_str`` is None.
    """
    period = config.MA_PERIOD
    if len(closes) < period + 5:
        raise ValueError(f"need at least {period + 5} closes, got {len(closes)}")
    price = closes[-1]
    ma = sum(closes[-period:]) / period
    ma_prev = sum(closes[-period - 5:-5]) / period
    rsi = compute_rsi(closes)
    vs_ma = (price - ma) / ma * 100.0
    roc5 = (price - closes[-6]) / closes[-6] * 100.0
    slope = (ma - ma_prev) / ma_prev * 100.0
    try:
        rel_str: float | None = (round(relative_strength(closes, benchmark), 2)
                                 if benchmark is not None else None)
    except ValueError:
        rel_str = None
    return Analysis(
        ticker=ticker,
        price=round(price, 2),
        ma20=round(ma, 2),
        rsi=round(rsi, 1),
        price_vs_ma_pct=round(vs_ma, 2),
        roc5_pct=round(roc5, 2),
        momentum=momentum_score(vs_ma, rsi, roc5, slope),
        rel_str=rel_str,
        volatility_pct=round(daily_volatility(closes), 2),
        days_below_ma=consecutive_closes_below_ma(closes),
    )


def fetch_closes(tickers: list[str], days: int = config.HISTORY_DAYS) -> dict[str, list[float]]:
    """Download daily closes for tickers, returning the last `days` sessions each.

    Fetches a wider calendar window than needed so weekends and holidays still
    leave `days` trading sessions. The final bar is today's live price during
    market hours.
    """
    import yfinance as yf  # imported lazily: slow import, not needed for --help

    data = yf.download(tickers, period=f"{days * 2}d", interval="1d",
                       auto_adjust=True, progress=False, group_by="ticker",
                       threads=True)
    if data is None or data.empty:
        raise RuntimeError("yfinance returned no data")

    multi = data.columns.nlevels > 1
    result: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            series = data[ticker]["Close"] if multi else data["Close"]
        except KeyError:
            continue
        closes = [float(c) for c in series.dropna().tolist()]
        if closes:
            result[ticker] = closes[-days:]
    return result


def analyze(tickers: list[str], days: int = config.HISTORY_DAYS) -> tuple[list[Analysis], dict[str, str]]:
    """Analyze tickers. Returns (results, errors) where errors maps ticker->message.

    The benchmark (SPY) is downloaded alongside for relative strength; if it
    is unavailable, ``rel_str`` is None for every result.
    """
    errors: dict[str, str] = {}
    bench = config.BENCHMARK_TICKER
    try:
        closes = fetch_closes(list(dict.fromkeys([*tickers, bench])), days)
    except Exception as e:  # network errors, yfinance failures
        return [], {t: f"download failed: {e}" for t in tickers}
    benchmark = closes.get(bench)

    results: list[Analysis] = []
    for ticker in tickers:
        if ticker not in closes:
            errors[ticker] = "no data returned"
            continue
        try:
            series = closes[ticker]
            # Align on the most recent sessions in case the histories differ in length.
            n = min(len(series), len(benchmark)) if benchmark else len(series)
            results.append(analyze_closes(ticker, series[-n:], benchmark[-n:] if benchmark else None))
        except ValueError as e:
            errors[ticker] = str(e)
    return results, errors


def relative_strengths(tickers: list[str], days: int = config.HISTORY_DAYS) -> dict[str, float]:
    """20-day relative strength vs. the benchmark for tickers (failures omitted)."""
    results, _ = analyze(tickers, days)
    return {r.ticker: r.rel_str for r in results if r.rel_str is not None}


def fetch_earnings_dates(tickers: list[str]) -> dict[str, date]:
    """Next scheduled earnings date per ticker from yfinance (failures omitted).

    ETFs and tickers without a published date are left out.
    """
    import yfinance as yf

    today = date.today()
    result: dict[str, date] = {}
    for ticker in tickers:
        try:
            dates = yf.Ticker(ticker).calendar.get("Earnings Date") or []
        except Exception:  # network/parse errors, missing calendar
            continue
        upcoming = sorted(d.date() if isinstance(d, datetime) else d
                          for d in dates if isinstance(d, date))
        upcoming = [d for d in upcoming if d >= today]
        if upcoming:
            result[ticker] = upcoming[0]
    return result


def format_table(results: list[Analysis]) -> str:
    """Render results as a fixed-width text table."""
    header = (f"{'TICKER':<7}{'PRICE':>10}{'MA20':>10}{'RSI':>7}{'vs MA%':>9}{'5D%':>8}{'MOM':>5}"
              f"{'REL_STR':>9}{'VOL%':>6}")
    lines = [header, "-" * len(header)]
    for r in results:
        rel = f"{r.rel_str:>+9.2f}" if r.rel_str is not None else f"{'n/a':>9}"
        lines.append(f"{r.ticker:<7}{r.price:>10.2f}{r.ma20:>10.2f}{r.rsi:>7.1f}"
                     f"{r.price_vs_ma_pct:>+9.2f}{r.roc5_pct:>+8.2f}{r.momentum:>5}"
                     f"{rel}{r.volatility_pct:>6.2f}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Technical analysis for stock tickers.")
    parser.add_argument("tickers", nargs="*", type=str.upper,
                        help="Tickers to analyze (default: STOCK_UNIVERSE)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table")
    parser.add_argument("--days", type=int, default=config.HISTORY_DAYS,
                        help=f"Trading sessions of history (default: {config.HISTORY_DAYS})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 if at least one ticker was analyzed."""
    args = parse_args(argv)
    tickers = args.tickers or list(config.STOCK_UNIVERSE)
    results, errors = analyze(tickers, args.days)

    if args.json:
        payload: dict[str, Any] = {"results": [asdict(r) for r in results], "errors": errors}
        print(json.dumps(payload, indent=2))
    else:
        if results:
            print(format_table(results))
        for ticker, msg in errors.items():
            print(f"ERROR {ticker}: {msg}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
