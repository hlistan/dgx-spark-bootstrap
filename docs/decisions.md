# Spark decisions

S-numbered, box-wide. Newest facts win; superseded text is edited, not appended.

## S1 — One self-contained repo is the source of truth for the box (2026-11-02)

Everything that runs on the Spark is defined here; the box is changed only via `make deploy` (rsync `--delete` into
`~/spark`) and `make <stack>-up`. The repo doesn't know its clients: they read `inventory.env` and hold the API key.
Values that identify one installation (addresses, host name, SSH alias) live in `inventory.env`, which is
gitignored; the repo tracks `inventory.env.example` and templates, and `make lint` rejects site values in tracked
files (2026-11-18). Runtime names are neutral (`spark-inference` project, `spark-*` containers, model `qwen3.8-27b`), so the box can
serve any client without renaming.

## S2 — Monitoring: Prometheus + Grafana, nvidia_gpu_exporter instead of DCGM (2026-11-02)

Adopted the pattern the DGX Spark community converged on (NVIDIA developer forum "Hardware + vLLM metrics
dashboard", RodriMora/dgx-spark-grafana-dashboard, galaxy-io/sparktop, the vLLM DGX Spark blog): Prometheus scraping
node_exporter, a GPU exporter and vLLM's native `/metrics`, Grafana on top. Added cAdvisor (per-container memory
matters on unified memory) and blackbox (liveness from outside the process).

GPU exporter: `utkuozdemir/nvidia_gpu_exporter`, not `dcgm-exporter`. On GB10 DCGM is reported to fail enumeration
on some DGX OS builds, the NGC tags we tried don't resolve, and on unified memory neither DCGM nor NVML has
framebuffer memory to report. nvidia_gpu_exporter was verified on this box: temperature, T.Limit, power, clocks and
all throttle-reason counters. Unified memory is watched via node_exporter `MemAvailable`.

Rejected: sparktop (TUI only, no history or alerts — fine as an ad-hoc tool, it can scrape these same exporters);
NVIDIA DGX Dashboard alone (no history, no alerts; it stays available on 127.0.0.1:11000).

Versions pinned to releases ≥ 2 weeks old; the dashboards are vendored at a pinned SHA/revision by a script, so
updates are reviewable diffs.

## S3 — Firewall unit covers all LAN-facing service ports, for a list of hosts (2026-11-02)

`spark-firewall`: DOCKER-USER for Docker-published ports (8000, which bypasses ufw/INPUT), INPUT for host-network
Grafana (3000), any number of allowed IPs. Kept ready, not installed (S6). Independently of it, exporters and
Prometheus bind 127.0.0.1, and Grafana requires a login.

## S4 — LLM: `nvidia/Qwen3.8-27B-NVFP4` + DFlash2 on NGC vLLM 26.10 (2026-10-28)

NVIDIA's validated Spark recipe (local-ai-inference-recipes, 2026-10-19): NVFP4 weights, fp8 KV cache, DFlash2
drafter with 7 speculative tokens (k=4 and k=10 measured ~17 % slower for chat here), 8 sequences, GPU util 0.60
(0.70+ hung nodes during startup autotune). Context 256k instead of the recipe's 128k (2026-11-02, S5): native for
Qwen3.8, no YaRN, and the KV pool at 0.60 holds it (the forum measured ~778k fp8 KV tokens at 0.45). Measured 37 tok/s decode on agent prompts, 23–24 tok/s on
prose; tool calls via `qwen3_xml`, reasoning parser `qwen3`. Prefix caching with a speculative drafter is safe only on
images carrying vllm#55601 (NGC 26.10.pre1; upstream from v0.32.0) — without it cache hits return corrupted text
(vllm#60174), so `LLM_IMAGE` stays on the recipe's tag. Video input is disabled (`--limit-mm-per-prompt video 0`):
this build predates the 0.31.0 fixes for GHSA-vxqj-p4gw-9h4c / GHSA-894j-wg8m-63h8.

## S5 — Coding only: one model, no voice, no fallback (2026-11-02)

The box serves coding clients only. Removed: the voice stack (Qwen3-ASR-1.7B, Chatterbox TTS behind the Caddy
`tts-gate`, the faster-whisper fallback; ports 8001/8002/8011) and the Qwen3.6-35B-A3B LLM fallback. The memory
they held (~13–16 GB) went into context (S4).

Model: kept Qwen3.8-27B NVFP4 + DFlash2 (S4), the community's quality pick for one Spark (NVIDIA forum and write-ups,
Aug–Oct 2026). Rejected: Qwen3-Coder-Next 80B-A3B NVFP4: about 2–3x faster (60–85 tok/s) but clearly weaker on
agentic coding (vendor SWE-bench Pro ≈ 44 vs ≈ 62). Qwen3.8-Flash-Next 125B-A6B: its NVFP4 checkpoint is
~122 GiB and runs on one Spark only with community vLLM patches (long boots, prefix-cache errors without them).
Revisit if Flash-Next gets a stock single-Spark recipe.

## S6 — Exposure: LAN bind + API key; firewall optional (2026-10-29)

The LAN is trusted (decision 2026-10-29): the LLM binds only to the Spark's LAN IP and requires `--api-key`.
vLLM's key covers only `/v1`, so `--allowed-origins '[]'` keeps browsers on allowed hosts from reading
unauthenticated endpoints (GHSA-vvpg-rrww-wpmh). Traffic is plain HTTP; acceptable on a home/office LAN. Install `spark-firewall` (S3) if untrusted hosts join; move to
Tailscale (WireGuard, ACL tags) if clients leave the LAN or the network is shared. Both IPs are DHCP reservations.

## S7 — Static quality gates before every commit and deploy (2026-11-02)

`make lint` (`scripts/lint.sh`) runs shellcheck, ruff (with the bandit `S` security rules) and `ruff format`,
yamllint, `docker compose config` for both stacks (with the `.env.example` values), `promtool check config` and
`promtool test rules` (`monitoring/prometheus/tests/`) using the box's own Prometheus image, gitleaks for secrets, and
`scripts/lint-policy.py`, which knows this repo's rules: ports bind only to the LAN IP or loopback, vLLM keeps
`--api-key` and `--allowed-origins '[]'`, images are pinned, GPU util ≤ 0.60, monitoring has memory caps and log
rotation, and `inventory.env` matches what is actually served (model, context, URL, Prometheus targets).
Tools are pinned and fetched on demand (uvx, Docker), so the result doesn't depend on the workstation.
`make deploy` depends on `make lint`; `make hooks` adds it as a pre-commit hook. Since the repo became public
(2026-11-18) GitHub Actions runs the same `make lint` on every push and pull request, with `inventory.env.example`
standing in for the site inventory; gitleaks also scans the whole git history. Actions are pinned to commit SHAs
and run with a read-only token; Dependabot proposes updates for Actions and the monitoring images after a 14-day
cooldown (S2). Not included: image CVE scanning (the LLM image is NVIDIA's dev build, pinned on purpose by S4).

## S8 — Periodic analysis and benchmark on the box (2026-11-02)

Prometheus alerts catch acute failures. Slow drift needs a look over hours and days, so the Spark analyses itself on
a schedule from the SSH user's crontab (`make schedule`; no sudo, runs while the workstation sleeps):
- 03:30 daily, `analysis/spark-bench.py`: fixed workloads (single-stream decode, ~32k-token context, 4 parallel
  streams), with greedy decoding, `ignore_eos`, thinking off and a nonce against the prefix cache. It waits for an
  idle server so it never competes with users. History goes to `~/spark-reports/bench.csv`.
- 06:15 daily / 06:45 Mondays, `analysis/spark-analyze.py --window 24h|7d`: about 25 stability and performance checks
  from Prometheus, logs and docker. Each gets a verdict and a concrete recommendation, written to
  `~/spark-reports/<date>-<window>.md` (`make report`). Reports are kept for 90 days.
Both publish results through node_exporter's textfile collector (the metrics folder is bind-mounted, because the
exporter runs as `nobody`). Alerts then fire on a benchmark more than 15 % below its 14-day median, on failing
checks, on a failed benchmark, and on a schedule that stopped (> 26 h). Results are not pushed anywhere
(2026-11-03, user's choice): the Grafana home dashboard **DGX Spark — health** shows alerts, every check of the
latest report with its recommendation, and the benchmark trend. Baseline 2026-11-02: decode 43.5 tok/s,
32k context 2.3k tok/s prefill / 44 tok/s decode, 4 streams 135 tok/s total.
