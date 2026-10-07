#!/usr/bin/env bash
# Deploy the chart to a kind cluster and prove it works: browser e2e through the web app, allowed paths open,
# and NetworkPolicy blocking everything else. Used by `make kind-test` and CI.
set -euo pipefail
CTX=${KIND_CONTEXT:-kind-bank-assistant}
NS=${NAMESPACE:-bank}
P=bank-uk-banking-agent-platform
K="kubectl --context $CTX -n $NS"
PY=${PYTHON:-.venv/bin/python}

helm upgrade --install bank deploy/helm/uk-banking-agent-platform -f deploy/helm/uk-banking-agent-platform/values-kind.yaml \
  --namespace "$NS" --create-namespace --kube-context "$CTX" --wait --timeout 5m
$K get pods

$K port-forward "svc/$P-web" 3081:8080 >/tmp/bank-pf.log 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -sf localhost:3081/healthz >/dev/null && break; sleep 1; done
WEB_URL=http://localhost:3081 "$PY" -m pytest e2e -q

fail=0
probe() {  # probe <from-deployment> <url> <expect: open|blocked>
  local out
  out=$($K exec "deploy/$1" -- python -c "
import urllib.request as u, urllib.error as e
try: u.urlopen('$2', timeout=3); print('open')
except e.HTTPError: print('open')
except Exception: print('blocked')")
  if [[ "$out" == "$3" ]]; then echo "ok    $1 -> $2 is $3"; else echo "FAIL  $1 -> $2 is $out, expected $3"; fail=1; fi
}
probe "$P-gateway" "http://$P-agent:8001/healthz" open
probe "$P-agent" "http://$P-accounts-mcp:9101/healthz" open
probe "$P-agent" "http://$P-service-mcp:9103/healthz" open
probe "$P-gateway" "http://$P-accounts-mcp:9101/healthz" blocked
probe "$P-accounts-mcp" "http://$P-agent:8001/healthz" blocked
probe "$P-accounts-mcp" "http://$P-gateway:8000/healthz" blocked
probe "$P-transactions-mcp" "https://example.com" blocked
if $K run np-probe --image=busybox:1.36 --restart=Never --rm -i --quiet -- \
     wget -q -T 3 -O- "http://$P-gateway:8000/healthz" >/dev/null 2>&1; then
  echo "FAIL  unlabelled pod -> gateway is open, expected blocked"; fail=1
else
  echo "ok    unlabelled pod -> gateway is blocked"
fi
exit $fail
