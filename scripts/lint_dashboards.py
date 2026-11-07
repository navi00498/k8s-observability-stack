#!/usr/bin/env python3
"""Static checks for Grafana dashboards in k8s/monitoring/dashboards.

- valid JSON, unique uid / panel ids, panels don't overlap on the grid
- every datasource reference points at a provisioned datasource uid
- every PromQL query is written to a rules file so `promtool check rules` can syntax-check it
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS = sorted((ROOT / "k8s/monitoring/dashboards").glob("*.json"))
DATASOURCE_UIDS = {"prometheus", "loki"}
OUT = ROOT / "build/dashboard-queries.rules.yaml"


def check(path: Path, errors: list[str], exprs: list[str]) -> None:
    dashboard = json.loads(path.read_text())
    name = path.name
    if not dashboard.get("uid"):
        errors.append(f"{name}: missing uid")
    ids, cells = set(), {}
    for panel in dashboard.get("panels", []):
        pid, title = panel.get("id"), panel.get("title", "?")
        if pid in ids:
            errors.append(f"{name}: duplicate panel id {pid}")
        ids.add(pid)
        grid = panel["gridPos"]
        if grid["x"] + grid["w"] > 24:
            errors.append(f"{name}: panel '{title}' is wider than the grid")
        for x in range(grid["x"], grid["x"] + grid["w"]):
            for y in range(grid["y"], grid["y"] + grid["h"]):
                if (x, y) in cells:
                    errors.append(f"{name}: panel '{title}' overlaps '{cells[(x, y)]}'")
                    break
                cells[(x, y)] = title
            else:
                continue
            break
        for target in panel.get("targets", []):
            ds = target.get("datasource") or panel.get("datasource") or {}
            if ds.get("uid") not in DATASOURCE_UIDS:
                errors.append(f"{name}: panel '{title}' uses unknown datasource {ds}")
            if ds.get("type") == "prometheus":
                exprs.append(target["expr"])
    for annotation in dashboard.get("annotations", {}).get("list", []):
        if annotation.get("datasource", {}).get("type") == "prometheus":
            exprs.append(annotation["expr"])


def main() -> int:
    errors: list[str] = []
    exprs: list[str] = []
    for path in DASHBOARDS:
        check(path, errors, exprs)
    OUT.parent.mkdir(exist_ok=True)
    rules = [{"record": f"dashboard:query_{i}", "expr": expr} for i, expr in enumerate(exprs)]
    OUT.write_text(json.dumps({"groups": [{"name": "dashboard-queries", "rules": rules}]}, indent=2))
    for error in errors:
        print(f"ERROR {error}")
    print(f"{len(DASHBOARDS)} dashboard(s), {len(exprs)} PromQL queries -> {OUT.relative_to(ROOT)}")
    return 1 if errors or not DASHBOARDS else 0


if __name__ == "__main__":
    sys.exit(main())
