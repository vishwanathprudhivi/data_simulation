# simcore developer commands (see CLAUDE.md).
PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: setup lint format test test-isaac test-redis coverage schemas api frontend e2e clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev,redis]"
	-$(BIN)/pre-commit install 2>/dev/null || true

lint:
	$(BIN)/ruff check src domains tests
	$(BIN)/ruff format --check src domains tests
	$(BIN)/mypy
	$(BIN)/lint-imports

format:
	$(BIN)/ruff format src domains tests
	$(BIN)/ruff check --fix src domains tests

test:
	$(BIN)/pytest -m "not isaac and not redis and not live_llm"

coverage:
	$(BIN)/pytest -m "not isaac and not redis and not live_llm" --cov=simcore --cov-report=term-missing

test-isaac:
	$(BIN)/pytest -m isaac

test-redis:
	$(BIN)/pytest -m redis

schemas:
	$(BIN)/simcore schemas export --out schemas

api:
	@echo "The API service arrives in milestone M7 (BRD 12)." && exit 1

frontend:
	@echo "The frontend arrives in milestone M11 (BRD 12)." && exit 1

e2e:
	@echo "Playwright e2e tests arrive in milestone M11 (BRD 12)." && exit 1

clean:
	rm -rf runs .pytest_cache .mypy_cache .ruff_cache .import_linter_cache
