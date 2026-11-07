CLUSTER       ?= obs-lab
IMAGE         ?= demo-app:0.1.0
MON_NS        ?= monitoring
KPS_VERSION   ?= 92.22.0
LOKI_VERSION  ?= 7.5.0
ALLOY_VERSION ?= 1.15.0
CRD_SCHEMAS   := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json

.DEFAULT_GOAL := help
.PHONY: help up down cluster image repos install apps verify chaos-test chaos latency calm \
        grafana prometheus alertmanager test test-app test-rules lint lint-manifests lint-dashboards

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## ---- Lab lifecycle -------------------------------------------------------

up: cluster image install apps ## Create the kind cluster and deploy everything
	@echo "\nReady. Run 'make grafana' and open http://localhost:3000 (admin/admin)."

down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

cluster: ## Create the kind cluster (idempotent)
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER) || kind create cluster --config kind/cluster.yaml --name $(CLUSTER) --wait 120s

image: ## Build the demo-app image and load it into kind
	docker build -t $(IMAGE) app
	kind load docker-image $(IMAGE) --name $(CLUSTER)

repos:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
	helm repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null
	helm repo update >/dev/null

install: repos ## Install kube-prometheus-stack, Loki and Alloy (pinned versions)
	helm upgrade --install kps prometheus-community/kube-prometheus-stack --version $(KPS_VERSION) \
	  -n $(MON_NS) --create-namespace -f helm/kube-prometheus-stack.yaml --wait --timeout 10m
	helm upgrade --install loki grafana/loki --version $(LOKI_VERSION) \
	  -n $(MON_NS) -f helm/loki.yaml --wait --timeout 10m
	helm upgrade --install alloy grafana/alloy --version $(ALLOY_VERSION) \
	  -n $(MON_NS) -f helm/alloy.yaml --wait --timeout 5m

apps: ## Deploy SLO rules, dashboard, demo-app and load generator
	kubectl apply -k k8s/monitoring
	kubectl apply -k k8s/demo-app
	kubectl -n demo rollout status deploy/demo-app --timeout=180s
	kubectl -n demo rollout status deploy/loadgen --timeout=180s

verify: ## End-to-end checks: scrape, rules, dashboard, logs
	scripts/verify.sh

chaos-test: ## verify + inject failures and prove the page reaches Alertmanager
	CHAOS=1 scripts/verify.sh

## ---- Game day -------------------------------------------------------------

chaos: ## Make 50% of requests fail (fast-burn page in ~10 min)
	kubectl -n demo set env deploy/demo-app ERROR_RATE=0.5

latency: ## Add 600ms latency (DemoAppHighLatency after ~12 min)
	kubectl -n demo set env deploy/demo-app EXTRA_LATENCY_MS=600

calm: ## Remove all injected faults
	kubectl -n demo set env deploy/demo-app ERROR_RATE=0 EXTRA_LATENCY_MS=0

grafana: ## Grafana on http://localhost:3000 (admin/admin, anonymous viewer enabled)
	kubectl -n $(MON_NS) port-forward svc/kps-grafana 3000:80

prometheus: ## Prometheus on http://localhost:9090
	kubectl -n $(MON_NS) port-forward svc/kps-prometheus 9090:9090

alertmanager: ## Alertmanager on http://localhost:9093
	kubectl -n $(MON_NS) port-forward svc/kps-alertmanager 9093:9093

## ---- Tests and lint (no cluster needed) -----------------------------------

test: test-app test-rules ## Run all offline tests

test-app: ## Unit tests for demo-app
	cd app && python3 -m unittest discover -s tests -v

build/demo-app-slo.rules.yaml: k8s/monitoring/prometheusrule-demo-app-slo.yaml
	@mkdir -p build
	yq '.spec' $< > $@

test-rules: build/demo-app-slo.rules.yaml ## promtool: check and unit-test the SLO alerts
	promtool check rules $<
	promtool test rules tests/rules/demo-app-slo.test.yaml

lint: lint-manifests lint-dashboards ## Validate manifests, Helm values and dashboards

lint-manifests:
	kubectl kustomize k8s/demo-app | kubeconform -strict -summary -schema-location default -schema-location '$(CRD_SCHEMAS)'
	kubectl kustomize k8s/monitoring | kubeconform -strict -summary -schema-location default -schema-location '$(CRD_SCHEMAS)'
	helm template kps prometheus-community/kube-prometheus-stack --version $(KPS_VERSION) -n $(MON_NS) -f helm/kube-prometheus-stack.yaml \
	  | kubeconform -summary -ignore-missing-schemas -schema-location default -schema-location '$(CRD_SCHEMAS)'
	helm template loki grafana/loki --version $(LOKI_VERSION) -n $(MON_NS) -f helm/loki.yaml \
	  | kubeconform -summary -ignore-missing-schemas -schema-location default
	helm template alloy grafana/alloy --version $(ALLOY_VERSION) -n $(MON_NS) -f helm/alloy.yaml \
	  | kubeconform -summary -ignore-missing-schemas -schema-location default

lint-dashboards:
	python3 scripts/lint_dashboards.py
	promtool check rules build/dashboard-queries.rules.yaml
