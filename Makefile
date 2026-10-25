SHELL := /bin/bash
UV := $(shell command -v uv 2>/dev/null || echo .tools/bin/uv)
COMPOSE ?= docker compose
PYTHON ?= .venv/bin/python
PNPM ?= $(if $(wildcard .tools/pnpm/node_modules/pnpm/bin/pnpm.cjs),node $(CURDIR)/.tools/pnpm/node_modules/pnpm/bin/pnpm.cjs,pnpm)
NEXT_BUILD_ARGS ?=
# Builds regenerate .next/types; even `make -j verify` must not race TypeScript.
.NOTPARALLEL:

.PHONY: setup up down native-setup migrate api worker web test contract
setup:
	@python3 scripts/setup.py
	CODE_VERSION=$$(git rev-parse HEAD) $(COMPOSE) build
up:
	$(COMPOSE) up -d db
	$(COMPOSE) run --rm api alembic upgrade head
	$(COMPOSE) up -d api worker web
down:
	$(COMPOSE) down
native-setup:
	@test -f .env || cp .env.example .env
	$(UV) sync --frozen
	$(MAKE) check-web-tools
	$(PNPM) --dir apps/web install --frozen-lockfile
migrate:
	$(UV) run alembic upgrade head
api:
	$(UV) run uvicorn yureatlas.api:app --host 127.0.0.1 --port 8000
worker:
	$(UV) run python -m yureatlas.worker
web:
	$(PNPM) --dir apps/web dev

.PHONY: check-python check-web-tools lint test-unit test-integration test-migrations typecheck build contract-check verify
check-python:
	@$(PYTHON) -c 'import sys; assert sys.version_info[:2] == (3, 12), "Use Python 3.12 (see .python-version)"'
check-web-tools:
	@node -e 'if (process.versions.node.split(".")[0] !== "24") throw new Error("Use Node 24 (see .nvmrc)")'
	@test "$$($(PNPM) --version)" = "10.33.0" || { echo "Use pnpm 10.33.0 (apps/web/package.json)"; exit 1; }
lint: check-python
	$(PYTHON) -m ruff check .
test-unit: check-python
	YUREATLAS_INTEGRATION=0 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 $(PYTHON) -m pytest -q -m 'not integration'
test-integration: check-python
	$(PYTHON) scripts/test_backend.py
test-migrations: check-python
	$(PYTHON) scripts/test_backend.py --migration-check
test: test-unit test-integration
typecheck: check-web-tools
	$(PNPM) --dir apps/web typecheck
build: check-web-tools
	NEXT_TELEMETRY_DISABLED=1 $(PNPM) --dir apps/web build $(NEXT_BUILD_ARGS)
contract-check: check-python check-web-tools
	$(PYTHON) scripts/export_openapi.py --check
	$(PNPM) --dir apps/web contract:check
verify: lint test test-migrations typecheck contract-check build
contract: check-python check-web-tools
	$(PYTHON) scripts/export_openapi.py
	$(PNPM) --dir apps/web generate

.PHONY: demo bootstrap-real ingest train forecast evaluate backtest backup restore
demo bootstrap-real ingest train forecast evaluate backtest:
	$(COMPOSE) exec -T api python -m yureatlas.cli $@ $(ARGS) --wait
backup:
	python3 scripts/backup.py backup
restore:
	python3 scripts/backup.py restore "$(BACKUP)"
