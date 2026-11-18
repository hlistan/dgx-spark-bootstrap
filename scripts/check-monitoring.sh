#!/usr/bin/env bash
# Check the monitoring stack from the workstation: every scrape target up, rules loaded, which alerts fire.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[ -f inventory.env ] || { echo "inventory.env is missing: cp inventory.env.example inventory.env, then edit it" >&2; exit 1; }
# shellcheck source=inventory.env.example
. ./inventory.env
rc=0; ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }; bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; rc=1; }
echo "Prometheus targets (on $SPARK_SSH)"
out=$(ssh -o ConnectTimeout=10 "$SPARK_SSH" 'curl -fsS 127.0.0.1:9090/api/v1/targets; echo; curl -fsS 127.0.0.1:9090/api/v1/rules; echo; curl -fsS 127.0.0.1:9090/api/v1/alerts') \
  || { bad "Prometheus not answering on the Spark (make monitoring-up)"; exit 1; }
python3 -I - "$out" <<'PY' || rc=1
import json, sys
t, r, a = (json.loads(x) for x in sys.argv[1].strip().split("\n"))
rc = 0
for x in t["data"]["activeTargets"]:
    up = x["health"] == "up"; rc |= not up
    name = x["labels"].get("service") or x["labels"]["job"]
    print(f"  {'\033[32m✓' if up else '\033[31m✗'}\033[0m {x['labels']['job']:<18} {name:<10} {x['scrapeUrl'][:60]} {x.get('lastError','')}")
n = sum(len(g["rules"]) for g in r["data"]["groups"])
print(f"Rules: {n} loaded in {len(r['data']['groups'])} groups")
firing = [x for x in a["data"]["alerts"] if x["state"] == "firing"]
print(f"Alerts firing: {len(firing)}")
for x in firing:
    print(f"  \033[33m!\033[0m {x['labels']['alertname']} [{x['labels'].get('severity')}] {x['annotations'].get('summary','')}")
sys.exit(rc)
PY
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$SPARK_GRAFANA_URL/api/health")
if [ "$code" = 200 ]; then ok "Grafana $SPARK_GRAFANA_URL"; else bad "Grafana $SPARK_GRAFANA_URL → $code"; fi
exit $rc
