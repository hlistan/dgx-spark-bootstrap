# Host — config that needs sudo on the Spark

Ubuntu 24.04.5 (DGX OS), kernel 7.0.0-nvidia, driver 580.178.04, CUDA 13.0. The SSH user is in the `docker` group;
nothing in `make` needs sudo — the steps below do, run them yourself on the Spark.

## Firewall — `firewall/spark-firewall.{sh,service}` (S3, S6)

When installed, only the hosts in `SPARK_ALLOWED_IPS` (`inventory.env`, normally the admin workstation) may reach the inference port 8000 (Docker's
`DOCKER-USER` chain; published ports bypass ufw) and Grafana 3000 (`INPUT`). SSH is untouched; exporters and
Prometheus listen on 127.0.0.1 and need no rule.

**Status: not installed, by decision** — the LAN is trusted (S6, 2026-10-29): the model port relies on the API key,
Grafana on its login. Install it when untrusted hosts join the network (then also consider Tailscale, S6).

```sh
# [Spark], after `make deploy` from the workstation
cd ~/spark/host/firewall
sudo install -m 755 spark-firewall.sh /usr/local/sbin/
echo 'SPARK_ALLOWED_IPS=192.0.2.20' | sudo tee /etc/default/spark-firewall   # several: "ip1 ip2"
sudo install -m 644 spark-firewall.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now spark-firewall
sudo iptables -L SPARK -nv; sudo iptables -L DOCKER-USER -n --line-numbers | head -3; sudo iptables -L INPUT -n --line-numbers | head -3
```
Test from a host **not** in `SPARK_ALLOWED_IPS`: `curl -m5 http://$SPARK_HOST:8000/health` and `:3000` must time out;
from an allowed host both answer. Remove: `sudo systemctl disable --now spark-firewall`.

## Already present on DGX OS (keep)

| Unit | Role |
|---|---|
| `nvidia-persistenced` | keeps the driver loaded |
| `nvidia-dgx-telemetry` | NVIDIA's telemetry agent |
| `dgx-dashboard` / `-admin` | NVIDIA DGX Dashboard on 127.0.0.1:11000 (`ssh -L 11000:127.0.0.1:11000 spark`) |
| `rasdaemon`, `smartmontools` | hardware error logging (`ras-mc-ctl --summary`), SMART (`sudo smartctl -a /dev/nvme0`) |

## Applied host settings (sudo)

- `vm.swappiness=1` in `/etc/sysctl.d/90-spark.conf` (2026-11-17): at the default 60 the kernel swapped out
  ~14 M pages a day with 24+ GiB still available (`SparkSwapping`). Swap stays enabled for real emergencies.
  `echo 'vm.swappiness=1' | sudo tee /etc/sysctl.d/90-spark.conf && sudo sysctl --system`

## Boot race: spark-llm doesn't start after a reboot

Docker can start before DHCP assigns the Spark's LAN IP. spark-llm publishes 8000 on that IP only (S6), so its start
fails with `failed to bind host port <LAN IP>:8000/tcp: cannot assign requested address`, and Docker doesn't
retry (seen 2026-11-17). Grafana is unaffected because it uses host networking. `host/llm-boot-start.sh` runs
`@reboot` from the user's crontab (`make schedule`): it reads `SPARK_HOST` from the deployed `inventory.env`, waits for that IP and Docker, then starts spark-llm only if its last
start failed, so a deliberate `docker stop` is kept. No sudo. Alternative with sudo: `net.ipv4.ip_nonlocal_bind=1`.

## GPU fault: "GPU requires reset" (Xid 119)

Signature: kernel `NVRM: Xid … 119 … GSP` timeout, then a storm of `gpuHandleSanityCheckRegReadError_GH100 …
0xbadf5600`; `nvidia-smi -q` shows "GPU requires reset"; alert `SparkGpuTelemetryLost`, analysis check "GPU kernel
errors". The running LLM keeps serving from its existing CUDA context, but nothing new can open one, so a restart of
`spark-llm` fails. `nvidia-smi -r` is refused (primary GPU). Recovery:

```sh
sudo nvidia-bug-report.sh --output-file ~/nvidia-bug-report-$(date +%F).log.gz   # evidence for NVIDIA
sudo apt update && sudo apt dist-upgrade && sudo fwupdmgr refresh && sudo fwupdmgr upgrade
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
sudo reboot            # if the fault survives a warm reboot: shut down, unplug the power brick 30 s, power on
```
Then `make check` from the workstation. If it recurs, open an NVIDIA support case with the bug report.
First seen 2026-11-11 09:53 (uptime 14 days).

## Recommended (optional, sudo)

- SSH keys only: `/etc/ssh/sshd_config.d/10-keys-only.conf` with `PasswordAuthentication no`, then `sudo systemctl reload ssh`.
- Page cache before (re)starting the LLM: `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` (inference/README.md step 7).
