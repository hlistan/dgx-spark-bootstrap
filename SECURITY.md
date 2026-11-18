# Security policy

## Supported versions

Only the latest commit on `main` is supported. There are no releases or backports.

## Reporting a vulnerability

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/hlistan/dgx-spark-bootstrap/security/advisories/new)
(repository **Security** tab → **Advisories**). Do not open a public issue for a security problem.

Include the affected file, what an attacker needs (network position, credentials) and what they gain. This is a
personal project maintained in spare time: expect a first reply within about a week. Please allow 90 days before
publishing details, or less once a fix is on `main`.

## Scope

In scope is what this repository ships: the Compose files, the Makefile and scripts, the firewall unit, the
Prometheus and Grafana configuration, and the documentation where it would lead an operator into an unsafe setup.

Report problems in the software this repo deploys to its own project: [vLLM](https://github.com/vllm-project/vllm/security),
[Grafana](https://github.com/grafana/grafana/security/policy), [Prometheus](https://prometheus.io/docs/operating/security/),
the exporters, and NVIDIA's container images and DGX OS ([NVIDIA PSIRT](https://www.nvidia.com/en-us/security/)).
If a pinned image here is affected by a published advisory, a normal issue or pull request is welcome.

## Security model and known limits

The setup is meant for one DGX Spark on a LAN whose hosts you trust. Read this before exposing it more widely.

- **No TLS.** The API key and Grafana logins cross the LAN in plain HTTP. Do not publish ports 8000 or 3000 to the
  internet; use a VPN, or a reverse proxy that terminates TLS, for anything beyond the LAN.
- **vLLM's API key covers `/v1` only.** `/metrics`, `/health`, `/version` and `/tokenize` answer without it. The
  optional firewall unit in [host/](host/README.md) limits the port to listed IPs.
- **One shared API key** for all clients, stored in `inference/.env` on the Spark and passed to vLLM on its command
  line. Any user on the Spark who can run `docker` or read the process list can see it. Treat every account on
  the Spark as trusted, and rotate the key by editing `.env` and running `docker compose up -d llm`.
- **Privileged monitoring containers.** cAdvisor runs privileged, node_exporter runs with AppArmor unconfined and
  the host filesystem mounted read-only, and membership of the `docker` group equals root on the Spark. All
  exporters and Prometheus listen on loopback only.
- **Pinned images are not scanned for CVEs.** The vLLM image is NVIDIA's pre-release build, pinned on purpose
  (decision S4 in [docs/decisions.md](docs/decisions.md)). Video input is disabled because of two advisories that
  affect it.
- **Secrets never belong in git.** `inventory.env` and the `.env` files are gitignored, and `make lint` runs
  gitleaks and a site-value check. If you fork, keep both gates on.
