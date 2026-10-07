#!/usr/bin/env bash
# Deploy the chart to a kind cluster and prove it works: browser e2e through the web app, allowed paths open,
# and NetworkPolicy blocking everything else. Used by `make kind-test` and CI.
set -euo pipefail
CTX=${KIND_CONTEXT:-kind-bank-assistant}
NS=${NAMESPACE:-bank}
P=bank-uk-banking-agent-platform
K="kubectl --context $CTX -n $NS"
PY=${PYTHON:-.venv/bin/python}

# mTLS: one certificate per component from a throwaway dev CA, as "<fullname>-<component>-tls" Secrets.
# Created once and reused: regenerating the CA on every run would leave running pods with certificates from the old
# CA, and mTLS would (correctly) refuse them.
[ -f .dev-certs-kind/ca.crt ] || "$PY" scripts/dev_certs.py --out .dev-certs-kind --fullname "$P" --namespace "$NS" >/dev/null
kubectl --context "$CTX" create namespace "$NS" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
for svc in web gateway agent demo-idp accounts-mcp transactions-mcp service-mcp redis; do
  $K create secret generic "$P-$svc-tls" --from-file=.dev-certs-kind/$svc --dry-run=client -o yaml \
    | kubectl --context "$CTX" apply -f - >/dev/null
done

helm upgrade --install bank deploy/helm/uk-banking-agent-platform -f deploy/helm/uk-banking-agent-platform/values-kind.yaml \
  --namespace "$NS" --create-namespace --kube-context "$CTX" --wait --timeout 5m
$K get pods

$K port-forward "svc/$P-web" 3081:8080 >/tmp/bank-pf.log 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -sf localhost:3081/healthz >/dev/null && break; sleep 1; done
WEB_URL=http://localhost:3081 "$PY" -m pytest e2e -q

# Replicas share runtime state: dispute the same payment several times through the real app. With per-replica
# memory (the old bug), roughly half the repeats would be accepted by the replica that hadn't seen the first one.
"$PY" - <<'PYEOF'
import sys, httpx
base = "http://localhost:3081"
c = httpx.Client(timeout=60)
token = c.post(f"{base}/auth/token", json={"customer_id": "C001"}).json()["access_token"]
h = {"Authorization": f"Bearer {token}"}
def say(text, sid=None):
    body = {"message": text} | ({"session_id": sid} if sid else {})
    r = c.post(f"{base}/api/v1/chat", json=body, headers=h).json()
    return r["session_id"], r["reply"]
accepted = refused = 0
for attempt in range(6):
    sid, reply = say("I don't recognise the Spotify payment, please dispute it")
    if "Reply YES" in reply:
        _, reply = say("yes", sid)
    accepted += "has been submitted" in reply
    refused += "already has an open dispute" in reply
# Exactly one dispute may ever exist: accepted once now, or already open from an earlier run (state is in Redis).
print(f"duplicate dispute attempts: {accepted} accepted, {refused} refused as duplicates (at most 1 accepted)")
sys.exit(0 if accepted <= 1 and accepted + refused == 6 else 1)
PYEOF

fail=0
probe() {  # probe <from-deployment> <url> <expect: open|blocked> [nocert]: calls with the pod's own mTLS identity
  local out
  out=$($K exec "deploy/$1" -- python -c "
import ssl, urllib.request as u, urllib.error as e
ctx = ssl.create_default_context(cafile='/certs/ca.crt')
if '${4:-}' != 'nocert': ctx.load_cert_chain('/certs/tls.crt', '/certs/tls.key')
try: u.urlopen('$2', timeout=3, context=ctx); print('open')
except e.HTTPError: print('open')
except Exception: print('blocked')")
  if [[ "$out" == "$3" ]]; then echo "ok    $1 -> $2 is $3"; else echo "FAIL  $1 -> $2 is $out, expected $3"; fail=1; fi
}
probe "$P-gateway" "https://$P-agent:8001/healthz" open
probe "$P-agent" "https://$P-accounts-mcp:9101/healthz" open
probe "$P-agent" "https://$P-service-mcp:9103/healthz" open
probe "$P-gateway" "https://$P-agent:8001/healthz" blocked nocert   # NetworkPolicy allows it; mTLS refuses it
probe "$P-gateway" "https://$P-accounts-mcp:9101/healthz" blocked
probe "$P-accounts-mcp" "https://$P-agent:8001/healthz" blocked
probe "$P-accounts-mcp" "https://$P-gateway:8000/healthz" blocked
probe "$P-transactions-mcp" "http://example.com" blocked   # plain HTTP, so only NetworkPolicy can stop it
if $K run np-probe --image=busybox:1.36 --restart=Never --rm -i --quiet -- \
     wget -q -T 3 -O- "http://$P-gateway:8000/healthz" >/dev/null 2>&1; then
  echo "FAIL  unlabelled pod -> gateway is open, expected blocked"; fail=1
else
  echo "ok    unlabelled pod -> gateway is blocked"
fi
# The audit chain written by the gateway and agent during these tests verifies.
if $K exec "deploy/$P-gateway" -- bank-audit verify; then echo "ok    audit chain intact"; else echo "FAIL  audit chain"; fail=1; fi
exit $fail
