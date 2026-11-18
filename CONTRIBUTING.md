# Contributing

Issues and pull requests are welcome. The repo describes one way to run one DGX Spark, so changes that keep it small
and reproducible are the easiest to merge. For anything larger than a fix, open an issue first.

## Set up

You need `make`, `python3`, [uv](https://docs.astral.sh/uv/) and Docker. No DGX Spark is needed to run the gates.

```sh
git clone https://github.com/hlistan/dgx-spark-bootstrap.git && cd dgx-spark-bootstrap
make hooks        # run the gates before every commit in this clone
make lint         # shellcheck, ruff, yamllint, compose config, promtool rule tests, policy check, gitleaks
```

Without an `inventory.env`, `make lint` uses `inventory.env.example`. CI runs the same command on every pull request.

## Rules

- **No site values or secrets.** Tracked files use the documentation ranges (`192.0.2.0/24`) and placeholders. Real
  addresses, host names and keys live in `inventory.env` and the `.env` files, which are gitignored. `make lint`
  fails on a private IP address, on a value from your `inventory.env`, and on anything gitleaks recognises. Check
  logs and screenshots you attach to issues for the same things.
- **Pin versions.** Container images, lint tools and GitHub Actions are pinned to an exact version (Actions to a
  commit SHA). Prefer releases that are at least two weeks old.
- **Keep exposure as it is** unless the change is about exposure: services bind to the LAN IP or loopback, vLLM keeps
  `--api-key` and `--allowed-origins '[]'`. `scripts/lint-policy.py` enforces this.
- **Record decisions.** A change to how the Spark is set up gets an entry or an edit in
  [docs/decisions.md](docs/decisions.md). Say what you measured and on which DGX OS and image versions.
- **Test alert rules.** A new or changed rule in `monitoring/prometheus/rules/` gets a case in
  `monitoring/prometheus/tests/`.
- **Vendored dashboards** (`dgx-spark-vllm.json`, `node-exporter-full.json`) are generated. Change the pin or the
  patch in `monitoring/grafana/vendor-dashboards.py` and run `make dashboards`; do not edit the JSON.

## Pull requests

Keep one topic per pull request and say how you tested it: `make lint` output, and `make check` on a real Spark if
the change affects what runs there. By contributing you agree that your work is released under the
[MIT License](LICENSE).

## Security problems

Do not open a public issue. See [SECURITY.md](SECURITY.md).
