# System map and integration health: design

Date: 2026-11-10. Status: for the owner's approval.

Throughout, "(builder's call)" marks what is left to whoever implements this, and "(designer's call)" marks a decision made while writing this document that the owner has not specifically weighed in on. Everything else was the owner's decision or was shown to the owner and accepted.

## Why

The owner opens the console to answer three questions:

1. What systems are deployed?
2. Are they healthy?
3. Are the integrations between them healthy?

The console answers the first two as a grid of cards, one chip per environment per system, and cannot answer the third: neither `catalog.yaml` nor the status API records which systems talk to each other, and no API checks those links. The systems are new and no integration has failed yet; the point is to have the answer ready when one does.

## Goals

- Replace the card grid with a **map**: one icon per system, on one grid, with lines between the systems that integrate.
- Show **one environment at a time**, chosen with a selector.
- Show the **health of each integration** on its line.
- Each API reports for itself whether it is healthy and exactly what is not.
- Clicking a system shows what is wrong in it.
- Tell on-demand environments apart on each system, with a label.

## Non-goals

- History, trends or alerts on integration failures.
- Editing anything from the console, or dragging icons around.
- Integration checks for anything other than the three APIs below. The personal page also calls Hermes; that line is not drawn.
- Detailed health for web applications, docs sites or Chimera.

## Anti-goals

Failures even if the feature technically works:

- yggdrasil needing a Heimdall login to see health. It would lose its detail exactly when Heimdall breaks.
- Detailed health reachable through Traefik or from the internet without the admin role.
- A dead or slow API holding up the map. The map appears first and the details follow, within a bounded wait.
- yggdrasil calling every API, and through them Mailgun and Pluggy, while nobody is looking.
- Exception text, connection strings or keys appearing in a health report.
- A deliberately stopped on-demand environment looking like a problem.
- A status conveyed by colour alone. Every status in the console has its own shape and label today, and lines must too.

## The model

Each API answers, on an internal port, a **detailed health report**: an overall status and one entry per thing it depends on. Some of those entries are integrations with other systems.

The catalog declares **which integrations exist**. The reports say **whether they work**. Lines come from the catalog so that they stay on the map when the API that would report them is silent.

### Integrations in scope

| Caller | Target | Check name | How it is checked |
|---|---|---|---|
| `fortuna-api` | system `heimdall` | `Heimdall` | Fortuna's real dependency, at the address and with the HTTP client its real calls use (`FORTUNA_HEIMDALL_JWKS_URL`, answered with a 2xx). |
| `heimdall-api` | system `hermes` | `Hermes` | `GET <HERMES_BASE_URL>/healthcheck`, 2xx. `NotConfigured` when `HermesConfigured` is false (Heimdall then only logs email). |
| `hermes-api` | external `Mailgun` | `Mailgun` | A call that reaches Mailgun and sends nothing (builder's call which). `NotConfigured` when `MailgunSettings.IsConfigured` is false. |

Fortuna's existing `Aggregator` (Pluggy) and `ExchangeRateSource` checks stay as rows in Fortuna's panel and get no line (designer's call). Drawing one later is one catalog entry.

A web front end and its own API are not an integration: they stay inside one system icon.

### Detailed health on an internal port

Each of the three APIs serves `GET /internal/health` on its **metrics port** (9464 by default; `HEIMDALL_METRICS_PORT`, `FORTUNA_METRICS_PORT`), told apart by `Connection.LocalPort` as the scrape endpoint already is. Traefik routes only port 8080, and nothing publishes 9464. No credential is needed.

- Containers on the `edge` and `telemetry` networks can reach that port, so the report must be safe for them to read. It carries only names, statuses and reason words.
- The path is deliberately not `/healthcheck/detailed` (designer's call). On a Heimdall not yet upgraded, that path is rewritten to the admin route and answers 401 rather than the 404 the rollout relies on.
- Hermes's `MetricsSetup.Use` and Fortuna's `UsePrometheusMetrics` close everything but the scrape path, and each needs this one route opened. Heimdall's `UseHeimdallMetrics` closes nothing today: every other request on 9464 runs the whole API, without Traefik's limits. This work closes it the same way (designer's call), so the port serves only the scrape and this route.
- **Heimdall's admin-only `/api/healthcheck/detailed` is removed (owner's decision)**, with the `/detailed` part of the `EdgePipeline` rewrite, so that `(?i)^healthcheck(/ready)?/?$` remains. `/healthcheck` and `/healthcheck/ready` stay. Its OpenAPI entry, `api-client` samples, tests and requirements (FR-HC-02, 04 to 07, UC-30) go or change with it; whether the check classes are reused for the internal route is the builder's call. See "Heimdall web" below for its one caller.
- Fortuna's admin-only `/healthcheck/detailed` on port 8080 keeps its callers, response shape and status rules. It gains the Heimdall entry, which fits its existing optional-service rule (designer's call).
- Hermes has no detailed check today, so it gets its first one, on the internal route only.

### Report contract

On `/internal/health`, all three APIs answer the same bare shape, not wrapped in Heimdall's `DataOutput` envelope:

```json
{
  "status": "Degraded",
  "services": [
    { "name": "Database", "status": "Healthy" },
    { "name": "Hermes", "status": "Unhealthy", "reason": "connection refused" }
  ]
}
```

- `status` of the report and of each entry: `Healthy`, `Degraded`, `Unhealthy` or `NotConfigured`, the values Fortuna already has. Heimdall's check interface is a boolean today and needs `Degraded` and `NotConfigured` added (builder's call how).
- `reason` is optional, and is one of the status API's probe vocabulary (`timeout`, `connection refused`, `connection failed`, `connection reset`, `connection closed`, `name not resolved`, `host unreachable`, `tls error`, `invalid response`, `request failed`) or `HTTP <code>`. Nothing else.
- An integration failing makes the report `Degraded`, as Fortuna's optional services do. A required service (the database) failing makes it `Unhealthy`.
- HTTP 200 for `Healthy` and `Degraded`, 503 for `Unhealthy`. The body is the same either way.
- Fields the three APIs have today and this does not list (Fortuna's `queueDepth`, `oldestPendingSeconds`, `isRequired`) may stay. The status API ignores them.

### Catalog

New optional keys, validated by both `scripts/catalog.py` and the status API's `CatalogLoader`:

```yaml
systems:
  - id: heimdall
    icon: shield
    applications:
      - id: heimdall-api
        detailedHealth: http://heimdall-api:9464/internal/health
        integrations:
          - { system: hermes, check: Hermes }
  - id: hermes
    icon: mail
    applications:
      - id: hermes-api
        detailedHealth: http://hermes-api:9464/internal/health
        integrations:
          - { external: Mailgun, check: Mailgun }
```

- `detailedHealth` is qualified with the environment exactly as `health` is.
- `integrations[].system` must be another system's id; `check` is the entry's `name` in the report; `external` is a display name for something outside the catalog. Exactly one of `system` and `external`.
- `icon` is one name from a fixed list. Flutter strips unused icons from a build, so the console maps names to constant icons and shows a neutral icon for an unknown name. An installation with no `icon` still works.

Icons for this catalog (Material icons; designer's call): Heimdall `shield`, Hermes `mail`, Fortuna `savings`, Chimera `construction`, Alexandria `local_library`, Orpheus `music_note`, .NET libraries `extension`, Personal sites `person`, Tech Craftsmen `business`, Yggdrasil `park`, externals `cloud`.

### Status API

**Fetching, only when a console asks (owner's decision).** There is no background loop for detailed health. `POST /api/status/refresh` fetches each `detailedHealth` with a 5 s timeout, in parallel, for each application that is neither `stopped` nor `not_deployed`, merges the results into the snapshot, and answers with the same body as `GET /api/status`.

- Requests that arrive during a fetch share it, and a result under 10 s old is reused (designer's call), so several consoles or repeated clicks cannot become a burst of calls.
- This relaxes the status API's rule that no request waits on a probe, for this one endpoint only. `GET /api/status` still answers from memory at once. The wait is bounded by the 5 s timeout.
- The status API's existing 15 s liveness loop is unchanged (designer's call). It only probes yggdrasil's own containers, and it is what writes status changes to the log.
- Results are kept per application per environment until the next fetch.

**Rules.** The owner's rule: an API that does not answer is treated as not healthy, and the console says the details could not be fetched.

| Outcome of the fetch | Effect on the application | Details shown |
|---|---|---|
| Report `Healthy` | None | The entries |
| Report `Degraded` or `Unhealthy` | At most `degraded` | The entries |
| No answer, timeout, non-JSON, or a status other than 200, 503 or 404 | `degraded` if it would otherwise be `up` | "Could not get more details": the reason |
| 404 | None | "This version does not report detailed health" |
| Not fetched yet (no console has asked since the status API started) | None | Nothing yet |

- The liveness rules in `docs/status-api.md` are unchanged and take precedence: a stopped or missing container is still `stopped` or `down`. A report can only worsen `up` to `degraded`. `down` stays reserved for what it means today.
- 404 is not treated as a failure so that a catalog entry can land before every environment runs an API version that has the route (designer's call; see Rollout).

**Integration status**, per integration per environment, first match wins:

| Status | When |
|---|---|
| `stopped` | The caller is `stopped`, or the target system is `stopped` or `not_deployed` in that environment |
| `unknown` | The caller did not answer, its report lacks the check, or nothing has been fetched yet |
| `not_configured` | The caller's report has the check `NotConfigured` |
| `up` | The caller's report has the check `Healthy` |
| `down` | The caller's report has the check `Degraded` or `Unhealthy` |

An integration appears in an environment only when the caller is in that environment and, for a `system` target, the catalog puts the target there too.

**A deliberately stopped target is not a problem (owner's decision).** In an on-demand environment one application can be left running while the rest are stopped (a Jenkins deploy with `DEPLOY_START=1`). The running caller's check against a stopped target fails, but that check is ignored when working out the caller's status: if every entry that is not healthy is an integration whose status above is `stopped`, the report counts as `Healthy`. Any other failing entry still counts as usual. The owner accepts that the map then shows the caller as healthy although that dependency is unavailable; the line reads "stopped" and the panel row says the target is stopped.

**Contract.** `docs/status-api.md` is changed first, as its first paragraph asks. All additions are optional, so the contract stays version 2 and old consoles and old APIs keep working:

- `systems[].icon`: string or `null`.
- `systems[].environments[].onDemand`: true when any application of the system in that environment is on demand. Per system because `onDemand` can be overridden per application.
- `systems[].environments[].integrations[]`: `{ "from": "heimdall-api", "system": "hermes", "external": null, "check": "Hermes", "status": "down", "reason": "connection refused" }`.
- `applications[].details`: `null` when the application has no `detailedHealth`, otherwise `{ "state": "reported" | "unavailable" | "unsupported" | "not_checked", "checkedAt": "...", "reason": "...", "status": "Degraded", "services": [ ... ] }`.
- `POST /api/status/refresh`: new, as above, with the same token. `YGGDRASIL_STATUS_CORS_ORIGINS` origins may now also use `POST`, as a console on one host can show another.
- The contract's "the console polls it every 30 s (fixed)" is replaced by the console's new behaviour below.

### Console

**Environment selector.** At the top of the overview, in place of each card's chips. Lists the environments of the current host, each with a small status mark in the status's shape, so a problem in an environment you are not viewing still shows (designer's call, shown to the owner). Segmented control, falling back to a dropdown when narrow, as the existing switcher does. The chosen environment is remembered per host; the first time, the last environment in catalog order (Production here). Beside it, a one-line summary such as "1 integration failing".

The existing top switcher picks between saved **hosts** and stays, smaller. In the console's code, `Environment` and `EnvironmentsController` mean a saved host connection; `HostEnvironment` is a development/production environment. New code should keep the two apart.

**Map (layout B).** Every system deployed in the selected environment on one even grid, drawn with `CustomPaint` for lines and widgets for icons; no new dependency (builder's call whether a package does this better).

- Icon in a circle tinted by status, status badge with the status's own shape, name beneath.
- Systems that integrate sit next to each other, so lines stay short and do not cross icons. Placement is computed from the catalog, not hand-kept per system (builder's call how).
- Each system keeps its spot when the environment changes. A system not deployed in that environment leaves its cell empty; a wholly empty row or column may collapse.
- Externals are dashed circles with an "external" caption and no status.
- Lines are arrows from caller to target. Each carries a small pill: `✓ OK`, `✕ Failing`, `? Unknown`, grey and dashed for `stopped` and `not_configured` with those words.
- On a phone the grid narrows to fewer columns, with the same rules.
- **On-demand label (owner's request):** when the system's `onDemand` is true in the selected environment, the tile shows "on demand" with the power icon the console already uses, whether or not it is currently stopped.
- A stopped system shows its stopped status and is not counted as a problem.
- The map needs a screen-reader equivalent: each system and integration exposes a label such as "Heimdall to Hermes: failing, connection refused".

**System panel.** Clicking a system, or a line (which opens the caller's panel), opens a side panel on a computer and a bottom sheet on a phone, with the map still visible:

- Icon, name, environment, description, status, "on demand" when it applies.
- Applications, each with status, version and deploy time, opening the existing application detail.
- "Reported by <application> · checked <time>": each service and integration, with status and reason.
- When `details.state` is `unavailable`, a clear message: "Could not get more details. The detailed health endpoint did not answer: <reason>." Its integrations read "Not checked".
- When the application has no `detailedHealth`, that section is absent.

**Refreshing (owner's decision).** The console no longer polls by itself.

- When the console starts, or switches to another host, it loads once: `GET /api/status` to draw the map, then `POST /api/status/refresh` to fill in details. After that the screen does not change unless the user asks.
- A **Refresh now** button (and pull-to-refresh) does that load again. Changing environment does not: one response already holds every environment.
- An **auto-refresh** switch, off by default, repeats the load on a chosen interval: every **1 minute** (the default), **5, 10, 15 or 30 minutes**. The choice is remembered on the device.
- With auto-refresh on, the timer pauses while the app or browser tab is hidden or the hosts screen covers it, and a load runs on return, as polling does today (designer's call). With it off, returning loads nothing.
- "Updated <time> ago" stays visible beside the button, since the screen can now be old with no failure to show. `AppConfig.refreshInterval` goes. The one-shot retry while the status API is starting stays.

**Removed:** the card grid, the per-environment chips, the 30 s poll, and the "Problems only" filter, as the map is small enough to scan. **Kept:** the host banner's failure, starting and stale states, pull-to-refresh and the offline demo. `console/assets/demo/status.json` and `console/tool/mock_status_server.dart` gain the new fields, so the demo shows a failing integration and an API that does not answer.

### Heimdall web

The Health screen in `heimdall-ui` (`/health`, `lib/features/health/presentation/health_screen.dart`) is the only caller of the admin endpoint being removed, and is **removed (owner's decision)**: route, route access entry, navigation to it, its controller and tests. `repository.ping()` stays, because the dashboard uses it, so the feature's data layer shrinks to liveness. `api/heimdall.json` and the generated client are regenerated from the new OpenAPI document, which the required `api-client-drift` check enforces.

## Constraints

- yggdrasil's flow: `feature/*` from `develop`, pull request to `develop`. The same in the three APIs.
- Each API's own specs (requirements documents, OpenAPI where the port's route belongs) are kept in step, as each repository's workflow requires.
- Heimdall's and Fortuna's CI can be killed for memory when they run at once, and merging many pull requests together floods Jenkins: **merge one repository at a time**, and let each finish.
- A check must not send email, write data or spend a paid quota.

## Rollout

1. `docs/status-api.md`, then the status API and console, tolerant of everything being absent. No visible change to health.
2. `heimdall-ui` without the Health screen, released before Heimdall drops the endpoint. Its regenerated client follows Heimdall's new OpenAPI document, so the two are coordinated (builder's call how, given the drift check).
3. The three APIs, one at a time, each released to its environments.
4. The catalog: `icon`, `detailedHealth` and `integrations`.

An environment still on an older API answers 404 and reads "does not report detailed health" until it is deployed.

## Left to the builder

- Placement algorithm, line routing and styling beyond the rules above.
- Whether the API reports cache their checks briefly.
- How Heimdall's boolean check interface grows.
- Which call proves Mailgun is reachable.
- Splitting this into pull requests and the order inside each repository.
