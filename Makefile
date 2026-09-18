# Personal Job Agent — runner
#
# Entry points verified from the repo (not assumed):
#   backend API : scripts/run_api.py   → uvicorn, HOST/PORT env, default 127.0.0.1:8077
#   telegram bot: scripts/run_bot.py   → long-polling Bot API process
#   dashboard   : dashboard/ npm run dev → astro dev, default port 1234
#   pipeline    : scripts/pipeline.py  → ingest → match → digest (systemd/cron entry)
#
# No n8n / external orchestrator exists in this system — scheduling is systemd
# timers (deploy/) or GitHub Actions (.github/workflows/digest.yml). There is
# nothing else to bring up for a meaningful local run.
#
# Python env: uses .venv/ (created by `make install`, via uv when available).

SHELL        := /bin/bash
VENV         := .venv
PY           := $(VENV)/bin/python
API_PORT     ?= 8077
DASH_PORT    ?= 1234

.PHONY: install setup onboard demo quickstart inbox run run_backend run_bot run_dashboard check test pipeline forage ask upskill doctor eval_assistant docker_up docker_down mcp mcp_check

install: ## backend + dashboard deps (idempotent)
	@if command -v uv >/dev/null 2>&1; then \
		[ -d $(VENV) ] || uv venv $(VENV); \
		uv pip install -q -e ".[dev,api,llm,telegram,mcp]" --python $(PY); \
	else \
		[ -d $(VENV) ] || python3 -m venv $(VENV); \
		$(PY) -m pip install -q -e ".[dev,api,llm,telegram,mcp]"; \
	fi
	@cd dashboard && npm install --silent
	@echo "✅ install done. Optional Tier-2 ATS: $(PY) -m playwright install chromium"

setup: ## interactive first-run config (.env + your profile) — safe to re-run
	@$(PY) scripts/setup.py

onboard: ## full guided config (identity, sources, LLM, email, Telegram) — safe to re-run
	@$(PY) scripts/onboard.py $(if $(CONFIG),--config $(CONFIG)) $(if $(PRINT_ONLY),--print-only)

demo: ## seed a throwaway store so the UI has something to show (never touches yours)
	@$(PY) scripts/seed_demo.py
	@echo ""
	@echo "  run it with:  JOBAGENT_DB_PATH=data/demo.db make run"

quickstart: ## keyless one-command first run: ready .env + demo store, then `make run`
	@[ -x "$(PY)" ] || { echo "❌ run: make install first"; exit 1; }
	@$(PY) scripts/quickstart.py

docker_up: ## build + start API and dashboard in containers (needs .env)
	@[ -f .env ] || { echo "❌ .env missing — run: make setup"; exit 1; }
	docker compose up -d --build
	@echo "  API       http://127.0.0.1:$(API_PORT)"
	@echo "  dashboard http://127.0.0.1:$(DASH_PORT)"

docker_down: ## stop the containers (data/ is a volume and survives)
	docker compose down

check: ## preflight: env file, required vars, ports, db — fail fast per item
	@fail=0; \
	if [ ! -f .env ]; then echo "❌ .env missing — cp .env.example .env and fill it in"; fail=1; \
	else \
		. ./.env 2>/dev/null; \
		if [ -z "$$GROQ_API_KEY$$GEMINI_API_KEY$$OPENROUTER_API_KEY$$OPENAI_API_KEY$$ANTHROPIC_API_KEY$$CUSTOM_LLM_BASE_URL" ]; then \
			echo "⚠️  no LLM key set — matching falls back to heuristic-only, apply drafting will fail"; fi; \
		[ -z "$$TELEGRAM_BOT_TOKEN" ] && echo "⚠️  TELEGRAM_BOT_TOKEN unset — bot + digest push disabled"; \
		[ -z "$$TELEGRAM_CHAT_ID" ]  && echo "⚠️  TELEGRAM_CHAT_ID unset — bot owner-gate + digest destination missing"; \
		[ -z "$$DASHBOARD_PASSWORD" ] && echo "⚠️  DASHBOARD_PASSWORD unset — dashboard Settings page disabled (fail-closed)"; \
	fi; \
	[ -x "$(PY)" ] || { echo "❌ $(VENV) missing — run: make install"; fail=1; }; \
	[ -f config/preferences.example.toml ] || { echo "❌ config/preferences.example.toml missing — broken checkout"; fail=1; }; \
	[ -x "$(PY)" ] && $(PY) scripts/check_profile.py; \
	[ -f data/cv_master.md ] || [ -f config/cv_master.md ] || echo "⚠️  no CV text (data/cv_master.md) — CV tailoring will use empty CV text; add it in Settings → CV"; \
	if lsof -nP -iTCP:$(API_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "❌ port $(API_PORT) already in use (API) — stop it or run: make run API_PORT=<other>"; fail=1; fi; \
	if lsof -nP -iTCP:$(DASH_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "❌ port $(DASH_PORT) already in use (dashboard)"; fail=1; fi; \
	[ -f data/jobagent.db ] || echo "ℹ️  no data/jobagent.db yet — will be created; seed with: make pipeline"; \
	[ $$fail -eq 0 ] && echo "✅ preflight ok" || exit 1

run_backend: ## API only (uvicorn on $(API_PORT))
	PORT=$(API_PORT) $(PY) scripts/run_api.py

run_bot: ## telegram bot only (long-polling)
	$(PY) scripts/run_bot.py

doctor: ## LLM chain, model cards, routing (offline; HEALTH=1 probes, PROBE=1 deep-probes)
	@$(PY) scripts/llm_doctor.py $(if $(HEALTH),--health) $(if $(PROBE),--probe)

eval_assistant: ## run the assistant eval set (spends LLM quota)
	@$(PY) scripts/eval_assistant.py $(if $(WEAK),--weak) --floors

ask: ## ask the assistant, e.g. make ask Q="is the pipeline healthy?"
	@$(PY) scripts/ask.py $(if $(EXPLAIN),--explain) "$(Q)"

mcp: ## MCP operator server on stdio for a coding agent (Claude Code reads .mcp.json; ADMIN=1 exposes config tools)
	@$(PY) -m jobagent.mcp $(if $(ADMIN),--admin)

mcp_check: ## build the MCP server in-process, list its tools/resources/prompts, assert no send/approve tool exists (offline)
	@$(PY) -m jobagent.mcp --check

upskill: ## skill-gap heatmap + learning plan from recorded match gaps (MIN=0.5)
	@$(PY) scripts/upskill.py $(MIN)

run_dashboard: ## dashboard only (astro dev on $(DASH_PORT))
	cd dashboard && JOBAGENT_API_URL=http://127.0.0.1:$(API_PORT) npm run dev -- --port $(DASH_PORT)

run: check ## API + dashboard together; prefixed logs; one Ctrl-C tears both down
	@trap 'kill 0' INT TERM EXIT; \
	( PORT=$(API_PORT) $(PY) scripts/run_api.py 2>&1 | sed -e 's/^/[api ] /' ) & \
	( cd dashboard && JOBAGENT_API_URL=http://127.0.0.1:$(API_PORT) npm run dev -- --port $(DASH_PORT) 2>&1 | sed -e 's/^/[dash] /' ) & \
	wait

inbox: ## scan the applying mailbox for outcomes (proposes only, never applies)
	$(PY) scripts/scan_inbox.py

forage: pipeline ## a forage — one ingest → match pass (alias of `make pipeline`)

pipeline: ## one ingest → match pass, no Telegram push
	$(PY) scripts/pipeline.py --no-send

test: ## offline suite (759 tests, no credentials)
	$(PY) -m pytest tests/ -q
