# DGX Spark — run from the admin workstation in this repo. Everything on the box goes through `ssh $(SPARK_SSH)`.
# Site values come from inventory.env (gitignored). Without it, the example is read so `make help` and `make lint`
# work on a fresh clone; any target that talks to the Spark stops with a hint instead.
include $(firstword $(wildcard inventory.env) inventory.env.example)
SITE   = $(if $(wildcard inventory.env),,$(error inventory.env is missing. Run: cp inventory.env.example inventory.env, then edit it))
SSH    = $(SITE)ssh -o ConnectTimeout=10 $(SPARK_SSH)
DIR   := ~/$(SPARK_DEPLOY_DIR)
# Compose on the Spark, in a stack folder: $(call on,inference,ps)
on     = $(SSH) 'cd $(DIR)/$(1) && docker compose $(2)'

.DEFAULT_GOAL := help
.PHONY: help lint hooks render deploy diff status check check-inference check-monitoring \
        schedule unschedule analyze bench report \
        inference-up inference-ps inference-logs \
        monitoring-env monitoring-up monitoring-down monitoring-ps monitoring-logs monitoring-reload \
        dashboards prom grafana

help: ## List targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Quality gates (S7) ───────────────────────────────────────────────────────
render: ## Fill the config templates from inventory.env (deploy and lint do this first)
	./scripts/render.sh

lint: render ## Static gates: shellcheck, ruff (+bandit), yamllint, compose, promtool + rule tests, policy + site-leak scan, gitleaks
	./scripts/lint.sh

hooks: ## Run `make lint` before every commit in this clone
	git config core.hooksPath .githooks && echo "pre-commit hook enabled (.githooks/pre-commit)"

# ── Repo → box ───────────────────────────────────────────────────────────────
RSYNC  = $(SITE)rsync -az --delete --exclude .git --exclude .DS_Store --exclude '.env' --exclude 'inference/voices/' \
         --exclude 'inference/logs/' --exclude __pycache__ --exclude .ruff_cache --exclude 'reports/' --exclude 'research_notes/'
deploy: lint ## Copy this repo to ~/spark on the Spark (never .env) after the gates pass; then *-up to apply
	$(RSYNC) ./ $(SPARK_SSH):$(DIR)/

diff: render ## Dry run: what deploy would change on the Spark
	$(RSYNC) -n --itemize-changes ./ $(SPARK_SSH):$(DIR)/

status: ## Box at a glance: GPU, memory, disk, containers, firewall
	@$(SSH) 'nvidia-smi --query-gpu=name,temperature.gpu,power.draw,utilization.gpu,clocks.sm --format=csv,noheader; \
	  free -g | sed -n 1,2p; df -h / | tail -1; \
	  docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"; \
	  printf "spark-firewall: "; systemctl is-active spark-firewall || true'

check: check-inference check-monitoring ## Probe inference as a client + monitoring targets/alerts

check-inference: ## LLM served, tool calls, exposure (needs SPARK_API_KEY in env or ENV_FILE=…)
	./scripts/check-inference.sh $(ENV_FILE)

check-monitoring: ## Scrape targets up, rules loaded, firing alerts, Grafana reachable
	./scripts/check-monitoring.sh

# ── Inference (inference/) ───────────────────────────────────────────────────
inference-up: ## Apply inference/compose.yaml (recreates only changed services, removes dropped ones; README step 6)
	$(call on,inference,up -d --remove-orphans)

inference-ps: ## Inference containers and health
	$(call on,inference,ps)

inference-logs: ## Tail inference logs: make inference-logs S=llm
	$(call on,inference,logs --tail=200 $(S))

# ── Monitoring (monitoring/) ─────────────────────────────────────────────────
monitoring-env: ## Create monitoring/.env on the Spark with a random Grafana password (once)
	$(SSH) 'cd $(DIR)/monitoring && [ ! -f .env ] && umask 077 && \
	  sed -e "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$$(openssl rand -base64 24 | tr -d /+=)|" \
	      -e "s|^SPARK_BIND_IP=.*|SPARK_BIND_IP=$(SPARK_HOST)|" \
	      -e "s|^SPARK_REPORTS_DIR=.*|SPARK_REPORTS_DIR=$$HOME/spark-reports|" .env.example > .env && echo "created monitoring/.env" \
	  || echo "monitoring/.env exists — left unchanged"'

monitoring-up: ## Start/apply the monitoring stack
	$(call on,monitoring,up -d --remove-orphans)

monitoring-down: ## Stop the monitoring stack (keeps data)
	$(call on,monitoring,down)

monitoring-ps: ## Monitoring containers
	$(call on,monitoring,ps)

monitoring-logs: ## Tail monitoring logs: make monitoring-logs S=prometheus
	$(call on,monitoring,logs --tail=200 $(S))

monitoring-reload: ## Reload Prometheus config + rules after deploy (no restart)
	$(SSH) 'docker exec spark-prometheus promtool check config /etc/prometheus/prometheus.yml && curl -fsS -X POST 127.0.0.1:9090/-/reload && echo reloaded'

# ── Periodic analysis (analysis/, S8) ────────────────────────────────────────
schedule: ## Install the nightly benchmark + daily/weekly analysis in the Spark's crontab (idempotent)
	$(SSH) 'mkdir -p ~/spark-reports/metrics && { crontab -l 2>/dev/null | sed "/^# BEGIN spark-analysis/,/^# END spark-analysis/d"; \
	  cat $(DIR)/analysis/crontab; } | crontab - && crontab -l | sed -n "/BEGIN spark-analysis/,/END spark-analysis/p"'

unschedule: ## Remove the analysis jobs from the Spark's crontab
	$(SSH) 'crontab -l 2>/dev/null | sed "/^# BEGIN spark-analysis/,/^# END spark-analysis/d" | crontab -'

analyze: ## Run the analysis now and print it: make analyze [W=7d]
	$(SSH) 'python3 -I $(DIR)/analysis/spark-analyze.py --window $(or $(W),24h) >/dev/null && cat ~/spark-reports/latest-$(or $(W),24h).md'

bench: ## Run the benchmark now (~2 min, waits for an idle LLM) and show recent history
	$(SSH) 'python3 -I $(DIR)/analysis/spark-bench.py && echo && tail -n 12 ~/spark-reports/bench.csv | column -ts,'

report: ## Show the latest report: make report [W=7d]
	$(SSH) 'cat ~/spark-reports/latest-$(or $(W),24h).md'

dashboards: ## Re-vendor the community dashboards at their pinned revisions
	python3 -I monitoring/grafana/vendor-dashboards.py

prom: ## Tunnel Prometheus to http://localhost:9090 (Ctrl-C to stop)
	$(SSH) -N -L 9090:127.0.0.1:9090

grafana: ## Open Grafana (admin password: ssh spark 'grep GRAFANA ~/spark/monitoring/.env')
	$(SITE)open $(SPARK_GRAFANA_URL)
