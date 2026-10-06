.PHONY: install test lint eval eval-claude demo keys token up down e2e

PY := .venv/bin/python

install:
	uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[dev,e2e]"

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check src tests scripts e2e && .venv/bin/mypy src

eval:
	.venv/bin/bank-eval

eval-claude:
	.venv/bin/bank-eval --provider anthropic --report eval-report-claude.json

demo:
	$(PY) scripts/demo.py

keys:
	$(PY) scripts/dev_idp.py keys

token:  ## token from the running demo IdP (via the web proxy)
	@curl -s -X POST localhost:$(or $(WEB_PORT),3080)/auth/token -H 'content-type: application/json' \
	  -d '{"customer_id": "$(or $(CUSTOMER),C001)"}' | $(PY) -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'

up:
	docker compose up --build -d
	@echo "Web app: http://localhost:$(or $(WEB_PORT),3080)"

e2e:  ## browser test against the running stack (make up first)
	$(PY) -m pytest e2e -q

down:
	docker compose down
