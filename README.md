# dgx-spark-bootstrap

Configuration for running one NVIDIA DGX Spark as a coding-LLM server on a local network. You manage the Spark
from a workstation with `make` and SSH; nothing is edited on the Spark by hand.

It sets up:

- **Inference.** vLLM serving `nvidia/Qwen3.8-27B-NVFP4` with the DFlash2 drafter as an OpenAI-compatible API:
  256k context, tool calls, vision, 8 parallel sequences. It binds to the Spark's LAN IP and requires an API key.
- **Monitoring.** Prometheus, Grafana and exporters for the GB10 GPU, the OS, containers and vLLM, with 24 alert
  rules and three dashboards.
- **Self-checks.** A nightly benchmark and a daily and weekly report of about 25 stability and performance checks,
  shown on the Grafana home dashboard.
- **Lint gates.** Shell, Python, YAML, Compose, Prometheus rule tests, secret scanning and a policy check, run
  before every deploy.

On our unit (DGX OS 7.6, 128 GB) the model uses about 73 GB of the unified memory, monitoring at most 2.4 GB, and the
benchmark medians are 42 tok/s for one stream and 125 tok/s in total for four. The reasons behind each choice are in
[docs/decisions.md](docs/decisions.md).

## Requirements

| Where | What |
|---|---|
| DGX Spark | DGX OS 7 with Docker and Compose v2 (both ship with it), a user in the `docker` group, 50 GB of free disk, a DHCP reservation for its LAN IP |
| Workstation (macOS or Linux) | `make`, `ssh`, `rsync`, `curl`, `python3`; for `make lint`: [uv](https://docs.astral.sh/uv/) and Docker |
| Network | A LAN you trust. Traffic is plain HTTP. See [Security](#security) |

## Quick start

About 30 minutes, most of it the 23 GB model download and the first compile.

1. On the workstation, clone the repo and give the Spark an SSH alias:

   ```sh
   git clone https://github.com/hlistan/dgx-spark-bootstrap.git && cd dgx-spark-bootstrap
   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_spark
   cat >> ~/.ssh/config <<'EOF'
   Host spark
     HostName 192.0.2.10
     User your-user
     IdentityFile ~/.ssh/id_ed25519_spark
   EOF
   ssh-copy-id -i ~/.ssh/id_ed25519_spark.pub spark
   ssh spark 'nvidia-smi -L && docker ps'    # no password prompt, no sudo
   ```

   Replace `192.0.2.10` and `your-user` with the Spark's LAN IP and your user on it.

2. Describe your installation:

   ```sh
   cp inventory.env.example inventory.env
   $EDITOR inventory.env                     # SPARK_HOST, SPARK_HOSTNAME and the three URLs
   ```

3. Copy the repo to `~/spark` on the Spark. This runs the lint gates first.

   ```sh
   make deploy
   ```

4. On the Spark, create the inference secrets and start the model:

   ```sh
   ssh spark
   cd ~/spark/inference
   cp .env.example .env && chmod 600 .env
   $EDITOR .env                              # SPARK_BIND_IP = the LAN IP, SPARK_API_KEY = `openssl rand -hex 32`
   docker compose pull llm
   sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
   docker compose up -d
   docker compose logs -f llm                # wait for "Application startup complete", 10 to 15 minutes
   ```

   The page-cache drop matters: on unified memory vLLM counts cached files as used memory and refuses to start.

5. Back on the workstation, start monitoring and the scheduled jobs, then test as a client:

   ```sh
   make monitoring-env monitoring-up schedule
   SPARK_API_KEY=<the key from step 4> make check
   make grafana                              # password: ssh spark 'grep GRAFANA ~/spark/monitoring/.env'
   ```

Point any OpenAI-compatible coding client at `SPARK_LLM_BASE_URL` with model `qwen3.8-27b`, a context window of
262144 and the API key. The full runbook, with a load test and troubleshooting, is
[inference/README.md](inference/README.md).

## Daily use

```sh
make help                 # all targets
make status               # GPU temperature, power and clock, memory, disk, containers
make report               # latest daily analysis (W=7d for the weekly one)
make check                # client probe of the LLM, scrape targets, firing alerts
make diff && make deploy  # preview, then copy the repo to the Spark
make inference-up         # apply inference changes
make monitoring-up        # apply monitoring changes (monitoring-reload for rules and scrape config only)
```

To change anything: edit here, then `make lint`, `make diff`, `make deploy`, `make <stack>-up`, `make check`.
`make deploy` uses `rsync --delete`, so files edited on the Spark are overwritten. Only `.env` files and logs are
left alone.

## Services

| Port | Service | Access |
|---|---|---|
| 8000 on the LAN IP | vLLM, model `qwen3.8-27b` | `Authorization: Bearer $SPARK_API_KEY` on `/v1`. `/metrics` and `/health` have no auth |
| 3000 on the LAN IP | Grafana | login `admin`, password in `monitoring/.env` on the Spark |
| 127.0.0.1 only | Prometheus 9090, node_exporter 9100, GPU exporter 9835, cAdvisor 8088, blackbox 9115 | `make prom`, or `ssh -L` |

## Site configuration and secrets

The repo contains no addresses, host names or keys of a real installation. Keep it that way if you fork or
contribute: `make lint` fails when a tracked file contains a private IP address or a value from your
`inventory.env`.

| What | Where it lives | In git |
|---|---|---|
| Addresses, host name, SSH alias, client-facing URLs | `inventory.env` on the workstation, copied to the Spark by `make deploy` | no; `inventory.env.example` is the template |
| SSH user, IP and key for the alias | `~/.ssh/config` on the workstation | no |
| `SPARK_API_KEY`, `HF_TOKEN`, image tag, bind IP | `~/spark/inference/.env` on the Spark | no; `inference/.env.example` |
| Grafana admin password, retention | `~/spark/monitoring/.env` on the Spark, created by `make monitoring-env` | no; `monitoring/.env.example` |
| Firewall allow-list, if the firewall is installed | `/etc/default/spark-firewall` on the Spark | no |
| Review reports and research notes about your unit | `reports/`, `research_notes/` | no |

Files that need a site value are templates. `monitoring/prometheus/prometheus.yml.tmpl` holds `@SPARK_HOST@` and
`@SPARK_HOSTNAME@`; `make render` writes the real `prometheus.yml` next to it, and deploy and lint run it for you.
To add another site value, put the key in `inventory.env.example` with a placeholder from the documentation
ranges (`192.0.2.0/24`), and read it from `inventory.env` in a script or template.

Back up `inventory.env` and the two `.env` files somewhere private, such as a password manager. They are the only
state that is not in this repo or reproducible from it.

## Security

The design assumes a trusted LAN. vLLM binds only to the Spark's LAN IP and requires the API key on `/v1`.
Exporters and Prometheus listen on loopback, and Grafana requires a login. There is no TLS. If untrusted hosts
share the network, install the firewall unit in [host/](host/README.md), which limits ports 8000 and 3000 to listed
IPs, or put the Spark behind a VPN. Video input is disabled because of two vLLM advisories that affect the pinned
image; the details are in decision S4.

## Repository layout

| Path | Contents |
|---|---|
| `inference/` | Compose file for vLLM and the step-by-step runbook |
| `monitoring/` | Compose file, Prometheus config template, alert rules with unit tests, Grafana dashboards |
| `analysis/` | `spark-bench.py`, `spark-analyze.py` and the crontab that `make schedule` installs |
| `host/` | Steps that need sudo on the Spark: firewall unit, sysctl, the boot-race fix, the GPU-fault runbook |
| `scripts/` | Client probes, the lint runner, the policy check, the template renderer |
| `docs/` | `decisions.md` (numbered S1 to S8) and `daily-review.md` (a checklist for a daily operator or agent review) |
| `inventory.env.example` | Template for the site inventory |

## Updating

Images are pinned to exact tags, and `make lint` rejects `latest`. To move to a newer vLLM image, set `LLM_IMAGE` in
`inference/.env` on the Spark and follow the "Upgrade vLLM" row in [inference/README.md](inference/README.md). The
image must include vllm#55601, or prefix caching with the drafter returns corrupted text. DGX OS, firmware and
driver updates go through NVIDIA's
[OS and Component Update Guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html).

## Contributing

Run `make hooks` once per clone so the lint gates run before every commit. Record a decision that changes how the
Spark is set up in `docs/decisions.md`.

## License

[MIT](LICENSE)
