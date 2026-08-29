# MailVerdict Helm Chart

Deploys [MailVerdict](https://github.com/frederikb96/mail-verdict) -- AI-powered email management with spam detection, a rule engine, and full-text search -- onto Kubernetes.

## Rendering from the registry

`helm template` and `helm pull` write `Pulled:` and `Digest:` lines to **stdout** when the chart
reference is an OCI URL, so piping straight into `kubectl apply -f -` fails with
`apiVersion not set, kind not set`. Strip the preamble, or render from a local copy:

```bash
helm pull oci://ghcr.io/frederikb96/charts/mail-verdict --version <version> -d ./charts
helm template mail-verdict ./charts/mail-verdict-<version>.tgz -f values.yaml | kubectl apply -f -
```

`helm pull` also requires the `-d` directory to exist already.

## Before you install: three steps

MailVerdict has no direct IMAP dependency -- it talks to mail exclusively through
[PostIMAP](https://github.com/frederikb96/postimap)'s PostgreSQL database. This chart does not
create the database or install PostIMAP for you:

1. **The shared PostgreSQL database.** Not a chart dependency for either service. See
   [`examples/cnpg-cluster.yaml`](examples/cnpg-cluster.yaml) for a worked
   [CloudNativePG](https://cloudnative-pg.io/) setup: one Cluster, PostIMAP's owner role, and
   MailVerdict's own role.
2. **PostIMAP**, its own released chart (`oci://ghcr.io/frederikb96/charts/postimap`), pointed at
   the same database. See [`examples/postimap-values.yaml`](examples/postimap-values.yaml). Its
   migrations run automatically on first start and create the `postimap_app` role that step 1's
   `GRANT` needs to already exist -- so run that `GRANT` after PostIMAP is up, not before.
3. **This chart**, pointed at the same database with MailVerdict's own role. Its own Alembic
   migrations run automatically on first start and create MailVerdict's owned tables
   (`account_prefs`, `folder_prefs`, `settings`, `verdicts`, `mail_tags`, `image_exceptions`) --
   there's no separate migration Job or Helm hook.

**PostIMAP is not a subchart.** It has its own release cadence, its own values surface, and its
own chart; bundling it here would duplicate both and drift out of sync. See
[`examples/values-production.yaml`](examples/values-production.yaml) for a realistic install of
this chart alongside it.

## Why exactly one replica

MailVerdict holds per-process in-memory state: the SSE `EventRing` (500 events per account,
monotonic IDs, replayed by `Last-Event-ID`) and the singleton PostgreSQL `LISTEN`/`NOTIFY`
consumers that drive the spam pipeline and rules engine both live in exactly one process. A
second replica would see a disjoint `EventRing` -- breaking SSE replay for any client pinned to
the other pod by a load balancer -- and would double-process every event on the shared `LISTEN`
channel, double-classifying mail and double-firing rules.

`replicaCount` is pinned to `1` by `values.schema.json` -- `--set replicaCount=2` fails validation
rather than deploying something broken. The Deployment also uses `strategy: Recreate` (not
configurable) instead of a rolling update, because briefly running an old and a new pod together
during a rollout has the exact same problem.

## Install

```bash
helm install mail-verdict oci://ghcr.io/frederikb96/charts/mail-verdict \
  --namespace mail --create-namespace \
  --set secret.create=true \
  --set secret.databaseUrl=postgresql+asyncpg://mailverdict:<password>@<host>:5432/postimap \
  --set secret.apiKey=<generated-key> \
  --set secret.anthropicApiKey=<key>
```

For anything beyond a quick test, write a values file instead -- see
[`examples/values-production.yaml`](examples/values-production.yaml) for a realistic one
(existing Secret, Ingress behind Traefik ForwardAuth, a second Ingress for programmatic clients,
`NetworkPolicy`).

### Configuration model

The image bundles `config/config.yaml` with defaults for everything. This chart's `config:` value
is a **sparse override** -- a ConfigMap rendered from it and mounted at `configOverridePath`,
deep-merged onto the image's defaults at startup. Its structure mirrors `config/config.yaml` in
the [repository](https://github.com/frederikb96/mail-verdict/blob/main/config/config.yaml)
exactly, so only set the keys you want to change:

```yaml
config:
  server:
    log_level: WARNING
  database:
    pool_size: 20
```

This is deliberately freeform. When the app gains new configuration sections in the future, set
them the same way -- no chart changes needed to use them.

Unlike a chart where the database host has no usable default, `config:` has **no required key**
here: the image's bundled `config.yaml` already resolves `server.api_key` from
`${MAIL_VERDICT_API_KEY}` and `database.url` from `${MAIL_VERDICT_DATABASE_URL}` -- both wired
from the Secret below, never duplicated in the ConfigMap.

Anything the config loader doesn't cover directly -- or that you'd rather set as a
`MAIL_VERDICT_SECTION_KEY` environment variable override instead of YAML -- goes through
`extraEnv`.

### Secrets

Three values, one Secret:

| Key (`existingSecretKeys.*`) | Env var | Required | Purpose |
|---|---|---|---|
| `databaseUrl` | `MAIL_VERDICT_DATABASE_URL` | yes | Full SQLAlchemy async URL, credentials included: `postgresql+asyncpg://mailverdict:<password>@<host>:5432/postimap`. No usable default -- the pod won't start without it. |
| `apiKey` | `MAIL_VERDICT_API_KEY` | no | The value clients must send as `X-API-Key`. Empty disables auth entirely (dev mode) -- see [Auth model](#auth-model) before leaving this unset on anything reachable outside a trusted network. |
| `anthropicApiKey` | `ANTHROPIC_API_KEY` | no | Read directly by the Anthropic SDK, not through `config.yaml`. The pod starts without it; the spam pipeline and LLM-backed rule actions fail at call time without it. |

- `secret.create: true` with `secret.databaseUrl` / `secret.apiKey` / `secret.anthropicApiKey` set
  -- the chart creates the Secret. Convenient for testing; the values end up in Helm release
  history and (if you commit the values file) in git, so avoid this for anything real.
- `existingSecret: <name>` -- reference a Secret you manage yourself (sealed-secrets,
  external-secrets, manually `kubectl create secret`, ...), with key names configurable via
  `existingSecretKeys`. Takes precedence over `secret.create`.

### Auth model

MailVerdict has no login page of its own -- it checks a single `X-API-Key` header (constant-time
comparison) on `/api/*` and `/mcp`, and nothing else. There is exactly one mechanism, deliberately:
put whatever authentication a browser needs (OIDC, basic auth, an internal SSO) in front of it at
the ingress, and have that layer inject the header. No identity provider is hard-coded into this
chart -- `ingress.annotations` is free-form.

A worked Traefik example, assuming [traefik/traefik](https://github.com/traefik/traefik-helm-chart)
with the CRD provider and some ForwardAuth-capable service already running at
`https://auth.example.com/verify`:

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: mailverdict-forwardauth
  namespace: mail
spec:
  forwardAuth:
    address: https://auth.example.com/verify
    authResponseHeaders:
      - X-Forwarded-User
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: mailverdict-inject-api-key
  namespace: mail
spec:
  headers:
    customRequestHeaders:
      X-API-Key: the-same-value-as-secret.apiKey
```

Chain both on the Ingress with the standard Traefik annotation, in order (auth first, then
inject):

```yaml
ingress:
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: >-
      mail-mailverdict-forwardauth@kubernetescrd,mail-mailverdict-inject-api-key@kubernetescrd
```

The `customRequestHeaders` value above is itself a plaintext secret sitting in a `Middleware`
object -- treat that CRD the same way you'd treat any other place holding the API key (restrict
RBAC read access to it, or generate it from a SealedSecret / external-secrets source rather than
committing it in plain YAML).

`apiIngress` is a second, independent Ingress for callers that authenticate themselves --
MCP clients, scripts -- and should not go through the ForwardAuth middleware at all, since they
already send `X-API-Key` directly. Route it at a path or host your ForwardAuth Middleware doesn't
match (a separate `IngressRoute` path exclusion, or simply a different subdomain).

Nothing in the application enforces this split; if your ingress setup makes ForwardAuth awkward
for some reason, plain API-key auth without any ForwardAuth layer works too -- disable `ingress`
and use only `apiIngress`, or drop the ForwardAuth middleware annotation from `ingress` and let
browsers send the key some other way you control.

### Readiness probe

Unlike a service whose readiness depends on having active work, MailVerdict's `/api/health`
readiness check only asserts the database is reachable and that PostIMAP's contract version
matches what the image was built against -- it does **not** depend on any mail account existing.
A fresh install with zero accounts configured becomes Ready as soon as the database check passes,
so `helm install --wait` and a normal rollout both complete without you needing to add an account
first.

`/api/health/live` (liveness) checks process-up only -- it never touches the database, so a
transient database outage doesn't cause a restart loop on top of the outage.

## Values

| Key | Default | Description |
|---|---|---|
| `replicaCount` | `1` | Pinned by `values.schema.json`. See [Why exactly one replica](#why-exactly-one-replica). |
| `image.repository` | `ghcr.io/frederikb96/mail-verdict` | Container image. |
| `image.pullPolicy` | `IfNotPresent` | |
| `image.tag` | `""` | Defaults to the chart's `appVersion`. |
| `imagePullSecrets` | `[]` | For a private GHCR repository/tag. |
| `nameOverride` / `fullnameOverride` | `""` | Standard chart name overrides. |
| `serviceAccount.create` | `true` | |
| `serviceAccount.automount` | `false` | MailVerdict never calls the Kubernetes API. |
| `serviceAccount.annotations` / `.name` | `{}` / `""` | |
| `podAnnotations` / `podLabels` | `{}` | |
| `podSecurityContext` | non-root, `fsGroup: 1000`, `seccompProfile: RuntimeDefault` | Matches the image's `appuser` (uid/gid 1000). |
| `securityContext` | read-only root filesystem, all capabilities dropped, no privilege escalation | The app writes nothing to disk at runtime beyond `/tmp`; a `tmp` `emptyDir` is mounted there as a safety margin. |
| `service.type` | `ClusterIP` | |
| `service.port` | `8080` | Must match `config.server.port` if you override the latter -- see the comment in `values.yaml`. |
| `resources` | `100m`/`256Mi` requests, `1Gi` memory limit, no CPU limit | No default CPU limit, to avoid throttling the event loop and the spam pipeline under load. |
| `livenessProbe` | `GET /api/health/live` | Process-liveness only. |
| `readinessProbe.enabled` | `true` | See [Readiness probe](#readiness-probe). |
| `readinessProbe.path` | `/api/health` | |
| `nodeSelector` / `tolerations` / `affinity` | `{}` / `[]` / `{}` | |
| `extraVolumes` / `extraVolumeMounts` | `[]` | |
| `extraEnv` / `extraEnvFrom` | `[]` | For `MAIL_VERDICT_SECTION_KEY` overrides or anything else the app reads from the environment. |
| `podDisruptionBudget.enabled` | `false` | Meaningless at `replicaCount: 1` beyond signalling voluntary-eviction intent; harmless to enable. |
| `podDisruptionBudget.minAvailable` / `.maxUnavailable` | `null` / `1` | |
| `networkPolicy.enabled` | `false` | |
| `networkPolicy.allowDNS` | `true` | |
| `networkPolicy.egress` | `[]` | **Required if you enable this** -- an empty list under an enabled policy means deny-all, not allow-all. See the comment in `values.yaml`. |
| `config` | `{}` | Sparse override merged onto the image's `config/config.yaml`. See [Configuration model](#configuration-model). |
| `configOverridePath` | `/app/config-custom/config.override.yaml` | Mount path for the rendered override; matches the image's working directory. |
| `secret.create` | `false` | See [Secrets](#secrets). |
| `secret.databaseUrl` / `secret.apiKey` / `secret.anthropicApiKey` | `""` | |
| `existingSecret` | `""` | Takes precedence over `secret.create`. |
| `existingSecretKeys.databaseUrl` / `.apiKey` / `.anthropicApiKey` | `MAIL_VERDICT_DATABASE_URL` / `MAIL_VERDICT_API_KEY` / `ANTHROPIC_API_KEY` | Key names to read from `existingSecret`. |
| `ingress.enabled` | `false` | Browser-facing route. See [Auth model](#auth-model). |
| `ingress.className` / `.annotations` / `.host` / `.path` / `.pathType` | `""` / `{}` / `""` / `/` / `Prefix` | |
| `ingress.tls.enabled` / `.secretName` | `false` / `""` | |
| `apiIngress.*` | same shape as `ingress.*`, all disabled/empty by default | Programmatic route, not behind ForwardAuth. See [Auth model](#auth-model). |

## Development

```bash
helm lint charts/mail-verdict
helm template mail-verdict charts/mail-verdict -f charts/mail-verdict/examples/values-production.yaml
```
