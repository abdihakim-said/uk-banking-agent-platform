.PHONY: install test lint eval eval-claude demo keys token up down

PY := .venv/bin/python

install:
	uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check src tests scripts && .venv/bin/mypy src

eval:
	.venv/bin/bank-eval

eval-claude:
	.venv/bin/bank-eval --provider anthropic --report eval-report-claude.json

demo:
	$(PY) scripts/demo.py

keys:
	$(PY) scripts/dev_idp.py keys

token:
	@$(PY) scripts/dev_idp.py token --customer $(or $(CUSTOMER),C001)

up: keys
	docker compose up --build -d

down:
	docker compose down
