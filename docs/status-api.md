# Status API contract

The status API (`status/`) runs once per environment host, next to Traefik. It reads `catalog.yaml`, probes every application, and answers the console (`console/`) and Prometheus. This document is the contract between the two: change it first, then both sides.

- **Public:** `https://yggdrasil.<domain>/api/...`, the same host as the console, routed by Traefik.
- **Internal:** `http://status:8080/...` on the `edge` and `telemetry` Docker networks.

## Authentication

Every `/api/*` endpoint requires `Authorization: Bearer <token>`, where the token is `YGGDRASIL_STATUS_TOKEN` in the host's `platform.env`. A missing or wrong token gets `401` with an empty body. The comparison is constant-time.

`/healthz` and `/internal/*` take no token. `/internal/*` answers only when the request arrives on the internal port **8081**, which is neither published nor routed; on 8080 it is `404`. That is how Prometheus reaches it without a secret.

CORS: the origins in `YGGDRASIL_STATUS_CORS_ORIGINS` (comma-separated, may be empty) may call `/api/*` with the `Authorization` header. This lets the console served by one environment show another environment. Same-origin use needs no entry.

## `GET /api/status`

The whole environment in one response. The console polls it (every 30 s by default).

```json
{
  "environment": "production",
  "generatedAt": "2026-09-18T18:04:11Z",
  "status": "degraded",
  "systems": [
    {
      "id": "heimdall",
      "name": "Heimdall",
      "description": "Identity and access management",
      "status": "up",
      "applications": [
        {
          "id": "heimdall-api",
          "name": "Heimdall API",
          "kind": "api",
          "status": "up",
          "url": "https://heimdall-api.example.com",
          "repository": "https://github.com/artur-rios/heimdall-api",
          "deployment": {
            "version": "1.4.0",
            "commit": "3f2a9c1",
            "deployedAt": "2026-09-17T21:40:02Z",
            "image": "heimdall-api:1.4.0-3f2a9c1"
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

Systems and the environment take the worst status of their applications, ranked `down` > `degraded` > `unknown` > `up`. `not_deployed` is neutral: it is ignored unless every application has it.

### Fields

- **`url`**: `https://<host>.<DOMAIN>` when the catalog gives a `host`; otherwise `null`.
- **`repository`**: `https://github.com/<owner>/<repository>` for applications with a repository; `null` for platform components.
- **`deployment`**: read from the container labels `scripts/deploy.sh` sets: `yggdrasil.version`, `yggdrasil.commit` and `yggdrasil.deployed_at`. `image` comes from the container. The object is `null` when there is no container. Each field is `null` when its label is missing, as for platform components.
- **`container`**: from the Docker API through the read-only socket proxy. `health` is `healthy`, `unhealthy`, `starting` or `null` (no health check). `null` when there is no container.
- **`probe`**: the last health probe, a `GET` to the catalog's `health` URL with a 5 s timeout. `error` is a short message such as `timeout` or `connection refused`, or `null`.

## `GET /api/systems/{id}`

One element of `systems`, shaped as above. `404` for an unknown id.

## `GET /healthz`

`200`, body `ok`. It does not depend on Docker or on any probe.

## `GET /internal/prometheus/targets` (port 8081)

[HTTP service discovery](https://prometheus.io/docs/prometheus/latest/http_sd/) for every catalog application with `metrics`:

```json
[
  { "targets": ["heimdall-api:9464"], "labels": { "system": "heimdall", "app": "heimdall-api", "kind": "api" } },
  { "targets": ["jenkins:8080"], "labels": { "system": "yggdrasil", "app": "jenkins", "kind": "platform", "__metrics_path__": "/prometheus/" } }
]
```

## How probing works

A background loop refreshes every application every 15 s (`YGGDRASIL_STATUS_INTERVAL_SECONDS`), with the probes running in parallel. Requests are answered from the latest results and never wait for a probe; `generatedAt` is the time of the last completed refresh.

Containers are found by the Compose labels on them:
- `com.docker.compose.project` is the catalog's `container.project`, defaulting to the application id.
- `com.docker.compose.service` is `container.service`, when the catalog sets one.

When several containers match, the running one wins.
