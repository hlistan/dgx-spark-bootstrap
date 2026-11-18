#!/usr/bin/env python3
"""Fetch the community Grafana dashboards at pinned revisions and adapt them to this box.

    python3 -I monitoring/grafana/vendor-dashboards.py      # rewrites monitoring/grafana/dashboards/*.json

Sources (both MIT / Apache, credited in monitoring/README.md):
- RodriMora/dgx-spark-grafana-dashboard: vLLM + node_exporter + nvidia_gpu_exporter for DGX Spark (GB10).
- Grafana.com 1860 "Node Exporter Full": the de-facto standard host dashboard.

Patches applied to the Spark dashboard (kept so a second vLLM model can be added without re-vendoring):
- a `model` variable over vLLM's `model_name` label, injected into every `vllm:*` selector;
- a stable uid/title so provisioning and links do not change between updates.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

SPARK_REPO, SPARK_SHA = "RodriMora/dgx-spark-grafana-dashboard", "0ea065652f861d4e7a7156bae2ee084ab491a2e8"
NODE_FULL_ID, NODE_FULL_REV = 1860, 45  # grafana.com revision numbers; the file's own "version" differs
OUT = Path(__file__).resolve().parent / "dashboards"


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


VLLM_SEL = re.compile(r"(vllm:[a-z_:]+)(\{([^}]*)\})?")


def scope_vllm(expr: str) -> str:
    def sub(m):
        name, inner = m.group(1), m.group(3)
        labels = 'model_name=~"$model"' + (f",{inner}" if inner else "")
        return f"{name}{{{labels}}}"

    return VLLM_SEL.sub(sub, expr)


def walk(panels):
    for p in panels:
        yield p
        yield from walk(p.get("panels", []))


def spark_dashboard() -> dict:
    d = get(f"https://raw.githubusercontent.com/{SPARK_REPO}/{SPARK_SHA}/dashboards/dgx-spark-vllm.json")
    for p in walk(d["panels"]):
        for t in p.get("targets", []):
            if "expr" in t:
                t["expr"] = scope_vllm(t["expr"])
    d.setdefault("templating", {}).setdefault("list", []).insert(
        0,
        {
            "name": "model",
            "label": "Model",
            "type": "query",
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "query": {"query": "label_values(vllm:num_requests_running, model_name)", "refId": "model"},
            "definition": "label_values(vllm:num_requests_running, model_name)",
            "refresh": 2,
            "includeAll": True,
            "multi": True,
            "current": {"text": "qwen3.8-27b", "value": ["qwen3.8-27b"]},
        },
    )
    d.update(uid="dgx-spark-vllm", title="DGX Spark — inference + GB10", id=None)
    d.setdefault("tags", []).extend(t for t in ["spark", "vllm", "gb10"] if t not in d.get("tags", []))
    return d


def node_full() -> dict:
    d = get(f"https://grafana.com/api/dashboards/{NODE_FULL_ID}/revisions/{NODE_FULL_REV}/download")
    d.update(id=None)
    return d


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for name, fn in [("dgx-spark-vllm.json", spark_dashboard), ("node-exporter-full.json", node_full)]:
        (OUT / name).write_text(json.dumps(fn(), indent=1, ensure_ascii=False) + "\n")
        print("wrote", OUT / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
