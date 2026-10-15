# Mock Stock Challenge — User Guide

An automated, long-only portfolio manager for a mock stock contest.
It starts with $50,000, trades 8 core tickers
(SPY, AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA) plus up to 5 watchlist
names found by a daily headline scan, and tries to beat both the
other contestants and the S&P 500.

This guide covers day-to-day operation. For design rationale see `DESIGN.md`.

---

## 1. What's in the box

| File | What it does |
|------|--------------|
| `trading_cycle.py` | Runs a trading decision cycle (full or risk-only) |
| `log_trade.py` | Records a BUY/SELL in the trade ledger (also usable by hand) |
| `app.py` | Flask exporter on port 8081: `/metrics`, `/holdings`, `/portfolio` |
| `config.py` | Every tunable constant (stops, thresholds) |
| `tech_analysis.py`, `investment_model.py`, `risk.py` | Indicators, scoring, stop logic |

Data lives in `~/.hermes/cache/stocks/`:
`trades.csv` (ledger), `peaks.json` (peak prices for trailing stops),
`news/{TICKER}.json` (news cache, 15-minute TTL; safe to delete).

---

## 2. Setup

```bash
cd ~/stocks_challenge
source .venv/bin/activate
pip install -r requirements.txt     # flask, yfinance, requests
```

Optional environment overrides (useful for testing against a scratch ledger):

| Variable | Default |
|----------|---------|
| `STOCKS_TRADES_CSV` | `~/.hermes/cache/stocks/trades.csv` |
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
| `/portfolio` | Cash, holdings value, total value, ROI, trade count, positions |
| `/holdings` | Open positions: shares, entry, current price, P&L, peak, trailing stop |
| `/metrics` | Prometheus gauges for Grafana |

State is rebuilt from the trade log on every request, so the exporter never
goes out of sync with `trades.csv`. Prices are cached 60 s; relative strength 15 min.

### Run a trading cycle

```bash
python trading_cycle.py --dry-run        # decide and print, log nothing
python trading_cycle.py                  # full daily cycle
python trading_cycle.py --risk-only      # stops + held-ticker news panic check
python trading_cycle.py --force          # run outside market hours
```

| Flag | Meaning |
|------|---------|
| `--dry-run` | Run the whole decision pipeline but do not log trades |
| `--risk-only` | Check stops and held-ticker news on held positions; no new entries |
| `--force` | Run even when the market is closed |
| `--exporter-url URL` | Read portfolio state from a different exporter |

**Recommended schedule** (US/Eastern): full cycle weekdays at 15:30,
risk-only every 60 minutes.

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

**Portfolio** — at most 3 positions, ranked by 20-day return vs SPY, minimum
$250 per buy. First allocation puts 60% in the top pick.

**Exits**

| Trigger | Rule |
|---------|------|
| Stop loss | 6–15% below entry depending on ticker (SPY 6, MSFT 7, AAPL/GOOGL/AMZN 8, META 9, NVDA 12, TSLA 15) |
| Trailing stop | Arms at +5% P&L, sells 8% below peak |
| Technical | SELL rating, momentum ≤ 4, or confirmed 20MA break (2 closes below, or 2% below) |
| News override | Strong news (≥ 3) can hold through a soft technical exit |
| News panic (hourly) | News score ≤ -3 **and** price below previous close sells the whole position |

There is no take-profit cap — winners run until the trailing stop hits.

### News handling

- **Caching:** news per ticker is cached for 15 minutes in
  `~/.hermes/cache/stocks/news/`. Failed fetches are never cached.
- **News outage is not a sell signal.** If news cannot be fetched for a
  held ticker, the daily cycle marks it stale: stop loss, trailing stop and
  the earnings rule still apply, but model/momentum/20MA sells are
  paused for that ticker. Buys need fresh news for the candidate, and if
  news fails for every holding no new buys happen that cycle.
- **Hourly check:** reads cached news for held tickers. It sells only when
  bad news (≤ -3) and a price below the previous close occur together. If
  news is unavailable it prints `news unavailable` and does nothing.
- **Opportunity scan:** each daily cycle scans three broad
  headline feeds (stocks to watch, analyst upgrades, earnings beats) for
  tickers on `WATCHLIST_EXTRA` in `config.py` and adds the top 5 by mention
  count. They are scored and must pass the same entry rules as core
  tickers. `--dry-run` output shows a `Headline scan:` line.

---

## 5. Grafana dashboard

Open `http://localhost:3000/d/mock-stock-challenge`. Prometheus scrapes
`/metrics` every 15 s; the dashboard refreshes every 30 s. It shows portfolio
total and ROI, holdings table with trailing stops, equity curve vs the $50K
baseline.

Key metrics: `portfolio_value`, `portfolio_cash`, `portfolio_roi`,
`portfolio_holdings_value`, `portfolio_trade_count`, and per-holding `holding_<ticker>_{shares,value,entry_price,peak_price,trailing_stop,rel_strength}`.

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `REJECTED: Insufficient cash` | The BUY costs more than available cash; reduce shares |
| `REJECTED: Insufficient shares` | You hold less than you tried to sell; check `/holdings` |
| `/metrics` returns `# error:` | `trades.csv` unreadable; check path and permissions |
| Holding shows `price_stale: true` | yfinance fetch failed; valued at entry price until it recovers |
| Port 8081 in use | Set `STOCKS_EXPORTER_PORT` and update the Prometheus target |
| Trailing stop shows 0 | Not armed yet (P&L below +5%) — expected |
| `news unavailable` / `news stale` warning | News feed fetch failed; stops still work, discretionary sells and buys are paused until it recovers |
| News looks out of date | Cache TTL is 15 min; delete `~/.hermes/cache/stocks/news/*.json` to force a refresh |
| Unexpected non-core ticker in the cycle | Came from the headline scan (`WATCHLIST_EXTRA`); edit that list or `HEADLINE_SCAN_MAX_TICKERS` in `config.py` |

---

## 7. Safety notes

- **Back up `trades.csv`** before manual edits; it is the single source of truth.
- Don't run two copies of `trading_cycle.py` against the same ledger at once
  (file locking protects writes, but decisions would race).

---

## 8. Open items to confirm before finalizing

`DESIGN.md` describes features I could not find in the current code. Verify
these before documenting them for users:

1. **Kill switch** (`.hermes/cache/stocks/KILL`) — no reference found in the code.
2. **`reason` column** in `trades.csv` — `log_trade.py` writes only
   `date, ticker, action, shares, price`; the re-entry cooldown depends on it.
3. **Heartbeat file and freshness/ROI-vs-SPY metrics** — not emitted by
   `app.py`'s `/metrics` (only the gauges listed in §5).
4. **`DRY_RUN` / `KILL_SWITCH_FILE` env vars** — `config.py` only reads
   `STOCKS_TRADES_CSV`, `STOCKS_PEAKS_JSON`, `STOCKS_EXPORTER_PORT`.
5. **Logging to `logs/`** with rotation — not seen in `config.py`.
6. **Alerts** — documented in `DESIGN.md` but depend on metrics from item 3.
