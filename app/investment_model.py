#!/usr/bin/env python3
"""Investment model for the Mock Stock Challenge.

Combines four inputs into a composite 1-10 score and a recommendation
(STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL) for each ticker:

  - Technical score (1-10): momentum from tech_analysis.py, adjusted for
    RSI extremes, price position vs. the 20MA and the classic entry setup.
  - News sentiment (-5..+5): recent headlines found by searching the web
    ("{ticker} stock news today" and "{ticker} earnings" on Google News, plus
    the Yahoo Finance headline feed), scored with a keyword lexicon for
    catalysts such as earnings beats/misses, launches, partnerships, lawsuits
    and regulation.
  - Earnings catalyst: +EARNINGS_CATALYST_BOOST if the company reported a
    positive quarter (beat / raised guidance) in the last EARNINGS_SIGNAL_DAYS
    days, to ride post-earnings drift.
  - Position context: for held tickers, P&L vs. entry, proximity to the
    ticker's stop loss, and how long the position has been open.

    composite = TECHNICAL_WEIGHT * technical + NEWS_WEIGHT * (news + 5)
                + earnings boost + position adjustment, clamped to 1-10

Usage:
    investment_model.py                  # score all STOCK_UNIVERSE tickers
    investment_model.py AAPL NVDA        # specific tickers
    investment_model.py --headlines      # also list scored headlines
    investment_model.py --json           # JSON output
    investment_model.py --no-news        # technical + position only (offline)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests

import config
from log_trade import EPSILON, TradeError, read_trades
from tech_analysis import Analysis, analyze

RECOMMENDATIONS: tuple[str, ...] = ("STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL")
BUY_RECOMMENDATIONS: frozenset[str] = frozenset({"STRONG_BUY", "BUY"})
SELL_RECOMMENDATIONS: frozenset[str] = frozenset({"SELL", "STRONG_SELL"})

GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
YAHOO_NEWS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) mock-stock-challenge/1.0"}
NEWS_CACHE_SECONDS = 900  # 15 minutes
NEWS_CACHE_DIR = Path.home() / ".hermes" / "cache" / "stocks" / "news"


# --- Data types -----------------------------------------------------------------

@dataclass
class NewsItem:
    """A single headline and its sentiment."""

    title: str
    source: str
    published: str          # ISO timestamp, "" if unknown
    link: str
    score: int              # -3..+3
    label: str              # positive, negative or neutral
    matches: list[str] = field(default_factory=list)
    earnings: bool = False  # reports on a quarterly result (not a preview)
    earnings_score: int = 0  # >0 beat/raise, <0 miss/cut; 0 if not earnings


@dataclass
class NewsSentiment:
    """Aggregated news sentiment for a ticker."""

    ticker: str
    score: float            # -5..+5
    label: str              # positive, negative or neutral
    headlines: list[NewsItem] = field(default_factory=list)
    error: str = ""

    @property
    def counts(self) -> dict[str, int]:
        """Number of positive/negative/neutral headlines."""
        result = {"positive": 0, "negative": 0, "neutral": 0}
        for h in self.headlines:
            result[h.label] += 1
        return result


@dataclass
class PositionContext:
    """An open position as seen by the model."""

    shares: float
    entry_price: float
    opened: str             # ISO date the position was opened, "" if unknown
    days_held: int | None
    pnl_pct: float | None   # vs. current price; None without a price


@dataclass
class TickerScore:
    """Composite model output for a ticker."""

    ticker: str
    score: float                    # 1-10
    recommendation: str
    technical_score: float          # 1-10
    news_score: float               # -5..+5
    position_adjustment: float
    analysis: Analysis | None
    news: NewsSentiment
    position: PositionContext | None
    reasons: list[str] = field(default_factory=list)
    earnings_boost: float = 0.0     # 0 or EARNINGS_CATALYST_BOOST

    @property
    def held(self) -> bool:
        """True if the ticker is currently held."""
        return self.position is not None

    @property
    def rel_str(self) -> float | None:
        """20-day relative strength vs. SPY, if known."""
        return self.analysis.rel_str if self.analysis else None

    @property
    def has_catalyst(self) -> bool:
        """True with a recent earnings beat or strongly positive news."""
        return self.earnings_boost > 0 or self.news_score >= config.NEWS_HOLD_OVERRIDE


# --- News search and sentiment ----------------------------------------------------

# (pattern, weight). Strong phrases are matched first and removed from the text
# so e.g. "beats estimates" is not also counted as a plain "beats". Generic
# words that are often non-financial ("rise", "SEC", "deal") only count in a
# market context: "shares rise", "SEC probe", "signs deal" (not "Prime Day deals").
_FIN = r"(?:EPS|earnings|revenue|sales|profit|results)"
_POSITIVE: list[tuple[str, int]] = [
    (r"beats? (?:\w+ )?(?:estimates|expectations|forecasts?|consensus)", 2),
    (r"tops? (?:\w+ )?(?:estimates|expectations|forecasts?)", 2),
    (rf"{_FIN} (?:\S+ ){{0,2}}?(?:beats?|tops?|exceed(?:s|ed)?|surpass(?:es|ed)?)", 2),
    (r"record (?:revenue|sales|profit|earnings|quarter|high)", 2),
    (r"raises? (?:\S+ ){0,3}?(?:guidance|outlook|forecast)", 2),
    (r"(?:raises|lifts|boosts) (?:\w+ )?price target", 2),
    (r"price target (?:raised|hike|boost)", 2),
    (r"all-time high", 2),
    (r"upgraded?|upgrades", 2),
    (r"soar(?:s|ed|ing)?|surg(?:e|es|ed|ing)|skyrocket(?:s|ed|ing)?|blowout", 2),
    (r"beats?|beat", 1),
    (r"launch(?:es|ed|ing)?|unveil(?:s|ed|ing)?|debut(?:s|ed)?|rolls? out", 1),
    (r"partnership|partners? with|teams? up|collaborat(?:ion|es)|alliance", 1),
    (r"(?:signs?|signed|inks?|inked|strikes?|struck|clinch(?:es|ed)?|lands?|landed|secures?|secured"
     r"|wins?|won|reach(?:es|ed)?|announces?|announced|seals?|sealed|closes?|closed) "
     r"(?:\S+ ){0,3}?deal|deal (?:with|to (?:buy|acquire)|worth)", 1),
    (r"contract|wins?|won|approv(?:al|ed|es)", 1),
    (r"buyback|repurchase|dividend (?:hike|increase)", 1),
    (rf"{_FIN} (?:grows?|grew|ris(?:e|es)|rose|increas(?:e|es|ed))", 1),
    (r"(?:shares?|stocks?|price|futures) (?:\S+ ){0,2}?(?:ris(?:e|es|ing)|rose)"
     r"|(?:ris(?:e|es)|rose) (?:\d+(?:\.\d+)?%|to (?:an? )?(?:record|high))", 1),
    (r"jump(?:s|ed)?|rall(?:y|ies|ied)|gain(?:s|ed)?|climb(?:s|ed)?|rebound(?:s|ed)?", 1),
    (r"bullish|outperform|overweight|strong (?:demand|growth|results|sales)|buy rating", 1),
]
_NEGATIVE: list[tuple[str, int]] = [
    (r"miss(?:es|ed)? (?:\w+ )?(?:estimates|expectations|forecasts?|consensus)", 2),
    (rf"{_FIN} (?:\S+ ){{0,2}}?(?:miss(?:es|ed)?|disappoint(?:s|ed)?|falls? short)", 2),
    (r"(?:cuts?|lowers?|slashes|withdraws) (?:\S+ ){0,3}?(?:guidance|outlook|forecast)", 2),
    (r"(?:weak|soft|disappointing|downbeat) (?:\S+ )?(?:guidance|outlook|forecast)", 2),
    (r"(?:cuts|lowers|slashes) (?:\w+ )?price target|price target (?:cut|lowered)", 2),
    (r"downgraded?|downgrades", 2),
    (r"lawsuit|sued|class action|fraud|antitrust|investigation|probe|subpoena", 2),
    (r"plung(?:e|es|ed)|plummet(?:s|ed)?|tumbl(?:e|es|ed)|crash(?:es|ed)?|recall(?:s|ed)?", 2),
    (r"miss(?:es|ed)?|disappoint(?:s|ed|ing|ment)?|falls? short", 1),
    (r"sues?|fined|fines?(?!-)|penalt(?:y|ies)|settlement", 1),
    (r"regulat(?:ion|ions|or|ors|ory)|crackdown|ban(?:s|ned)?|tariffs?|sanctions?", 1),
    (r"(?-i:SEC|DOJ|FTC)(?= (?:\S+ )?(?:charges?|probes?|investigat\w*|sues?|sued|suing|scrutiny"
     r"|inquiry|complaint|subpoena\w*|lawsuit))|eu commission|european commission", 1),
    (r"layoffs?|job cuts|restructuring", 1),
    (r"fall(?:s|ing)?|fell|drop(?:s|ped)?|slid(?:e|es)?|sink(?:s)?|sank|slump(?:s|ed)?|declin(?:e|es|ed)", 1),
    (r"bearish|underperform|underweight|sell rating|sell-?off|selloff", 1),
    (r"warn(?:s|ed|ing)?|delay(?:s|ed)?|concerns?|fears?|weak(?:er|ness)?|short seller", 1),
]
_POSITIVE_RE = [(re.compile(rf"\b(?:{p})\b", re.I), w) for p, w in _POSITIVE]
_NEGATIVE_RE = [(re.compile(rf"\b(?:{p})\b", re.I), w) for p, w in _NEGATIVE]

# Earnings detection: a headline reports a result if it has earnings context
# and is not a preview/speculation; its direction comes from beat/raise vs.
# miss/cut terms and the share price reaction.
_EARNINGS_CONTEXT_RE = re.compile(
    r"\b(?:earnings|EPS|revenue|guidance|outlook|quarter(?:ly)?|Q[1-4]|results|profit|sales)\b", re.I)
_EARNINGS_PREVIEW_RE = re.compile(
    r"\?|\b(?:ahead of|preview|what to (?:expect|watch)|expected to|set to report|to report|"
    r"before earnings|earnings date|week ahead|on deck|could|will|options traders)\b", re.I)
_EARNINGS_POSITIVE_RE = [re.compile(rf"\b(?:{p})\b", re.I) for p in (
    r"beats?|beat|tops?|exceed(?:s|ed)?|surpass(?:es|ed)?|blowout|crush(?:es|ed)?",
    r"(?:raises?|lifts?|boosts?|hikes?) (?:\S+ ){0,3}?(?:guidance|outlook|forecast)",
    r"record (?:revenue|sales|profit|earnings|quarter)|strong (?:results|quarter|guidance|outlook)",
    r"(?:shares?|stocks?) (?:\S+ ){0,2}?(?:jump\w*|surg\w*|soar\w*|rall\w*|ris(?:e|es|ing)|rose|gain\w*|pop\w*)",
)]
_EARNINGS_NEGATIVE_RE = [re.compile(rf"\b(?:{p})\b", re.I) for p in (
    r"miss(?:es|ed)?|falls? short|disappoint\w*",
    r"(?:cuts?|lowers?|slashes|withdraws) (?:\S+ ){0,3}?(?:guidance|outlook|forecast)",
    r"(?:weak|soft|downbeat) (?:\S+ )?(?:guidance|outlook|forecast|quarter|results)",
    r"(?:shares?|stocks?) (?:\S+ ){0,2}?(?:fall\w*|fell|drop\w*|plung\w*|tumbl\w*|sink\w*|sank|slid\w*|slump\w*)",
)]


def _lexicon_hits(text: str, patterns: list[tuple[re.Pattern[str], int]]) -> tuple[int, list[str]]:
    """Sum the weights of matching patterns, consuming matched text as it goes."""
    total, hits = 0, []
    for regex, weight in patterns:
        for m in regex.finditer(text):
            total += weight
            hits.append(m.group(0).lower())
        text = regex.sub(lambda m: " " * len(m.group(0)), text)
    return total, hits


def score_headline(title: str) -> tuple[int, list[str]]:
    """Score one headline from -3 (very negative) to +3 (very positive).

    Returns (score, matched terms); negative matches are prefixed with "-".
    """
    pos, pos_hits = _lexicon_hits(title, _POSITIVE_RE)
    neg, neg_hits = _lexicon_hits(title, _NEGATIVE_RE)
    score = int(max(-3, min(3, pos - neg)))
    return score, [f"+{h}" for h in pos_hits] + [f"-{h}" for h in neg_hits]


def score_earnings_headline(title: str) -> tuple[bool, int]:
    """Classify a headline as an earnings report and score its direction.

    Returns (is_earnings_report, score) where score > 0 means a beat/raise and
    < 0 a miss/cut. Previews and speculation ("will Nvidia beat?") are not
    reports.
    """
    if not _EARNINGS_CONTEXT_RE.search(title) or _EARNINGS_PREVIEW_RE.search(title):
        return False, 0
    pos = sum(1 for r in _EARNINGS_POSITIVE_RE if r.search(title))
    neg = sum(1 for r in _EARNINGS_NEGATIVE_RE if r.search(title))
    return True, pos - neg


def _label(value: float, threshold: float) -> str:
    """Map a signed value to positive/negative/neutral."""
    if value >= threshold:
        return "positive"
    if value <= -threshold:
        return "negative"
    return "neutral"


def _parse_rss(xml_text: str, default_source: str) -> list[tuple[str, str, datetime | None, str]]:
    """Parse an RSS feed into (title, source, published, link) tuples."""
    items = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):  # Google appends " - Source"
            title = title[: -len(source) - 3].strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = None
        items.append((title, source or default_source, published, (item.findtext("link") or "").strip()))
    return items


def relevance_pattern(ticker: str) -> re.Pattern[str]:
    """Regex matching headlines that mention ticker or its NEWS_ALIASES."""
    names = [ticker, *config.NEWS_ALIASES.get(ticker, [])]
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, names)) + r")(?!\w)", re.I)


def search_news(ticker: str, session: requests.Session | None = None) -> list[NewsItem]:
    """Search the web for recent headlines about ticker.

    Queries Google News for ``NEWS_QUERY_TEMPLATE`` and
    ``EARNINGS_QUERY_TEMPLATE`` plus the Yahoo Finance headline feed, keeps
    headlines that mention the ticker or company, dedupes by title, drops
    headlines older than ``NEWS_LOOKBACK_HOURS`` (or ``EARNINGS_SIGNAL_DAYS``
    if longer) and returns up to ``NEWS_MAX_HEADLINES``, newest first. Raises
    RuntimeError only if every source fails.
    """
    http = session or requests.Session()
    sources = [(GOOGLE_NEWS_URL.format(query=quote_plus(template.format(ticker=ticker))), "Google News")
               for template in (config.NEWS_QUERY_TEMPLATE, config.EARNINGS_QUERY_TEMPLATE)]
    sources.append((YAHOO_NEWS_URL.format(ticker=quote_plus(ticker)), "Yahoo Finance"))
    raw: list[tuple[str, str, datetime | None, str]] = []
    failures: list[str] = []
    for url, name in sources:
        try:
            resp = http.get(url, headers=HTTP_HEADERS, timeout=config.NEWS_TIMEOUT_SECONDS)
            resp.raise_for_status()
            raw.extend(_parse_rss(resp.text, name))
        except (requests.RequestException, ET.ParseError) as e:
            failures.append(f"{name}: {e}")
    if failures and not raw:
        raise RuntimeError("; ".join(failures))

    lookback = max(config.NEWS_LOOKBACK_HOURS, config.EARNINGS_SIGNAL_DAYS * 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)
    relevant = relevance_pattern(ticker)
    seen: set[str] = set()
    fresh = []
    for title, source, published, link in raw:
        key = re.sub(r"[^a-z0-9]", "", title.lower())
        if (key in seen or not relevant.search(title)
                or (published is not None and published < cutoff)):
            continue
        seen.add(key)
        fresh.append((title, source, published, link))
    fresh.sort(key=lambda r: r[2] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    items = []
    for title, source, published, link in fresh[: config.NEWS_MAX_HEADLINES]:
        score, hits = score_headline(title)
        is_earnings, earnings_score = score_earnings_headline(title)
        items.append(NewsItem(title, source, published.isoformat() if published else "",
                              link, score, _label(score, 1), hits, is_earnings, earnings_score))
    return items


def aggregate_sentiment(ticker: str, headlines: list[NewsItem]) -> NewsSentiment:
    """Combine headline scores into a -5..+5 sentiment score.

    score = 5 * net / (gross + NEWS_DAMPING), where net and gross are the
    recency-weighted sum and absolute sum of headline scores. The result
    reflects the balance of good vs. bad news; the damping term keeps one
    or two headlines from producing an extreme score.
    """
    now = datetime.now(timezone.utc)
    net = gross = 0.0
    for h in headlines:
        if h.published:
            age_h = (now - datetime.fromisoformat(h.published)).total_seconds() / 3600
            weight = 1.0 if age_h <= 24 else 0.6
        else:
            weight = 0.5
        net += weight * h.score
        gross += weight * abs(h.score)
    score = round(5 * net / (gross + config.NEWS_DAMPING), 1)
    return NewsSentiment(ticker, score, _label(score, 1), headlines)


def earnings_catalyst(ticker: str, headlines: list[NewsItem],
                      now: datetime | None = None) -> tuple[float, list[str]]:
    """Score boost for a positive earnings report in the last EARNINGS_SIGNAL_DAYS days.

    Returns (EARNINGS_CATALYST_BOOST, reasons) when dated earnings-report
    headlines in the window lean positive (more beat/raise headlines than
    miss/cut ones), else (0.0, reasons). The benchmark ETF never gets a boost.
    """
    if ticker.upper() == config.BENCHMARK_TICKER:
        return 0.0, []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=config.EARNINGS_SIGNAL_DAYS)
    recent = [h for h in headlines
              if h.earnings and h.published and datetime.fromisoformat(h.published) >= cutoff]
    beats = sum(1 for h in recent if h.earnings_score > 0)
    misses = sum(1 for h in recent if h.earnings_score < 0)
    if beats and beats > misses:
        return config.EARNINGS_CATALYST_BOOST, [f"earnings beat ({beats}+/{misses}-) "
                                                f"+{config.EARNINGS_CATALYST_BOOST:g}"]
    if misses > beats:
        return 0.0, [f"earnings miss ({beats}+/{misses}-)"]
    return 0.0, []


def news_sentiment(ticker: str, session: requests.Session | None = None) -> NewsSentiment:
    """Search news for ticker and score it. Failures yield a neutral score with error set."""
    try:
        return aggregate_sentiment(ticker, search_news(ticker, session))
    except Exception as e:  # network/parse failures must not stop the cycle
        return NewsSentiment(ticker, 0.0, "neutral", error=str(e))


def _news_cached(ticker: str, session: requests.Session | None = None) -> NewsSentiment:
    """news_sentiment() with a file cache; errored results are never cached."""
    path = NEWS_CACHE_DIR / f"{ticker.upper()}.json"
    try:
        entry = json.loads(path.read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])).total_seconds()
        if not entry.get("error") and 0 <= age < NEWS_CACHE_SECONDS:
            return NewsSentiment(ticker, entry["sentiment"], _label(entry["sentiment"], 1),
                                 [NewsItem(**h) for h in entry["headlines"]])
    except Exception:  # missing/corrupt cache: fall through to a fresh fetch
        pass
    news = news_sentiment(ticker, session)
    if not news.error:
        entry = {"ticker": ticker.upper(), "sentiment": news.score,
                 "headlines": [asdict(h) for h in news.headlines],
                 "fetched_at": datetime.now(timezone.utc).isoformat(), "error": None}
        try:
            NEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entry))
        except OSError:
            pass  # caching is best-effort
    return news


def news_for_tickers(tickers: list[str]) -> dict[str, NewsSentiment]:
    """Fetch news sentiment for several tickers concurrently."""
    with requests.Session() as session, ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(lambda t: _news_cached(t, session), tickers)
        return dict(zip(tickers, results))


def scan_market_headlines(session: requests.Session | None = None) -> list[str]:
    """Tickers from ``WATCHLIST_EXTRA`` seen in positive broad-market headlines.

    Fetches the ``HEADLINE_SCAN_QUERIES`` feeds, scores each headline and
    keeps those with a positive score. Only watchlist symbols (whole-word,
    uppercase) are extracted, never free-form text. Tickers already in
    STOCK_UNIVERSE are excluded. Returns at most ``HEADLINE_SCAN_MAX_TICKERS``,
    most-mentioned first. Feed failures are skipped; returns [] if all fail.
    """
    http = session or requests.Session()
    watch = [t for t in dict.fromkeys(t.upper() for t in config.WATCHLIST_EXTRA)
             if t not in config.STOCK_UNIVERSE]
    patterns = {t: re.compile(rf"(?<![\w.$]){re.escape(t)}(?![\w.])") for t in watch}
    mentions: dict[str, int] = {}
    seen: set[str] = set()
    for query in config.HEADLINE_SCAN_QUERIES:
        try:
            resp = http.get(GOOGLE_NEWS_URL.format(query=quote_plus(query)), headers=HTTP_HEADERS,
                            timeout=config.NEWS_TIMEOUT_SECONDS)
            resp.raise_for_status()
            items = _parse_rss(resp.text, "Google News")
        except (requests.RequestException, ET.ParseError):
            continue
        cutoff = datetime.now(timezone.utc) - timedelta(hours=config.NEWS_LOOKBACK_HOURS)
        for title, _source, published, _link in items:
            key = re.sub(r"[^a-z0-9]", "", title.lower())
            if key in seen or (published is not None and published < cutoff):
                continue
            seen.add(key)
            if score_headline(title)[0] <= 0:
                continue
            for ticker, pattern in patterns.items():
                if pattern.search(title):
                    mentions[ticker] = mentions.get(ticker, 0) + 1
    ranked = sorted(mentions, key=lambda t: (-mentions[t], t))
    return ranked[: config.HEADLINE_SCAN_MAX_TICKERS]


# --- Position context -------------------------------------------------------------

def open_positions(trades: list[dict[str, str]]) -> dict[str, tuple[float, float, str]]:
    """Replay trades into {ticker: (shares, avg entry price, opened date)}.

    The opened date is the date of the BUY that took the position from flat;
    adding to an open position keeps the original date.
    """
    positions: dict[str, tuple[float, float, str]] = {}
    for row in trades:
        try:
            ticker = row["ticker"].strip().upper()
            action = row["action"].strip().upper()
            shares = float(row["shares"])
            price = float(row["price"])
            day = row.get("date", "").strip()
        except (KeyError, ValueError, AttributeError):
            continue  # compute_portfolio already warns about malformed rows
        held, entry, opened = positions.get(ticker, (0.0, 0.0, day))
        if action == "BUY":
            total = held + shares
            positions[ticker] = (total, (held * entry + shares * price) / total, opened if held else day)
        elif action == "SELL":
            held -= shares
            if held <= EPSILON:
                positions.pop(ticker, None)
            else:
                positions[ticker] = (held, entry, opened)
    return positions


def position_context(ticker: str, trades: list[dict[str, str]], price: float | None,
                     today: date | None = None) -> PositionContext | None:
    """Build the position context for ticker, or None if it is not held."""
    pos = open_positions(trades).get(ticker)
    if pos is None:
        return None
    shares, entry, opened = pos
    try:
        days = ((today or date.today()) - date.fromisoformat(opened)).days
    except ValueError:
        days = None
    pnl = (price / entry - 1) * 100 if price and entry else None
    return PositionContext(shares, round(entry, 4), opened, days,
                           round(pnl, 2) if pnl is not None else None)


# --- Scoring ----------------------------------------------------------------------

def passes_entry(a: Analysis) -> bool:
    """True if the analysis satisfies the classic technical entry rules."""
    return (a.price_vs_ma_pct >= config.ENTRY_MIN_PCT_ABOVE_MA
            and a.momentum >= config.ENTRY_MIN_MOMENTUM
            and config.ENTRY_RSI_MIN <= a.rsi <= config.ENTRY_RSI_MAX)


def technical_score(a: Analysis) -> tuple[float, list[str]]:
    """Score the technical setup 1-10 from momentum, RSI and MA position."""
    score = float(a.momentum)
    reasons = [f"momentum {a.momentum}"]
    if passes_entry(a):
        score += 1
        reasons.append("entry setup")
    if a.price >= a.ma20:
        score += 0.5
    else:
        score -= 1
        reasons.append(f"below 20MA ({a.price_vs_ma_pct:+.1f}%)")
    if a.rsi > config.RSI_OVERBOUGHT:
        score -= 1.5
        reasons.append(f"overbought RSI {a.rsi:.0f}")
    elif a.rsi < config.RSI_OVERSOLD:
        score -= 0.5
        reasons.append(f"oversold RSI {a.rsi:.0f}")
    return max(1.0, min(10.0, score)), reasons


def position_adjustment(ticker: str, pos: PositionContext | None) -> tuple[float, list[str]]:
    """Adjust the composite for an existing position (-2..+1).

    There is no take-profit penalty: winners are managed by the trailing stop.
    """
    if pos is None or pos.pnl_pct is None:
        return 0.0, []
    adj, reasons = 0.0, [f"held {pos.days_held if pos.days_held is not None else '?'}d, "
                         f"P&L {pos.pnl_pct:+.1f}%"]
    stop = config.STOP_LOSS_BY_TICKER.get(ticker, config.DEFAULT_STOP_LOSS_PCT)
    if pos.pnl_pct <= -0.75 * stop:
        adj -= 1.0
        reasons.append("near stop loss")
    elif pos.pnl_pct > 0:
        adj += 0.3
    if (pos.days_held is not None and pos.days_held >= config.STALE_POSITION_DAYS
            and pos.pnl_pct < config.STALE_POSITION_MIN_GAIN_PCT):
        adj -= 0.5
        reasons.append("stale position")
    return max(-2.0, min(1.0, adj)), reasons


def qualifies_for_entry(s: TickerScore) -> bool:
    """True if a non-held ticker meets the model-augmented entry rules.

    Requires a BUY/STRONG_BUY that is not overbought (RSI <= RSI_OVERBOUGHT)
    and either passes the classic setup (>= ENTRY_MIN_PCT_ABOVE_MA above the
    20MA, momentum, RSI <= ENTRY_RSI_MAX) or is a STRONG_BUY above the 20MA.
    A STRONG_BUY with an earnings beat may enter even below the 20MA.
    """
    a = s.analysis
    if a is None or s.held or s.recommendation not in BUY_RECOMMENDATIONS:
        return False
    if a.rsi > config.RSI_OVERBOUGHT:
        return False
    if a.price <= a.ma20:
        return s.recommendation == "STRONG_BUY" and s.earnings_boost > 0
    return passes_entry(a) or s.recommendation == "STRONG_BUY"


def recommend(score: float) -> str:
    """Map a 1-10 composite score to a recommendation."""
    if score >= config.STRONG_BUY_SCORE:
        return "STRONG_BUY"
    if score >= config.BUY_SCORE:
        return "BUY"
    if score <= config.STRONG_SELL_SCORE:
        return "STRONG_SELL"
    if score <= config.SELL_SCORE:
        return "SELL"
    return "HOLD"


def combine(ticker: str, analysis: Analysis | None, news: NewsSentiment,
            position: PositionContext | None) -> TickerScore:
    """Combine precomputed inputs into a TickerScore."""
    reasons: list[str] = []
    if analysis is None:
        tech = 5.0
        reasons.append("no technical data")
    else:
        tech, tech_reasons = technical_score(analysis)
        reasons += tech_reasons
    if analysis is not None and analysis.rel_str is not None:
        reasons.append(f"RS {analysis.rel_str:+.1f}")
    boost = 0.0
    if news.error:
        reasons.append("news unavailable")
    else:
        c = news.counts
        reasons.append(f"news {news.score:+.1f} ({c['positive']}+/{c['negative']}-/{c['neutral']}=)")
        boost, earnings_reasons = earnings_catalyst(ticker, news.headlines)
        reasons += earnings_reasons
    adj, pos_reasons = position_adjustment(ticker, position)
    reasons += pos_reasons

    composite = (config.TECHNICAL_WEIGHT * tech
                 + config.NEWS_WEIGHT * (news.score + 5)
                 + boost
                 + adj)
    composite = round(max(1.0, min(10.0, composite)), 1)
    # Without price data we cannot trade on the ticker, so never recommend a buy.
    rec = recommend(composite) if analysis else "HOLD"
    return TickerScore(ticker, composite, rec, round(tech, 1), news.score, adj,
                       analysis, news, position, reasons, boost)


def score_ticker(ticker: str, *, analysis: Analysis | None = None,
                 news: NewsSentiment | None = None,
                 trades: list[dict[str, str]] | None = None,
                 fetch_news: bool = True) -> TickerScore:
    """Score one ticker 1-10 and recommend STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL.

    Any input not supplied is fetched: technical analysis via yfinance, news
    via web search, and the position from the trade log.
    """
    ticker = ticker.upper()
    if analysis is None:
        results, _ = analyze([ticker])
        analysis = results[0] if results else None
    if news is None:
        news = news_sentiment(ticker) if fetch_news else NewsSentiment(ticker, 0.0, "neutral",
                                                                       error="news disabled")
    if trades is None:
        trades = read_trades(config.TRADES_CSV)
    position = position_context(ticker, trades, analysis.price if analysis else None)
    return combine(ticker, analysis, news, position)


def score_universe(tickers: list[str], trades: list[dict[str, str]] | None = None,
                   fetch_news: bool = True) -> tuple[list[TickerScore], dict[str, str]]:
    """Score several tickers, best first. Returns (scores, technical errors).

    Prices are downloaded in one batch and news is searched concurrently.
    """
    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    analyses, errors = analyze(tickers)
    by_ticker = {a.ticker: a for a in analyses}
    if fetch_news:
        news = news_for_tickers(tickers)
    else:
        news = {t: NewsSentiment(t, 0.0, "neutral", error="news disabled") for t in tickers}
    if trades is None:
        trades = read_trades(config.TRADES_CSV)
    scores = [score_ticker(t, analysis=by_ticker.get(t), news=news[t], trades=trades)
              if t in by_ticker else combine(t, None, news[t], None)
              for t in tickers]
    scores.sort(key=lambda s: (s.score, s.analysis.momentum if s.analysis else 0), reverse=True)
    return scores, errors


# --- Output -----------------------------------------------------------------------

def format_table(scores: list[TickerScore]) -> str:
    """Render scores as a fixed-width text table."""
    header = (f"{'TICKER':<7}{'PRICE':>10}{'TECH':>6}{'NEWS':>6}{'EARN':>6}{'POS':>6}"
              f"{'SCORE':>7}  {'REC':<12}REASONS")
    lines = [header, "-" * len(header)]
    for s in scores:
        price = f"{s.analysis.price:>10.2f}" if s.analysis else f"{'n/a':>10}"
        lines.append(f"{s.ticker:<7}{price}{s.technical_score:>6.1f}{s.news_score:>+6.1f}"
                     f"{s.earnings_boost:>+6.1f}{s.position_adjustment:>+6.1f}{s.score:>7.1f}  "
                     f"{s.recommendation:<12}"
                     f"{'; '.join(s.reasons)}")
    return "\n".join(lines)


def format_headlines(s: TickerScore, limit: int = 8) -> str:
    """Render the most relevant scored headlines for a ticker."""
    if s.news.error:
        return f"  {s.ticker}: news unavailable ({s.news.error})"
    if not s.news.headlines:
        return f"  {s.ticker}: no recent headlines"
    # Show headlines that moved the score first, then the newest neutral ones.
    ranked = sorted(s.news.headlines, key=lambda h: abs(h.score), reverse=True)[:limit]
    lines = [f"  {s.ticker} ({s.news.label} {s.news.score:+.1f}):"]
    for h in ranked:
        lines.append(f"    [{h.score:+d}] {h.title} ({h.source})")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Score tickers with the investment model.")
    parser.add_argument("tickers", nargs="*", type=str.upper,
                        help="Tickers to score (default: STOCK_UNIVERSE)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table")
    parser.add_argument("--headlines", action="store_true", help="List scored headlines")
    parser.add_argument("--no-news", action="store_true",
                        help="Skip the news search (technical + position only)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 if at least one ticker had technical data."""
    args = parse_args(argv)
    tickers = args.tickers or list(config.STOCK_UNIVERSE)
    try:
        scores, errors = score_universe(tickers, fetch_news=not args.no_news)
    except TradeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        payload: dict[str, Any] = {"scores": [asdict(s) for s in scores], "errors": errors}
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_table(scores))
        if args.headlines:
            print("\nHeadlines:")
            for s in scores:
                print(format_headlines(s))
        for ticker, msg in errors.items():
            print(f"ERROR {ticker}: {msg}", file=sys.stderr)
    return 0 if any(s.analysis for s in scores) else 1


if __name__ == "__main__":
    sys.exit(main())
