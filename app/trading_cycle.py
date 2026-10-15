#!/usr/bin/env python3
"""Run one full trading cycle for the Mock Stock Challenge.

Steps:
  1. Query /metrics from the portfolio exporter (falls back to the trade log).
  2. Score every holding and STOCK_UNIVERSE ticker with investment_model
     (technical analysis + web news search + earnings catalyst + position
     context), and record each holding's peak price for the trailing stop.
  3. Initial portfolio (no trades logged yet): buy the top 2-3 tickers by
     relative strength vs. SPY, 60% to the first and the rest split evenly.
     Otherwise: review holdings (HOLD or SELL) and pick new entries, rotating
     out the weakest holding (by relative strength) when at MAX_POSITIONS.
  4. Execute trades via log_trade.py (sells first, then buys).
  5. Print a cycle summary.

With --risk-only (the hourly check) steps 2-3 are replaced by a price lookup
and only the stop loss / trailing stop rules are applied; nothing is bought.

Exit rules (first match wins):
  - per-ticker stop loss (STOP_LOSS_BY_TICKER), or trailing stop: once the
    peak is TRAILING_STOP_ACTIVATION% above entry, a TRAILING_STOP_PCT% drop
    from the peak (no take-profit cap)
  - model recommends SELL or STRONG_SELL
  - momentum fade (<= 4), or a confirmed 20MA break (MA_BREAK_CONFIRM_DAYS
    closes below it, or MA_BREAK_HARD_PCT% below), unless news is strongly
    positive (>= NEWS_HOLD_OVERRIDE) or a STRONG_BUY has a catalyst
Entry rules: see investment_model.qualifies_for_entry. Candidates are ranked
by relative strength vs. SPY plus a volatility bonus.

Rules: long-only, no leverage (buys never exceed cash, which
log_trade.py enforces), fractional shares.
The cycle is skipped outside regular US market hours (9:30-16:00 ET, Mon-Fri)
unless --force is given. Use --dry-run to see decisions without trading.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import config
import risk
from investment_model import (BUY_RECOMMENDATIONS, SELL_RECOMMENDATIONS, TickerScore,
                              format_headlines, format_table, open_positions,
                              qualifies_for_entry, scan_market_headlines, score_universe,
                              _news_cached)
from log_trade import TradeError, _fmt_number, load_portfolio, read_trades
from tech_analysis import Analysis, fetch_earnings_dates

ET = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
LOG_TRADE = Path(__file__).resolve().parent / "log_trade.py"


@dataclass
class Holding:
    """A position as reported by the exporter (entry price may come from the log)."""

    ticker: str
    shares: float
    entry_price: float = 0.0


@dataclass
class Snapshot:
    """Portfolio state at the start of the cycle."""

    cash: float
    total_value: float
    holdings: dict[str, Holding]
    source: str


@dataclass
class Decision:
    """A trade decision for one ticker."""

    ticker: str
    action: str          # HOLD, SELL or BUY
    reason: str
    shares: float = 0.0
    price: float = 0.0
    executed: bool = False
    error: str = ""


@dataclass
class CycleResult:
    """Everything that happened during a cycle, for the summary."""

    snapshot: Snapshot | None = None
    initial: bool = False
    risk_only: bool = False
    scores: list[TickerScore] = field(default_factory=list)
    holding_decisions: list[Decision] = field(default_factory=list)
    buy_decisions: list[Decision] = field(default_factory=list)
    candidates: list[TickerScore] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def trades(self) -> list[Decision]:
        """All SELL and BUY decisions, in execution order."""
        return [d for d in self.holding_decisions if d.action == "SELL"] + self.buy_decisions


# --- Market hours -------------------------------------------------------------

def is_market_open(now: datetime | None = None) -> bool:
    """True during regular US market hours (does not account for holidays)."""
    now = (now or datetime.now(ET)).astimezone(ET)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


# --- Step 1: portfolio state --------------------------------------------------

_METRIC_RE = re.compile(r'^([a-zA-Z_:][\w:]*)(?:\{([^}]*)\})?\s+([-+0-9.eE]+|NaN)\s*$')


def parse_metrics(text: str) -> dict[str, float]:
    """Parse Prometheus text into {name or name{labels}: value}."""
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line.strip())
        if m:
            name, labels, value = m.groups()
            key = f"{name}{{{labels}}}" if labels else name
            metrics[key] = float(value)
    return metrics


def holdings_from_metrics(metrics: dict[str, float]) -> dict[str, Holding]:
    """Extract holdings from metrics.

    Supports both ``holding_{ticker}_shares`` (this app) and the labeled
    ``holding_shares{ticker="X"}`` format used by the older exporter.
    """
    holdings: dict[str, Holding] = {}
    for key, value in metrics.items():
        m = re.fullmatch(r"holding_([a-z0-9.\-]+)_shares", key) or \
            re.fullmatch(r'holding_shares\{ticker="([^"]+)"\}', key)
        if m and value > 0:
            ticker = m.group(1).upper()
            entry = metrics.get(f"holding_{ticker.lower()}_entry_price", 0.0)
            holdings[ticker] = Holding(ticker, value, entry)
    return holdings


def get_snapshot(exporter_url: str) -> Snapshot:
    """Query the exporter for portfolio state, falling back to the trade log.

    Entry prices missing from the metrics are filled in from the trade log.
    """
    log_state = load_portfolio(config.TRADES_CSV)
    try:
        resp = requests.get(f"{exporter_url}/metrics", timeout=30)
        resp.raise_for_status()
        metrics = parse_metrics(resp.text)
        cash = metrics["portfolio_cash"]
        total = metrics["portfolio_value"]
        holdings = holdings_from_metrics(metrics)
        for ticker, h in holdings.items():
            if not h.entry_price and ticker in log_state.positions:
                h.entry_price = log_state.positions[ticker].entry_price
        return Snapshot(cash, total, holdings, f"exporter {exporter_url}")
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"WARNING: exporter unavailable ({e}); using trade log", file=sys.stderr)
        holdings = {t: Holding(t, p.shares, p.entry_price) for t, p in log_state.positions.items()}
        # Value at cost; prices are refreshed by the analysis step.
        total = log_state.cash + sum(p.cost_basis for p in log_state.positions.values())
        return Snapshot(log_state.cash, total, holdings, "trade log (valued at cost)")


# --- Step 3a: exits -----------------------------------------------------------

def ma_break_confirmed(a: Analysis) -> bool:
    """True when a 20MA break passes the hysteresis filter.

    Requires MA_BREAK_CONFIRM_DAYS consecutive closes below the 20MA, or price
    at least MA_BREAK_HARD_PCT% below it.
    """
    return (a.price < a.ma20
            and (a.days_below_ma >= config.MA_BREAK_CONFIRM_DAYS
                 or a.price_vs_ma_pct <= -config.MA_BREAK_HARD_PCT))


def earnings_soon(earnings_date: date | None, today: date | None = None) -> bool:
    """True if earnings fall within EARNINGS_EXIT_DAYS trading days (weekdays; holidays ignored)."""
    if earnings_date is None:
        return False
    day = today or date.today()
    n = 0
    while day < earnings_date and n <= config.EARNINGS_EXIT_DAYS:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n += 1
    return earnings_date >= (today or date.today()) and n <= config.EARNINGS_EXIT_DAYS


def decide_exit(holding: Holding, s: TickerScore | None, peak: float = 0.0,
                news_stale: bool = False, earnings_date: date | None = None) -> Decision:
    """Apply exit rules to a holding. The first matching rule wins.

    With ``news_stale`` (news fetch failed) only risk exits apply;
    discretionary model/news-driven sells are suppressed.
    """
    if s is None or s.analysis is None:
        return Decision(holding.ticker, "HOLD", "no analysis data available")
    a = s.analysis
    sell = Decision(holding.ticker, "SELL", "", holding.shares, a.price)
    model = f"score {s.score} {s.recommendation}, news {s.news_score:+.1f}"
    levels = risk.stop_levels(holding.ticker, holding.entry_price, max(peak, a.price))
    if holding.entry_price > 0:
        pnl_pct = (a.price / holding.entry_price - 1) * 100
        stop_reason = risk.check_stops(levels, a.price)
        if stop_reason:
            sell.reason = stop_reason
            return sell
    else:
        pnl_pct = float("nan")
    if earnings_soon(earnings_date):
        sell.reason = f"earnings avoidance (reports {earnings_date}); exit regardless of P&L"
        return sell
    if news_stale:
        return Decision(holding.ticker, "HOLD",
                        f"news fetch failed; discretionary exits suppressed (stops still apply); {model}")
    if s.recommendation in SELL_RECOMMENDATIONS:
        sell.reason = f"model {model}"
        return sell
    soft_exit = ""
    if a.momentum <= config.EXIT_MOMENTUM_FADE:
        soft_exit = f"momentum fade (score {a.momentum})"
    elif ma_break_confirmed(a):
        soft_exit = f"below 20MA ({a.price_vs_ma_pct:+.2f}%, {a.days_below_ma} closes)"
    override = ""
    if soft_exit:
        if s.news_score >= config.NEWS_HOLD_OVERRIDE:
            override = f"{soft_exit} overridden by positive news; "
        elif s.recommendation == "STRONG_BUY" and s.has_catalyst:
            override = f"{soft_exit} overridden by STRONG_BUY catalyst; "
        else:
            sell.reason = f"{soft_exit}; {model}"
            return sell
    elif a.price < a.ma20:
        override = f"below 20MA unconfirmed ({a.days_below_ma} close); "
    pnl = "n/a" if math.isnan(pnl_pct) else f"{pnl_pct:+.1f}%"
    trail = f", trail stop {levels.trailing_stop:.2f}" if levels.trailing_active else ""
    return Decision(holding.ticker, "HOLD",
                    f"{override}P&L {pnl}, peak {levels.peak:.2f}{trail}, {model}, "
                    f"momentum {a.momentum}, {a.price_vs_ma_pct:+.2f}% vs MA, RS {_rs(s)}")


# --- Step 3b: entries ---------------------------------------------------------

def _rs(s: TickerScore) -> str:
    """Relative strength for display."""
    return "n/a" if s.rel_str is None else f"{s.rel_str:+.1f}"


def strength_rank(s: TickerScore) -> float:
    """Ranking value: relative strength vs. SPY plus a volatility bonus.

    The bonus (VOLATILITY_RANK_BONUS per 1% daily volatility) tips close calls
    toward high-beta names like TSLA and NVDA. Unknown strength ranks last.
    """
    if s.analysis is None or s.rel_str is None:
        return -math.inf
    return s.rel_str + config.VOLATILITY_RANK_BONUS * s.analysis.volatility_pct


def rank_candidates(scores: list[TickerScore], exclude: set[str]) -> list[TickerScore]:
    """Return entry-qualified candidates, best first (by strength_rank, then score)."""
    eligible = [s for s in scores if s.ticker not in exclude and qualifies_for_entry(s)]
    return sorted(eligible, key=lambda s: (strength_rank(s), s.score), reverse=True)


def pick_initial_portfolio(scores: list[TickerScore]) -> list[TickerScore]:
    """Choose the initial positions, strongest relative strength first.

    Takes up to INITIAL_MAX_PICKS tickers rated BUY or better. If fewer than
    INITIAL_MIN_PICKS qualify, fills up with the best HOLD-rated tickers
    scoring at least INITIAL_FILL_MIN_SCORE. SELL-rated tickers are never
    picked, so the result may be shorter (or empty) in a weak market.
    """
    ranked = sorted((s for s in scores if s.analysis), key=lambda s: (strength_rank(s), s.score),
                    reverse=True)
    picks = [s for s in ranked if s.recommendation in BUY_RECOMMENDATIONS][:config.INITIAL_MAX_PICKS]
    for s in ranked:
        if len(picks) >= config.INITIAL_MIN_PICKS:
            break
        if s not in picks and s.recommendation == "HOLD" and s.score >= config.INITIAL_FILL_MIN_SCORE:
            picks.append(s)
    return picks


def initial_allocations(n: int) -> list[float]:
    """Percent of capital per pick: INITIAL_TOP_ALLOCATION_PCT to the first,
    the rest (less the MIN_CASH_PCT reserve) split evenly among the others."""
    if n <= 0:
        return []
    top = config.INITIAL_TOP_ALLOCATION_PCT
    if n == 1:
        return [top]
    rest = (100 - top - config.MIN_CASH_PCT) / (n - 1)
    return [top] + [rest] * (n - 1)


def size_shares(budget: float, price: float) -> float:
    """Fractional shares (rounded down to SHARE_DECIMALS) affordable with budget.

    Returns 0 when the budget is below MIN_TRADE_VALUE.
    """
    if budget < config.MIN_TRADE_VALUE or price <= 0:
        return 0.0
    scale = 10 ** config.SHARE_DECIMALS
    return math.floor(budget / price * scale) / scale


def plan_initial_buys(picks: list[TickerScore], cash: float) -> list[Decision]:
    """Build BUY decisions for the initial portfolio from available cash."""
    decisions = []
    for rank, (s, pct) in enumerate(zip(picks, initial_allocations(len(picks)))):
        price = s.analysis.price
        shares = size_shares(cash * pct / 100, price)
        if shares > 0:
            label = "highest conviction" if rank == 0 else f"pick #{rank + 1}"
            decisions.append(Decision(s.ticker, "BUY",
                                      f"initial {label}, {pct:.1f}% allocation; score {s.score} "
                                      f"{s.recommendation}, news {s.news_score:+.1f}, RS {_rs(s)}",
                                      shares, price))
    return decisions


def plan_buys(candidates: list[TickerScore], slots: int, cash: float,
              total_value: float) -> list[Decision]:
    """Build BUY decisions for the best candidates that fit in the open slots.

    Spendable cash (keeping the MIN_CASH_PCT reserve) is split evenly across
    the chosen candidates, each capped at MAX_POSITION_PCT of the portfolio.
    """
    picks = candidates[:max(slots, 0)]
    spendable = cash - total_value * config.MIN_CASH_PCT / 100
    decisions = []
    for i, s in enumerate(picks):
        budget = min(total_value * config.MAX_POSITION_PCT / 100, spendable / (len(picks) - i))
        shares = size_shares(budget, s.analysis.price)
        if shares > 0:
            spendable -= shares * s.analysis.price
            decisions.append(Decision(s.ticker, "BUY",
                                      f"score {s.score} {s.recommendation}, news {s.news_score:+.1f}, "
                                      f"momentum {s.analysis.momentum}, RS {_rs(s)}",
                                      shares, s.analysis.price))
    return decisions


# --- Step 4: execution --------------------------------------------------------

def execute(decision: Decision, dry_run: bool) -> None:
    """Run log_trade.py for a decision, recording success or failure on it."""
    cmd = [sys.executable, str(LOG_TRADE), decision.ticker, decision.action,
           _fmt_number(decision.shares), f"{decision.price:.2f}"]
    if dry_run:
        print(f"  [dry-run] {' '.join(cmd[1:])}")
        return
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        decision.error = str(e)
        return
    output = (proc.stdout + proc.stderr).strip()
    print("  " + output.replace("\n", "\n  "))
    if proc.returncode == 0:
        decision.executed = True
    else:
        decision.error = output or f"exit code {proc.returncode}"



# --- Orchestration ------------------------------------------------------------

def plan_rotations(decisions: list[Decision], candidates: list[TickerScore],
                   by_ticker: dict[str, TickerScore], holdings: dict[str, Holding],
                   open_slots: int) -> list[str]:
    """Free slots for stronger candidates by selling the weakest holdings.

    For each candidate that does not fit in ``open_slots``, the kept holding
    with the lowest relative strength is turned into a SELL if the candidate
    beats it by at least ROTATION_MIN_REL_STR_EDGE points. Mutates
    ``decisions`` and returns notes describing each rotation.
    """
    notes = []
    for cand in candidates[max(open_slots, 0):]:
        kept = [d for d in decisions if d.action == "HOLD"
                and d.ticker in by_ticker and by_ticker[d.ticker].rel_str is not None]
        if cand.rel_str is None or not kept:
            break
        weakest = min(kept, key=lambda d: by_ticker[d.ticker].rel_str)
        w = by_ticker[weakest.ticker]
        if cand.rel_str - w.rel_str < config.ROTATION_MIN_REL_STR_EDGE:
            break
        holding = holdings.get(weakest.ticker)
        if holding is None or w.analysis is None:
            break
        weakest.action, weakest.shares, weakest.price = "SELL", holding.shares, w.analysis.price
        weakest.reason = (f"rotation: weakest RS {_rs(w)} vs candidate {cand.ticker} RS {_rs(cand)}; "
                          f"score {w.score} {w.recommendation}")
        notes.append(f"rotating {w.ticker} -> {cand.ticker} (RS {_rs(w)} -> {_rs(cand)})")
    return notes


def _review_and_scan(result: CycleResult, snap: Snapshot, by_ticker: dict[str, TickerScore],
                     peaks: dict[str, float],
                     stale: frozenset[str] = frozenset(),
                     earnings: dict[str, date] | None = None) -> None:
    """Step 3 for an existing portfolio: exit decisions, then new entries."""
    earnings = earnings or {}
    print("== Step 3a: review holdings")
    for ticker, holding in snap.holdings.items():
        d = decide_exit(holding, by_ticker.get(ticker), peaks.get(ticker, 0.0),
                        ticker in stale, earnings.get(ticker))
        result.holding_decisions.append(d)
        print(f"  {ticker}: {d.action} - {d.reason}")
    if not snap.holdings:
        print("  no holdings")

    print("== Step 3b: scan for opportunities")
    if snap.holdings and stale >= set(snap.holdings):
        result.notes.append("news fetch failed for every held ticker: no new buys (BUY requires fresh news)")
        print("  all news sources failed: skipping new buys")
        return
    # BUY requires fresh news: drop candidates whose own news fetch failed.
    fresh = [s for s in result.scores if not s.news.error]
    skip = {t for t, d in earnings.items() if earnings_soon(d)}
    if skip - set(snap.holdings):
        print(f"  skipping (earnings within {config.EARNINGS_EXIT_DAYS} days): "
              f"{', '.join(sorted(skip - set(snap.holdings)))}")
    candidates = rank_candidates(fresh, exclude=set(snap.holdings) | skip)
    result.candidates = candidates
    for s in candidates:
        a = s.analysis
        print(f"  candidate {s.ticker}: score {s.score} {s.recommendation}, news {s.news_score:+.1f}, "
              f"earnings {s.earnings_boost:+.1f}, momentum {a.momentum}, RSI {a.rsi}, "
              f"{a.price_vs_ma_pct:+.2f}% vs MA, RS {_rs(s)}, vol {a.volatility_pct:.2f}%")
    if not candidates:
        print("  no candidates meet entry rules")
        return
    remaining = sum(1 for d in result.holding_decisions if d.action != "SELL")
    if remaining >= config.MAX_POSITIONS:
        notes = plan_rotations(result.holding_decisions, candidates, by_ticker, snap.holdings,
                               config.MAX_POSITIONS - remaining)
        for note in notes:
            print(f"  {note}")
        result.notes += notes
        remaining = sum(1 for d in result.holding_decisions if d.action != "SELL")
        if remaining >= config.MAX_POSITIONS:
            result.notes.append(f"max positions ({config.MAX_POSITIONS}) reached and no candidate "
                                f"beats the weakest holding by {config.ROTATION_MIN_REL_STR_EDGE:g} RS points")
            return
    sells = [d for d in result.holding_decisions if d.action == "SELL"]
    # Value holdings at fresh prices where available, and assume sells fill.
    held_value = sum(h.shares * (by_ticker[t].analysis.price
                                 if t in by_ticker and by_ticker[t].analysis else h.entry_price)
                     for t, h in snap.holdings.items())
    total = snap.cash + held_value
    cash = snap.cash + sum(d.shares * d.price for d in sells)
    result.buy_decisions = plan_buys(candidates, config.MAX_POSITIONS - remaining, cash, total)
    for d in result.buy_decisions:
        print(f"  selected {d.ticker}: {_fmt_number(d.shares)} shares @ {d.price:.2f} "
              f"(${d.shares * d.price:,.2f})")
    if not result.buy_decisions:
        result.notes.append(f"insufficient cash to buy {candidates[0].ticker} within risk limits")


def _initial_portfolio(result: CycleResult, snap: Snapshot,
                       earnings: dict[str, date] | None = None) -> None:
    """Step 3 for a fresh start: pick and size the initial positions."""
    print("== Step 3: initial portfolio (no trades logged yet)")
    earnings = earnings or {}
    picks = pick_initial_portfolio([s for s in result.scores
                                    if not earnings_soon(earnings.get(s.ticker))])
    result.candidates = picks
    if not picks:
        result.notes.append("no tickers qualify for the initial portfolio; staying in cash")
        print("  no qualifying tickers")
        return
    if len(picks) < config.INITIAL_MIN_PICKS:
        result.notes.append(f"only {len(picks)} ticker(s) qualified for the initial portfolio")
    result.buy_decisions = plan_initial_buys(picks, snap.cash)
    for d in result.buy_decisions:
        print(f"  selected {d.ticker}: {_fmt_number(d.shares)} shares @ {d.price:.2f} "
              f"(${d.shares * d.price:,.2f}) - {d.reason}")


def _position_dates(snap: Snapshot, trades: list[dict[str, str]]) -> dict[str, tuple[float, str]]:
    """{ticker: (entry price, opened date)} for the snapshot's holdings."""
    opened = {t: day for t, (_, _, day) in open_positions(trades).items()}
    return {t: (h.entry_price, opened.get(t, "")) for t, h in snap.holdings.items()}


def _start(exporter_url: str, result: CycleResult) -> tuple[Snapshot, list[dict[str, str]]]:
    """Step 1: load portfolio state into result."""
    print("== Step 1: portfolio state")
    snap = get_snapshot(exporter_url)
    result.snapshot = snap
    trades = read_trades(config.TRADES_CSV)
    result.initial = not trades
    print(f"  source={snap.source} cash=${snap.cash:,.2f} total=${snap.total_value:,.2f} "
          f"holdings={', '.join(f'{t}:{_fmt_number(h.shares)}' for t, h in snap.holdings.items()) or 'none'}")
    return snap, trades


def _execute_all(result: CycleResult, dry_run: bool) -> None:
    """Step 4: execute sells, then buys (skipped if any sell failed)."""
    print("== Step 4: execute trades" + (" (dry run)" if dry_run else ""))
    sells = [d for d in result.holding_decisions if d.action == "SELL"]
    for d in sells:
        execute(d, dry_run)
    sell_failed = any(d.error for d in sells)
    for d in result.buy_decisions:
        if sell_failed:
            d.error = "skipped because a sell failed"
        else:
            execute(d, dry_run)
    if not result.trades:
        print("  no trades")


def _previous_close(ticker: str) -> float | None:
    """Previous session's close from yfinance, or None if unavailable."""
    import yfinance as yf  # imported lazily: slow import

    try:
        value = yf.Ticker(ticker).fast_info["previousClose"]
        return float(value) if value and value > 0 else None
    except Exception:  # network/parse errors from yfinance
        return None


def run_risk_check(exporter_url: str, dry_run: bool) -> CycleResult:
    """Hourly risk check: sell holdings that hit their stop loss or trailing stop.

    Skips technical analysis; fetches last prices, updates the recorded peaks
    and compares price to the stop levels. News is checked for held tickers
    only: a score <= NEWS_PANIC_SELL combined with a price below the previous
    close liquidates the position (neither signal sells on its own).
    """
    result = CycleResult(risk_only=True)
    snap, trades = _start(exporter_url, result)
    result.initial = False

    print("== Step 2: risk check (prices vs. stop levels; no analysis, no new entries)")
    if not snap.holdings:
        print("  no holdings")
        _execute_all(result, dry_run)
        return result
    prices, errors = risk.fetch_prices(list(snap.holdings))
    result.errors = errors
    peaks = risk.update_peaks(_position_dates(snap, trades), prices, save=not dry_run)
    with requests.Session() as session:
        news = {t: _news_cached(t, session) for t in snap.holdings}
    for ticker, holding in snap.holdings.items():
        price = prices.get(ticker)
        if price is None:
            d = Decision(ticker, "HOLD", f"no price available ({errors.get(ticker, 'unknown error')})")
        else:
            levels = risk.stop_levels(ticker, holding.entry_price, peaks.get(ticker, price))
            reason = risk.check_stops(levels, price)
            pnl = (price / holding.entry_price - 1) * 100 if holding.entry_price else float("nan")
            trail = (f"trail stop {levels.trailing_stop:.2f}" if levels.trailing_active
                     else "trail inactive")
            status = (f"price {price:.2f}, P&L {pnl:+.1f}%, peak {levels.peak:.2f}, "
                      f"stop {levels.stop_loss:.2f}, {trail}")
            n = news[ticker]
            if n.error:
                status += f", news unavailable ({n.error})"
            else:
                status += f", news {n.score:+.1f}"
                if not reason and n.score <= config.NEWS_PANIC_SELL:
                    prev = _previous_close(ticker)
                    if prev is not None and price < prev:
                        reason = (f"negative news {n.score:+.1f} <= {config.NEWS_PANIC_SELL:g} "
                                  f"and price below previous close {prev:.2f}")
            d = (Decision(ticker, "SELL", f"{reason}; {status}", holding.shares, round(price, 2))
                 if reason else Decision(ticker, "HOLD", status))
        result.holding_decisions.append(d)
        print(f"  {ticker}: {d.action} - {d.reason}")

    _execute_all(result, dry_run)
    return result


def run_cycle(exporter_url: str, dry_run: bool) -> CycleResult:
    """Run steps 1-4 and return the result for summarizing."""
    result = CycleResult()
    snap, trades = _start(exporter_url, result)

    print("== Step 2: score tickers (technicals + news search + position)")
    tickers = list(dict.fromkeys([*snap.holdings, *config.STOCK_UNIVERSE]))
    result.scores, result.errors = score_universe(tickers, trades=trades)
    by_ticker = {s.ticker: s for s in result.scores}
    for ticker, msg in result.errors.items():
        print(f"  WARNING: analysis failed for {ticker}: {msg}", file=sys.stderr)
    for s in result.scores:
        if s.news.error:
            print(f"  WARNING: news search failed for {s.ticker}: {s.news.error}", file=sys.stderr)
    print("  " + format_table(result.scores).replace("\n", "\n  "))
    print("  Top headlines:")
    for s in result.scores:
        print("  " + format_headlines(s, limit=3).replace("\n", "\n  "))

    extra = [t for t in scan_market_headlines() if t not in by_ticker]
    print("  Headline scan: " + (", ".join(extra) or "no new tickers"))
    if extra:
        extra_scores, extra_errors = score_universe(extra, trades=trades)
        result.scores = sorted([*result.scores, *extra_scores], key=lambda s: (
            s.score, s.analysis.momentum if s.analysis else 0), reverse=True)
        result.errors.update(extra_errors)
        by_ticker.update({s.ticker: s for s in extra_scores})
        for s in extra_scores:
            print(f"  {s.ticker}: score {s.score} {s.recommendation}, news {s.news_score:+.1f}")

    stale = frozenset(t for t in snap.holdings if t in by_ticker and by_ticker[t].news.error)
    for t in sorted(stale):
        print(f"  WARNING: news stale for held {t}; suppressing discretionary news-driven "
              f"sells/buys (stop loss and trailing stop still apply)", file=sys.stderr)

    prices = {t: s.analysis.price for t, s in by_ticker.items() if s.analysis}
    peaks = risk.update_peaks(_position_dates(snap, trades), prices, save=not dry_run)

    earnings = fetch_earnings_dates(list(by_ticker))
    soon = sorted(t for t, d in earnings.items() if earnings_soon(d))
    print("  Earnings within window: " + (", ".join(f"{t} ({earnings[t]})" for t in soon) or "none"))

    if result.initial:
        _initial_portfolio(result, snap, earnings)
    else:
        _review_and_scan(result, snap, by_ticker, peaks, stale, earnings)

    _execute_all(result, dry_run)
    return result


def print_summary(result: CycleResult, dry_run: bool) -> None:
    """Step 5: print a human-readable cycle summary."""
    print("== Step 5: cycle summary")
    snap = result.snapshot
    if snap:
        roi = (snap.total_value - config.INITIAL_CASH) / config.INITIAL_CASH * 100
        print(f"  Start of cycle: total ${snap.total_value:,.2f} | cash ${snap.cash:,.2f} | ROI {roi:+.2f}%")
    if result.initial:
        print("  Mode: initial portfolio")
    if result.risk_only:
        print("  Mode: risk check only (stops and trailing stops)")
    for d in result.holding_decisions:
        if d.action == "HOLD":
            print(f"  HOLD {d.ticker}: {d.reason}")
    for d in result.trades:
        status = "DRY-RUN" if dry_run else ("OK" if d.executed else f"FAILED ({d.error})")
        print(f"  {d.action} {_fmt_number(d.shares)} {d.ticker} @ {d.price:.2f} [{status}] - {d.reason}")
    if not result.trades:
        print("  No trades this cycle.")
    for note in result.notes:
        print(f"  Note: {note}")
    if not dry_run and any(d.executed for d in result.trades):
        state = load_portfolio(config.TRADES_CSV)
        print(f"  Cash after trades: ${state.cash:,.2f} | positions: "
              f"{', '.join(f'{t}:{_fmt_number(p.shares)}' for t, p in state.positions.items()) or 'none'}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Run one Mock Stock Challenge trading cycle.")
    parser.add_argument("--dry-run", action="store_true", help="Decide but do not log trades")
    parser.add_argument("--force", action="store_true", help="Run even outside market hours")
    parser.add_argument("--risk-only", action="store_true",
                        help="Only check stop losses and trailing stops (no analysis, news or buys); "
                             "for the hourly check")
    parser.add_argument("--exporter-url", default=config.PORTFOLIO_EXPORTER_URL,
                        help=f"Portfolio exporter base URL (default: {config.PORTFOLIO_EXPORTER_URL})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns non-zero if any trade failed."""
    args = parse_args(argv)
    now = datetime.now(ET)
    print(f"Trading cycle at {now:%Y-%m-%d %H:%M %Z}")
    if not is_market_open(now) and not args.force:
        print("Market closed (9:30-16:00 ET, Mon-Fri); skipping cycle. Use --force to override.")
        return 0
    try:
        cycle = run_risk_check if args.risk_only else run_cycle
        result = cycle(args.exporter_url, args.dry_run)
    except TradeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print_summary(result, args.dry_run)
    return 1 if any(d.error for d in result.trades) else 0


if __name__ == "__main__":
    sys.exit(main())
