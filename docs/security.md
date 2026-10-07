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

## What's still not covered

- **Strong customer authentication step-up** for high-risk actions. Confirmation is a typed or clicked "yes" inside
  an authenticated session.
- **Penetration test and formal threat model.**
- **Egress to the model API by hostname.** NetworkPolicy works on IP ranges; use an egress proxy or Cilium FQDN
  policies.
- **Per-service authorisation from certificate identity.** mTLS proves "a service with a certificate from our CA";
  which service may call which is enforced by NetworkPolicy and the internal service token. A mesh
  AuthorizationPolicy, or SPIFFE IDs, would add identity-based authorisation.
