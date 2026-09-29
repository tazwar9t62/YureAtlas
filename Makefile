SHELL := /bin/bash
UV := $(shell command -v uv 2>/dev/null || echo .tools/bin/uv)
COMPOSE ?= docker compose

.PHONY: setup up down native-setup migrate api worker web test contract
setup:
	@python3 scripts/setup.py
	$(COMPOSE) build
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
	$(UV) run pytest
	cd apps/web && pnpm typecheck
contract:
	$(UV) run python scripts/export_openapi.py
	cd apps/web && pnpm generate
