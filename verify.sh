#!/usr/bin/env bash
# Post-install verification for the Mock Stock Challenge. Exit code 0 = all pass.
APP_DIR="$HOME/stocks_challenge"
DATA_DIR="$HOME/.hermes/cache/stocks"
fail=0
check() {  # check <label> <command...>
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then printf 'PASS  %s\n' "$label"
  else printf 'FAIL  %s\n' "$label"; fail=1; fi
}
active() { systemctl is-active --quiet "$1"; }

echo "== Services =="
for s in prometheus grafana-server stocks-exporter stocks-prices stocks-trades; do
  check "service $s running" active "$s"
done

echo "== Python =="
check "venv exists" test -x "$APP_DIR/.venv/bin/python"
check "imports (flask yfinance requests prometheus_client + app modules)" \
  bash -c "cd '$APP_DIR' && .venv/bin/python -c 'import flask, yfinance, requests, prometheus_client, config, log_trade, risk, tech_analysis, investment_model, trading_cycle'"

echo "== Data =="
check "trades.csv has header" grep -q '^date,ticker,action,shares,price' "$DATA_DIR/trades.csv"
check "news cache dir" test -d "$DATA_DIR/news"

echo "== Endpoints =="
check "exporter :8081 /portfolio" curl -fsS --max-time 20 localhost:8081/portfolio
check "exporter :8081 /metrics has portfolio_value" \
  bash -c "curl -fsS --max-time 20 localhost:8081/metrics | grep -q '^portfolio_value'"
check "prices :8080 /metrics" curl -fsS --max-time 10 localhost:8080/metrics
check "trades :8082 /metrics" curl -fsS --max-time 10 localhost:8082/metrics
check "Prometheus :9090 healthy" curl -fsS --max-time 10 localhost:9090/-/healthy
check "Prometheus scraping portfolio target (up==1)" \
  bash -c "curl -fsS --max-time 10 'localhost:9090/api/v1/query?query=up%7Bjob%3D%22portfolio%22%7D' | jq -e '.data.result[0].value[1]==\"1\"'"
check "Grafana :3000 healthy" bash -c "curl -fsS --max-time 10 localhost:3000/api/health | jq -e '.database==\"ok\"'"

echo
[ $fail -eq 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED - see SETUP.md > Troubleshooting"
exit $fail
