#!/usr/bin/env python3
"""Periodic Spark analysis: stability and performance over a time window (docs/decisions.md S8).

Runs ON the Spark (cron, `make schedule`; on demand `make analyze`). Reads Prometheus on loopback, the nightly
benchmark history (spark-bench.py) and a few local facts (docker, nvidia-smi), then writes:
  $SPARK_REPORTS_DIR/<date>-<window>.md   report: verdict per check + concrete recommendations
  $SPARK_REPORTS_DIR/latest-<window>.md   symlink to the newest
  $SPARK_REPORTS_DIR/metrics/spark_analysis_<window>.prom   node_exporter textfile → alert SparkAnalysisFindings and
                                                           the Grafana home dashboard (one series per check)

    python3 -I analysis/spark-analyze.py [--window 24h|7d] [--stdout]

Stdlib only (python 3.12 on DGX OS). Thresholds are tuned to this box: GB10, 128 GB unified memory, one vLLM at
--gpu-memory-utilization 0.60, Qwen3.8-27B + DFlash2 (S4: ~23–37 tok/s single-stream decode).
"""

import argparse
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

PROM = os.environ.get("SPARK_PROMETHEUS", "http://127.0.0.1:9090")
OUT = Path(os.environ.get("SPARK_REPORTS_DIR", Path.home() / "spark-reports"))
INFERENCE_DIR = Path(os.environ.get("SPARK_INFERENCE_DIR", Path.home() / "spark/inference"))
GiB = 1024**3
KEEP_DAYS = 90

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
LEVEL = {OK: 0, INFO: 1, WARN: 2, FAIL: 3}  # spark_analysis_check value; the dashboard maps it back to a colour
ICON = {OK: "✅", INFO: "ℹ️", WARN: "⚠️", FAIL: "❌"}


class Report:
    def __init__(self, window: str):
        self.window = window
        self.rows: list[tuple[str, str, str, str]] = []  # section, check, status, detail
        self.recs: list[str] = []
        self.advice: dict[str, str] = {}  # check → recommendation, for warn/fail rows
        self.facts: list[tuple[str, str]] = []

    def add(self, section, check, status, detail, rec=None):
        self.rows.append((section, check, status, detail))
        if rec and status in (WARN, FAIL):
            self.recs.append(f"**{check}** — {rec}")
            self.advice[check] = rec

    def count(self, status):
        return sum(1 for r in self.rows if r[2] == status)


# ── data sources ─────────────────────────────────────────────────────────────
def q(expr: str) -> list[tuple[dict, float]]:
    """Instant query → [(labels, value)]. Empty list when there is no data or Prometheus errors."""
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)["data"]["result"]
    except Exception:
        return []
    out = []
    for x in data:
        try:
            v = float(x["value"][1])
        except (KeyError, ValueError):
            continue
        if v == v:  # drop NaN (empty histograms, 0/0 ratios)
            out.append((x["metric"], v))
    return out


def q1(expr: str) -> float | None:
    r = q(expr)
    return r[0][1] if r else None


def sh(*argv: str, timeout: int = 60) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def gpu_kernel_errors(w: str) -> tuple[list[str] | None, int]:
    """Xid lines (deduplicated) and the count of other NVRM error lines in the kernel journal over the window.

    The user reads the journal via the adm group. ~10 s even during a 28k-lines/hour error storm.
    """
    since = {"24h": "-24h", "7d": "-7d"}.get(w, f"-{w}")
    try:
        p = subprocess.run(
            ["journalctl", "-k", "--since", since, "-o", "cat", "--no-pager", "-g", "NVRM"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception:
        return None, 0
    if p.returncode not in (0, 1):  # 1 = no matches
        return None, 0
    xid, other = [], 0
    for ln in p.stdout.splitlines():
        m = re.search(r"Xid \(([^)]*)\): (\d+)", ln)
        if m:
            xid.append(f"Xid {m.group(2)}")
        elif re.search(r"bad register read|Assertion failed|requires reset|GSP", ln):
            other += 1
    return sorted(set(xid)), other


def num(cell: str) -> float | None:
    return float(cell) if cell else None


def fmt(v: float | None, spec: str = ".1f", unit: str = "") -> str:
    return "n/a" if v is None else f"{v:{spec}}{unit}"


# ── checks ───────────────────────────────────────────────────────────────────
def stability(r: Report, w: str):
    s = "Stability"
    restarts = [
        (m.get("name", "?"), v) for m, v in q(f'changes(container_start_time_seconds{{name=~"spark-.*"}}[{w}])')
    ]
    bad = [f"{n}×{int(v)}" for n, v in restarts if v > 0]
    r.add(
        s,
        "Container restarts",
        WARN if bad else OK,
        ", ".join(bad) or "none",
        "check `make inference-logs` around the restart time; repeated restarts of spark-llm usually mean OOM or a "
        "failed healthcheck during a long compile",
    )

    # A container hitting its own mem_limit also counts as a kernel OOM kill; the remainder is box-wide exhaustion.
    cont = [
        (m.get("name", "?"), v) for m, v in q(f'increase(container_oom_events_total{{name=~"spark-.*"}}[{w}]) > 0.5')
    ]
    kern = q1(f"increase(node_vmstat_oom_kill[{w}])") or 0
    n_cont = sum(round(v) for _, v in cont)
    box = max(0, round(kern) - n_cont)
    names = ", ".join(f"{n}×{round(v)}" for n, v in cont)
    r.add(
        s,
        "OOM kills",
        FAIL if box else WARN if cont else OK,
        f"over its own mem_limit: {names or 'none'}; box-wide: {box}",
        "box-wide: unified memory ran out — lower LLM_GPU_UTIL or --max-num-seqs, or find the stray process "
        "(`ps aux --sort=-rss | head`)"
        if box
        else f"raise mem_limit for {names} in monitoring/compose.yaml (it comes out of vLLM's headroom; keep it small)",
    )

    for m, v in q(
        f'avg_over_time(probe_success{{job="blackbox"}}[{w}]) and on (service) probe_success{{job="blackbox"}}'
    ):
        st = FAIL if v < 0.99 else WARN if v < 0.999 else OK
        r.add(
            s,
            f"Availability {m.get('service')}",
            st,
            f"{v * 100:.2f}%",
            "see InferenceEndpointDown in Prometheus alerts history for the outage windows",
        )

    for m, v in q(f"avg_over_time(up[{w}]) < 0.99 and on (job, instance) up"):
        r.add(
            s,
            f"Scrape target {m.get('job')}",
            WARN,
            f"up {v * 100:.1f}% of the time",
            "an exporter flapped; `make monitoring-logs S=<service>`",
        )

    mem = q1(f"min_over_time(node_memory_MemAvailable_bytes[{w}])")
    r.add(
        s,
        "Min available memory",
        OK if mem is None or mem >= 15 * GiB else FAIL if mem < 8 * GiB else WARN,
        fmt(mem and mem / GiB, ".1f", " GiB"),
        "headroom below plan (≥ 40 GB idle); find who grew with cAdvisor (container_memory_rss) before tuning vLLM",
    )

    swp = q1(f"increase(node_vmstat_pswpout[{w}])") or 0
    r.add(
        s,
        "Swap-out",
        WARN if swp > 10_000 else OK,
        f"{swp:,.0f} pages",
        "memory pressure: something besides vLLM is using memory, or utilization is too high",
    )

    # A faulted GB10 ("GPU requires reset" in nvidia-smi, e.g. after Xid 119) keeps serving from existing CUDA
    # contexts, but the exporter silently drops T.Limit, power and throttle series. Missing ≠ fine.
    gpu_up = q1('max(up{job="gpu"})')
    tlim_now = q1("count(nvidia_smi_temperature_gpu_tlimit)")
    r.add(
        s,
        "GPU telemetry",
        FAIL if gpu_up == 1 and tlim_now is None else OK if gpu_up == 1 else WARN,
        "complete"
        if gpu_up == 1 and tlim_now is not None
        else "T.Limit/power/throttle series missing — nvidia-smi likely shows 'GPU requires reset'"
        if gpu_up == 1
        else "gpu exporter down",
        "check `nvidia-smi -q` and the kernel log (GPU kernel errors below); recovery needs a reboot — software reset "
        "is refused on GB10; the running LLM keeps working but won't come back after a restart",
    )

    xid, regerr = gpu_kernel_errors(w)
    r.add(
        s,
        "GPU kernel errors",
        OK if xid is None or (not xid and not regerr) else FAIL if xid or regerr > 100 else WARN,
        "journal unreadable"
        if xid is None
        else f"{len(xid)} Xid, {regerr:,} other NVRM errors" + (f" — {'; '.join(xid[:2])}" if xid else ""),
        "Xid 119 (GSP timeout) followed by 'bad register read 0xbadf5600' means the GPU is faulted: capture "
        "`sudo nvidia-bug-report.sh`, apply updates, power off with a 30 s cold drain; NVIDIA case if it recurs",
    )

    therm = q1(
        f"sum(increase(nvidia_smi_clocks_event_reasons_counters_hw_thermal_slowdown_seconds[{w}]))"
        f" + sum(increase(nvidia_smi_clocks_event_reasons_counters_sw_thermal_slowdown_seconds[{w}]))"
    )
    brake = q1(f"sum(increase(nvidia_smi_clocks_event_reasons_counters_hw_power_brake_slowdown_seconds[{w}]))")
    tmax = q1(f"max_over_time(nvidia_smi_temperature_gpu[{w}])")
    tlim = q1(f"min_over_time(nvidia_smi_temperature_gpu_tlimit[{w}])")
    r.add(
        s,
        "GPU thermal throttling",
        WARN if (therm or 0) > 1 else OK,
        f"{fmt(therm, '.0f', ' s')} throttled, max {fmt(tmax, '.0f', ' °C')}, "
        f"closest to T.Limit {fmt(tlim, '.0f', ' °C')}",
        "improve airflow / lower ambient; the GB10 loses clocks (and tok/s) when throttling",
    )
    r.add(
        s,
        "GPU power brake",
        WARN if (brake or 0) > 1 else OK,
        fmt(brake, ".0f", " s"),
        "hardware power brake: check the power adapter and its outlet",
    )

    ce = q1(f"sum(increase(node_edac_correctable_errors_total[{w}]))")
    ue = q1(f"sum(increase(node_edac_uncorrectable_errors_total[{w}]))")
    r.add(
        s,
        "Memory ECC errors",
        FAIL if (ue or 0) > 0 else WARN if (ce or 0) > 0 else OK,
        f"correctable {fmt(ce, '.0f')}, uncorrectable {fmt(ue, '.0f')}",
        "run `ras-mc-ctl --summary`; contact NVIDIA support if they recur",
    )

    free = q1('node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}')
    # Median of the last 7 daily deltas: a one-off model download is a step, not a trend (predict_linear
    # extrapolated a 1.4 TB download into "-58263 GiB in 30 days").
    avail = [q1(f'node_filesystem_avail_bytes{{mountpoint="/"}} offset {d}d') for d in range(8)]
    deltas = [a - b for a, b in zip(avail, avail[1:], strict=False) if a is not None and b is not None]
    full_in = avail[0] + 30 * statistics.median(deltas) if avail[0] is not None and deltas else None
    st = OK if free is None or free > 0.15 else WARN
    if full_in is not None and full_in < 0:
        st = WARN
    r.add(
        s,
        "Root disk",
        st,
        f"{fmt(free and free * 100, '.0f', '% free')}, 30-day projection "
        f"{fmt(full_in and full_in / GiB, '.0f', ' GiB')}",
        "prune unused images (see Housekeeping) and old HF revisions",
    )

    nvme = q1(f'max_over_time(node_hwmon_temp_celsius{{chip=~"nvme.*", sensor="temp1"}}[{w}])')
    r.add(s, "NVMe max temperature", WARN if (nvme or 0) > 70 else OK, fmt(nvme, ".0f", " °C"), "check airflow")

    fired = sorted({m["alertname"] for m, _ in q(f'count_over_time(ALERTS{{alertstate="firing"}}[{w}])')})
    r.add(
        s,
        "Alerts fired in window",
        WARN if fired else OK,
        ", ".join(fired) or "none",
        "review each in Prometheus (`make prom`, /alerts) and fix the cause or tune the rule",
    )

    logs = sh("docker", "logs", "--since", w, "spark-llm", timeout=120)
    pat = re.compile(r"\b(ERROR|CRITICAL)\b|Traceback|CUDA error|out of memory|NCCL error", re.I)
    hits = [ln for ln in logs.splitlines() if pat.search(ln)]
    sample = "; ".join(h.strip()[:140] for h in hits[:2])
    r.add(
        s,
        "spark-llm log errors",
        WARN if hits else OK,
        f"{len(hits)} lines" + (f" — e.g. {sample}" if hits else ""),
        "read `make inference-logs S=llm`; corrupted output after cache hits means the image lost vllm#55601 (S4)",
    )


def performance(r: Report, w: str):
    s = "Performance"
    reqs = q1(f"sum(increase(vllm:request_success_total[{w}]))") or 0
    gen = q1(f"sum(increase(vllm:generation_tokens_total[{w}]))") or 0
    prm = q1(f"sum(increase(vllm:prompt_tokens_total[{w}]))") or 0
    cached = q1(f"sum(increase(vllm:prompt_tokens_cached_total[{w}]))") or 0
    r.facts.append(
        (
            "Traffic",
            f"{reqs:,.0f} requests, {prm / 1e6:.2f} M prompt tokens "
            f"({cached / prm * 100 if prm else 0:.0f}% from prefix cache), {gen / 1e6:.2f} M generated",
        )
    )
    if reqs < 5:
        r.add(s, "Live traffic", INFO, f"only {reqs:.0f} requests — latency checks skipped; see the benchmark")
    else:

        def hq(metric, qn):
            return q1(f"histogram_quantile({qn}, sum by (le) (increase(vllm:{metric}_bucket[{w}])))")

        ttft50, ttft95 = hq("time_to_first_token_seconds", 0.5), hq("time_to_first_token_seconds", 0.95)
        r.add(
            s,
            "Time to first token",
            WARN if (ttft95 or 0) > 20 else OK,
            f"p50 {fmt(ttft50, '.2f', ' s')}, p95 {fmt(ttft95, '.2f', ' s')}",
            "long prompts without prefix-cache hits; check prompt sizes below and whether clients keep a stable prefix",
        )

        tpot50, tpot95 = (
            hq("request_time_per_output_token_seconds", 0.5),
            hq("request_time_per_output_token_seconds", 0.95),
        )
        d50 = 1 / tpot50 if tpot50 else None
        d05 = 1 / tpot95 if tpot95 else None
        r.add(
            s,
            "Decode speed per request",
            WARN if d50 is not None and d50 < 15 else OK,
            f"median {fmt(d50, '.1f', ' tok/s')}, slowest 5% {fmt(d05, '.1f', ' tok/s')}",
            "below the recipe's ~23–37 tok/s: look at throttling, concurrency and drafter acceptance",
        )

        qt95 = hq("request_queue_time_seconds", 0.95)
        r.add(
            s,
            "Queue time p95",
            WARN if (qt95 or 0) > 5 else OK,
            fmt(qt95, ".2f", " s"),
            "requests wait for a slot: more concurrent clients than --max-num-seqs (8)",
        )

        p99 = hq("request_prompt_tokens", 0.99)
        r.facts.append(("Prompt size p99 (histogram bucket)", "≤ " + fmt(p99, ",.0f", " tokens")))

    kv = q1(f"max_over_time(vllm:kv_cache_usage_perc[{w}])")
    pre = q1(f"sum(increase(vllm:num_preemptions_total[{w}]))") or 0
    r.add(
        s,
        "KV cache peak",
        WARN if (kv or 0) > 0.9 or pre > 0 else OK,
        f"{fmt(kv and kv * 100, '.0f', '%')}, {pre:.0f} preemptions",
        "the KV pool is too small for the load: cap client context, lower --max-num-seqs, or trade context for pool",
    )

    run_max = q1(f"max_over_time(vllm:num_requests_running[{w}])")
    wait_max = q1(f"max_over_time(vllm:num_requests_waiting[{w}])")
    rej = q1(f"sum(increase(vllm:admission_rejections_total[{w}]))") or 0
    sat = (run_max or 0) >= 8 and (wait_max or 0) > 0
    r.add(
        s,
        "Concurrency",
        WARN if sat or rej > 0 else OK,
        f"peak running {fmt(run_max, '.0f')}/8, peak waiting {fmt(wait_max, '.0f')}, rejected {rej:.0f}",
        "saturated at --max-num-seqs 8 while KV had room: try 12 and re-run the load test (inference/README step 9)"
        if (kv or 1) < 0.7
        else "saturated and KV is busy too: the box is at capacity for this workload",
    )

    accepted = f"sum(increase(vllm:spec_decode_num_accepted_tokens_total[{w}]))"
    acc = q1(f"{accepted} / sum(increase(vllm:spec_decode_num_draft_tokens_total[{w}]))")
    alen = q1(f"1 + {accepted} / sum(increase(vllm:spec_decode_num_drafts_total[{w}]))")
    r.add(
        s,
        "DFlash2 drafter",
        WARN if acc is not None and acc < 0.3 else OK,
        f"acceptance {fmt(acc and acc * 100, '.0f', '%')}, {fmt(alen, '.2f')} tokens per step",
        "low acceptance for this traffic: benchmark num_speculative_tokens 5 vs 7 (S4)",
    )

    hit = q1(f"sum(increase(vllm:prefix_cache_hits_total[{w}])) / sum(increase(vllm:prefix_cache_queries_total[{w}]))")
    r.add(
        s,
        "Prefix cache hit rate",
        INFO if hit is not None and hit < 0.3 and reqs >= 5 else OK,
        fmt(hit and hit * 100, ".0f", "%"),
        "agents normally hit > 50%; low rates mean clients rewrite early context (timestamps in system prompts)",
    )

    util = q1(f"avg_over_time(nvidia_smi_utilization_gpu_ratio[{w}])")
    pw = q1(f"avg_over_time(nvidia_smi_power_draw_watts[{w}])")
    r.facts.append(("GPU average", f"utilization {fmt(util and util * 100, '.0f', '%')}, power {fmt(pw, '.0f', ' W')}"))


def benchmark(r: Report):
    s = "Benchmark"
    csv = OUT / "bench.csv"
    if not csv.exists():
        r.add(s, "Nightly benchmark", WARN, "no history yet", "run `make bench` once and check `make schedule`")
        return
    rows = [ln.split(",") for ln in csv.read_text().splitlines()[1:] if ln.count(",") >= 7]
    by_case: dict[str, list[list[str]]] = {}
    for x in rows:
        by_case.setdefault(x[1], []).append(x)
    for case, xs in sorted(by_case.items()):
        last = xs[-1]
        if last[7] != "ok":
            r.add(s, f"bench {case}", WARN, f"last run {last[0]}: {last[7]}", "see ~/spark-reports/cron.log")
            continue
        dec = float(last[4]) if last[4] else None
        hist = [float(x[4]) for x in xs[-15:-1] if x[7] == "ok" and x[4]]
        med = statistics.median(hist) if hist else None
        st = WARN if dec and med and dec < 0.85 * med else OK
        detail = (
            f"{last[0][:10]}: decode {fmt(dec, '.1f', ' tok/s')}, "
            f"prefill {fmt(num(last[3]), ',.0f', ' tok/s')}, "
            f"TTFT {fmt(num(last[2]), '.2f', ' s')}; 14-run median decode {fmt(med, '.1f', ' tok/s')}"
        )
        r.add(
            s,
            f"bench {case}",
            st,
            detail,
            "throughput regressed against its own history: compare with throttling above and any image/driver change",
        )


def housekeeping(r: Report):
    s = "Housekeeping"
    used = set(sh("docker", "ps", "-a", "--format", "{{.Image}}").split())
    unused = []
    for ln in sh("docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}").splitlines():
        img, _, size = ln.partition("\t")
        if img not in used and not img.endswith(":<none>"):
            unused.append(f"{img} ({size})")
    r.add(s, "Unused images", INFO if unused else OK, ", ".join(unused) or "none", None)
    if unused:
        r.recs.append(
            "**Unused images** — reclaim disk: `docker image rm " + " ".join(u.split(" ")[0] for u in unused) + "`"
        )

    env = {}
    p = INFERENCE_DIR / ".env"
    if p.exists():
        for ln in p.read_text().splitlines():
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                env[k] = v
    ip = env.get("SPARK_BIND_IP", "127.0.0.1")
    try:
        with urllib.request.urlopen(f"http://{ip}:8000/version", timeout=5) as resp:
            r.facts.append(("vLLM", json.load(resp).get("version", "?")))
    except Exception:
        r.facts.append(("vLLM", "unreachable"))
    r.facts.append(("LLM image", sh("docker", "inspect", "-f", "{{.Config.Image}}", "spark-llm").strip() or "?"))
    drv = sh("nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader").strip()
    r.facts.append(("Driver / kernel", f"{drv or '?'} / {os.uname().release}"))


# ── output ───────────────────────────────────────────────────────────────────
def render(r: Report) -> str:
    now = dt.datetime.now().astimezone()
    n = {k: r.count(k) for k in (FAIL, WARN, INFO, OK)}
    head = FAIL if n[FAIL] else WARN if n[WARN] else OK
    out = [
        f"# Spark analysis — last {r.window} — {now:%Y-%m-%d %H:%M %Z}",
        "",
        f"**{ICON[head]} {n[FAIL]} failing, {n[WARN]} warnings, {n[INFO]} notes, {n[OK]} passing.**",
        "",
    ]
    if r.recs:
        out += ["## Recommendations", ""] + [f"{i}. {x}" for i, x in enumerate(r.recs, 1)] + [""]
    section = None
    for sec, check, st, detail in r.rows:
        if sec != section:
            out += ["", f"## {sec}", "", "| | Check | Result |", "|---|---|---|"]
            section = sec
        out.append(f"| {ICON[st]} | {check} | {detail.replace('|', '/')} |")
    out += ["", "## Facts", ""] + [f"- **{k}:** {v}" for k, v in r.facts] + [""]
    return "\n".join(out)


def label(v: str) -> str:
    """Prometheus text-format label value: escaped, one line, bounded."""
    v = " ".join(v.split())[:300]
    return v.replace("\\", "\\\\").replace('"', '\\"')


def metrics(r: Report) -> str:
    w = r.window
    m = [
        "# HELP spark_analysis_last_run_timestamp_seconds When analysis/spark-analyze.py last finished.",
        "# TYPE spark_analysis_last_run_timestamp_seconds gauge",
        f'spark_analysis_last_run_timestamp_seconds{{window="{w}"}} {dt.datetime.now().timestamp():.0f}',
        "# HELP spark_analysis_findings Checks per status in the latest analysis.",
        "# TYPE spark_analysis_findings gauge",
    ] + [f'spark_analysis_findings{{window="{w}",severity="{k}"}} {r.count(k)}' for k in (FAIL, WARN, INFO, OK)]
    m += [
        "# HELP spark_analysis_check Latest result per check: 0 ok, 1 info, 2 warn, 3 fail (labels carry the text).",
        "# TYPE spark_analysis_check gauge",
    ]
    for i, (sec, check, st, detail) in enumerate(r.rows):
        m.append(
            f'spark_analysis_check{{window="{w}",order="{i:02d}",section="{label(sec)}",check="{label(check)}",'
            f'result="{label(detail)}",advice="{label(r.advice.get(check, ""))}"}} {LEVEL[st]}'
        )
    return "\n".join(m) + "\n"


def write_atomic(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--window", default="24h", choices=["24h", "7d"])
    ap.add_argument("--stdout", action="store_true", help="print the report instead of writing files")
    a = ap.parse_args()

    r = Report(a.window)
    stability(r, a.window)
    performance(r, a.window)
    benchmark(r)
    housekeeping(r)
    text = render(r)
    if a.stdout:
        print(text)
        return

    (OUT / "metrics").mkdir(parents=True, exist_ok=True)
    name = OUT / f"{dt.date.today()}-{a.window}.md"
    name.write_text(text)
    link = OUT / f"latest-{a.window}.md"
    link.unlink(missing_ok=True)
    link.symlink_to(name.name)
    write_atomic(OUT / "metrics" / f"spark_analysis_{a.window}.prom", metrics(r))

    cutoff = dt.date.today() - dt.timedelta(days=KEEP_DAYS)
    for f in OUT.glob("20*-*.md"):
        try:
            if dt.date.fromisoformat(f.name[:10]) < cutoff:
                f.unlink()
        except ValueError:
            pass
    log = OUT / "cron.log"
    if log.exists() and log.stat().st_size > 5_000_000:
        write_atomic(log, log.read_text()[-1_000_000:])
    print(f"{name}: {r.count(FAIL)} fail, {r.count(WARN)} warn")


if __name__ == "__main__":
    main()
