"""demo-app: a small "orders" API instrumented the way production services should be.

- RED metrics (Rate, Errors, Duration) in Prometheus format on /metrics
- One structured JSON log line per request on stdout (picked up by Grafana Alloy -> Loki)
- Fault injection through environment variables so you can watch SLO alerts fire:
    ERROR_RATE=0.2        fraction of /api requests that fail with HTTP 500
    EXTRA_LATENCY_MS=400  latency added to every /api request
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TextIO

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)

VERSION = "0.1.0"
LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
MAX_ORDER_ID = 1000

disable_created_metrics()


@dataclass
class Config:
    error_rate: float = 0.0
    extra_latency_ms: int = 0
    log_stream: TextIO = sys.stdout

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        error_rate = min(max(float(env.get("ERROR_RATE", "0") or 0), 0.0), 1.0)
        return cls(error_rate=error_rate, extra_latency_ms=max(int(env.get("EXTRA_LATENCY_MS", "0") or 0), 0))


class Metrics:
    def __init__(self, registry: CollectorRegistry):
        self.registry = registry
        self.requests = Counter(
            "http_requests", "HTTP requests served", ["method", "route", "code"], registry=registry
        )
        self.latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route"],
            buckets=LATENCY_BUCKETS,
            registry=registry,
        )
        self.in_flight = Gauge("http_requests_in_flight", "Requests currently being served", registry=registry)
        self.orders = Counter("demo_orders_created", "Orders created (business KPI)", registry=registry)
        info = Gauge("demo_app_build_info", "Build information", ["version"], registry=registry)
        info.labels(VERSION).set(1)
        self.fault_error_rate = Gauge("demo_app_fault_error_rate", "Configured injected error rate", registry=registry)
        self.fault_latency = Gauge("demo_app_fault_latency_seconds", "Configured injected latency", registry=registry)


# (method, regex, route label, base latency range in seconds)
ROUTES = [
    ("GET", re.compile(r"^/api/orders$"), "/api/orders", (0.015, 0.06)),
    ("GET", re.compile(r"^/api/orders/(\d+)$"), "/api/orders/{id}", (0.005, 0.03)),
    ("POST", re.compile(r"^/api/checkout$"), "/api/checkout", (0.06, 0.25)),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_handler(config: Config, metrics: Metrics) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"demo-app/{VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # silence default access log; we emit JSON instead
            pass

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode())

        def _log(self, level: str, msg: str, **fields) -> None:
            line = json.dumps({"ts": _now(), "level": level, "msg": msg, **fields})
            with lock:
                config.log_stream.write(line + "\n")
                config.log_stream.flush()

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(min(length, 65536))
            if path == "/metrics" and method == "GET":
                return self._send(200, generate_latest(metrics.registry), CONTENT_TYPE_LATEST)
            if path in ("/healthz", "/readyz") and method == "GET":
                return self._json(200, {"status": "ok"})
            if path == "/" and method == "GET":
                return self._json(200, {"service": "demo-app", "version": VERSION})
            self._api(method, path)

        def _api(self, method: str, path: str) -> None:
            start = time.perf_counter()
            metrics.in_flight.inc()
            route, status, payload = "unmatched", 404, {"error": "not found"}
            try:
                for route_method, pattern, label, (low, high) in ROUTES:
                    match = pattern.match(path)
                    if not match or route_method != method:
                        continue
                    route = label
                    time.sleep(random.uniform(low, high) + config.extra_latency_ms / 1000)
                    if random.random() < config.error_rate:
                        status, payload = 500, {"error": "injected failure"}
                    else:
                        status, payload = self._handle(label, match)
                    break
                self._json(status, payload)
            finally:
                elapsed = time.perf_counter() - start
                metrics.in_flight.dec()
                metrics.requests.labels(method, route, str(status)).inc()
                metrics.latency.labels(method, route).observe(elapsed)
                level = "error" if status >= 500 else "warn" if status >= 400 else "info"
                self._log(
                    level,
                    "request",
                    method=method,
                    path=path,
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 1),
                )

        def _handle(self, route: str, match: re.Match) -> tuple[int, dict]:
            if route == "/api/orders":
                return 200, {"orders": [{"id": i, "total": round(random.uniform(5, 500), 2)} for i in range(1, 6)]}
            if route == "/api/orders/{id}":
                order_id = int(match.group(1))
                if order_id > MAX_ORDER_ID:
                    return 404, {"error": f"order {order_id} not found"}
                return 200, {"id": order_id, "status": random.choice(["paid", "shipped", "delivered"])}
            metrics.orders.inc()
            return 201, {"id": random.randint(1, MAX_ORDER_ID), "status": "paid"}

    return Handler


def create_server(host: str = "0.0.0.0", port: int = 8080, config: Config | None = None) -> ThreadingHTTPServer:
    config = config or Config.from_env()
    metrics = Metrics(CollectorRegistry())
    metrics.fault_error_rate.set(config.error_rate)
    metrics.fault_latency.set(config.extra_latency_ms / 1000)
    server = ThreadingHTTPServer((host, port), make_handler(config, metrics))
    server.daemon_threads = True
    return server


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    config = Config.from_env()
    server = create_server(port=port, config=config)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        json.dumps(
            {
                "ts": _now(),
                "level": "info",
                "msg": "starting",
                "port": port,
                "version": VERSION,
                "error_rate": config.error_rate,
                "extra_latency_ms": config.extra_latency_ms,
            }
        ),
        flush=True,
    )
    server.serve_forever()
    print(json.dumps({"ts": _now(), "level": "info", "msg": "stopped"}), flush=True)


if __name__ == "__main__":
    main()
