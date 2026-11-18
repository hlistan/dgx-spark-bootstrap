#!/usr/bin/env bash
# Probe the Spark inference stack over the LAN, as a client would (run from the workstation, not the Spark).
# Checks: the coding LLM is served at the expected context and makes tool calls; exposure (S6).
#
#   scripts/check-inference.sh [ENV_FILE]
# SPARK_API_KEY (and optionally SPARK_HOST) come from the environment, else from ENV_FILE — any client's env file
# with SPARK_API_KEY=…. Endpoint, model and context default to ../inventory.env.
set -uo pipefail
ENV_FILE=${1:-}
INV="$(dirname "$0")/../inventory.env"
# Read keys literally — never `source` an env file: values may contain `$`.
envget() { [ -n "$2" ] && [ -f "$2" ] && sed -n "s/^$1=//p" "$2" | tail -1 | sed -E 's/[[:space:]]+#.*$//; s/^["'\'']//; s/["'\'']$//'; }
SPARK_HOST=${SPARK_HOST:-$(envget SPARK_HOST "$ENV_FILE")}; SPARK_HOST=${SPARK_HOST:-$(envget SPARK_HOST "$INV")}
SPARK_API_KEY=${SPARK_API_KEY:-$(envget SPARK_API_KEY "$ENV_FILE")}
MODEL=${SPARK_LLM_MODEL:-$(envget SPARK_LLM_MODEL "$INV")}; CTX=${SPARK_LLM_CONTEXT:-$(envget SPARK_LLM_CONTEXT "$INV")}
: "${SPARK_HOST:?SPARK_HOST not set}" "${SPARK_API_KEY:?SPARK_API_KEY not set (env or ENV_FILE)}" "${MODEL:?}" "${CTX:?}"
B="http://$SPARK_HOST"; AUTH=(-H "Authorization: Bearer $SPARK_API_KEY")
rc=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; rc=1; }

echo "LLM  $B:8000"
len=$(curl -fsS -m 10 "${AUTH[@]}" "$B:8000/v1/models" | python3 -c 'import sys,json
m=[x for x in json.load(sys.stdin)["data"] if x["id"]==sys.argv[1]]; print(m[0].get("max_model_len","?") if m else "")' "$MODEL" 2>/dev/null)
if [ -z "$len" ]; then bad "$MODEL not listed"
elif [ "$len" = "$CTX" ]; then ok "model $MODEL served, context $len"
else bad "model $MODEL served with context $len (inventory says $CTX)"; fi
# Tool calling. Gate on tool_choice "auto" — what agent clients normally send (tool_choice unset).
# max_tokens must leave room for the model's reasoning before the call (256 was too small).
# tool_choice "required" uses xgrammar-constrained decoding, which with speculative decoding
# logs "Failed to advance FSM" and is occasionally flaky — informational only.
toolreq() {
  curl -fsS -m 180 "${AUTH[@]}" -H 'Content-Type: application/json' "$B:8000/v1/chat/completions" -d '{
  "model":"'"$MODEL"'","max_tokens":4096,"messages":[{"role":"user","content":"Add 2 and 3 using the tool."}],
  "tools":[{"type":"function","function":{"name":"add","description":"Add numbers",
   "parameters":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}}}],
  "tool_choice":"'"$1"'"}' | python3 -c 'import sys,json;m=json.load(sys.stdin)["choices"][0];print(len(m["message"].get("tool_calls") or []), m.get("finish_reason"))' 2>/dev/null
}
pass=0; for _ in 1 2 3; do r=$(toolreq auto); [ "${r%% *}" -gt 0 ] 2>/dev/null && pass=$((pass+1)) || last="$r"; done
if [ $pass -eq 3 ]; then ok "tool calls (auto): 3/3"
else bad "tool calls (auto): $pass/3 — last: ${last:-no response}; check --tool-call-parser / chat template"; fi
r=$(toolreq required); [ "${r%% *}" -gt 0 ] 2>/dev/null && ok "forced tool call (required) returned" \
  || printf '  \033[33m!\033[0m forced tool call failed (%s) — known xgrammar+speculative-decoding flake, informational\n' "${r:-no response}"

# Exposure (S6): /v1 refuses calls without the key; the server grants browsers no cross-origin access
# (GHSA-vvpg-rrww-wpmh).
echo "Exposure"
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$B:8000/v1/models")
if [ "$code" = 401 ]; then ok "LLM without key → 401"; else bad "LLM without key → $code (expected 401)"; fi
acao=$(curl -s -m 10 -o /dev/null -D - -X OPTIONS -H 'Origin: http://example.com' -H 'Access-Control-Request-Method: POST' \
       "$B:8000/v1/chat/completions" | tr -d '\r' | sed -n 's/^[Aa]ccess-[Cc]ontrol-[Aa]llow-[Oo]rigin: //p')
if [ -z "$acao" ]; then ok "LLM grants no cross-origin access"; else bad "LLM allows origin $acao"; fi
exit $rc
