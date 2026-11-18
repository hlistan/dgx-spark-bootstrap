#!/usr/bin/env python3
"""Nightly LLM benchmark on the Spark: fixed workloads, so throughput is comparable night to night (S8).

Runs ON the Spark (cron 03:30, `make schedule`; on demand `make bench`). Talks to vLLM like a client would and records:
  $SPARK_REPORTS_DIR/bench.csv                          history (spark-analyze.py compares against it)
  $SPARK_REPORTS_DIR/metrics/spark_bench.prom           node_exporter textfile → alerts SparkBenchRegression/Failed

Cases (thinking off, greedy, ignore_eos so every run generates exactly the same number of tokens):
  decode    short coding prompt, 512 tokens out          single-stream decode speed (what an agent feels)
  ctx32k    ~32k-token code context, 256 tokens out      prefill speed and decode speed at depth
  conc4     4 × decode in parallel                        aggregate throughput (decode tok/s summed)
Each prompt starts with a fresh nonce, so the prefix cache can't make a run look faster than real first contact.
Skips (status "busy") if real requests are running and stay running for 10 minutes: it never competes with users.

    python3 -I analysis/spark-bench.py
"""

import concurrent.futures as cf
import datetime as dt
import json
import os
import statistics
import time
import urllib.request
import uuid
from pathlib import Path

OUT = Path(os.environ.get("SPARK_REPORTS_DIR", Path.home() / "spark-reports"))
INFERENCE_DIR = Path(os.environ.get("SPARK_INFERENCE_DIR", Path.home() / "spark/inference"))
INVENTORY = INFERENCE_DIR.parent / "inventory.env"
HEADER = "timestamp,case,ttft_s,prefill_tps,decode_tps,prompt_tokens,completion_tokens,status\n"


def envfile(p: Path) -> dict[str, str]:
    """Read KEY=value literally (never source: values may contain `$`)."""
    out = {}
    if p.exists():
        for ln in p.read_text().splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
    return out


ENV = envfile(INFERENCE_DIR / ".env")
BASE = f"http://{ENV.get('SPARK_BIND_IP', '127.0.0.1')}:8000"
KEY = ENV.get("SPARK_API_KEY", "")
MODEL = envfile(INVENTORY).get("SPARK_LLM_MODEL", "qwen3.8-27b")

TASK = (
    "Write a Python module implementing an LRU cache with TTL expiry: a class with get, put, delete and "
    "a background-free expiry check on access, full type hints, docstrings, and pytest tests covering eviction "
    "order, expiry and capacity edge cases."
)


def code_context(tokens: int) -> str:
    """Deterministic synthetic repository dump of roughly `tokens` tokens (~30 tokens per function, measured)."""
    body = []
    for i in range(tokens // 30):
        body.append(f"def handler_{i}(x: int) -> int:\n    return (x * {i % 97}) ^ {i}\n")
    return "".join(body)


def running_requests() -> float:
    try:
        with urllib.request.urlopen(f"{BASE}/metrics", timeout=10) as r:
            for ln in r.read().decode().splitlines():
                if ln.startswith("vllm:num_requests_running"):
                    return float(ln.rsplit(" ", 1)[1])
    except Exception:
        return -1
    return 0


def stream(prompt: str, max_tokens: int) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": f"[run {uuid.uuid4()}]\n{prompt}"}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    first = last = None
    usage = {}
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if chunk.get("usage"):
                usage = chunk["usage"]
            for c in chunk.get("choices", []):
                d = c.get("delta", {})
                if d.get("content") or d.get("reasoning_content") or d.get("reasoning"):
                    now = time.monotonic()
                    first = first or now
                    last = now
    if first is None or not usage:
        raise RuntimeError("no tokens streamed")
    n_out, n_in = usage["completion_tokens"], usage["prompt_tokens"]
    ttft = first - t0
    return {
        "ttft": ttft,
        "prefill": n_in / ttft,
        "decode": (n_out - 1) / (last - first) if last > first else 0.0,
        "n_in": n_in,
        "n_out": n_out,
    }


def main():
    OUT.joinpath("metrics").mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    rows: list[tuple] = []
    ok = True

    # Don't compete with real users: wait up to 10 min for an idle server.
    for _ in range(20):
        if running_requests() == 0:
            break
        time.sleep(30)
    else:
        rows.append((ts, "all", "", "", "", "", "", "busy" if running_requests() > 0 else "unreachable"))
        ok = False

    if ok:
        try:
            stream("Say OK.", 8)  # warm-up: first request after idle pays graph/launch costs
            cases = {
                "decode": lambda: stream(TASK, 512),
                "ctx32k": lambda: stream(code_context(32_000) + "\n\n" + TASK, 256),
            }
            for name, fn in cases.items():
                x = fn()
                rows.append(
                    (
                        ts,
                        name,
                        f"{x['ttft']:.3f}",
                        f"{x['prefill']:.0f}",
                        f"{x['decode']:.2f}",
                        x["n_in"],
                        x["n_out"],
                        "ok",
                    )
                )
            with cf.ThreadPoolExecutor(4) as pool:
                xs = list(pool.map(lambda _: stream(TASK, 256), range(4)))
            rows.append(
                (
                    ts,
                    "conc4",
                    f"{statistics.median(x['ttft'] for x in xs):.3f}",
                    "",
                    f"{sum(x['decode'] for x in xs):.2f}",
                    xs[0]["n_in"],
                    sum(x["n_out"] for x in xs),
                    "ok",
                )
            )
        except Exception as e:  # recorded, not raised: cron should log one line, the alert does the rest
            rows.append((ts, "all", "", "", "", "", "", f"error: {str(e)[:120].replace(',', ';')}"))
            ok = False

    csv = OUT / "bench.csv"
    if not csv.exists():
        csv.write_text(HEADER)
    with csv.open("a") as f:
        for r in rows:
            f.write(",".join(str(c) for c in r) + "\n")

    m = [
        "# HELP spark_bench_last_run_timestamp_seconds When analysis/spark-bench.py last ran.",
        "# TYPE spark_bench_last_run_timestamp_seconds gauge",
        f"spark_bench_last_run_timestamp_seconds {time.time():.0f}",
        "# HELP spark_bench_last_success 1 if the last benchmark completed, 0 if it failed or was skipped.",
        "# TYPE spark_bench_last_success gauge",
        f"spark_bench_last_success {int(ok)}",
    ]
    # Keep the previous good numbers when this run failed, so the regression alert compares like with like.
    prev = OUT / "metrics" / "spark_bench.prom"
    if ok:
        for metric, idx, help_ in (
            ("decode_tokens_per_second", 4, "Decode tokens/s (conc4: summed over 4 streams)."),
            ("prefill_tokens_per_second", 3, "Prompt tokens/s until the first token."),
            ("ttft_seconds", 2, "Time to first token."),
        ):
            m += [f"# HELP spark_bench_{metric} {help_}", f"# TYPE spark_bench_{metric} gauge"]
            m += [f'spark_bench_{metric}{{case="{r[1]}"}} {r[idx]}' for r in rows if r[idx] != ""]
    elif prev.exists():
        keep = ("spark_bench_decode_", "spark_bench_prefill_", "spark_bench_ttft_")
        for ln in prev.read_text().splitlines():
            name = ln.split()[2] if ln.startswith("#") else ln
            if name.startswith(keep):
                m.append(ln)
    tmp = prev.with_suffix(".tmp")
    tmp.write_text("\n".join(m) + "\n")
    tmp.replace(prev)
    for r in rows:
        print(" ".join(str(c) for c in r))


if __name__ == "__main__":
    main()
