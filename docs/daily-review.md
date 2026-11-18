# Daily Spark review — instructions for the 24 h agent loop

Run from the admin workstation, in this repo, once every 24 h (`/loop 24h …`, started 2026-11-17). The box already
measures itself (S8: 03:30 benchmark, 06:15 analysis). This review adds what the box can't do alone: it reads those
results as a trend, checks upstream for fixes and new releases, and turns both into a short, ranked list of actions.

## Rules

- **Read-only on the box.** Use `ssh spark` with read commands only. Do not run `make deploy`, `*-up`, `docker rm`,
  `docker image rm`, `drop_caches`, `nvidia-smi -lgc`, `apt`, `fwupdmgr upgrade` or reboot. Write the proposed command
  into the report instead. The operator applies changes through the normal flow
  (`make lint → diff → deploy → <stack>-up → check`, README).
- **Repo edits:** only `reports/daily/` (new files). Propose any config change as a diff in the report. Don't commit.
- **Evidence over advice.** Every recommendation names its metric, value and source. Mark each source as
  *official* (docs.nvidia.com, build.nvidia.com, NVIDIA staff posts, vLLM docs and releases) or *community*.
  Don't recommend a community workaround unless this box shows its symptom.
- **Respect decisions** in `docs/decisions.md` (S1–S8). To challenge one, cite the new evidence and name the S number.
- If `ssh spark` fails, say so at the top of the report, still do the upstream checks, and stop there.

## 1. Collect (box)

```sh
ssh spark 'cat ~/spark-reports/latest-24h.md'                 # 25 checks + recommendations (S8)
ssh spark 'ls -t ~/spark-reports/*-7d.md | head -1 | xargs cat' # weekly, if newer than the last review
ssh spark 'tail -45 ~/spark-reports/bench.csv'                 # ~14 days of benchmark rows
ssh spark 'tail -30 ~/spark-reports/cron.log'                  # did the jobs run cleanly?
ssh spark 'uptime; free -g; swapon --show; cat /proc/sys/vm/swappiness; df -h / ~; docker ps --format "{{.Names}} {{.Status}}"'
ssh spark 'nvidia-smi; nvidia-smi -q -d PERFORMANCE,CLOCK | head -60'   # throttle reasons, clocks (memory fields are N/A on GB10: use /proc/meminfo)
ssh spark 'cat /etc/dgx-release | grep -E "VERSION|DATE"; uname -r; nvidia-smi --query-gpu=driver_version --format=csv,noheader'
ssh spark 'apt list --upgradable 2>/dev/null | wc -l; fwupdmgr get-updates --no-unreported-check 2>/dev/null | head -30'   # read-only
ssh spark 'journalctl -k -b --since "-24h" -p warning --no-pager | grep -viE "audit" | tail -40'   # Xid, NVRM, OOM, thermal, nvme
ssh spark 'last -x reboot shutdown | head -5'                  # unexpected reboots = possible log-less hard power-off
```

Also run `make check-monitoring` (needs no key) for scrape targets and firing alerts.

## 2. Check the box against known DGX Spark issues

| Area | Look for | Known cause and fix (source) |
|---|---|---|
| Unexpected reboots | `last -x` shows a reboot with no clean shutdown; no Xid/OOM in the logs | GB10 log-less hard power-off under sustained load. Community fix: clock cap `nvidia-smi -lgc 300,2200` via systemd unit (~5 % decode cost), firmware update, airflow; RMA if fieldiag thermal fails (community: tonyd2wild/dgx-spark-hard-poweroff-fix, NVIDIA forum threads 373251, 377365) |
| Thermal | GPU > 85 °C under load, NVMe > 70 °C, `SW Thermal Slowdown`/`HW Slowdown` active, fan not ramping | Clear intake vents; check that ambient temperature hasn't changed; compare with the benchmark trend (community) |
| Power / clock stuck low | Power stuck at ~5–30 W or clock ~650 MHz under load, benchmark −15 % or worse | PD/EC firmware negotiation; apply firmware updates and do a 30 s AC cold drain (community, forum 355255, 370304) |
| Unified memory | `MemAvailable` min < 15 GiB, swap-out growing, `SparkSwapping` alert, OOM kills | Find the consumer (cAdvisor per container). Fixes in order: cap or fix the offending container; `vm.swappiness=1` (community); flush page cache only before an LLM restart (official: build.nvidia.com vLLM troubleshooting); never raise GPU util above 0.60 (S4) |
| Container restarts / OOM | Restarts or `OOMKilled` for any `spark-*` container | Too low `mem_limit` or a leak; propose a new limit in compose with the memory-budget impact (README: monitoring ≤ 2.1 GB) |
| Disk | Root < 30 % free, or the 30-day projection runs out | Unused images, old HF revisions, Prometheus retention; give exact `docker image rm` / `hf cache` commands |
| vLLM health | Errors in logs, preemptions > 0, KV peak > 85 %, queue p95 > 2 s, rejected > 0 | Compare with S4 limits (8 seqs, 256k ctx); recommend changes only with measurements |
| Speculative decoding | DFlash2 acceptance < 40 % or tokens/step dropping over the week | Recheck `num_speculative_tokens` (S4 measured k=7 best); look for a newer drafter checkpoint |
| Prefix cache | Hit rate < 70 % on agent traffic | Client prompt layout changed, or the cache is too small; correctness: vllm#60174 (S4) |
| Benchmark drift | Any series > 10 % below its 14-run median, or a downward trend over 7 days | Correlate with temperature, clocks, memory, image or driver changes |
| GPU fault | `NVRM` Xid (esp. 119) or `bad register read 0xbadf5600` in the kernel log, "GPU requires reset" in `nvidia-smi -q`, alert `SparkGpuTelemetryLost` | Reboot needed; runbook in `host/README.md` (official update guide + community reports) |
| After a reboot | `spark-llm` not running, `cannot assign requested address` or name-resolution errors in its log; `llm-boot-start.sh` line in `cron.log` | Boot races with DHCP; `host/README.md` "Boot race" |
| Scheduled jobs | Missing report/bench row for today, errors in `cron.log` | `make schedule`; check the textfile metrics folder permissions |

## 3. Upstream checks (web)

Search, and fetch the primary page, for anything newer than the last review's date. Keep it to about 10 lookups.

1. **DGX OS / firmware / driver:** NVIDIA forum "DGX Spark Software Updates" announcements, the
   [OS and Component Update Guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html), DGX OS 7
   release notes. Compare with the box's `/etc/dgx-release`, driver and kernel. Updates go through the DGX Dashboard or
   `apt dist-upgrade` + `fwupdmgr refresh/upgrade` + reboot, in a maintenance window, after
   `make inference-up` preparation (official).
2. **NGC vLLM container:** [release notes](https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/) for a
   tag newer than the one in `inference/compose.yaml`. Check its DGX Spark known issues and whether it keeps
   vllm#55601 (S4). Note ≥ 2-week-old rule for pins (S2).
3. **Security:** vLLM GitHub security advisories newer than the running vLLM version (the report's Facts section),
   especially anything reachable through `/v1` or the multimodal path (S4, S6).
4. **Model / recipe:** NVIDIA local-ai-inference-recipes and the NVIDIA DGX Spark forum for a newer Qwen3.8-27B
   recipe, drafter, or a stock single-Spark recipe for the S5 "revisit" candidates.
5. **New stability reports:** forum threads from the last few days about GB10 hangs, power-offs, memory or thermal
   problems that match this box's symptoms.

## 4. Write the report

Write `reports/daily/<YYYY-MM-DD>.md` (create the folder if needed):

```markdown
# Spark daily review — <date>
**Verdict:** 🟢 healthy | 🟡 attention | 🔴 action needed — one sentence why.

## Actions (ranked, max 5)
1. **<title>** — evidence (metric, value, trend) → proposed change (exact command or compose diff) → expected
   effect and risk → source (official/community link) → which S decision it touches.

## Trends (vs previous review)
Decode / ctx32k / conc4 benchmark vs 14-run median; min MemAvailable; swap-out; restarts; disk free; TTFT p95.

## Upstream
New releases, advisories, recipes or issue reports since the last review, each with a link and "applies / doesn't
apply because …". "Nothing new" is a valid answer.

## Carried over
Open actions from the previous review that are still valid (check `reports/daily/` for the last file); drop the
ones that were fixed and say so.
```

Then reply in the session with the verdict and the top action in at most 5 lines. Don't repeat an unchanged action
at length: write "still open since <date>".
