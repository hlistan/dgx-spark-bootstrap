# Inference stack — DGX Spark runbook

Serves one coding LLM over the local network only, as an OpenAI-compatible API. The box holds no client state.
Decisions: `../docs/decisions.md` (S4 model, S5 coding only, S6 exposure). Monitoring: `../monitoring/README.md`.

| Service (container) | Port (LAN IP only) | Model | API |
|---|---|---|---|
| `llm` (`spark-llm`) | 8000 | `nvidia/Qwen3.8-27B-NVFP4` + DFlash2 drafter, served as `qwen3.8-27b` | OpenAI `/v1/chat/completions` — tools, thinking, vision, 256k ctx (NVIDIA recipe, S4) |

Expected memory (128 GB unified): llm ≈ 73 GB (`LLM_GPU_UTIL=0.60`), ≥ 40 GB headroom. Disk: ≈ 50 GB for the image
and weights. First-time setup: about 30 min, mostly downloads and the first compile.

Commands marked **[Spark]** run on the Spark in `~/spark/inference`, **[WS]** on the admin workstation in this repo.

---

## Step 1 — SSH key access  [WS]
Create a key and an alias `spark` in `~/.ssh/config` (`HostName` = the Spark's LAN IP, `User` = your user on it,
`IdentityFile ~/.ssh/id_ed25519_spark`). Install the key once; it asks for that user's password:
```sh
ssh-copy-id -i ~/.ssh/id_ed25519_spark.pub spark
ssh spark 'hostname && nvidia-smi -L'        # must log in with no password prompt
```
On first connect, compare the host key fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on the
Spark's own console.
Optional hardening: `../host/README.md`.

## Step 2 — Check the Spark  [Spark]
```sh
nvidia-smi                                   # GB10 visible, driver loaded
docker --version && docker compose version   # Docker + Compose v2 ship with DGX OS
docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi   # GPU inside containers
df -h ~                                      # ≥ 50 GB free
```
If `docker` needs sudo: `sudo usermod -aG docker $USER`, then log out and back in.

**Network:** reserve DHCP addresses for the Spark (`SPARK_HOST`) and the hosts in `SPARK_ALLOWED_IPS`
(`../inventory.env`). The model port binds to the Spark's IP; if it changes, the stack stops being reachable.

## Step 3 — Copy the repo  [WS] → [Spark]
`make deploy` rsyncs this repo to `~/spark` (never `.env`). Re-run it after any change.
The firewall is optional (S6); to install it, see `../host/README.md`.

## Step 4 — Configure  [Spark]
```sh
cd ~/spark/inference
cp .env.example .env && chmod 600 .env
openssl rand -hex 32                          # → SPARK_API_KEY
```
Edit `.env`:
| Key | Value |
|---|---|
| `SPARK_BIND_IP` | the Spark's LAN IP, same as `SPARK_HOST` in `inventory.env` |
| `SPARK_API_KEY` | the generated key; give the same value to every client |
| `LLM_IMAGE` | keep `nvcr.io/nvidia/vllm:26.10.pre1-py3` (NVIDIA's recipe image for this model) |
| `HF_TOKEN` | optional (models are public; a token avoids download rate limits) |

## Step 5 — Pull  [Spark]
```sh
docker compose pull llm                       # NGC vLLM aarch64 image
```

## Step 6 — Start  [Spark]
Free the page cache first. Otherwise vLLM counts cached files on unified memory as "used" and refuses to start at
the configured utilisation.
```sh
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'

docker compose up -d --remove-orphans
docker compose logs -f llm        # first run: downloads ~23 GB + compiles; wait for
                                  # "Application startup complete" (10–15 min), then Ctrl-C
docker compose ps                 # llm "healthy"
```

## Step 7 — Smoke tests  [WS]
`make check-inference ENV_FILE=<file with SPARK_API_KEY=…>` (or `SPARK_API_KEY=… make check-inference`): model listed
with the 256k context from `../inventory.env`, tool calls 3/3, exposure (S6).

## Step 8 — Coding clients  [WS]
Clients need `SPARK_API_KEY` and `SPARK_LLM_BASE_URL` / `SPARK_LLM_MODEL` / `SPARK_LLM_CONTEXT` from
`../inventory.env`. Any OpenAI-compatible coding agent works. Point it at the base URL, use the model
`qwen3.8-27b`, and set its context window to 262144.
- Thinking is on by default (reasoning parser `qwen3`). It is better for agentic coding but slower. A client that
  wants quick edits can send `"chat_template_kwargs": {"enable_thinking": false}` per request.
- Thinking depth: `"reasoning_effort"` is `xhigh` by default; `medium` and `low` also work, as a top-level request
  field or inside `chat_template_kwargs`. Measured 2026-11-20 on three small prompts (2 runs each): no level gave a
  wrong answer, and lower effort was not reliably shorter (bug fix: 460–1860 reasoning tokens at `xhigh`, 115–340 at
  `medium`; code trace: 230–295 at `xhigh`, 455–555 at `medium`). Thinking off got the code trace wrong 2/2. So the
  server default stays `xhigh`; a client can lower it per request.
- Tools: `qwen3_xml` parser with auto tool choice; clients send OpenAI `tools` as usual.
- Don't send `presence_penalty` (S4): sampling comes from the model's `generation_config.json`.

Containers on Docker Desktop reach the LAN through their host, so the Spark sees the host's IP. That IP must be
in `SPARK_ALLOWED_IPS` if the firewall is installed. Firewall check (if installed): from a host **not** in the list,
`curl -m5 http://$SPARK_HOST:8000/health` must time out.

## Step 9 — Load test before relying on it  [Spark]
Run 8 concurrent ~30k-token chats and watch memory (or Grafana).
```sh
# terminal 1
watch -n2 'free -g; nvidia-smi --query-gpu=temperature.gpu,power.draw,utilization.gpu --format=csv'
# terminal 2
set -a; . ./.env; set +a
python3 -c 'import json;print(json.dumps({"model":"qwen3.8-27b","max_tokens":300,"messages":[{"role":"user","content":("def f(x): return x + 1\n"*3000)+"\nSummarise this file in 3 bullets."}]}))' > /tmp/big.json
seq 8 | xargs -P8 -I{} sh -c 'curl -s -o /dev/null -w "chat {}: %{http_code} %{time_total}s\n" http://$SPARK_BIND_IP:8000/v1/chat/completions -H "Authorization: Bearer $SPARK_API_KEY" -H "Content-Type: application/json" -d @/tmp/big.json'
```
Pass: all `200`, available memory stays ≥ 10 GB, no container restarts (`docker compose ps`).
On OOM, lower `LLM_GPU_UTIL` to 0.50, or set `--max-model-len 131072` on `llm` — then update `SPARK_LLM_CONTEXT` in
`../inventory.env` so clients size their context to match. Then `docker compose up -d llm`.

---

## Operations  [Spark]
| Task | Command |
|---|---|
| Status / health | `docker compose ps` (or `make status` / Grafana from the workstation) |
| Logs | `docker compose logs -f --tail=100 llm` |
| Firewall status | `sudo iptables -L SPARK -nv` · disable: `sudo systemctl stop spark-firewall` (`../host/README.md`) |
| Restart | `docker compose restart llm` |
| Stop / start | `docker compose down` / step 6 |
| After a reboot | nothing: `restart: unless-stopped` (+ `spark-firewall.service` if installed) brings it back |
| Upgrade vLLM | set a newer `LLM_IMAGE` in `.env` (it must carry vllm#55601, S4) → `docker compose pull llm` → steps 6, 7, 9 → keep it only if all pass |

## Troubleshooting
| Symptom | Fix |
|---|---|
| `cannot assign requested address` on start | the Spark's IP changed: `ip -4 addr`; fix the DHCP reservation or `SPARK_BIND_IP` |
| vLLM: "free memory … less than desired GPU memory utilization" | drop the page cache (step 6) or lower `LLM_GPU_UTIL` |
| Log shows `xgrammar … Failed to advance FSM` | appears only with `tool_choice: required` (constrained decoding); ignore unless `auto` tool calls fail too |
| Tool calls come back as plain text, the model ignores the newest message, or emits `<\|mask_start\|>` | speculative decoding + prefix caching on an image without the fix (vllm#60174). Keep `LLM_IMAGE` at the recipe's tag; `docker compose up -d llm` clears a poisoned cache |
| Tool calls empty or malformed | add `--chat-template` with the model's tool template; re-run the step 7 tool test |
| A client can't connect (timeout) | client IP still in `SPARK_ALLOWED_IPS`? — else update `/etc/default/spark-firewall` + `sudo systemctl restart spark-firewall`; `sudo iptables -L SPARK -nv` shows drop counters |
