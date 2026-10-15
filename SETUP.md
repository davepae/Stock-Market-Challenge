# Mock Stock Challenge - Student Setup Guide

**Platform: Raspberry Pi OS / Debian only.** Other distros are not supported.

The quick path is `./install.sh` (Section 2). Sections 3-8 describe the same
steps by hand, so you can see what the script does or recover when it fails.

## 1. Prerequisites
- Raspberry Pi OS (or another Debian-based OS), 64-bit recommended, internet access
- A normal user with `sudo` (do **not** run the installer as root)
- Python **3.11 or newer** - check with `python3 -V` (Raspberry Pi OS Bookworm ships 3.11)
- git, curl, jq (the installer adds anything missing)
- ~1 GB free disk space

## 2. Automated install
```bash
cd student_distro
./install.sh
```
It checks the OS and Python, installs packages, builds the venv, creates data
folders, configures Prometheus and Grafana, starts everything, and runs
`verify.sh`. It is safe to re-run.

## 3. What gets installed where
| Item | Location |
|---|---|
| App code + `.venv` | `~/stocks_challenge/` |
| Trade log | `~/.hermes/cache/stocks/trades.csv` |
| News cache | `~/.hermes/cache/stocks/news/` |
| Prometheus config | `/etc/prometheus/prometheus.yml` |
| Grafana datasource / dashboard | `/etc/grafana/provisioning/...`, `/var/lib/grafana/dashboards/stocks/` |
| Exporter services | `stocks-exporter` (:8081), `stocks-prices` (:8080), `stocks-trades` (:8082) |

The `.venv` is **not** shipped: virtualenvs hard-code absolute paths and CPU
architecture, so it must be rebuilt on your machine (step 5).

## 4. System packages (manual)
```bash
sudo apt-get update
sudo apt-get install -y curl jq git python3-venv python3-pip prometheus
```
Grafana is not in Debian's repos; add Grafana's apt repo:
```bash
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://apt.grafana.com/gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
  | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update && sudo apt-get install -y grafana
```

## 5. Python app and venv (manual)
```bash
mkdir -p ~/stocks_challenge && cp -r student_distro/app/. ~/stocks_challenge/
cd ~/stocks_challenge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # flask, yfinance, requests, prometheus_client
```

## 6. Data directories (manual)
```bash
mkdir -p ~/.hermes/cache/stocks/news
cp student_distro/student_trades.csv ~/.hermes/cache/stocks/trades.csv   # header only
```
`trades.csv` columns: `date,ticker,action,shares,price`. The portfolio
(starting cash $5,000,000) is rebuilt from this file. Always add trades with
`python log_trade.py TICKER BUY|SELL SHARES PRICE`, which validates them.

## 7. Prometheus and Grafana configuration (manual)
```bash
sudo cp student_distro/prometheus.yml /etc/prometheus/prometheus.yml
sudo cp student_distro/monitoring/grafana-datasource.yml /etc/grafana/provisioning/datasources/stocks.yml
sudo cp student_distro/monitoring/grafana-dashboards.yml /etc/grafana/provisioning/dashboards/stocks.yml
sudo mkdir -p /var/lib/grafana/dashboards/stocks
sudo cp student_distro/monitoring/dashboard.json /var/lib/grafana/dashboards/stocks/mock-stock-challenge.json
sudo systemctl restart prometheus grafana-server
```
Prometheus scrapes `localhost:8081` (portfolio, from `app.py`). It also
scrapes `:8080` (prices) and `:8082` (trade table) because the dashboard's
price and trade-history panels need those metrics.

**Grafana login:** open `http://<pi-address>:3000`, sign in as `admin` / `admin`
and choose a new password. No OAuth/account setup is required: the datasource
and dashboard are provisioned from files. If you want to add your own
dashboards or users, do that in the Grafana UI.

## 8. Starting services
The installer creates systemd services. Manually:
```bash
sudo systemctl enable --now prometheus grafana-server stocks-exporter stocks-prices stocks-trades
systemctl status stocks-exporter       # or: journalctl -u stocks-exporter -e
```
Without systemd you can run the exporter in a terminal:
`cd ~/stocks_challenge && source .venv/bin/activate && python app.py`

## 9. Verify
```bash
~/stocks_challenge/verify.sh        # or student_distro/verify.sh
```
Every line should say `PASS`. By hand:
```bash
curl -s localhost:8081/portfolio | jq        # cash 5000000 on a fresh install
curl -s localhost:8081/metrics | head
curl -s localhost:9090/api/v1/targets | jq '.data.activeTargets[].health'
```
Then open `http://<pi-address>:3000` -> Dashboards -> **Mock Stock Challenge**.

## 10. Using the app
```bash
cd ~/stocks_challenge && source .venv/bin/activate
python trading_cycle.py                     # run one rebalance cycle
python log_trade.py AAPL BUY 10 200.00      # log a trade manually
```
Strategy constants (stops, universe, thresholds) live in
`config.py`. See `USER_GUIDE.md` for the rules and strategy.

### Scheduling
Run a cycle every 2 hours on weekdays with cron (`crontab -e`):
```cron
0 */2 * * 1-5 cd $HOME/stocks_challenge && .venv/bin/python trading_cycle.py >> $HOME/.hermes/cache/stocks/cycle.log 2>&1
```
(The cycle skips itself when the market is closed.) If you use an agent
scheduler such as Hermes, its `jobs.json` entries have the shape
`{"name", "script": "trading_cycle.py", "schedule": {"kind": "interval", "minutes": 120}, "enabled": true}` -
configure your own; nothing is pre-set.

## 11. Troubleshooting
| Symptom | Fix |
|---|---|
| `install.sh`: "Debian-based OS required" | Unsupported OS; use Raspberry Pi OS. |
| "Python 3.11+ required" | Upgrade the OS (`sudo apt full-upgrade`) or use Raspberry Pi OS Bookworm or newer. |
| `E: Unable to locate package prometheus` | Run `sudo apt-get update`; on very old releases upgrade the OS. |
| Grafana package not found / GPG error | Re-run the Section 4 Grafana commands; check internet and `date` (wrong clock breaks TLS). |
| `:8081` connection refused | `journalctl -u stocks-exporter -e`. Port busy? `ss -ltnp | grep 8081`. Override with `STOCKS_EXPORTER_PORT` (and update `prometheus.yml`). |
| `/portfolio` is slow or prices missing | yfinance rate-limits or network is down; prices are cached 60 s, retry shortly. |
| Prometheus target `DOWN` | Exporter not running (see above). Check `sudo promtool check config /etc/prometheus/prometheus.yml`. |
| Grafana panels "No data" | Datasource uid must be `ag164tx65ph4wd` (provisioned file). Check Connections -> Data sources -> Prometheus -> Save & test; set time range to "Last 6 hours". Empty trade log = few panels have data. |
| `ModuleNotFoundError` | Activate the venv: `source ~/stocks_challenge/.venv/bin/activate`, then `pip install -r requirements.txt`. |
| Trade rejected | Not enough cash/shares - read the error message. |
| Start over | `sudo systemctl stop stocks-exporter stocks-prices stocks-trades`, copy `student_trades.csv` over `trades.csv`, restart. |
