#!/bin/sh
# Start spark-llm after boot once the LAN IP exists (run @reboot from the crontab, analysis/crontab).
#
# Docker can come up before DHCP assigns SPARK_HOST; spark-llm publishes 8000 on that IP only (S6), so its
# start fails with "cannot assign requested address" and Docker does not retry (seen 2026-11-17).
# Only a container whose last start *failed* is started, so an operator's `docker stop` is respected.
set -u
# The IP is SPARK_HOST from the deployed inventory.env, or the first argument.
IP="${1:-$(sed -n 's/^SPARK_HOST=//p' "$(dirname "$0")/../inventory.env" 2>/dev/null | tail -1)}"
: "${IP:?SPARK_HOST not found in inventory.env}"
NAME=spark-llm
i=0
while [ "$i" -lt 120 ]; do # up to 10 min
  if ip -4 -o addr show | grep -qF " $IP/" && docker info >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 5
done

running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null) || exit 0
err=$(docker inspect -f '{{.State.Error}}' "$NAME")
if [ "$running" = false ] && [ -n "$err" ]; then
  echo "$(date -Is) $NAME failed at boot ($err); starting"
  docker start "$NAME"
fi
