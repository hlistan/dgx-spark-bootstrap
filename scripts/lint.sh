#!/usr/bin/env bash
# Static quality gates for this repo (docs/decisions.md S7). `make lint`; also the git pre-commit hook (`make hooks`).
# Every tool is pinned and fetched on demand: Python tools via uvx, the rest as Docker images. Needs only uv + Docker.
# Nothing here talks to the Spark.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

SHELLCHECK=shellcheck-py==0.11.0.1
YAMLLINT=yamllint==1.38.0
RUFF=ruff==0.14.0
GITLEAKS=zricethezav/gitleaks:v8.30.1
# promtool from the exact Prometheus image the box runs.
PROMETHEUS=$(sed -n 's|^ *image: \(prom/prometheus:[^ ]*\)$|\1|p' monitoring/compose.yaml)

rc=0
step() {
  local name=$1; shift
  printf '\033[1m▸ %s\033[0m\n' "$name"
  if out=$("$@" 2>&1); then printf '  \033[32m✓\033[0m\n'
  else printf '%s\n  \033[31m✗ %s failed\033[0m\n' "$out" "$name"; rc=1; fi
}
files() { find . -path ./.git -prune -o -path ./inference/voices -prune -o -type f "$@" -print | sort; }

# bash 3.2 (macOS) has no mapfile; file names here never contain newlines.
IFS=$'\n' read -r -d '' -a SH < <(files -name '*.sh'; echo ./.githooks/pre-commit)
IFS=$'\n' read -r -d '' -a PY < <(files -name '*.py')

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# Site values come from inventory.env, or from the example on a clone that has none yet.
INV=inventory.env; [ -f "$INV" ] || INV=inventory.env.example
HOST=$(sed -n 's/^SPARK_HOST=//p' "$INV" | tail -1)
./scripts/render.sh || exit 1
# Compose files are validated with the example env files (their real defaults) plus this site's bind IP.
sed -e 's/^SPARK_API_KEY=$/SPARK_API_KEY=lint-dummy/' -e "s/^SPARK_BIND_IP=.*/SPARK_BIND_IP=$HOST/" \
  inference/.env.example > "$TMP/inference.env"
sed -e 's/^GRAFANA_ADMIN_PASSWORD=.*$/GRAFANA_ADMIN_PASSWORD=lint-dummy/' -e "s/^SPARK_BIND_IP=.*/SPARK_BIND_IP=$HOST/" \
  monitoring/.env.example > "$TMP/monitoring.env"

step "shellcheck (shell scripts)"       uvx -q --from "$SHELLCHECK" shellcheck -x -S style "${SH[@]}"
step "ruff (Python lint + bandit S)"    uvx -q "$RUFF" check "${PY[@]}"
step "ruff format --check"              uvx -q "$RUFF" format --check "${PY[@]}"
step "yamllint"                         uvx -q "$YAMLLINT" -s .
step "dashboards are valid JSON"        python3 -I -c 'import json,sys; [json.load(open(f)) for f in sys.argv[1:]]' monitoring/grafana/dashboards/*.json
step "compose config: inference"        docker compose -f inference/compose.yaml --env-file "$TMP/inference.env" config -q
step "compose config: monitoring"       docker compose -f monitoring/compose.yaml --env-file "$TMP/monitoring.env" config -q
step "promtool check config + rules"    docker run --rm --entrypoint promtool -v "$PWD/monitoring/prometheus:/etc/prometheus:ro" \
                                          "$PROMETHEUS" check config /etc/prometheus/prometheus.yml
step "promtool test rules"              docker run --rm --entrypoint promtool -v "$PWD/monitoring/prometheus:/etc/prometheus:ro" \
                                          -w /etc/prometheus/tests "$PROMETHEUS" test rules spark.test.yml
step "policy (exposure, pinning, inventory consistency, site leaks)" \
                                        python3 -I scripts/lint-policy.py "$TMP/inference.env" "$TMP/monitoring.env" "$INV"
step "gitleaks (secrets, files)"        docker run --rm -v "$PWD:/repo:ro" "$GITLEAKS" dir /repo --no-banner --redact \
                                          --config /repo/.gitleaks.toml
# Every commit, not only the working tree: a secret that was committed and later removed is still published.
# Skipped where .git is not a directory (a git worktree), because the container can't follow the link.
[ -d .git ] && \
step "gitleaks (secrets, git history)"  docker run --rm -v "$PWD:/repo:ro" "$GITLEAKS" git /repo --no-banner --redact \
                                          --config /repo/.gitleaks.toml

if [ $rc = 0 ]; then printf '\033[32mall gates passed\033[0m\n'; else printf '\033[31mgates failed\033[0m\n'; fi
exit $rc
