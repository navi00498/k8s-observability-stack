"""Steady synthetic traffic for demo-app so dashboards and SLOs always have data."""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from collections import Counter

# (weight, method, path factory)
TRAFFIC = [
    (5, "GET", lambda: "/api/orders"),
    (8, "GET", lambda: f"/api/orders/{random.randint(1, 1010)}"),
    (2, "POST", lambda: "/api/checkout"),
]


def pick() -> tuple[str, str]:
    weights = [w for w, _, _ in TRAFFIC]
    _, method, path = random.choices(TRAFFIC, weights=weights)[0]
    return method, path()


def send(base_url: str, method: str, path: str, timeout: float = 5.0) -> int:
    data = b"{}" if method == "POST" else None
    request = urllib.request.Request(base_url + path, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0


def main() -> None:
    base_url = os.environ.get("TARGET_URL", "http://demo-app.demo.svc:8080").rstrip("/")
    rps = max(float(os.environ.get("RPS", "5")), 0.1)
    report_every = 30
    stats: Counter[str] = Counter()
    last_report = time.monotonic()
    print(json.dumps({"level": "info", "msg": "loadgen starting", "target": base_url, "rps": rps}), flush=True)
    while True:
        started = time.monotonic()
        status = send(base_url, *pick())
        stats["error" if status == 0 or status >= 500 else str(status)[0] + "xx"] += 1
        if started - last_report >= report_every:
            print(json.dumps({"level": "info", "msg": "loadgen stats", **stats}), flush=True)
            stats.clear()
            last_report = started
        time.sleep(max(0.0, 1 / rps - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
