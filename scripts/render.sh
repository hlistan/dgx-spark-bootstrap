#!/usr/bin/env bash
# Render the files that carry site values from their templates (`make render`; deploy and lint run it first).
# Values come from inventory.env, or from inventory.env.example when there is no inventory yet (lint on a fresh clone).
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
INV=inventory.env; [ -f "$INV" ] || INV=inventory.env.example
get() { sed -n "s/^$1=//p" "$INV" | tail -1; }
host=$(get SPARK_HOST); name=$(get SPARK_HOSTNAME)
: "${host:?SPARK_HOST missing in $INV}" "${name:?SPARK_HOSTNAME missing in $INV}"
out=monitoring/prometheus/prometheus.yml
{ echo "# Rendered from prometheus.yml.tmpl by scripts/render.sh. Do not edit."
  sed -e "s/@SPARK_HOST@/$host/g" -e "s/@SPARK_HOSTNAME@/$name/g" "$out.tmpl"; } > "$out.new"
if grep -v "^#" "$out.new" | grep -q "@SPARK_[A-Z_]*@"; then echo "unresolved placeholder in $out" >&2; rm -f "$out.new"; exit 1; fi
mv "$out.new" "$out"
