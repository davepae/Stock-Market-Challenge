# Mock Stock Challenge (student edition)

Long-only paper-trading app. Starting cash $50,000.
Platform: Raspberry Pi OS / Debian only. Code lives in `~/stocks_challenge/`.

## Layout
- `trading_cycle.py` - one rebalance cycle: reads portfolio, scores tickers, executes trades via `log_trade.py`
- `investment_model.py` - news sentiment + composite scoring; news cache in `~/.hermes/cache/stocks/news/`
- `tech_analysis.py` - MA / RSI / momentum / relative strength vs SPY (yfinance)
- `risk.py` - stops and position sizing
- `config.py` - ALL tunable constants (universe, stops, thresholds). Edit here, not in code.
- `log_trade.py` - validate + append trades to the CSV (`python log_trade.py AAPL BUY 10 345.20`)
- `app.py` - portfolio exporter, Flask :8081 (`/metrics`, `/holdings`, `/portfolio`)
- `exporters/` - `stock_exporter.py` (:8080 prices), `trades_exporter.py` (:8082 trade table)

## Data
- Source of truth: `~/.hermes/cache/stocks/trades.csv` (`date,ticker,action,shares,price`).
  Portfolio is rebuilt from this file; never edit it by hand while a cycle runs.
- Env overrides: `STOCKS_TRADES_CSV`, `STOCKS_EXPORTER_PORT`.

## Commands
- Activate: `cd ~/stocks_challenge && source .venv/bin/activate`
- Run a cycle: `python trading_cycle.py`
- Verify install: `./verify.sh`
- Services: `systemctl status stocks-exporter stocks-prices stocks-trades prometheus grafana-server`

## Rules (enforced by log_trade.validate_trade)
Long-only stocks/ETFs, no shorting/options/margin, cash never negative. Holdings are exited and buys skipped within 3 trading days of earnings (`EARNINGS_EXIT_DAYS`).
