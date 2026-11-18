# Monitoring — hardware, OS, inference

The community-standard stack for a DGX Spark serving vLLM (decision S2): Prometheus scrapes exporters + vLLM's own
`/metrics`; Grafana shows the community DGX Spark dashboard and Node Exporter Full. Everything runs in Docker on the
host network; exporters bind to `127.0.0.1`, Grafana to the LAN IP behind a login.

| Layer | Source | Port | Key signals |
|---|---|---|---|
| Hardware (GPU) | `nvidia_gpu_exporter` 1.15.1 (nvidia-smi) | 9835 | temp, **T.Limit** headroom, power, SM clock vs max, throttle reason counters (thermal / power brake / power cap) |
| Hardware (host) | node_exporter hwmon / EDAC | 9100 | NVMe temps, thermal zones, memory ECC (EDAC) |
| OS | node_exporter 1.12.1 | 9100 | **MemAvailable** (unified memory — the number that decides vLLM OOM), swap, disk, net, load, systemd unit states, NTP |
| Containers | cAdvisor 0.60.6 | 8088 | per-container CPU / memory working set / restarts |
| Inference | vLLM `/metrics` (llm :8000) | — | tok/s, TTFT / ITL / E2E histograms, KV-cache %, running / waiting, preemptions, prefix-cache hits, spec-decode acceptance |
| Availability | blackbox_exporter 0.29.0 | 9115 | `/health` of llm, Grafana |

Why not DCGM: `dcgm-exporter` is reported to fail to enumerate GB10 on several DGX OS builds and, like NVML, has no
framebuffer memory to report on unified memory. `nvidia_gpu_exporter` reads everything `nvidia-smi` shows (verified
on this box 2026-11-02) and is what the community dashboard is built on. GPU memory is tracked as host memory.

## Dashboards (`grafana/dashboards`, provisioned read-only)

- **DGX Spark — health** (`spark-health`, home; ours) — what needs attention: alerts firing now and their
  history, every check of the latest daily/weekly analysis with its result and recommendation (**Analysis window**
  switches 24h/7d), and the trends of the nightly benchmark (with the regression-alert line) and of the analysis.
  Fed by `spark_analysis_check` (one series per check) and the other `spark_analysis_*` / `spark_bench_*` metrics.
- **DGX Spark — inference + GB10** (`dgx-spark-vllm`) — [RodriMora/dgx-spark-grafana-dashboard](https://github.com/RodriMora/dgx-spark-grafana-dashboard)
  (MIT) @ `0ea0656`, patched by `grafana/vendor-dashboards.py`: a **Model** variable (so a second vLLM model can be told apart) injected into
  every `vllm:*` query. The "cloud-equivalent cost" cards are upstream's pricing assumptions — informational only.
- **Node Exporter Full** — grafana.com [1860](https://grafana.com/grafana/dashboards/1860) rev 45 (Apache-2.0).

License texts and the changes made to both: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

To update: bump the pinned SHA/revision in `vendor-dashboards.py`, `make dashboards`, review the diff, deploy.
Don't edit dashboards in the UI (`allowUiUpdates: false`); export the JSON into a new file here if you build one.

## Alerts (`prometheus/rules/spark.yml`)

24 rules in 5 groups: hardware (T.Limit < 10 °C, thermal throttling, power brake, NVMe > 70 °C, EDAC errors),
OS (MemAvailable < 8 GiB, swapping, disk < 10 %, failed systemd unit, NTP), inference (endpoint
down, container restart, KV cache > 90 %, queueing, preemptions, p95 TTFT > 20 s, drafter acceptance < 25 %),
analysis (benchmark > 15 % below its 14-day median, benchmark failed, failing analysis checks, schedule stale,
textfile unreadable; fed by `../analysis/` via node_exporter's textfile collector, S8), and scrape target down.
Unit tests: `prometheus/tests/spark.test.yml`, run by `make lint` (S7). They are shown on the Grafana home dashboard (**DGX Spark — health**) and in Prometheus (`make prom` →
http://localhost:9090/alerts). Nothing is pushed anywhere: by choice, the dashboard is the place to look.

## Operations

| Task | Command (workstation, repo root) |
|---|---|
| First start | `make deploy monitoring-env monitoring-up check-monitoring` |
| Rules / scrape config changed | `make deploy monitoring-reload` |
| Compose / dashboards changed | `make deploy monitoring-up` |
| Targets + firing alerts | `make check-monitoring` |
| Grafana password | `ssh spark 'grep GRAFANA ~/spark/monitoring/.env'` |
| Prometheus UI | `make prom` → http://localhost:9090 |
| NVIDIA's own DGX Dashboard | `ssh -L 11000:127.0.0.1:11000 spark` → http://localhost:11000 |
| Stop (data kept) | `make monitoring-down` |

Retention: 30 d or 20 GB, whichever first (`.env`). Memory caps: Prometheus 1 GB, Grafana 768 MB, rest ≤ 256 MB.

## Gotchas found on this box

- node_exporter's **cpufreq** collector hangs > 10 s on GB10 (`cppc_cpufreq`), failing every 2nd–3rd scrape — disabled.
- DGX OS's docker AppArmor profile blocks D-Bus → node_exporter runs `apparmor=unconfined` for the systemd collector.
- vLLM's `--api-key` doesn't cover `/metrics` and `/health`; Prometheus scrapes them on the LAN IP from the host
  itself, which the DOCKER-USER firewall rule doesn't filter.
- `nvidia-smi` memory fields read N/A on GB10 by design; watch `node_memory_MemAvailable_bytes` instead.

## Next steps (not done)

- Alert delivery (deliberately not done, 2026-11-03): if wanted later, Alertmanager → Telegram / e-mail.
- Off-box copy: the box monitors itself; if it dies, so do the alerts. A tiny blackbox probe from the workstation would cover that.
