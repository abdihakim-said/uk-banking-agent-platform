# Security controls

How the platform protects data in transit, the session store and the record of what happened. Each control is tested
in CI, both in unit tests and on a real Kubernetes cluster (`deploy/kind/smoke-test.sh`).

## Mutual TLS on every internal hop

```
browser ─HTTP─▶ web (nginx) ═mTLS═▶ gateway ═mTLS═▶ agent ═mTLS═▶ accounts / transactions / service MCP servers
                    ╚═mTLS═▶ demo IdP ◀═mTLS═╝       gateway, agent, MCP servers ═mTLS + password═▶ Redis
```

- **Certificates:** every component has its own certificate from an internal CA, valid for both server and client
  authentication. Each side verifies the other.
- **Who's refused:** a caller with no certificate, or one from another CA, can't complete the handshake. That holds
  even when a NetworkPolicy would let its packets through, so the network rules and mTLS are independent layers.
- **Where certificates come from:**
  - Kubernetes: `tls.certManager.enabled: true` requests one `Certificate` per component from your issuer (Vault
    PKI, AD CS, AWS Private CA and so on). cert-manager renews them every 90 days.
  - Locally: `scripts/dev_certs.py` creates a throwaway CA. `make up` and the kind smoke test call it.
- **Probes and scraping:** Kubernetes probes and Prometheus can't present client certificates, so `/healthz`,
  `/readyz` and `/metrics` are served on a separate internal port (9464). `/readyz` performs a real mTLS call to the
  next service and a Redis `PING`, so "ready" means the secure path works.
- **Implementation:** Python services run uvicorn with `ssl_cert_reqs=CERT_REQUIRED`, and nginx uses
  `proxy_ssl_certificate` / `proxy_ssl_verify`. The MCP client uses a custom `httpx2` client: MCP SDK 2.x runs on
  httpx2, not httpx.
- **Service mesh alternative:** in a cluster with Istio or Linkerd, mesh mTLS (`PeerAuthentication: STRICT`) can
  replace the in-app TLS. Set `tls.enabled: false` and let the mesh do it. In-app mTLS is used here so the same
  guarantee holds in docker compose and on any cluster.

## Redis: TLS, client certificates and a password

- TLS only: there's no plain-text port (`--port 0 --tls-port 6379`).
- `--tls-auth-clients yes`: clients must present a certificate from the internal CA.
- `--requirepass`: the password comes from the secret store (`BANK_REDIS_PASSWORD`).
- In production, use a managed Redis with TLS and AUTH. The app refuses to start in `prod` unless the URL is
  `rediss://` and a password is set.

Redis holds the PII placeholder vault, so all three layers matter.

## Tamper-evident audit log

| | |
|---|---|
| **What's recorded** | Sign-in failures, rate limiting, refused sessions, every chat turn's route and outcome, kinds of personal data hidden, input-guard blocks, actions proposed, confirmed, cancelled, abandoned, expired or executed (with the reference), output-guard replacements, spent budgets |
| **What's never recorded** | Message text, or the values of any personal data |
| **Integrity** | Each entry carries an HMAC-SHA256 over its content and the previous entry's MAC, keyed by `BANK_AUDIT_HMAC_KEY`. Editing, deleting, inserting, reordering or truncating entries is detected, and without the key nobody can rebuild a valid chain. |
| **Many replicas** | Appends are serialised with an optimistic Redis transaction, so concurrent writers keep one linear chain (tested with 30 concurrent writes) |
| **Checking it** | `bank-audit verify` and `bank-audit tail` (`make audit` locally). CI verifies the chain after the browser tests and on the Kubernetes cluster. |

**In production**, also ship the stream (or the `"event": "audit"` log lines) to write-once storage, such as S3 Object
Lock or Azure immutable blob storage, for the retention period your regulator requires, and keep the HMAC key in an
HSM-backed key vault.

## Strong customer authentication for high-risk actions

Replacing a card or changing contact details needs a one-time code on top of the customer's "yes", in the style of
PSD2 SCA.

1. The agent marks the pending action high-risk and computes its **binding**: a SHA-256 fingerprint of the tool and
   its exact arguments. This is the "dynamic linking" PSD2 requires.
2. The IdP sends a 6-digit code to the registered phone (shown as a simulated SMS in the demo). Codes are valid for 5
   minutes, single use, and three wrong codes end the challenge.
3. A correct code returns a **step-up token** (`acr=urn:bank:sca:otp`, `txn=<binding>`, valid 5 minutes).
4. The gateway verifies the token: signature, issuer, audience, same customer, freshness.
5. The agent runs the action only if `txn` matches the action it's holding. Otherwise the action stays pending and
   nothing changes.

Tested: refused without a code, with a code for another action, for another customer, when stale, and when the
token isn't SCA. The browser test covers the whole flow, including a wrong code.

## Egress allowlist

The agent has no direct route to the internet. In compose it's on the internal network only; in Kubernetes,
NetworkPolicy allows it to reach only the egress proxy. The proxy (Squid, non-root, read-only) allows HTTPS
`CONNECT` to the hostnames in `egressProxy.allowedDomains` (by default `api.anthropic.com`) and refuses everything
else. Even a fully hijacked model can't send data to an attacker's server.

Tested in compose and on the kind cluster: model API reachable, `example.com` refused by the proxy, and going around
the proxy blocked by the network. Managed alternatives: Azure Firewall or AWS Network Firewall FQDN rules, or Cilium
FQDN policies.

## Automated security testing

These checks run in CI on every push. **They are not a penetration test**, which needs an independent tester.

| Check | Tool | What it covers |
|---|---|---|
| AI red team | `bank-redteam` (`evals/redteam.yaml`) | 39 attacks in 10 categories: instruction override, another customer's data, bypassing confirmation and SCA, PII extraction, regulated advice, social engineering, encoded and multilingual attacks, indirect injection through a poisoned transaction description, tool abuse. An attack fails the build if it changes state, leaks data or secrets, or gets raw PII to a model; success is judged from the bank's own state, not from the reply. Run it against Claude with `bank-redteam --provider anthropic`. |
| Static analysis | Semgrep (`p/python`, `p/security-audit`), ruff `S` rules (Bandit) | Injection, unsafe deserialisation, weak crypto, subprocess misuse… |
| Secrets | Gitleaks over the whole git history | Keys and tokens committed by mistake (`.gitleaks.toml` allowlists only test fixtures and dev placeholders) |
| Dependencies | pip-audit | Known CVEs in installed packages |
| Images | Trivy (app, web, egress proxy) | OS and library CVEs, failing on fixable HIGH or CRITICAL |
| Infrastructure as code | Trivy config | Dockerfiles, compose file and Helm chart misconfigurations |
| Running app | OWASP ZAP baseline | Missing security headers, information disclosure and similar issues in the live web app |

## What's still not covered

- **An independent penetration test and a formal threat model.**
- **Per-service authorisation from certificate identity.** mTLS proves "a service with a certificate from our CA";
  which service may call which is enforced by NetworkPolicy and the internal service token. A mesh
  AuthorizationPolicy, or SPIFFE IDs, would add identity-based authorisation.
