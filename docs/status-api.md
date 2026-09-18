# Status API contract

The status API (`status/`) runs once per environment host, next to Traefik. It reads `catalog.yaml`, probes every application deployed to its environment, and answers the console (`console/`) and Prometheus. This document is the contract between the two: change it first, then both sides.

- **Public:** `https://yggdrasil.<domain>/api/...`, the same host as the console, routed by Traefik.
- **Internal:** `http://status:8080/...` on the `edge` and `telemetry` Docker networks. The `/internal/*` endpoints are on port 8081 (`http://status:8081/internal/...`).

## Which applications

Only those deployed to the API's environment (`YGGDRASIL_ENVIRONMENT`, one of the catalog's `environments`). An application whose `environments` map in the catalog leaves that environment out is not reported, probed or scraped: it is absent from `/api/status`, `/api/systems/{id}` and `/internal/prometheus/targets`, not shown as `not_deployed`. A system with none of its applications left is absent too, and `/api/systems/{id}` answers `404` for it. An application without an `environments` map is in every environment.

## Authentication

Every `/api/*` endpoint requires `Authorization: Bearer <token>`, where the token is `YGGDRASIL_STATUS_TOKEN` in the host's `platform.env`. A missing or wrong token gets `401` with an empty body and `WWW-Authenticate: Bearer`. Both sides are hashed with SHA-256 and compared in constant time, so neither the token nor its length leaks through timing. The API refuses to start with a token shorter than 32 characters.

`/healthz` and `/internal/*` take no token. `/internal/*` answers only when the request arrives on the internal port **8081**, which is neither published nor routed; on 8080 it is `404`. That is how Prometheus reaches it without a secret.

CORS: the origins in `YGGDRASIL_STATUS_CORS_ORIGINS` (comma-separated, may be empty) may call `/api/*` with the `Authorization` header. This lets the console served by one environment show another environment. Same-origin use needs no entry.

## `GET /api/status`

The whole environment in one response. The console polls it (every 30 s by default).

```json
{
  "environment": "production",
  "environmentName": "Production",
  "generatedAt": "2026-09-18T18:04:11Z",
  "status": "degraded",
  "systems": [
    {
      "id": "shop",
      "name": "Shop",
      "description": "The online store",
      "status": "up",
      "applications": [
        {
          "id": "shop-api",
          "name": "Shop API",
          "kind": "api",
          "status": "up",
          "url": "https://shop-api.example.com",
          "repository": "https://github.com/acme/shop-api",
          "deployment": {
            "version": "1.4.0",
            "commit": "3f2a9c1",
            "deployedAt": "2026-09-17T21:40:02Z",
            "image": "shop-api:1.4.0-3f2a9c1"
          },
          "container": {
            "state": "running",
            "health": "healthy",
            "startedAt": "2026-09-17T21:40:05Z",
            "restartCount": 0
          },
          "probe": {
            "healthy": true,
            "statusCode": 200,
            "latencyMs": 12,
            "checkedAt": "2026-09-18T18:04:09Z",
            "error": null
          }
        }
      ]
    }
  ]
}
```

### Status values

| Status | Application | System / environment |
|---|---|---|
| `up` | Container running (and `healthy` if it has a health check), **and** the probe returns 2xx | Every application is `up` or `not_deployed` |
| `degraded` | Container running, but the probe fails or is slower than 2 s, or Docker reports `unhealthy` or `starting`, or it restarted in the last 10 minutes | Anything else that is not `down` |
| `down` | A container exists but is not running, or the probe fails and there is no container information to say otherwise | Every deployed application is `down` |
| `not_deployed` | No container for the application on this host | Every application is `not_deployed` |
| `unknown` | The status API could not reach Docker, so it can't tell | — |

A system, and the environment over all applications, is:

1. `not_deployed` when no application is deployed;
2. otherwise `down` when every *deployed* application is `down`;
3. otherwise `degraded` when any application is `down` or `degraded` (one `down` next to one `up` is a degraded system, not a down one);
4. otherwise `unknown` when any is `unknown`;
5. otherwise `up`.

`not_deployed` applications never make a system worse.

When the Docker proxy is unreachable, applications whose probe fails are `down` and those whose probe passes are `unknown`. Applications that are not deployed on that host usually fail their probe (their host name does not resolve), so they show `down` too until Docker is back.

### Fields

- **`environment`**: the environment's `id` in the catalog, i.e. `YGGDRASIL_ENVIRONMENT`. **`environmentName`** is its `name`, for display.
- **`url`**: `https://<host>.<DOMAIN>` when the catalog gives a `host`; otherwise `null`.
- **`repository`**: `https://github.com/<owner>/<repository>` for applications with a repository; `null` for platform components.
- **`deployment`**: read from the container labels `scripts/deploy.sh` sets: `yggdrasil.version`, `yggdrasil.commit` and `yggdrasil.deployed_at`. `image` comes from the container. The object is `null` when there is no container. Each field is `null` when its label is missing, as for platform components.
- **`container`**: from the Docker API through the read-only socket proxy. `health` is `healthy`, `unhealthy`, `starting` or `null` (no health check). `null` when there is no container.
- **`probe`**: the last health probe, a `GET` to the catalog's `health` URL with a 5 s timeout and no redirects followed (so a 3xx is unhealthy). `null` for `not_deployed` applications, which are not probed.
  - `statusCode` is the HTTP answer.
  - `error` is `null` whenever an answer came back, even a failing one. Otherwise it is one of `timeout`, `connection refused`, `name not resolved`, `host unreachable`, `connection reset` or `tls error`.
- **Restarted recently**: `restartCount > 0` and `startedAt` less than 10 minutes ago. A fresh deploy is a new container with 0 restarts, so it doesn't count.
- **`deployedAt`**: normalised to UTC (`...Z`) when the label parses as a date, otherwise passed through as written.

## `GET /api/systems/{id}`

One element of `systems`, shaped as above. `404` for an unknown id, and for a system with no application in this environment.

## `GET /healthz`

`200`, body `ok`. It does not depend on Docker or on any probe.

## `GET /internal/prometheus/targets` (port 8081)

[HTTP service discovery](https://prometheus.io/docs/prometheus/latest/http_sd/) for every application in this environment with `metrics`:

```json
[
  { "targets": ["shop-api:9464"], "labels": { "system": "shop", "app": "shop-api", "kind": "api" } },
  { "targets": ["jenkins:8080"], "labels": { "system": "yggdrasil", "app": "jenkins", "kind": "platform", "__metrics_path__": "/prometheus/" } }
]
```

## How probing works

A background loop refreshes every application every 15 s (`YGGDRASIL_STATUS_INTERVAL_SECONDS`), with the probes running in parallel. Requests are answered from the latest results and never wait for a probe; `generatedAt` is the time of the last completed refresh.

Until the first refresh completes, a few seconds after start, `/api/status` and `/api/systems/{id}` answer `503` with `Retry-After: 5` and an empty body. That is deliberately different from an all-`unknown` answer, which would be indistinguishable from "Docker is unreachable". `/internal/prometheus/targets` comes from the catalog alone and answers from the start.

Containers are found by the Compose labels on them:
- `com.docker.compose.project` is the catalog's `container.project`, defaulting to the application id.
- `com.docker.compose.service` is `container.service`, when the catalog sets one.

When several containers match, the running one wins.

## Configuration

| Variable | Default | |
|---|---|---|
| `YGGDRASIL_STATUS_TOKEN` | — | Required, at least 32 characters |
| `YGGDRASIL_ENVIRONMENT` | — | Required. The `id` of one of the catalog's `environments`; reported as `environment` |
| `YGGDRASIL_DOMAIN` | — | Required. Builds application `url`s |
| `YGGDRASIL_CATALOG_PATH` | `/app/catalog.yaml` | |
| `YGGDRASIL_DOCKER_URL` | `http://docker-proxy:2375` | |
| `YGGDRASIL_STATUS_INTERVAL_SECONDS` | `15` | 1–3600 |
| `YGGDRASIL_STATUS_INTERNAL_PORT` | `8081` | Must not be 8080 |
| `YGGDRASIL_STATUS_CORS_ORIGINS` | empty | Comma-separated origins |

Every setting and the catalog are validated at start-up, the settings first, then the catalog, then that `YGGDRASIL_ENVIRONMENT` is one of the catalog's environments. On any error, the service prints every problem it found and exits with code 1 instead of starting half-configured.
