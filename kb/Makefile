# Knowledge backend (copy of the Plancy KB) — developer commands. Run `make help` for the list.
# The stack itself (postgres, opensearch, memgraph, kb-api, kb-worker) is started from deploy/mvp, not from here.
API      ?= http://127.0.0.1:8720
TENANT   ?= company
P        ?= user:admin
PY       ?= uv run python
# Every backing service on a dead port: the unit tests never reach a database, a search cluster or the network.
OFFLINE  ?= env KB_ENV=test KB_LLM_ENABLED=false KB_PG_DSN=postgresql://kb:kb@127.0.0.1:1/kb \
            KB_OS_URL=http://127.0.0.1:1 KB_MEMGRAPH_URI=bolt://127.0.0.1:1 PYTHONDONTWRITEBYTECODE=1

.DEFAULT_GOAL := help
.PHONY: help test test-unit test-sim test-integration check-english stack ask why impact stale stats

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

test: test-unit test-sim ## Unit tests of the backend and of the simulator (no infra)

test-unit: ## Backend unit tests, offline
	$(OFFLINE) PYTHONPATH=app $(PY) -m pytest -q -p no:cacheprovider -m "not integration and not llm" tests/unit

test-sim: ## Simulator (kbsim) tests, offline; run from sim/ so that `tests` is the simulator's package
	cd sim && $(OFFLINE) PYTHONPATH=.:../app $(PY) -m pytest -q -p no:cacheprovider -m "not integration and not llm" tests

test-integration: ## Integration tests: need a running stack and KB_PG_DSN / KB_OS_URL / KB_MEMGRAPH_URI pointing at it
	KB_ENV=test PYTHONPATH=app $(PY) -m pytest -q -p no:cacheprovider -m integration tests/integration

check-english: ## Fail when any file of this directory contains Cyrillic letters
	@! grep -rlP '[\x{0400}-\x{04FF}]' --exclude-dir=.venv --exclude-dir=out . || { echo "Cyrillic found"; exit 1; }

stack: ## The compose stack lives in deploy/mvp
	@echo "Start, stop, seed and reindex the stack from deploy/mvp (see deploy/mvp/Makefile)."

stats: ## Counts and projection watermarks of a tenant
	@curl -fs -H 'X-KB-Principal: $(P)' "$(API)/api/v1/admin/stats?tenant=$(TENANT)" | python3 -m json.tool

ask: ## Ask a question: make ask Q="Why did we launch experiment EXP-7?" [P=user:jordan.baird]
	@curl -fs -X POST "$(API)/api/v1/ask" -H 'Content-Type: application/json' -H 'X-KB-Principal: $(P)' \
	  -d "$$(python3 -c 'import json,sys; print(json.dumps({"tenant":"$(TENANT)","question":sys.argv[1]}))' "$(Q)")" | python3 -m json.tool

why: ## Causal trace: make why ID=jira.EXP-7
	@curl -fs -H 'X-KB-Principal: $(P)' "$(API)/api/v1/causal/why/$(ID)?tenant=$(TENANT)" | python3 -m json.tool

impact: ## What-if impact of changing an entity: make impact ID=confluence.page-310002.dec-1 FIELDS=statement
	@curl -fs -X POST "$(API)/api/v1/causal/impact" -H 'Content-Type: application/json' -H 'X-KB-Principal: $(P)' \
	  -d '{"tenant":"$(TENANT)","entity_id":"$(ID)","fields":["$(or $(FIELDS),*)"]}' | python3 -m json.tool

stale: ## Dependencies based on superseded or deleted versions
	@curl -fs -H 'X-KB-Principal: $(P)' "$(API)/api/v1/causal/stale?tenant=$(TENANT)" | python3 -m json.tool
