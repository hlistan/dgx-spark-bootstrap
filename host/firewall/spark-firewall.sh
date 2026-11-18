#!/bin/sh
# DMATCH/IMATCH below are argument lists, expanded unquoted on purpose (word splitting):
# shellcheck disable=SC2086
# Spark firewall: only the allowed LAN hosts may reach the Spark's service ports. SSH (22) is never touched.
#   - Docker-published ports (inference 8000) bypass ufw/INPUT, so their rule lives in DOCKER-USER and
#     matches the original, pre-DNAT port.
#   - Host-network ports (Grafana 3000) go through INPUT.
#   - Exporters and Prometheus listen on 127.0.0.1 only and need no rule.
# Locally generated traffic (Prometheus scraping vLLM on the LAN IP) traverses neither chain's filter here.
#
# Usage: spark-firewall.sh "<ip> [<ip> …]" [apply|remove]   — installed as a systemd unit (host/README.md).
set -eu
ALLOW=${1:?allowed source IPs, e.g. "192.0.2.20"}; ACTION=${2:-apply}
DOCKER_PORTS=${SPARK_DOCKER_PORTS:-8000}
HOST_PORTS=${SPARK_HOST_PORTS:-3000}

DMATCH="-p tcp -m conntrack --ctorigdstport $DOCKER_PORTS --ctdir ORIGINAL -j SPARK"
IMATCH="-p tcp -m multiport --dports $HOST_PORTS -j SPARK"

# Clean slate.
while iptables -C DOCKER-USER $DMATCH 2>/dev/null; do iptables -D DOCKER-USER $DMATCH; done
while iptables -C INPUT $IMATCH 2>/dev/null; do iptables -D INPUT $IMATCH; done
iptables -F SPARK 2>/dev/null && iptables -X SPARK
[ "$ACTION" = remove ] && { echo "spark firewall removed"; exit 0; }

iptables -N SPARK
iptables -A SPARK -i lo -j RETURN
for ip in $ALLOW; do iptables -A SPARK -s "$ip" -j RETURN; done
iptables -A SPARK -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SPARK -j DROP
iptables -I DOCKER-USER 1 $DMATCH
iptables -I INPUT 1 $IMATCH
echo "spark firewall: ports $DOCKER_PORTS (docker) and $HOST_PORTS (host) allowed only from: $ALLOW"
