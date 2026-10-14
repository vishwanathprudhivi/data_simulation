# simcore developer commands (see CLAUDE.md).
PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: setup lint format test test-isaac test-redis coverage schemas api worker frontend frontend-build frontend-types-check e2e clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev,redis,service]"
	-$(BIN)/pre-commit install 2>/dev/null || true

lint:
	$(BIN)/ruff check src domains services tests
	$(BIN)/ruff format --check src domains services tests
	$(BIN)/mypy
	$(BIN)/lint-imports

format:
	$(BIN)/ruff format src domains services tests
	$(BIN)/ruff check --fix src domains services tests

test:
	$(BIN)/pytest -m "not isaac and not redis and not live_llm"

coverage:
	$(BIN)/pytest -m "not isaac and not redis and not live_llm" --cov=simcore --cov-report=term-missing

test-isaac:
	$(BIN)/pytest -m isaac

test-redis:  # needs SIMCORE_REDIS_URL, e.g. redis://localhost:6379/0
	$(BIN)/pytest -m redis

schemas:
	$(BIN)/simcore schemas export --out schemas

api:
	SIMCORE_EXAMPLES=$(PWD)/examples $(BIN)/python -m services.api

worker:  # standalone worker (needs SIMCORE_BROKER=redis and SIMCORE_REDIS_URL)
	$(BIN)/python -m services.workers --pool cpu

frontend:  # dev server on :5173 proxying /api to `make api`
	cd frontend && npm install --no-audit --no-fund && npm run gen:types && npm run dev

frontend-build:  # types from schemas/, typecheck, production bundle in frontend/dist
	cd frontend && npm ci --no-audit --no-fund && npm run build

frontend-types-check:  # generated TS types must match schemas/ (run after `make schemas`)
	cd frontend && npm run gen:types && git diff --exit-code -- src/generated

e2e: frontend-build  # Playwright AC-UI-01/02 against the API serving the built SPA
	cd frontend && npx playwright test

clean:
	rm -rf runs .pytest_cache .mypy_cache .ruff_cache .import_linter_cache
