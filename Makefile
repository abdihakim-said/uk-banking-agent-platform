.PHONY: install test lint eval eval-claude demo keys token up down e2e kind-up kind-test kind-down rules

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

kind-up:  ## local Kubernetes cluster with the images loaded
	kind create cluster --config deploy/kind/cluster.yaml --wait 120s || true
	docker compose build
	kind load docker-image uk-banking-agent-platform:local uk-banking-agent-platform-web:local --name bank-assistant

kind-test:  ## deploy the Helm chart to kind, run browser e2e and NetworkPolicy checks
	./deploy/kind/smoke-test.sh

kind-down:
	kind delete cluster --name bank-assistant

rules:  ## regenerate SLO rules + dashboard, then validate and unit-test the rules
	$(PY) scripts/gen_slo_rules.py && $(PY) scripts/gen_dashboard.py
	docker run --rm -v "$$PWD/deploy:/deploy:ro" -w /deploy/prometheus/tests --entrypoint promtool prom/prometheus:v2.55.1 test rules slo_test.yaml
