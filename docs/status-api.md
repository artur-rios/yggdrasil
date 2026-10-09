# Status API contract

The status API (`status/`) runs once per host, next to Traefik. It reads `catalog.yaml`, probes every application deployed to the environments that host runs, and answers the console (`console/`) and Prometheus. This document is the contract between the two: change it first, then both sides.

- **Public:** `https://yggdrasil.<domain>/api/...`, the same host as the console, routed by Traefik.
- **Internal:** `http://status:8080/...` on the `edge` and `telemetry` Docker networks. The `/internal/*` endpoints are on port 8081 (`http://status:8081/internal/...`).

This is version 2 of the contract: one response per host, with every environment of the host in it. Version 1 (one environment per host, top-level `environment` and `systems[].applications`) is gone from the API; the console still reads it, to show a host that has not been upgraded.

## Environments and applications

A host runs one or more of the catalog's `environments`: `YGGDRASIL_ENVIRONMENTS`, comma-separated ids (for example `development,homologation,production`). They are reported in catalog order, whatever order they are listed in.

For each of them, the API reports the applications deployed to it. An application whose `environments` map in the catalog leaves an environment out is not reported, probed or scraped there: it is absent from that environment in `/api/status`, `/api/systems/{id}` and `/internal/prometheus/targets`, not shown as `not_deployed`. An application without an `environments` map is in every environment. A system with no application in any of the host's environments is absent, and `/api/systems/{id}` answers `404` for it.

Several environments share one Docker engine, so an application is found and reached under environment-qualified names. Platform components (`kind: platform`) are not: there is one of each per host, serving every environment.

| | Application (`api`, `web`, `worker`) in environment `<env>` | Platform component |
|---|---|---|
| Compose project (`com.docker.compose.project`) | `<container.project or id>-<env>`, e.g. `heimdall-api-production` | `container.project` or the id, e.g. `yggdrasil` |
| Compose service (`com.docker.compose.service`) | `container.service`, when the catalog sets one | The same |
| Health URL probed | The catalog's `health`, its host qualified: `http://heimdall-api.production:8080/healthcheck` | The catalog's `health` as written |
| Metrics target | The catalog's `metrics`, its host qualified: `heimdall-api.production:9464` | The catalog's `metrics` as written |
| `url` | `https://<host><hostSuffix>.<DOMAIN>`, e.g. `https://heimdall-dev.example.com` | `https://<host>.<DOMAIN>` |
| Probed | Once per environment | Once per refresh; the same result appears in every environment it is in |

`hostSuffix` and `onDemand` are the application's options in that environment, resolved like every catalog option: the default, then the environment's value, then the application's override (see [catalog.md](catalog.md)).

## Authentication

Every `/api/*` endpoint requires `Authorization: Bearer <token>`, where the token is `YGGDRASIL_STATUS_TOKEN` in the host's `platform.env`. A missing or wrong token gets `401` with an empty body and `WWW-Authenticate: Bearer`. Both sides are hashed with SHA-256 and compared in constant time, so neither the token nor its length leaks through timing. The API refuses to start with a token shorter than 32 characters.

`/healthz` and `/internal/*` take no token. `/internal/*` answers only when the request arrives on the internal port **8081**, which is neither published nor routed; on 8080 it is `404`. That is how Prometheus reaches it without a secret.

Generate the token with `openssl rand -hex 32`. The scheme `Bearer` is case-insensitive, and whitespace around the token is ignored.

CORS: the origins in `YGGDRASIL_STATUS_CORS_ORIGINS`, in the host's `platform.env`, may call `/api/*` from a browser. This lets the console served by one host show another host. Same-origin use needs no entry.
- Comma-separated, may be empty. Each origin is exactly `https://host` or `https://host:port` (`http://` is accepted too): no path or query (a trailing `/` is tolerated). An invalid entry stops the service at start-up.
- Allowed: `GET` with the `Authorization` header. Preflight answers are cached for 1 hour.

## `GET /api/status`

The whole host in one response. The console polls it every 30 s (fixed) while its overview is visible. Abridged below to one system with one application per environment:

```json
{
  "host": "example.com",
  "generatedAt": "2026-10-08T18:04:11Z",
  "status": "up",
  "environments": [
    { "id": "development", "name": "Development", "onDemand": true, "status": "stopped" },
    { "id": "homologation", "name": "Homologation", "onDemand": true, "status": "stopped" },
    { "id": "production", "name": "Production", "onDemand": false, "status": "up" }
  ],
  "systems": [
    {
      "id": "heimdall",
      "name": "Heimdall",
      "description": "Identity and access management",
      "status": "up",
      "environments": [
        {
          "environment": "development",
          "status": "stopped",
          "applications": [
            {
              "id": "heimdall-api",
              "name": "Heimdall API",
              "kind": "api",
              "status": "stopped",
              "url": "https://heimdall-api-dev.example.com",
              "repository": "https://github.com/acme/heimdall-api",
              "deployment": {
                "version": "1.5.0",
                "commit": "8d01e7a",
                "deployedAt": "2026-10-07T14:12:40Z",
                "image": "heimdall-api:development-1.5.0-8d01e7a"
              },
              "container": {
                "state": "exited",
                "health": null,
                "startedAt": null,
                "restartCount": null
              },
              "probe": null
            }
          ]
        },
        {
          "environment": "homologation",
          "status": "stopped",
          "applications": [
            {
              "id": "heimdall-api",
              "name": "Heimdall API",
              "kind": "api",
              "status": "stopped",
              "url": "https://heimdall-api-hml.example.com",
              "repository": "https://github.com/acme/heimdall-api",
              "deployment": {
                "version": "1.4.0",
                "commit": "3f2a9c1",
                "deployedAt": "2026-10-01T09:30:12Z",
                "image": "heimdall-api:homologation-1.4.0-3f2a9c1"
              },
              "container": {
                "state": "exited",
                "health": null,
                "startedAt": null,
                "restartCount": null
              },
              "probe": null
            }
          ]
        },
        {
          "environment": "production",
          "status": "up",
          "applications": [
            {
              "id": "heimdall-api",
              "name": "Heimdall API",
              "kind": "api",
              "status": "up",
              "url": "https://heimdall-api.example.com",
              "repository": "https://github.com/acme/heimdall-api",
              "deployment": {
                "version": "1.4.0",
                "commit": "3f2a9c1",
                "deployedAt": "2026-10-02T21:40:02Z",
                "image": "heimdall-api:production-1.4.0-3f2a9c1"
              },
              "container": {
                "state": "running",
                "health": "healthy",
                "startedAt": "2026-10-02T21:40:05Z",
                "restartCount": null
              },
              "probe": {
                "healthy": true,
                "statusCode": 200,
                "latencyMs": 12,
                "checkedAt": "2026-10-08T18:04:09Z",
                "error": null
              }
            }
          ]
        }
      ]
    }
  ]
}
```

### Status values

| Status | Application |
|---|---|
| `up` | Container running (and `healthy` if it has a health check), **and** the probe returns 2xx |
| `degraded` | Container running, but the probe fails or is slower than 2 s, or Docker reports `unhealthy` or `starting`, or it restarted in the last 10 minutes |
| `stopped` | In an on-demand environment (`onDemand: true`): the container exists and is `exited` or `created`, i.e. stopped normally (`ygg env stop`, or an automatic deploy that left it stopped). Neutral, not a problem. Not probed |
| `down` | A container exists but is not `running` (any other Docker state: `restarting`, `paused`, `dead`..., and `exited` or `created` outside an on-demand environment), or the probe fails and there is no container information to say otherwise |
| `not_deployed` | No container for the application in that environment on this host |
| `unknown` | The status API could not reach Docker, so it can't tell |

Platform components are never `stopped`: they serve every environment and are always meant to run, so a stopped one is `down`.

### Roll-up

One rule gives the status of everything made of members:

| Level | Members |
|---|---|
| A system in one environment (`systems[].environments[].status`) | Its applications in that environment |
| An environment (`environments[].status`) | Its applications that are not platform components, in every system |
| A system (`systems[].status`) | Its environments (`systems[].environments[].status`) |
| The host (`status`) | The environments (`environments[].status`), and each platform component once |

1. `not_deployed` when every member is `not_deployed`, or there are none;
2. otherwise `stopped` when every member that is not `not_deployed` is `stopped`;
3. otherwise, over the members that are neither `not_deployed` nor `stopped`:
   1. `down` when every one is `down`;
   2. otherwise `degraded` when any is `down` or `degraded` (one `down` next to one `up` is degraded, not down);
   3. otherwise `unknown` when any is `unknown`;
   4. otherwise `up`.

So `not_deployed` and `stopped` members never make anything worse, and a stopped development environment next to a running production one leaves the host `up`.

Platform components are left out of the environments' roll-up, and counted once in the host's: they belong to the host, not to an environment, and counting their `up` in every environment would show an environment whose applications are all stopped as `up`. A platform problem still shows: in the platform system (in every environment) and in the host's `status`.

When the Docker proxy is unreachable, applications whose probe fails are `down` and those whose probe passes are `unknown`. Without Docker, a stopped application can't be told from a broken one either, and applications that are not deployed on that host usually fail their probe (their host name does not resolve): both show `down` until Docker is back.

### Fields

- **`host`**: the host's `YGGDRASIL_DOMAIN`, for display.
- **`environments`**: the host's environments, in catalog order. `id` and `name` are the catalog's; `onDemand` is the environment's own option (not an application's override).
- **`systems`**: in catalog order. **`systems[].environments`** lists, in catalog order, only the host's environments the system has at least one application in; `environment` is the environment's `id`. The platform system is in every environment its components are in.
- **Application objects** (`systems[].environments[].applications[]`):
  - **`url`**: `https://<host><hostSuffix>.<DOMAIN>` when the catalog gives a `host`, with the application's `hostSuffix` in that environment (platform components: no suffix); otherwise `null`.
  - **`repository`**: `https://github.com/<owner>/<repository>` for applications with a repository; `null` for platform components.
  - **`deployment`**: read from the container labels `scripts/deploy.sh` sets: `yggdrasil.version`, `yggdrasil.commit` and `yggdrasil.deployed_at`. `image` is the image Docker lists for the container: its name, or its id (`sha256:...`) once that name points to another image (a rebuilt `:local` image, a re-pulled tag). The object is `null` when there is no container. Each field is `null` when its label is missing, as for platform components.
  - **`container`**: from Docker's container list (`GET /containers/json`), the only request the status API's socket proxy lets through: not the container inspect, which would also hand the status API every container's environment, i.e. every application's secrets (see [Docker access](#docker-access)). `null` when there is no container.
    - `health` is `healthy`, `unhealthy`, `starting` or `null` (no health check).
    - `startedAt` is when the container last started, `null` when it isn't up (`exited`, `created`, `restarting`, `dead`). For a container that has not restarted, it is the container's creation time, to the second: Compose starts a container right after creating it, or once its `depends_on` are healthy. For a container that has restarted, the list only says "Up 5 minutes", so `startedAt` is as precise as that text: to the second under a minute, the minute under an hour, then the hour, the day, the week and the month.
    - `restartCount` is always `null`: only the inspect has the count. The field stays so that consoles reading it keep working.
    - **Restarted**: started again more than 5 minutes after it was created. That is Docker's restart policy after a crash, a `docker restart`, or the Docker engine or the host restarting. A redeploy is a new container, so it is not a restart, and neither is a start delayed by `depends_on`. Starting a stopped container again is one too (the list can't tell it from a crash), so in an on-demand environment, where that is how the environment is turned on (`ygg env start`), restarts don't count at all.
  - **`probe`**: the last health probe, a `GET` to the (qualified) health URL with a 5 s timeout and no redirects followed (so a 3xx is unhealthy). `null` for `not_deployed` and `stopped` applications, which are not probed.
    - `statusCode` is the HTTP answer.
    - `error` is `null` whenever an answer came back, even a failing one. Otherwise it is one of `timeout`, `connection refused`, `connection failed`, `connection reset`, `connection closed`, `name not resolved`, `host unreachable`, `tls error`, `invalid response` or `request failed` (anything else).
    - `latencyMs` is set on failures too: the time until the probe gave up.
  - **Restarted recently**: restarted (see `container`) and `startedAt` less than 10 minutes ago, outside an on-demand environment. A fresh deploy is a new container, so it doesn't count.
  - **`deployedAt`**: normalised to UTC (`...Z`) when the label parses as a date, otherwise passed through as written.

## `GET /api/systems/{id}`

One element of `systems`, shaped as above. `404` for an unknown id, and for a system with no application in any of the host's environments.

## `GET /healthz`

`200`, body `ok`. It does not depend on Docker or on any probe.

## `GET /internal/prometheus/targets` (port 8081)

[HTTP service discovery](https://prometheus.io/docs/prometheus/latest/http_sd/): one group per environment of the host and application in it with `metrics`, its host qualified with the environment and labelled with it; then each platform component with `metrics` once, without an `environment` label:

```json
[
  { "targets": ["heimdall-api.development:9464"], "labels": { "system": "heimdall", "app": "heimdall-api", "kind": "api", "environment": "development" } },
  { "targets": ["heimdall-api.homologation:9464"], "labels": { "system": "heimdall", "app": "heimdall-api", "kind": "api", "environment": "homologation" } },
  { "targets": ["heimdall-api.production:9464"], "labels": { "system": "heimdall", "app": "heimdall-api", "kind": "api", "environment": "production" } },
  { "targets": ["jenkins:8080"], "labels": { "system": "yggdrasil", "app": "jenkins", "kind": "platform", "__metrics_path__": "/prometheus/" } }
]
```

Groups follow the catalog: systems, then their applications, then each application's environments. The targets of a stopped on-demand environment are listed too, and are `up == 0` in Prometheus while it is stopped.

## How probing works

A background loop refreshes every application in every environment of the host every 15 s (`YGGDRASIL_STATUS_INTERVAL_SECONDS`), with the probes running in parallel. Requests are answered from the latest results and never wait for a probe; `generatedAt` is the time of the last completed refresh. A refresh that fails keeps the previous results, so a `generatedAt` that stops advancing means refreshes are failing: see `docker logs yggdrasil-status-1`.

Authentication comes first: a wrong token gets `401` even while the API is starting.

Until the first refresh completes, a few seconds after start, `/api/status` and `/api/systems/{id}` answer `503` with `Retry-After: 5` and an empty body. That is deliberately different from an all-`unknown` answer, which would be indistinguishable from "Docker is unreachable". `/internal/prometheus/targets` comes from the catalog alone and answers from the start.

Containers are found by the Compose labels on them, as in the table in [Environments and applications](#environments-and-applications): the project (`<container.project or id>-<environment>` for applications, `container.project` or the id for platform components) and, when the catalog sets `container.service`, the service. When several containers match, the running one wins, then the newest. An application with no container, or stopped in an on-demand environment, is not probed.

## Docker access

The status API answers the internet, so it never holds the Docker socket. It reads Docker through `docker-proxy` in `platform/compose.yml` ([wollomatic/socket-proxy](https://github.com/wollomatic/socket-proxy), pinned by digest), on an internal network shared with nothing else. The proxy lets through exactly one request, `GET /containers/json` (with or without an API version prefix), and only from the `status` container. Everything else gets `403`: the container inspect and its environment, logs, archives, events, exec, and any request that writes. A `docker proxy answered 403` in the status API's log means that allowlist no longer matches what the API asks for.

Traefik and Alloy go through proxies of their own, with the allowlists their Docker clients need; see the comments in `platform/compose.yml`. `scripts/test_socket_proxies.py` checks the three allowlists, and with `YGG_DOCKER_TESTS=1` runs them, with Traefik and Alloy, against a fake Docker API.

## Configuration

On a host, `platform/compose.yml` sets these from `platform.env`. The last column says where each one comes from.

| Variable | Default | | Set on a host from |
|---|---|---|---|
| `YGGDRASIL_STATUS_TOKEN` | — | Required, at least 32 characters | `YGGDRASIL_STATUS_TOKEN` in `platform.env` |
| `YGGDRASIL_ENVIRONMENTS` | — | Required unless `YGGDRASIL_ENVIRONMENT` is set. Comma-separated `id`s of the catalog's `environments` this host runs; reported in catalog order | `ENVIRONMENTS` in `platform.env` |
| `YGGDRASIL_ENVIRONMENT` | — | Legacy: one environment `id`, read as a one-element `YGGDRASIL_ENVIRONMENTS` when that is not set, and ignored when it is | `ENVIRONMENT` in an older `platform.env` |
| `YGGDRASIL_DOMAIN` | — | Required. The host's domain: builds application `url`s, reported as `host`. A bare domain: no scheme and no `/` | `DOMAIN` in `platform.env` |
| `YGGDRASIL_CATALOG_PATH` | `/app/catalog.yaml` | | Fixed by `platform/compose.yml`: the host's `catalog.yaml` |
| `YGGDRASIL_DOCKER_URL` | `http://docker-proxy:2375` | | Fixed by `platform/compose.yml` |
| `YGGDRASIL_STATUS_INTERVAL_SECONDS` | `15` | 1–3600 | Not passed through: edit `platform/compose.yml` to change it |
| `YGGDRASIL_STATUS_INTERNAL_PORT` | `8081` | Must not be 8080 | Not passed through (Prometheus expects 8081) |
| `YGGDRASIL_STATUS_CORS_ORIGINS` | empty | Comma-separated origins | `YGGDRASIL_STATUS_CORS_ORIGINS` in `platform.env` |

Every setting and the catalog are validated at start-up, in three stages: the settings, then the catalog (including each environment's and override's `hostSuffix` and `onDemand`), then that every id in `YGGDRASIL_ENVIRONMENTS` is one of the catalog's environments. The first stage with errors prints all of them (`yggdrasil-status: ...`, YAML errors with their line and column) and the service exits with code 1 instead of starting half-configured. Read them with `docker logs yggdrasil-status-1`.
