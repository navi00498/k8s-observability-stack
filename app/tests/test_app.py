import io
import json
import threading
import unittest
import urllib.request

import loadgen
from app import Config, create_server


class AppTests(unittest.TestCase):
    def start(self, **config):
        self.logs = io.StringIO()
        server = create_server("127.0.0.1", 0, Config(log_stream=self.logs, **config))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def metrics(self, url):
        with urllib.request.urlopen(url + "/metrics") as response:
            return response.read().decode()

    def test_routes_and_red_metrics(self):
        url = self.start()
        self.assertEqual(loadgen.send(url, "GET", "/api/orders"), 200)
        self.assertEqual(loadgen.send(url, "GET", "/api/orders/42"), 200)
        self.assertEqual(loadgen.send(url, "GET", "/api/orders/5000"), 404)
        self.assertEqual(loadgen.send(url, "POST", "/api/checkout"), 201)
        self.assertEqual(loadgen.send(url, "GET", "/nope/123"), 404)

        text = self.metrics(url)
        self.assertIn('http_requests_total{code="200",method="GET",route="/api/orders"} 1.0', text)
        self.assertIn('http_requests_total{code="404",method="GET",route="/api/orders/{id}"} 1.0', text)
        self.assertIn('http_requests_total{code="404",method="GET",route="unmatched"} 1.0', text)
        self.assertIn('http_request_duration_seconds_bucket{le="+Inf",method="POST",route="/api/checkout"} 1.0', text)
        self.assertIn("demo_orders_created_total 1.0", text)
        self.assertIn('demo_app_build_info{version="0.1.0"} 1.0', text)
        self.assertNotRegex(text, r"_created[{ ]")

    def test_health_and_metrics_are_not_counted(self):
        url = self.start()
        for path in ("/healthz", "/readyz", "/"):
            self.assertEqual(loadgen.send(url, "GET", path), 200)
        self.assertNotIn("http_requests_total{", self.metrics(url))

    def test_error_injection(self):
        url = self.start(error_rate=1.0)
        self.assertEqual(loadgen.send(url, "GET", "/api/orders"), 500)
        self.assertEqual(loadgen.send(url, "GET", "/healthz"), 200)
        text = self.metrics(url)
        self.assertIn('http_requests_total{code="500",method="GET",route="/api/orders"} 1.0', text)
        self.assertIn("demo_app_fault_error_rate 1.0", text)

    def test_latency_injection(self):
        url = self.start(extra_latency_ms=300)
        loadgen.send(url, "GET", "/api/orders/1")
        text = self.metrics(url)
        self.assertIn('http_request_duration_seconds_bucket{le="0.25",method="GET",route="/api/orders/{id}"} 0.0', text)
        self.assertIn('http_request_duration_seconds_bucket{le="0.5",method="GET",route="/api/orders/{id}"} 1.0', text)

    def test_structured_logs(self):
        url = self.start(error_rate=1.0)
        loadgen.send(url, "GET", "/api/orders?page=2")
        record = json.loads(self.logs.getvalue().splitlines()[-1])
        self.assertEqual(record["level"], "error")
        self.assertEqual(record["route"], "/api/orders")
        self.assertEqual(record["path"], "/api/orders")
        self.assertEqual(record["status"], 500)
        self.assertGreater(record["duration_ms"], 0)

    def test_config_from_env_is_clamped(self):
        config = Config.from_env({"ERROR_RATE": "7", "EXTRA_LATENCY_MS": "-5"})
        self.assertEqual((config.error_rate, config.extra_latency_ms), (1.0, 0))
        self.assertEqual(Config.from_env({}).error_rate, 0.0)

    def test_loadgen_handles_unreachable_target(self):
        self.assertEqual(loadgen.send("http://127.0.0.1:9", "GET", "/api/orders", timeout=1), 0)
        method, path = loadgen.pick()
        self.assertIn(method, {"GET", "POST"})
        self.assertTrue(path.startswith("/api/"))


if __name__ == "__main__":
    unittest.main()
