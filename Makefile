SHELL := /bin/bash
UV := $(shell command -v uv 2>/dev/null || echo .tools/bin/uv)
COMPOSE ?= docker compose

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
	cd apps/web && pnpm install --frozen-lockfile
migrate:
	$(UV) run alembic upgrade head
api:
	$(UV) run uvicorn yureatlas.api:app --host 127.0.0.1 --port 8000
worker:
	$(UV) run python -m yureatlas.worker
web:
	cd apps/web && pnpm dev
test:
	$(UV) run python scripts/test_backend.py
	cd apps/web && pnpm typecheck
	cd apps/web && pnpm test:e2e
contract:
	$(UV) run python scripts/export_openapi.py
	cd apps/web && pnpm generate

.PHONY: demo bootstrap-real ingest train forecast evaluate backtest backup restore
demo bootstrap-real ingest train forecast evaluate backtest:
	$(COMPOSE) exec -T api python -m yureatlas.cli $@ $(ARGS) --wait
backup:
	python3 scripts/backup.py backup
restore:
	python3 scripts/backup.py restore "$(BACKUP)"
