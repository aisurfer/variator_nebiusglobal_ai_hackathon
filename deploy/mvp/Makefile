# VARIATOR MVP stack: frequent commands. Run `make -C deploy/mvp help` for the list.
MVP_DIR     := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
ROOT        := $(abspath $(MVP_DIR)/../..)
# -p: the project name is explicit, so a COMPOSE_PROJECT_NAME in the environment cannot redirect the commands
COMPOSE     ?= docker compose -p variator-mvp -f $(MVP_DIR)/compose.yaml
MIN_FREE_GB ?= 10
# reindex: empty TENANT = every tenant in the journal (the MVP keeps one tenant per future branch)
TENANT      ?=
# projections rebuilt from Postgres (the consumers of kb.worker)
PROJECTIONS ?= descriptors search graph
# drain processes for search (default in kb: half the cores, up to 8); lower it on a small server
PARTS       ?=
API_IMAGE   ?= variator-mvp-api:dev
ARTIFACTS_VOLUME ?= variator-mvp-artifacts
SET         ?= $(notdir $(abspath $(DIR)))
# seed: a corpus delivery (fixtures/timeline.jsonl.gz, config/sources.yaml, config/people.yaml) and its tenant
DELIVERY    ?=
SEED_TENANT ?= $(or $(TENANT),company)
GIT_COMMIT  ?= $(shell git -C $(ROOT) rev-parse HEAD 2>/dev/null)
CONSUMER_ARGS = $(foreach c,$(PROJECTIONS),--consumer $(c))
# precompute and release (scripts/mvp/pipeline.py)
PY           ?= $(ROOT)/.venv/bin/python
RELEASE_ROOT ?= $(or $(MVP_RELEASE_ROOT),/tmp/variator-$(shell id -u)/mvp-release)
ARGS         ?=
# precompute: the costs it may spend; PRECOMPUTE_FLAGS= runs only the stages that need none
PRECOMPUTE_FLAGS ?= --llm --kb --publish
# app-rebuild: the services rebuilt and restarted alone (--no-deps); the KB services keep running
S_APP        ?= variator-api workspace

.DEFAULT_GOAL := help
URL          ?= http://127.0.0.1:8088
SPEC         ?= tests/e2e/demo.spec.ts tests/e2e/tour.spec.ts

.PHONY: help config disk-guard up down ps logs dump-kb restore-kb reindex seed seed-artifacts precompute precompute-dry precompute-status app-rebuild images demo drop-artifacts

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-15s %s\n",$$1,$$2}'

config: ## Validate compose.yaml (no images, no containers)
	$(COMPOSE) config -q

disk-guard: ## Refuse when the Docker data disk has less than MIN_FREE_GB free (default 10)
	@root=$$(docker info -f '{{.DockerRootDir}}' 2>/dev/null); root=$${root:-/}; \
	free_kb=$$(df -Pk "$$root" 2>/dev/null | awk 'NR==2 {print $$4}'); \
	if [ -z "$$free_kb" ]; then root=/; free_kb=$$(df -Pk / | awk 'NR==2 {print $$4}'); fi; \
	free_gb=$$((free_kb / 1048576)); \
	if [ "$$free_kb" -lt $$(( $(MIN_FREE_GB) * 1048576 )) ]; then \
	  echo "disk-guard: $$free_gb GB free on $$root, $(MIN_FREE_GB) GB needed: refused"; exit 1; \
	fi; \
	echo "disk-guard: $$free_gb GB free on $$root (minimum $(MIN_FREE_GB) GB)"

up: disk-guard ## Build the images and start the stack; wait until every service is healthy
	VARIATOR_GIT_COMMIT=$(GIT_COMMIT) $(COMPOSE) up -d --build --wait --wait-timeout 900
	@echo "up: done. Default URL: http://127.0.0.1:8088 (change it with MVP_* in .env)"

down: ## Stop and remove the containers (the data volumes stay)
	$(COMPOSE) --profile tools down

ps: ## Show the service status
	$(COMPOSE) --profile tools ps

logs: ## Follow the logs: make logs [S=kb-api]
	$(COMPOSE) logs -f --tail=200 $(S)

dump-kb: ## Write the KB database to a pg_dump custom-format file: make dump-kb OUT=kb.dump
	@[ -n "$(OUT)" ] || { echo "dump-kb: OUT=<file> is required"; exit 2; }
	$(COMPOSE) exec -T postgres pg_dump -Fc -U kb -d kb > "$(OUT).part"
	mv "$(OUT).part" "$(OUT)"

restore-kb: disk-guard ## Replace the KB database with a pg_dump -Fc file: make restore-kb DUMP=kb.dump
	@[ -n "$(DUMP)" ] || { echo "restore-kb: DUMP=<file made by pg_dump -Fc> is required"; exit 2; }
	@[ -f "$(DUMP)" ] || { echo "restore-kb: no such file: $(DUMP)"; exit 2; }
	@head -c 5 "$(DUMP)" | grep -q PGDMP || { echo "restore-kb: $(DUMP) is not a pg_dump -Fc file"; exit 2; }
	$(COMPOSE) up -d --wait postgres
	$(COMPOSE) stop kb-worker kb-api
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U kb -d postgres \
	  -c 'DROP DATABASE IF EXISTS kb WITH (FORCE)' -c 'CREATE DATABASE kb OWNER kb'
	$(COMPOSE) exec -T postgres pg_restore -U kb -d kb --no-owner --no-acl --exit-on-error < "$(DUMP)"
	@# the outbox keeps the xids of the dumping cluster: spend as many here, or the workers wait for them forever
	@n=$$($(COMPOSE) exec -T postgres psql -U kb -d kb -Atc "SELECT greatest(0, coalesce(max(xid::text::bigint), 0) \
	  - pg_current_xact_id()::text::bigint + 1) FROM outbox") || exit 1; \
	printf '%s\n' "SELECT 'SELECT pg_current_xact_id()' FROM generate_series(1, $$n) \gexec" \
	  | $(COMPOSE) exec -T postgres psql -q -o /dev/null -U kb -d kb || exit 1; \
	echo "restore-kb: $$n transaction ids spent (the outbox xids of the dump)"
	$(COMPOSE) up -d --wait kb-api kb-worker
	@echo "restore-kb: done. Now rebuild search and graph from Postgres: make reindex"

reindex: ## Rebuild search (OpenSearch) and graph (Memgraph) from Postgres: make reindex [TENANT=company] [PARTS=2]
	@tenants="$(TENANT)"; \
	if [ -z "$$tenants" ]; then \
	  tenants=$$($(COMPOSE) exec -T postgres psql -U kb -d kb -Atc 'SELECT tenant_id FROM tenant ORDER BY 1') || exit 1; \
	fi; \
	[ -n "$$tenants" ] || { echo "reindex: no tenants in the journal"; exit 1; }; \
	for t in $$tenants; do \
	  echo "reindex: $$t ($(PROJECTIONS))"; \
	  $(COMPOSE) exec -T kb-api python -m kb.cli reindex --tenant "$$t" $(CONSUMER_ARGS) || exit 1; \
	  $(COMPOSE) exec -T kb-api python -m kb.cli drain --tenant "$$t" --until-idle \
	    $(CONSUMER_ARGS) $(if $(PARTS),--parts $(PARTS)) || exit 1; \
	done

seed: disk-guard ## Load a corpus delivery into a tenant from scratch: make seed DELIVERY=path/delivery [TENANT=company] [PARTS=4]
	@[ -n "$(DELIVERY)" ] || { echo "seed: DELIVERY=<delivery directory> is required"; exit 2; }
	@for f in fixtures/timeline.jsonl.gz config/sources.yaml config/people.yaml; do \
	  [ -f "$(DELIVERY)/$$f" ] || { echo "seed: $(DELIVERY)/$$f is missing"; exit 2; }; done
	@# The worker stays off while the journal loads; then drain --until-idle projects it in parallel parts.
	$(COMPOSE) stop kb-worker
	@start=$$(date +%s); \
	$(COMPOSE) run --rm --no-deps -T \
	  -v "$(abspath $(DELIVERY))/fixtures:/srv/fixtures/$(SEED_TENANT):ro" \
	  -v "$(abspath $(DELIVERY))/config:/srv/config/tenants/$(SEED_TENANT):ro" \
	  kb-api python -m kb.cli seed --tenant "$(SEED_TENANT)" --reset --no-background || exit 1; \
	echo "seed: journal loaded in $$(( $$(date +%s) - start )) s"; start=$$(date +%s); \
	$(COMPOSE) exec -T kb-api python -m kb.cli drain --tenant "$(SEED_TENANT)" --until-idle \
	  $(CONSUMER_ARGS) $(if $(PARTS),--parts $(PARTS)) || exit 1; \
	echo "seed: projections drained in $$(( $$(date +%s) - start )) s"
	@# up, not start: a worker container made from an older image or environment is recreated
	$(COMPOSE) up -d --no-deps --no-build kb-worker

seed-artifacts: disk-guard ## Copy one artifact set into the artifacts volume: make seed-artifacts DIR=path/<set> [SET=name]
	@[ -n "$(DIR)" ] || { echo "seed-artifacts: DIR=<artifact set directory> is required"; exit 2; }
	@[ -f "$(DIR)/manifest.json" ] || { echo "seed-artifacts: $(DIR)/manifest.json is missing"; exit 2; }
	@printf '%s\n' '$(SET)' | grep -Eqx '[A-Za-z0-9][A-Za-z0-9._-]{0,127}' \
	  || { echo "seed-artifacts: bad set name: $(SET)"; exit 2; }
	@docker volume inspect $(ARTIFACTS_VOLUME) >/dev/null 2>&1 \
	  || { echo "seed-artifacts: no volume $(ARTIFACTS_VOLUME): run make up first"; exit 2; }
	docker run --rm --pull never --network none --user 0:0 --entrypoint sh \
	  -v "$(abspath $(DIR))":/src:ro -v $(ARTIFACTS_VOLUME):/dst $(API_IMAGE) \
	  -c 'set -e; rm -rf "/dst/$$1.tmp"; cp -R /src "/dst/$$1.tmp"; rm -rf "/dst/$$1"; mv "/dst/$$1.tmp" "/dst/$$1"; chmod -R a+rX "/dst/$$1"' \
	  sh '$(SET)'
	@echo "seed-artifacts: $(SET) copied; check: curl -fs http://127.0.0.1:8730/api/variator/artifacts"

precompute: disk-guard ## Run every stale stage of the precompute pipeline (H1): make precompute [ARGS="--only a,b"] [PRECOMPUTE_FLAGS=]
	$(PY) $(ROOT)/scripts/mvp/pipeline.py --root $(RELEASE_ROOT) run $(PRECOMPUTE_FLAGS) $(ARGS)

precompute-dry: ## Show each pipeline stage: fresh, run (and why), blocked or not landed; run nothing
	$(PY) $(ROOT)/scripts/mvp/pipeline.py --root $(RELEASE_ROOT) run --dry-run $(ARGS)

precompute-status: ## Show the record of each pipeline stage (time, notes) from pipeline.json
	$(PY) $(ROOT)/scripts/mvp/pipeline.py --root $(RELEASE_ROOT) status

app-rebuild: disk-guard ## Rebuild and restart only variator-api and workspace (--no-deps): make app-rebuild [S_APP=variator-api]
	VARIATOR_GIT_COMMIT=$(GIT_COMMIT) $(COMPOSE) up -d --no-deps --build --wait --wait-timeout 300 $(S_APP)
	@curl -fsS http://127.0.0.1:8730/api/variator/version; echo

images: disk-guard ## Build the three VARIATOR images from this checkout (no container restarts)
	$(COMPOSE) build kb-api variator-api workspace

demo: ## Playwright demo run against a site: make demo URL=https://<domain> (the origin; the tests open /workspace/) [SPEC=...]
	cd $(ROOT)/workspace && PLAYWRIGHT_BASE_URL=$(URL) npx playwright test $(SPEC) --project=desktop

drop-artifacts: ## Remove one artifact set from the artifacts volume: make drop-artifacts DROP=<set>
	@printf '%s\n' '$(DROP)' | grep -Eqx '[A-Za-z0-9][A-Za-z0-9._-]{0,127}' || { echo "drop-artifacts: DROP=<set> is required"; exit 2; }
	docker run --rm --pull never --network none --user 0:0 --entrypoint sh -v $(ARTIFACTS_VOLUME):/dst $(API_IMAGE) \
	  -c 'rm -rf "/dst/$$1"' sh '$(DROP)'
	@echo "drop-artifacts: $(DROP) removed"
