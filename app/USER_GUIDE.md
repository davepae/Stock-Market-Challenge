# Mock Stock Challenge — User Guide

An automated, long-only portfolio manager for a mock stock contest.
It starts with $5,000,000, trades 8 core tickers
(SPY, AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA) plus up to 5 watchlist
names found by a headline scan, and ranks candidates by 20-day relative
strength against the S&P 500 (SPY).

This guide covers day-to-day operation and describes only what the code
implements. `DESIGN.md` is a design document and may describe features that
are not implemented.

---

## 1. What's in the box

| File | What it does |
|------|--------------|
| `trading_cycle.py` | Runs a trading decision cycle (full or risk-only) |
| `log_trade.py` | Records a BUY/SELL in the trade ledger (also usable by hand) |
| `app.py` | Flask exporter on port 8081: `/metrics`, `/holdings`, `/portfolio` |
| `config.py` | Every tunable constant (stops, thresholds) |
| `tech_analysis.py` | Price history, moving average, RSI, momentum, relative strength |
| `investment_model.py` | Composite scoring, news/earnings handling, entry rules |
| `risk.py` | Stop levels, peak tracking, last-price lookup |
| `requirements.txt` | `flask`, `yfinance`, `requests` |
| `student_distro/` | Prometheus/Grafana monitoring bundle and its setup docs |

Data lives in `~/.hermes/cache/stocks/`:
`trades.csv` (ledger: `date, ticker, action, shares, price`),
`peaks.json` (peak prices for trailing stops),
`news/{TICKER}.json` (news cache, 15-minute TTL; safe to delete).

---

## 2. Setup

```bash
cd /home/ysagent/stocks_challenge
source .venv/bin/activate
pip install -r requirements.txt
```

Optional environment overrides (useful for testing against a scratch ledger):

| Variable | Default |
|----------|---------|
| `STOCKS_TRADES_CSV` | `/home/ysagent/.hermes/cache/stocks/trades.csv` |
| `STOCKS_PEAKS_JSON` | `peaks.json` beside the trade log |
| `STOCKS_EXPORTER_PORT` | `8081` |

---

## 3. Everyday tasks

### Start the portfolio exporter

```bash
python app.py            # serves on http://localhost:8081
```

| Endpoint | Returns |
|----------|---------|
| `/portfolio` | Cash, holdings value, total value, ROI, trade count, initial cash, number of positions |
| `/holdings` | Open positions: shares, entry, current price, P&L, peak, trailing stop, `price_stale` |
| `/metrics` | Prometheus gauges |

State is rebuilt from the trade log on every request. Prices are cached 60 s;
relative strength 15 min.

### Run a trading cycle

```bash
python trading_cycle.py --dry-run        # decide and print, log nothing
python trading_cycle.py                  # full cycle
python trading_cycle.py --risk-only      # stops + held-ticker news panic check
python trading_cycle.py --force          # run outside market hours
```

| Flag | Meaning |
|------|---------|
| `--dry-run` | Run the whole decision pipeline but do not log trades |
| `--risk-only` | Check stops and held-ticker news on held positions; no new entries |
| `--force` | Run even outside regular market hours (9:30–16:00 ET, Mon–Fri; holidays are not detected) |
| `--exporter-url URL` | Read portfolio state from a different exporter (default `http://localhost:8081`) |

The cycle reads portfolio state from the exporter's `/metrics` and falls back
to the trade log if the exporter is unreachable.

**Schedule.** The Hermes cron jobs (`~/.hermes/cron/jobs.json`) run the full
cycle weekdays at 15:30 (system local time) and `--risk-only` every 60
minutes. The cycle itself skips when the market is closed.

### Log a trade by hand

```bash
python log_trade.py AAPL BUY 10 345.20
python log_trade.py AAPL SELL 5 350.00
```

Trades are validated first: a BUY needs enough cash, a SELL needs enough shares.
Rejections print `REJECTED: …` and exit with code 1. Fractional shares are fine.
Use `--trades-file PATH` to write to a different ledger.

> Tip: always `--dry-run` first after changing anything in `config.py`.

---

## 4. How it decides

**Entry** — price ≥ 1% above its 20-day MA, momentum ≥ 5/10, RSI between 40 and 75.

**Score** — 70% technicals + 30% news (+1.5 after a positive earnings report).
Ratings: STRONG_BUY ≥ 7.0, BUY ≥ 5.8, SELL ≤ 4.0, STRONG_SELL ≤ 2.8.

**Portfolio** — at most 3 positions, ranked by 20-day return vs SPY (plus a
small volatility bonus), minimum $250 per buy. The first allocation puts 60%
in the top pick and splits the rest evenly. When full, a candidate can replace
the weakest holding if its relative strength is at least 3 points higher.

**Exits** (first match wins)

| Trigger | Rule |
|---------|------|
| Stop loss | 6–15% below entry depending on ticker (SPY 6, MSFT 7, AAPL/GOOGL/AMZN 8, META 9, NVDA 12, TSLA 15; others 10) |
| Trailing stop | Arms once the peak is 5% above entry; sells at 8% below the peak |
| Earnings avoidance | Within 3 trading days of earnings, holdings are sold and no buys are made |
| Technical | SELL/STRONG_SELL rating, momentum ≤ 4, or confirmed 20MA break (2 closes below, or 2% below) |
| News override | News score ≥ 3 (or a STRONG_BUY with an earnings catalyst) holds through the momentum/20MA exits |
| News panic (risk-only) | News score ≤ -3 **and** price below previous close sells the whole position |

There is no take-profit cap — winners run until the trailing stop hits. The
trailing stop is `peak × (1 − 8%)`; there is no separate breakeven floor.

### News handling

- **Caching:** news per ticker is cached for 15 minutes in
  `~/.hermes/cache/stocks/news/`. Failed fetches are never cached.
- **News outage is not a sell signal.** If news cannot be fetched for a
  held ticker, the full cycle marks it stale: stop loss, trailing stop and
  earnings avoidance still apply, but model/momentum/20MA sells are
  paused for that ticker. Buys need fresh news for the candidate, and if
  news fails for every holding no new buys happen that cycle.
- **Risk-only check:** reads cached news for held tickers. It sells only when
  bad news (≤ -3) and a price below the previous close occur together. If
  news is unavailable it reports `news unavailable` and applies only the stops.
- **Opportunity scan:** each full cycle scans three headline feeds
  (stocks to watch today, analyst upgrade, earnings beat) for tickers on
  `WATCHLIST_EXTRA` in `config.py` and adds up to 5 by mention count. They
  are scored and must pass the same entry rules as core tickers. `--dry-run`
  output shows a `Headline scan:` line.

---

## 5. Monitoring (Prometheus / Grafana)

The `student_distro/` bundle configures Prometheus to scrape `localhost:8081`
(`/metrics`) every 15 s, and ships a Grafana dashboard
(`student_distro/monitoring/dashboard.json`, uid `mock-stock-challenge`,
30 s refresh) served at `http://<host>:3000`. See `student_distro/SETUP.md`.

Metrics emitted by `app.py`: `portfolio_value`, `portfolio_cash`,
`portfolio_roi`, `portfolio_holdings_value`, `portfolio_trade_count`, and per
holding `holding_<ticker>_{shares,value,entry_price,peak_price,trailing_stop,rel_strength}`
(`rel_strength` is omitted when it cannot be computed).

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `REJECTED: Insufficient cash` | The BUY costs more than available cash; reduce shares |
| `REJECTED: Insufficient shares` | You hold less than you tried to sell; check `/holdings` |
| `/metrics` returns `# error:` | `trades.csv` unreadable or malformed; check path and permissions |
| Holding shows `price_stale: true` | yfinance fetch failed; valued at entry price until it recovers |
| Port 8081 in use | Set `STOCKS_EXPORTER_PORT` and update the Prometheus target |
| Trailing stop shows 0 | Not armed yet (peak below +5% over entry) — expected |
| `news unavailable` / `news stale` warning | News fetch failed; stops still work, discretionary sells and buys are paused until it recovers |
| News looks out of date | Cache TTL is 15 min; delete `~/.hermes/cache/stocks/news/*.json` to force a refresh |
| Unexpected non-core ticker in the cycle | Came from the headline scan (`WATCHLIST_EXTRA`); edit that list or `HEADLINE_SCAN_MAX_TICKERS` in `config.py` |
| `Market closed … skipping cycle` | Outside 9:30–16:00 ET Mon–Fri; use `--force` to override |

---

## 7. Safety notes

- **Back up `trades.csv`** before manual edits; it is the single source of truth.
- Don't run two copies of `trading_cycle.py` against the same ledger at once
  (file locking protects writes, but decisions would race).
