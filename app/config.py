#!/usr/bin/env python3
"""Configuration constants for the Mock Stock Challenge.

``TRADES_CSV`` and ``PORTFOLIO_EXPORTER_PORT`` can be overridden through the
``STOCKS_TRADES_CSV`` and ``STOCKS_EXPORTER_PORT`` environment variables,
which is handy for testing against a scratch trade log.
"""

import os

# --- Contest rules ---------------------------------------------------------
# Long-only stocks/ETFs, no shorting/options/leverage/margin, cash never
# negative (enforced by log_trade.validate_trade), fractional shares allowed,
# dividends excluded (price-only P&L).
SHARE_DECIMALS: int = 4                      # fractional share precision
MIN_TRADE_VALUE: float = 250                 # skip buys smaller than this (USD)

# --- Portfolio -------------------------------------------------------------
INITIAL_CASH: float = 5000000
PORTFOLIO_EXPORTER_PORT: int = int(os.environ.get("STOCKS_EXPORTER_PORT", 8081))
PORTFOLIO_EXPORTER_URL: str = f"http://localhost:{PORTFOLIO_EXPORTER_PORT}"
DATA_DIR: str = os.environ.get(
    "STOCKS_DATA_DIR", os.path.expanduser("~/.hermes/cache/stocks")
)
NEWS_CACHE_DIR: str = os.path.join(DATA_DIR, "news")
TRADES_CSV: str = os.environ.get(
    "STOCKS_TRADES_CSV", os.path.join(DATA_DIR, "trades.csv")
)
# Highest price observed per open position, for trailing stops.
PEAKS_JSON: str = os.environ.get(
    "STOCKS_PEAKS_JSON", os.path.join(os.path.dirname(TRADES_CSV), "peaks.json")
)

# --- Scoring cache ---------------------------------------------------
SCORING_JSON: str = os.path.join(os.path.dirname(TRADES_CSV), "scoring.json")
PLANS_JSON: str = os.path.join(os.path.dirname(TRADES_CSV), "plans.json")

# --- Universe --------------------------------------------------------------
STOCK_UNIVERSE: list[str] = ["SPY", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]
# Tickers the headline scan may add to the universe (only these are ever
# extracted from headlines, so free-text noise can't become a symbol).
WATCHLIST_EXTRA: list[str] = [
    "CRM", "ADBE", "INTC", "QCOM", "AMD", "PYPL", "SHOP", "SQ", "SNOW", "PLTR",
    "COIN", "MSTR", "JPM", "BAC", "GS", "C", "V", "MA", "NFLX", "DIS",
]

# --- Risk management (percent values) --------------------------------------
# Stop loss below entry is volatility-scaled per ticker:
# clamp(3 * daily volatility% * sqrt(5), STOP_LOSS_MIN_PCT, STOP_LOSS_MAX_PCT).
# No take profit: winners run until the trailing stop is hit.
STOP_LOSS_MIN_PCT: float = 5
STOP_LOSS_MAX_PCT: float = 15
DEFAULT_STOP_LOSS_PCT: float = 10      # fallback when no volatility is known
TRAILING_STOP_PCT: float = 8           # sell when price falls this far below the peak
TRAILING_STOP_ACTIVATION: float = 5    # trailing stop arms once P&L reaches this
MAX_POSITION_PCT: float = 18           # hard cap per position; larger ones are trimmed
MIN_CASH_PCT: float = 5.0              # cash reserve never deployed by buys/top-ups
MAX_POSITIONS: int = 10
MAX_NEW_BUYS_PER_CYCLE: int = 3        # new positions opened per cycle, best score first
REBALANCE_MIN_WEIGHT_FRACTION: float = 1 / 3  # sell positions below this share of target weight
ROTATION_MIN_REL_STR_EDGE: float = 3   # candidate must beat the weakest holding's
                                       # relative strength by this to replace it

# --- Strategy thresholds ---------------------------------------------------
ENTRY_MIN_PCT_ABOVE_MA: float = 1      # price must be >= 1% above the 20MA
ENTRY_MIN_MOMENTUM: int = 5
ENTRY_RSI_MIN: float = 40
ENTRY_RSI_MAX: float = 75
EXIT_MOMENTUM_FADE: int = 4            # sell when momentum drops to this or lower
# Hysteresis for the below-20MA exit: sell only after this many consecutive
# closes below the 20MA, or immediately once price is this far below it.
MA_BREAK_CONFIRM_DAYS: int = 2
MA_BREAK_HARD_PCT: float = 2

# --- Technical analysis ----------------------------------------------------
HISTORY_DAYS: int = 90                 # trading sessions of history to analyse
MA_PERIOD: int = 20
LONG_MA_PERIOD: int = 50               # trend filter: no entries below it; SPY below it = no new buys
RSI_PERIOD: int = 14
BENCHMARK_TICKER: str = "SPY"
REL_STR_PERIOD: int = 20               # sessions for relative strength vs the benchmark
VOLATILITY_RANK_BONUS: float = 1.0     # rank bonus per 1% daily volatility, so
                                       # TSLA/NVDA win when relative strength is close

# --- Investment model ------------------------------------------------------
# Composite (1-10) = TECHNICAL_WEIGHT * technical(1-10)
#                  + NEWS_WEIGHT * (news(-5..+5) + 5)
#                  + earnings catalyst boost
#                  + position adjustment
TECHNICAL_WEIGHT: float = 0.70
NEWS_WEIGHT: float = 0.30
STRONG_BUY_SCORE: float = 7.0          # composite >= this -> STRONG_BUY
BUY_SCORE: float = 5.8                 # composite >= this -> BUY
SELL_SCORE: float = 4.0                # composite <= this -> SELL
STRONG_SELL_SCORE: float = 2.8         # composite <= this -> STRONG_SELL
RSI_OVERBOUGHT: float = 80             # technical penalty above this RSI
RSI_OVERSOLD: float = 30               # technical penalty below this RSI
STALE_POSITION_DAYS: int = 7           # held this long with little gain -> small penalty
STALE_POSITION_MIN_GAIN_PCT: float = 2
NEWS_HOLD_OVERRIDE: float = 3          # news >= this overrides soft technical exits
NEWS_PANIC_SELL: float = -3            # hourly check: sell on news <= this AND price
                                       # below the previous close

# --- Initial portfolio -----------------------------------------------------
INITIAL_MIN_PICKS: int = 2
INITIAL_MAX_PICKS: int = MAX_POSITIONS
INITIAL_TOP_ALLOCATION_PCT: float = 10  # highest-conviction pick (at least an equal share); rest split evenly
INITIAL_FILL_MIN_SCORE: float = 5.0     # HOLD-rated picks may fill up to MIN_PICKS

# --- News ------------------------------------------------------------------
NEWS_QUERY_TEMPLATE: str = "{ticker} stock news today"
EARNINGS_QUERY_TEMPLATE: str = "{ticker} earnings"
EARNINGS_SIGNAL_DAYS: int = 3          # days to watch for post-earnings drift
EARNINGS_CATALYST_BOOST: float = 1.5   # composite boost after an earnings beat
EARNINGS_BOOST_MAX_NEWS: float = 2     # boost only applies when news score is below this
                                       # (higher news already reflects the beat)
NEWS_SCORE_CAP: float = 1              # news contributes at most +/- this to the composite
                                       # until validated
REENTRY_COOLDOWN_DAYS: int = 3          # business days after a discretionary sell before re-entry
MIN_HOLD_SESSIONS: int = 2              # sessions to hold before discretionary (non-stop) sells
EARNINGS_EXIT_DAYS: int = 3             # trading days before earnings: exit holdings, skip buys
NEWS_LOOKBACK_HOURS: int = 72          # ignore headlines older than this
NEWS_MAX_HEADLINES: int = 20           # per ticker, newest first
NEWS_TIMEOUT_SECONDS: int = 15
HEADLINE_SCAN_QUERIES: list[str] = ["stocks to watch today", "analyst upgrade", "earnings beat"]
HEADLINE_SCAN_MAX_TICKERS: int = 5     # new tickers added to the universe per cycle
NEWS_DAMPING: float = 3                # dampens scores built from few headlines
# A headline only counts if it mentions the ticker or one of these names
# (case-insensitive, whole words). Symbols of 3 characters or fewer must match
# case-exactly ("V", not "v"). Tickers not listed match on symbol only.
NEWS_ALIASES: dict[str, list[str]] = {
    "SPY": ["S&P 500", "S&P", "stock market", "Wall Street", "Dow Jones", "Nasdaq",
            "stock futures", "Fed", "inflation"],
    "AAPL": ["Apple", "iPhone"],
    "MSFT": ["Microsoft", "Azure"],
    "GOOGL": ["Alphabet", "Google", "YouTube", "Waymo"],
    "AMZN": ["Amazon", "AWS"],
    "META": ["Meta", "Facebook", "Instagram", "WhatsApp", "Zuckerberg"],
    "NVDA": ["Nvidia"],
    "TSLA": ["Tesla", "Cybertruck", "Cybercab", "robotaxi"],
    "V": ["Visa"],
    "MA": ["Mastercard"],
    "C": ["Citigroup", "Citi"],
    "GS": ["Goldman Sachs", "Goldman"],
    "SQ": ["Square", "Block Inc"],
}

# --- Caching ---------------------------------------------------------------
PRICE_CACHE_SECONDS: int = 60
REL_STR_CACHE_SECONDS: int = 900
