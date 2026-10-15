#!/usr/bin/env bash
# Mock Stock Challenge - student installer (Debian / Raspberry Pi OS only).
# Run as your normal user (NOT root) from inside the student_distro folder:
#   ./install.sh
# Safe to re-run. Steps mirror SETUP.md.
set -euo pipefail

DISTRO="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/stocks_challenge"
DATA_DIR="$HOME/.hermes/cache/stocks"
TICKERS="SPY,AAPL,MSFT,GOOGL,AMZN,META,NVDA,TSLA"

step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Preflight ------------------------------------------------------------
step "Checking OS, user and Python"
[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root (the script uses sudo where needed)."
[ -r /etc/os-release ] || die "Cannot read /etc/os-release."
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" raspbian "*) echo "OS: ${PRETTY_NAME:-$ID}" ;;
  *) die "Debian-based OS (Raspberry Pi OS) required; found '${PRETTY_NAME:-unknown}'." ;;
esac
command -v apt-get >/dev/null || die "apt-get not found."
command -v sudo    >/dev/null || die "sudo not found; install it as root: apt-get install sudo"
command -v python3 >/dev/null || sudo apt-get install -y python3
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "Python 3.11+ required; found $(python3 -V 2>&1)."
echo "Python: $(python3 -V)"
[ -f "$DISTRO/app/trading_cycle.py" ] || die "Run this from the student_distro folder."

# --- 2. System packages ------------------------------------------------------
step "Installing system packages"
sudo apt-get update
sudo apt-get install -y curl jq git ca-certificates gnupg python3-venv python3-pip prometheus

# Grafana is not in Debian's repos: add Grafana's official apt repository.
if ! dpkg -s grafana >/dev/null 2>&1; then
  step "Adding Grafana apt repository and installing Grafana"
  sudo mkdir -p /etc/apt/keyrings
  curl -fsSL https://apt.grafana.com/gpg.key | sudo gpg --dearmor --yes -o /etc/apt/keyrings/grafana.gpg
  echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
    | sudo tee /etc/apt/sources.list.d/grafana.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y grafana
fi

# --- 3. App files + Python venv ---------------------------------------------
step "Installing app to $APP_DIR"
mkdir -p "$APP_DIR"
cp -r "$DISTRO"/app/. "$APP_DIR"/
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
cp "$DISTRO/CLAUDE.md" "$DISTRO/verify.sh" "$APP_DIR/"

# --- 4. Data directories -----------------------------------------------------
step "Creating data directories"
mkdir -p "$DATA_DIR/news"
if [ ! -f "$DATA_DIR/trades.csv" ]; then
  cp "$DISTRO/student_trades.csv" "$DATA_DIR/trades.csv"
  echo "Created empty $DATA_DIR/trades.csv"
else
  echo "Keeping existing $DATA_DIR/trades.csv"
fi

# --- 5. Prometheus config ----------------------------------------------------
step "Configuring Prometheus (scrapes :8080, :8081, :8082)"
[ -f /etc/prometheus/prometheus.yml.orig ] || sudo cp /etc/prometheus/prometheus.yml /etc/prometheus/prometheus.yml.orig
sudo install -m 644 "$DISTRO/prometheus.yml" /etc/prometheus/prometheus.yml

# --- 6. Grafana provisioning (datasource + dashboard) ------------------------
step "Provisioning Grafana datasource and dashboard"
sudo install -D -m 644 "$DISTRO/monitoring/grafana-datasource.yml" /etc/grafana/provisioning/datasources/stocks.yml
sudo install -D -m 644 "$DISTRO/monitoring/grafana-dashboards.yml" /etc/grafana/provisioning/dashboards/stocks.yml
sudo install -D -m 644 "$DISTRO/monitoring/dashboard.json" /var/lib/grafana/dashboards/stocks/mock-stock-challenge.json

# --- 7. systemd units for the three exporters --------------------------------
step "Creating systemd services"
unit() {  # unit <name> <description> <exec args...>
  local name=$1 desc=$2; shift 2
  sudo tee "/etc/systemd/system/$name.service" >/dev/null <<UNIT
[Unit]
Description=$desc
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $*
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
}
unit stocks-exporter "Stocks portfolio exporter (:8081)" "$APP_DIR/app.py"
unit stocks-prices   "Stocks price exporter (:8080)"     "$APP_DIR/exporters/stock_exporter.py" "$TICKERS"
unit stocks-trades   "Stocks trades exporter (:8082)"    "$APP_DIR/exporters/trades_exporter.py"
sudo systemctl daemon-reload
sudo systemctl enable --now stocks-exporter stocks-prices stocks-trades
sudo systemctl enable prometheus grafana-server
sudo systemctl restart prometheus grafana-server

# --- 8. Verify + next steps --------------------------------------------------
step "Running verification (services may need ~20s to warm up)"
sleep 20
"$DISTRO/verify.sh" || echo "Some checks failed - see SETUP.md > Troubleshooting."

cat <<MSG

Next steps:
  1. Open Grafana:  http://$(hostname -I | awk '{print $1}'):3000
     Log in admin / admin and set a new password when prompted.
     The "Mock Stock Challenge" dashboard is already provisioned.
  2. Log a first trade:   cd $APP_DIR && source .venv/bin/activate
                          python log_trade.py AAPL BUY 10 200.00
  3. Run a cycle:         python trading_cycle.py
  4. Schedule cycles with cron (see SETUP.md > Scheduling).
Checklist: ./verify.sh all PASS; dashboard loads; /portfolio shows cash 50000.
MSG
