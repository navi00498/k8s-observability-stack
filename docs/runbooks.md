# Runbooks

Runbooks for the alerts defined in [`k8s/monitoring/prometheusrule-demo-app-slo.yaml`](../k8s/monitoring/prometheusrule-demo-app-slo.yaml).
Every alert links here through its `runbook_url` annotation, so whoever is on call goes straight from the
page to the steps.

**SLO:** 99.5% of demo-app API requests succeed (non-5xx) over a rolling 30 days.
**Error budget:** 0.5% of requests, about 3.6 hours of full outage a month.

| Burn rate | Budget gone in | Alert | Action |
|---|---|---|---|
| 14.4x for 1h | ~2 days | `DemoAppErrorBudgetBurnFast` (critical) | Page: respond now |
| 6x for 6h | ~5 days | `DemoAppErrorBudgetBurnFast` (critical) | Page: respond now |
| 3x for 1d | ~10 days | `DemoAppErrorBudgetBurnSlow` (warning) | Ticket: fix this week |
| 1x for 3d | 30 days | `DemoAppErrorBudgetBurnSlow` (warning) | Ticket: fix this week |

Each condition also needs a short window (5m, 30m, 2h or 6h) to agree. That's how an alert resets quickly once the
problem is fixed, instead of firing for another hour.

---

## DemoAppErrorBudgetBurnFast

**Impact:** a large share of users are getting errors right now. At this rate the monthly error budget runs out
within days.

1. **Confirm and scope.** Open the *Demo App - SLO and RED overview* dashboard.
   - *Requests by route and status*: is it one route or all of them? Which status codes?
   - *Error ratio vs SLO*: when did it start? Does it line up with a deploy (annotation) or a traffic change?
2. **Check what changed:**
   ```bash
   kubectl -n demo rollout history deploy/demo-app
   kubectl -n demo describe deploy/demo-app | grep -A3 -i env      # fault-injection knobs, config changes
   kubectl -n demo get events --sort-by=.lastTimestamp | tail -20
   ```
3. **Read the errors.** Use the *Errors and warnings* log panel, or Grafana Explore → Loki:
   ```logql
   {namespace="demo", app="demo-app", level="error"} | json | line_format "{{.status}} {{.method}} {{.route}} {{.duration_ms}}ms"
   sum by (route) (count_over_time({namespace="demo", app="demo-app", level="error"} | json [5m]))
   ```
4. **Mitigate first, then root-cause.**
   - Bad deploy: `kubectl -n demo rollout undo deploy/demo-app`
   - Bad config or feature flag: revert it (lab: `make calm`)
   - Dependency down: fail over, or degrade gracefully
5. **Verify recovery.** The 5m error ratio drops under the threshold and the alert resolves within minutes.
6. **Follow up.** Write a short incident note with timeline, impact (budget consumed) and action items.

## DemoAppErrorBudgetBurnSlow

**Impact:** errors are elevated but nobody needs to be woken up. If nothing changes, the SLO will be missed this
month.

1. Look at the *Error budget left (30d)* stat and the *burn rate* panel. Is the burn steady or getting worse?
2. Use the Loki queries above to find the top failing routes and error messages.
3. Open a ticket with the evidence, and prioritise reliability work over features until the burn is back under 1x.

## DemoAppHighLatency

**Impact:** p95 latency has been above 500ms for 10 minutes, so users notice slow pages.

1. Check the *Latency percentiles* panel. Is p50 elevated too (everything is slow), or only p95/p99 (some requests)?
2. Check saturation: the *Pod CPU* and *Pod memory* panels, and throttling:
   ```promql
   rate(container_cpu_cfs_throttled_periods_total{namespace="demo",container="demo-app"}[5m])
     / rate(container_cpu_cfs_periods_total{namespace="demo",container="demo-app"}[5m])
   ```
3. Find the slow requests:
   ```logql
   {namespace="demo", app="demo-app"} | json | duration_ms > 500
   ```
4. Mitigate: scale out (`kubectl -n demo scale deploy/demo-app --replicas=4`), roll back, or remove injected
   latency (lab: `make calm`).

## DemoAppDown

**Impact:** Prometheus has no healthy demo-app targets. Either the service is down or monitoring is blind.

1. Are the pods running?
   ```bash
   kubectl -n demo get pods -l app.kubernetes.io/name=demo-app -o wide
   kubectl -n demo describe pod -l app.kubernetes.io/name=demo-app | tail -30
   ```
   Look for CrashLoopBackOff, OOMKilled, ImagePullBackOff and failing readiness probes.
2. If the pods are healthy, it's a discovery problem. Check that the Service has endpoints and the ServiceMonitor
   selects it:
   ```bash
   kubectl -n demo get endpoints demo-app
   kubectl -n demo get servicemonitor demo-app -o yaml
   ```
   Then check Prometheus → *Status → Targets* for `serviceMonitor/demo/demo-app`.
3. If the service itself is down, follow the fast-burn runbook steps 2–4.
