#!/usr/bin/env python3
"""Repo policy gate (S7): rules that generic linters can't know, checked on the resolved compose config.

    python3 -I scripts/lint-policy.py <inference.env> <monitoring.env> <inventory>     # called by scripts/lint.sh

- Exposure (S6): published ports bind only to the LAN IP or loopback; vLLM keeps its API key (VLLM_API_KEY in the
  environment, never on the command line) and --allowed-origins '[]';
  exporters listen on 127.0.0.1; Grafana keeps anonymous access off.
- Pinning (S2): every image has an explicit version tag, never latest/main/nightly.
- Operations: restart policy and healthcheck on inference; memory caps and log rotation on monitoring.
- Consistency: inventory.env (what clients read) matches what the compose files actually serve, and Prometheus
  scrapes the same IP/host.
- Safety rails from S4: LLM_GPU_UTIL ≤ 0.60 (0.70+ hung the box at startup).
- Site leaks: no file that git would track contains a private (RFC 1918) address or a value from inventory.env.
  Site values belong in inventory.env and the .env files, which are gitignored.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def envfile(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def compose(stack: str, env: str) -> dict:
    res = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / stack / "compose.yaml"),
            "--env-file",
            env,
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def flag(cmd: list[str], name: str) -> str | None:
    """Value of `--name value` or `--name=value` in an argv list."""
    for i, a in enumerate(cmd):
        if a == name and i + 1 < len(cmd):
            return cmd[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def check_image(svc: str, image: str | None) -> None:
    if not image:
        err(f"{svc}: no image")
        return
    ref = image.split("@", 1)[0]
    tag = ref.rsplit(":", 1)[1] if ":" in ref.rsplit("/", 1)[-1] else ""
    if not tag or tag in {"latest", "main", "master", "nightly", "dev"}:
        err(f"{svc}: image {image!r} is not pinned to a version tag")


PRIVATE_IP = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
SITE_KEYS = ("SPARK_HOST", "SPARK_HOSTNAME", "SPARK_ALLOWED_IPS")


def check_site_leaks(inv: dict[str, str]) -> None:
    """Files git tracks (or would track) must not name this installation."""
    example = envfile(ROOT / "inventory.env.example")
    # Values the operator set that differ from the example's placeholders; short words would match everywhere.
    site = {v for k in SITE_KEYS for v in inv.get(k, "").split() if v not in example.get(k, "").split() and len(v) > 4}
    res = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    for name in filter(None, res.stdout.split("\0")):
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            hits = set(PRIVATE_IP.findall(line)) | {v for v in site if v in line}
            for h in sorted(hits):
                err(f"{name}:{n}: site value {h!r} in a tracked file (keep it in inventory.env)")


def main() -> int:
    inf_env, mon_env = sys.argv[1], sys.argv[2]
    inv = envfile(ROOT / sys.argv[3])
    ienv = envfile(Path(inf_env))
    bind_ip = ienv.get("SPARK_BIND_IP", "")
    allowed_ips = {bind_ip, "127.0.0.1"}

    if bind_ip != inv.get("SPARK_HOST"):
        err(f"inference SPARK_BIND_IP {bind_ip} != inventory SPARK_HOST {inv.get('SPARK_HOST')}")
    if envfile(Path(mon_env)).get("SPARK_BIND_IP") != bind_ip:
        err("monitoring SPARK_BIND_IP differs from inference's")

    # ── inference ────────────────────────────────────────────────────────────
    inf = compose("inference", inf_env)
    for name, s in inf["services"].items():
        check_image(name, s.get("image"))
        if s.get("restart") not in {"unless-stopped", "always"}:
            err(f"inference/{name}: restart policy {s.get('restart')!r}")
        if not s.get("healthcheck"):
            err(f"inference/{name}: no healthcheck")
        for p in s.get("ports", []):
            if p.get("host_ip") not in allowed_ips:
                err(f"inference/{name}: port {p.get('published')} binds {p.get('host_ip') or 'all interfaces'} (S6)")

    llm = inf["services"].get("llm")
    if not llm:
        err("inference: no `llm` service")
    else:
        cmd = llm.get("command") or []
        if not (llm.get("environment") or {}).get("VLLM_API_KEY"):
            err("llm: VLLM_API_KEY missing from the environment: /v1 would be open (S6)")
        if any(a == "--api-key" or a.startswith("--api-key=") for a in cmd):
            err("llm: pass the key as VLLM_API_KEY, not --api-key: a command line is readable by every user (S6)")
        if flag(cmd, "--allowed-origins") != "[]":
            err("llm: --allowed-origins must be '[]' (GHSA-vvpg-rrww-wpmh, S6)")
        if flag(cmd, "--served-model-name") != inv.get("SPARK_LLM_MODEL"):
            err(f"llm: served model {flag(cmd, '--served-model-name')} != inventory SPARK_LLM_MODEL")
        if flag(cmd, "--max-model-len") != inv.get("SPARK_LLM_CONTEXT"):
            err(f"llm: --max-model-len {flag(cmd, '--max-model-len')} != inventory SPARK_LLM_CONTEXT")
        util = float(flag(cmd, "--gpu-memory-utilization") or 0)
        if not 0 < util <= 0.60:
            err(f"llm: --gpu-memory-utilization {util} outside (0, 0.60] (S4: 0.70+ hung the box)")
        url = inv.get("SPARK_LLM_BASE_URL", "")
        published = {str(p.get("published")) for p in llm.get("ports", [])}
        m = re.match(r"http://([\d.]+):(\d+)/v1$", url)
        if not m or m.group(1) != bind_ip or m.group(2) not in published:
            err(f"inventory SPARK_LLM_BASE_URL {url} doesn't match llm's published port {published} on {bind_ip}")

    # ── monitoring ───────────────────────────────────────────────────────────
    mon = compose("monitoring", mon_env)
    for name, s in mon["services"].items():
        check_image(name, s.get("image"))
        if not s.get("mem_limit"):
            err(f"monitoring/{name}: no mem_limit (every byte comes out of vLLM's unified-memory pool)")
        if (s.get("logging") or {}).get("options", {}).get("max-size") is None:
            err(f"monitoring/{name}: no log rotation")
        for a in s.get("command") or []:
            if re.match(r"--(web\.listen-address|listen_ip)", a) and "127.0.0.1" not in a:
                err(f"monitoring/{name}: {a} not on loopback (S6)")
    graf = mon["services"].get("grafana", {}).get("environment", {})
    if graf.get("GF_AUTH_ANONYMOUS_ENABLED") != "false" or graf.get("GF_SERVER_HTTP_ADDR") != bind_ip:
        err("grafana: anonymous access must stay off and it must bind to SPARK_BIND_IP")

    # ── prometheus ───────────────────────────────────────────────────────────
    prom = (ROOT / "monitoring/prometheus/prometheus.yml").read_text()
    for ip in set(re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", prom)) - {"127.0.0.1"}:
        if ip != inv.get("SPARK_HOST"):
            err(f"prometheus.yml targets {ip}, inventory SPARK_HOST is {inv.get('SPARK_HOST')}")
    for host in set(re.findall(r"host_id:\s*([\w-]+)|replacement:\s*(spark-[\w-]+)", prom)):
        h = host[0] or host[1]
        if h != inv.get("SPARK_HOSTNAME"):
            err(f"prometheus.yml host_id {h} != inventory SPARK_HOSTNAME {inv.get('SPARK_HOSTNAME')}")

    check_site_leaks(inv)

    for e in errors:
        print(f"  policy: {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
