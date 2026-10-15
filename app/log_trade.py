#!/usr/bin/env python3
"""Log a BUY or SELL trade to the Mock Stock Challenge trade log.

Usage:
    log_trade.py AAPL BUY 10 345.20
    log_trade.py AAPL SELL 5 350.00

The trade is validated against the current portfolio (rebuilt from the trade
log) before being appended: BUY requires enough cash, SELL requires enough
shares. This module also provides the shared trade-log helpers used by
``app.py`` and ``trading_cycle.py``.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import config

CSV_FIELDS: list[str] = ["date", "ticker", "action", "shares", "price"]
EPSILON: float = 1e-6


class TradeError(Exception):
    """Raised when a trade is invalid or the trade log cannot be processed."""


@dataclass
class Position:
    """An open position, tracked with average cost basis."""

    ticker: str
    shares: float = 0.0
    entry_price: float = 0.0

    @property
    def cost_basis(self) -> float:
        """Total cost of the shares currently held."""
        return self.shares * self.entry_price


@dataclass
class PortfolioState:
    """Cash and open positions derived from the trade log."""

    cash: float = float(config.INITIAL_CASH)
    positions: dict[str, Position] = field(default_factory=dict)
    trade_count: int = 0


def read_trades(path: str | Path = config.TRADES_CSV) -> list[dict[str, str]]:
    """Read all trades from the CSV under a shared (read) file lock.

    Returns an empty list if the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        with open(path, newline="") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return list(csv.DictReader(f))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        raise TradeError(f"Cannot read trade log {path}: {e}") from e


def compute_portfolio(trades: list[dict[str, str]]) -> PortfolioState:
    """Replay trades in order to compute cash and positions (average cost)."""
    state = PortfolioState()
    for lineno, row in enumerate(trades, start=2):
        try:
            ticker = row["ticker"].strip().upper()
            action = row["action"].strip().upper()
            shares = float(row["shares"])
            price = float(row["price"])
        except (KeyError, ValueError, AttributeError) as e:
            print(f"WARNING: skipping malformed trade on line {lineno}: {row} ({e})",
                  file=sys.stderr)
            continue

        pos = state.positions.setdefault(ticker, Position(ticker))
        if action == "BUY":
            total = pos.shares + shares
            pos.entry_price = (pos.cost_basis + shares * price) / total if total else 0.0
            pos.shares = total
            state.cash -= shares * price
        elif action == "SELL":
            pos.shares -= shares
            state.cash += shares * price
        else:
            print(f"WARNING: unknown action {action!r} on line {lineno}", file=sys.stderr)
            continue
        state.trade_count += 1

        if pos.shares <= EPSILON:
            del state.positions[ticker]

    return state


def load_portfolio(path: str | Path = config.TRADES_CSV) -> PortfolioState:
    """Read the trade log and compute the resulting portfolio state."""
    return compute_portfolio(read_trades(path))


def _fmt_number(value: float) -> str:
    """Format a number compactly: integers without decimals, others rounded."""
    return str(int(value)) if float(value).is_integer() else f"{value:.6f}".rstrip("0")


def validate_trade(state: PortfolioState, ticker: str, action: str,
                   shares: float, price: float) -> None:
    """Raise TradeError if the trade is not allowed given the portfolio state."""
    if shares <= 0:
        raise TradeError("Shares must be positive")
    if price <= 0:
        raise TradeError("Price must be positive")
    if action == "BUY":
        cost = shares * price
        if cost > state.cash + EPSILON:
            raise TradeError(
                f"Insufficient cash: BUY {_fmt_number(shares)} {ticker} @ {price:.2f} "
                f"costs ${cost:,.2f} but only ${state.cash:,.2f} available"
            )
    elif action == "SELL":
        held = state.positions.get(ticker, Position(ticker)).shares
        if shares > held + EPSILON:
            raise TradeError(
                f"Insufficient shares: cannot SELL {_fmt_number(shares)} {ticker}, "
                f"only {_fmt_number(held)} held"
            )
    else:
        raise TradeError(f"Invalid action {action!r}; expected BUY or SELL")


def log_trade(ticker: str, action: str, shares: float, price: float,
              path: str | Path = config.TRADES_CSV,
              trade_date: str | None = None) -> PortfolioState:
    """Validate and append a trade to the log, returning the new portfolio state.

    The file is held under an exclusive lock for the whole read-validate-append
    sequence so concurrent writers cannot overdraw cash or oversell shares.
    """
    ticker = ticker.strip().upper()
    action = action.strip().upper()
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", newline="") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                content = f.read()
                rows = list(csv.DictReader(content.splitlines())) if content else []
                state = compute_portfolio(rows)
                validate_trade(state, ticker, action, shares, price)

                writer = csv.writer(f)
                if not content.strip():
                    writer.writerow(CSV_FIELDS)
                elif not content.endswith("\n"):
                    f.write("\n")
                row = [trade_date or date.today().isoformat(), ticker, action,
                       _fmt_number(shares), _fmt_number(round(price, 4))]
                writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())
                rows.append(dict(zip(CSV_FIELDS, row)))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        raise TradeError(f"Cannot write trade log {path}: {e}") from e
    return compute_portfolio(rows)


def _positive_float(value: str) -> float:
    """argparse type: a strictly positive float."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be positive")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Log a trade to the Mock Stock Challenge.")
    parser.add_argument("ticker", help="Ticker symbol, e.g. AAPL")
    parser.add_argument("action", type=str.upper, choices=["BUY", "SELL"], help="BUY or SELL")
    parser.add_argument("shares", type=_positive_float, help="Number of shares (fractional ok)")
    parser.add_argument("price", type=_positive_float, help="Fill price per share")
    parser.add_argument("--trades-file", default=config.TRADES_CSV,
                        help=f"Trade log path (default: {config.TRADES_CSV})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = parse_args(argv)
    try:
        state = log_trade(args.ticker, args.action, args.shares, args.price, args.trades_file)
    except TradeError as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1

    ticker = args.ticker.upper()
    held = state.positions.get(ticker, Position(ticker)).shares
    print(f"LOGGED: {args.action} {_fmt_number(args.shares)} {ticker} @ ${args.price:,.2f} "
          f"(${args.shares * args.price:,.2f})")
    print(f"Cash: ${state.cash:,.2f} | {ticker} shares held: {_fmt_number(held)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
