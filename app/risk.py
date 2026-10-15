#!/usr/bin/env python3
"""Risk helpers shared by trading_cycle.py and app.py.

  - Per-ticker stop loss (STOP_LOSS_BY_TICKER).
  - Trailing stop: tracks the highest price observed for each open position
    in PEAKS_JSON. Once the peak is TRAILING_STOP_ACTIVATION% above entry, the
    position is sold if price falls TRAILING_STOP_PCT% below the peak
    (no take-profit cap).
  - Lightweight last-price lookup for the hourly --risk-only check.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import config


# --- Stop levels --------------------------------------------------------------

def stop_loss_pct(ticker: str) -> float:
    """Stop loss below entry for ticker, in percent."""
    return config.STOP_LOSS_BY_TICKER.get(ticker.upper(), config.DEFAULT_STOP_LOSS_PCT)


def trailing_params() -> tuple[float, float]:
    """(activation %, trail %) for the trailing stop."""
    return config.TRAILING_STOP_ACTIVATION, config.TRAILING_STOP_PCT


@dataclass
class StopLevels:
    """Exit levels for an open position."""

    ticker: str
    entry: float
    peak: float
    stop_loss: float             # fixed stop price below entry
    trailing_stop: float | None  # trailing stop price, None until activated

    @property
    def trailing_active(self) -> bool:
        """True once the trailing stop is armed."""
        return self.trailing_stop is not None

    @property
    def effective_stop(self) -> float:
        """The higher of the fixed and trailing stop prices."""
        return max(self.stop_loss, self.trailing_stop or 0.0)


def stop_levels(ticker: str, entry: float, peak: float) -> StopLevels:
    """Compute the fixed and trailing stop prices for a position."""
    activation, trail = trailing_params()
    peak = max(peak, entry)
    fixed = entry * (1 - stop_loss_pct(ticker) / 100)
    trailing = peak * (1 - trail / 100) if entry > 0 and peak >= entry * (1 + activation / 100) else None
    return StopLevels(ticker, entry, peak, fixed, trailing)


def check_stops(levels: StopLevels, price: float) -> str:
    """Return the exit reason if price breaches a stop, else ""."""
    if levels.entry <= 0:
        return ""
    pnl = (price / levels.entry - 1) * 100
    if price <= levels.stop_loss:
        return (f"stop loss ({pnl:+.1f}% vs entry {levels.entry:.2f}, "
                f"limit -{stop_loss_pct(levels.ticker):g}%)")
    if levels.trailing_stop is not None and price <= levels.trailing_stop:
        drop = (price / levels.peak - 1) * 100
        return (f"trailing stop ({drop:+.1f}% from peak {levels.peak:.2f}, "
                f"stop {levels.trailing_stop:.2f}; P&L {pnl:+.1f}%)")
    return ""


# --- Peak tracking ------------------------------------------------------------

def load_peaks(path: str | Path = config.PEAKS_JSON) -> dict[str, dict[str, object]]:
    """Read {ticker: {"opened": date, "peak": price}}; empty if missing or unreadable."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"WARNING: cannot read peaks file {path}: {e}", file=sys.stderr)
        return {}


def merge_peaks(stored: dict[str, dict[str, object]],
                positions: dict[str, tuple[float, str]],
                prices: dict[str, float]) -> dict[str, dict[str, object]]:
    """Combine stored peaks with fresh prices for the open positions.

    ``positions`` maps ticker -> (entry price, opened date). A stored peak
    from a different (earlier) position in the same ticker is discarded, and
    the peak is never below the entry price. Closed positions are dropped.
    """
    merged: dict[str, dict[str, object]] = {}
    for ticker, (entry, opened) in positions.items():
        old = stored.get(ticker, {})
        try:
            peak = float(old["peak"]) if old.get("opened") == opened else 0.0
        except (KeyError, TypeError, ValueError):
            peak = 0.0
        merged[ticker] = {"opened": opened,
                          "peak": round(max(peak, entry, prices.get(ticker, 0.0)), 4)}
    return merged


def update_peaks(positions: dict[str, tuple[float, str]], prices: dict[str, float],
                 path: str | Path = config.PEAKS_JSON, save: bool = True) -> dict[str, float]:
    """Record new highs for open positions and return {ticker: peak}.

    With save=False the file is only read (used by dry runs and the exporter).
    The read-modify-write is serialized with a lock file and written atomically.
    """
    path = Path(path)
    if not save:
        merged = merge_peaks(load_peaks(path), positions, prices)
        return {t: float(v["peak"]) for t, v in merged.items()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            merged = merge_peaks(load_peaks(path), positions, prices)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, path)
    except OSError as e:
        print(f"WARNING: cannot update peaks file {path}: {e}", file=sys.stderr)
        merged = merge_peaks(load_peaks(path), positions, prices)
    return {t: float(v["peak"]) for t, v in merged.items()}


# --- Prices -------------------------------------------------------------------

def fetch_prices(tickers: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """Latest prices from yfinance. Returns (prices, errors by ticker)."""
    import yfinance as yf  # imported lazily: slow import

    prices: dict[str, float] = {}
    errors: dict[str, str] = {}
    for ticker in tickers:
        try:
            price = yf.Ticker(ticker).fast_info["lastPrice"]
            if price and price > 0:
                prices[ticker] = float(price)
            else:
                errors[ticker] = f"invalid price {price!r}"
        except Exception as e:  # network/parse errors from yfinance
            errors[ticker] = str(e)
    return prices, errors
