# Running on Kubernetes

The Helm chart is in `deploy/helm/uk-banking-agent-platform`. Argo CD manifests are in `deploy/argocd`.

## Local cluster (kind)

```bash
make kind-up     # kind cluster + locally built images
make kind-test   # helm install, browser e2e through the web app, NetworkPolicy checks
kubectl -n bank port-forward svc/bank-uk-banking-agent-platform-web 3081:8080   # then open http://localhost:3081
make kind-down
```

`values-kind.yaml` runs one replica of everything, in-cluster Redis, the demo IdP, and the offline model fallback. It
needs no API key. CI runs the same smoke test on every push.

## What the chart deploys

| Component | Replicas (default) | Probes | Notes |
|---|---|---|---|
| web (nginx) | 2 | `/healthz` | Only entry point. Proxies `/api` to the gateway and `/auth` to the demo IdP when enabled. |
| gateway | 2, HPA 2–6 | live `/healthz`, ready `/readyz` (Redis + agent) | |
| agent | 2, HPA 2–10 | live `/healthz`, ready `/readyz` (Redis + 3 MCP servers) | 90s grace period so LLM tool loops can finish. Only pod holding the model API key. |
| accounts / transactions / service MCP | 2 each | `/healthz` | No egress at all beyond DNS. |
| demo IdP | off | `/healthz` | Dev only. The app refuses to start when `environment: prod`. |
| Redis | off | TCP | Dev only. The chart refuses `redis.deploy` in prod. |

Every pod:
- runs as non-root with a read-only root filesystem, all capabilities dropped, and the `RuntimeDefault` seccomp profile
- has no service-account token
- has a `preStop` drain and rolling updates with `maxUnavailable: 0`
- spreads across nodes
- has a PodDisruptionBudget

## NetworkPolicy

The release starts from **default deny** (ingress and egress) and then allows only:

```
ingress controller ─▶ web ─▶ gateway ─▶ agent ─▶ accounts-mcp / transactions-mcp / service-mcp
                       └──▶ demo IdP ◀──┘  │
                     gateway, agent ─▶ Redis
                     agent ─▶ HTTPS outside the cluster (LLM API)
                     gateway ─▶ HTTPS outside the cluster (IdP JWKS; only when the demo IdP is off)
                     monitoring namespace ─▶ :9464 on every pod;  every pod ─▶ OTLP collector (when configured)
```

`deploy/kind/smoke-test.sh` proves both sides on a real cluster:
- **allowed paths work:** gateway → agent, and agent → each MCP server
- **everything else is blocked:**
  - gateway → MCP server
  - MCP server → agent
  - MCP server → internet
  - an unlabelled pod → gateway

Kubernetes NetworkPolicy can't filter by hostname. For "the agent may reach only api.anthropic.com", add an egress
gateway or an FQDN-aware policy (for example Cilium).

## Secrets

The chart expects a Secret with `BANK_CONTEXT_TOKEN_SECRET`, `BANK_INTERNAL_SERVICE_TOKEN` and `ANTHROPIC_API_KEY`.
- **Production:** `externalSecrets.enabled: true` creates an `ExternalSecret` from a `ClusterSecretStore` (Azure Key
  Vault, AWS Secrets Manager and so on).
- **Alternative:** `secrets.existingSecret` points at a Secret you manage yourself.
- **Local only:** `secrets.create`. The chart refuses it in prod.

Each pod gets only the keys it needs; the model API key reaches the agent only.

## Monitoring

With kube-prometheus-stack installed, set `monitoring.serviceMonitor`, `monitoring.prometheusRule` and
`monitoring.grafanaDashboard` to `enabled: true`. The rules and the dashboard are the same files Docker Compose uses
(`files/rules`, `files/dashboards`). See [slo.md](slo.md).

## GitOps with Argo CD

1. `kubectl apply -f deploy/argocd/project.yaml -f deploy/argocd/application-dev.yaml -f deploy/argocd/application-prod.yaml`
2. The repository is private. Give Argo CD read access with a GitHub App or a deploy key:
   `argocd repo add https://github.com/abdihakim-said/uk-banking-agent-platform.git --github-app-id ...`
3. Create the `ghcr-pull` image pull secret in both namespaces (the GHCR packages are private).

On every merge to `main`, CI:
- builds the images once
- pushes them to GHCR as `sha-<commit>`
- commits that tag into `values-dev.yaml`

Argo CD then syncs dev automatically.

**Production is promoted by a pull request** that sets the same tag in `values-prod.yaml`, followed by a manual sync,
which only the `prod-deployers` role can do. The image running in prod is byte-for-byte the one tested in dev.

Not verified here: Argo CD itself hasn't been run against a cluster. The manifests are schema-validated in CI.
