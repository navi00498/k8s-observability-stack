#!/usr/bin/env bash
# End-to-end checks against a running lab (local kind cluster or CI).
#
#   scripts/verify.sh                 # pipeline checks: metrics, rules, dashboards, logs
#   CHAOS=1 scripts/verify.sh         # + inject failures and prove the SLO alert reaches Alertmanager
#   SCREENSHOT_DIR=docs CHAOS=1 ...   # + capture Grafana / Alertmanager screenshots (needs node + Chrome)
set -euo pipefail

MON_NS=${MON_NS:-monitoring}
APP_NS=${APP_NS:-demo}
TRIES=${TRIES:-40}
SLEEP=${SLEEP:-6}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

for tool in kubectl curl jq; do
  command -v "$tool" >/dev/null || { echo "verify.sh needs '$tool' on PATH" >&2; exit 1; }
done

pids=()
# "${pids[@]+...}" keeps macOS bash 3.2 happy under `set -u` when the array is empty
cleanup() { for pid in ${pids[@]+"${pids[@]}"}; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT

port_forward() { # namespace service local:remote
  kubectl -n "$1" port-forward "svc/$2" "$3" >/dev/null 2>&1 &
  pids+=($!)
}

retry() { # description command...
  local desc=$1
  shift
  for ((i = 1; i <= TRIES; i++)); do
    if "$@" >/dev/null 2>&1; then
      echo "PASS  $desc"
      return 0
    fi
    sleep "$SLEEP"
  done
  echo "FAIL  $desc"
  return 1
}

prom() { curl -fsS --get http://localhost:9090/api/v1/query --data-urlencode "query=$1"; }
prom_value() { prom "$1" | jq -r '.data.result[0].value[1] // empty'; }
loki_count() {
  curl -fsS --get http://localhost:3100/loki/api/v1/query_range \
    --data-urlencode "query=$1" --data-urlencode "since=15m" --data-urlencode "limit=50" |
    jq '[.data.result[].values[]] | length'
}

targets_up() { [ "$(prom_value 'sum(up{job="demo-app"})')" -ge 2 ]; }
recording_rules() { [ -n "$(prom_value 'job:slo_errors_per_request:ratio_rate5m{job="demo-app"}')" ]; }
alert_rules_loaded() {
  curl -fsS http://localhost:9090/api/v1/rules?type=alert |
    jq -e '[.data.groups[] | select(.name == "demo-app-slo.alerts") | .rules[].name] | length == 4'
}
slo_healthy() { [ "$(prom 'ALERTS{job="demo-app",alertstate="firing"}' | jq '.data.result | length')" -eq 0 ]; }
grafana_dashboard() {
  curl -fsS "http://localhost:3000/api/search?query=Demo%20App" | jq -e 'any(.[]; .uid == "demo-app-slo")'
}
grafana_datasources() {
  curl -fsS -u admin:admin http://localhost:3000/api/datasources | jq -e '[.[].uid] | contains(["prometheus", "loki"])'
}
loki_has_logs() { [ "$(loki_count '{namespace="demo", app="demo-app"}')" -gt 0 ]; }
loki_has_errors() { [ "$(loki_count '{namespace="demo", app="demo-app", level="error"}')" -gt 0 ]; }
fast_burn_firing() {
  [ "$(prom 'ALERTS{alertname="DemoAppErrorBudgetBurnFast",alertstate="firing"}' | jq '.data.result | length')" -gt 0 ]
}
alertmanager_received() {
  curl -fsS --get http://localhost:9093/api/v2/alerts --data-urlencode 'filter=alertname="DemoAppErrorBudgetBurnFast"' |
    jq -e 'length > 0 and .[0].labels.severity == "critical"'
}

screenshot() { # url output
  [ -n "${SCREENSHOT_DIR:-}" ] || return 0
  mkdir -p "$SCREENSHOT_DIR"
  node "$ROOT/scripts/screenshot.mjs" "$1" "$SCREENSHOT_DIR/$2" "${3:-1600}" "${4:-1000}" && echo "SHOT  $SCREENSHOT_DIR/$2"
}

kubectl -n "$APP_NS" rollout status deploy/demo-app --timeout=180s >/dev/null
kubectl -n "$APP_NS" rollout status deploy/loadgen --timeout=180s >/dev/null
port_forward "$MON_NS" kps-prometheus 9090:9090
port_forward "$MON_NS" kps-grafana 3000:80
port_forward "$MON_NS" kps-alertmanager 9093:9093
port_forward "$MON_NS" loki 3100:3100
sleep 3

echo "== Observability pipeline"
retry "Prometheus scrapes both demo-app pods (ServiceMonitor)" targets_up
retry "SLO recording rules produce data (PrometheusRule)" recording_rules
retry "All 4 SLO alert rules are loaded" alert_rules_loaded
retry "No demo-app alerts firing at baseline" slo_healthy
retry "Grafana provisioned the SLO dashboard (sidecar)" grafana_dashboard
retry "Grafana has Prometheus and Loki datasources" grafana_datasources
retry "Loki has demo-app logs (Alloy -> Loki)" loki_has_logs

if [ "${CHAOS:-0}" = "1" ]; then
  echo "== Chaos: inject 30% errors and follow the alert end to end"
  kubectl -n "$APP_NS" set env deploy/demo-app ERROR_RATE=0.3 >/dev/null
  kubectl -n "$APP_NS" rollout status deploy/demo-app --timeout=180s >/dev/null
  TRIES=80 retry "Error logs are searchable in Loki (level=error)" loki_has_errors
  TRIES=120 retry "DemoAppErrorBudgetBurnFast is firing in Prometheus" fast_burn_firing
  TRIES=30 retry "Alertmanager received the page (severity=critical)" alertmanager_received
  screenshot "http://localhost:9093/#/alerts?silenced=false&inhibited=false&active=true&filter=%7Bjob%3D%22demo-app%22%7D" \
    alertmanager-firing.png 1400 700
  kubectl -n "$APP_NS" set env deploy/demo-app ERROR_RATE=0 >/dev/null
  kubectl -n "$APP_NS" rollout status deploy/demo-app --timeout=180s >/dev/null
  echo "INFO  Recovered: ERROR_RATE=0"
  if [ -n "${SCREENSHOT_DIR:-}" ]; then
    sleep "${SCREENSHOT_DELAY:-240}"
    screenshot "http://localhost:3000/d/demo-app-slo/demo-app?orgId=1&kiosk&from=now-30m&to=now" \
      grafana-dashboard.png 1600 1750
  fi
fi

echo "All checks passed."
