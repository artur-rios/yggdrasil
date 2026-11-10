# System Map and Integration Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the console's card grid with a one-environment-at-a-time map of system icons and integration lines, fed by detailed health reports each API serves on an internal port.

**Architecture:** `catalog.yaml` declares icons, report URLs and integrations. Heimdall, Hermes and Fortuna serve `GET /internal/health` on their metrics port. The status API fetches those reports only when a console calls `POST /api/status/refresh`, derives each integration's status, and adds both to the existing v2 response. The Flutter console draws the map from that response and refreshes only on start, on request, or on an opt-in timer.

**Tech Stack:** .NET 10 minimal APIs and xUnit (status API, three APIs), Flutter with Riverpod (console, heimdall-ui), Python 3 `unittest` (`scripts/catalog.py`).

**Spec:** `docs/superpowers/specs/2026-11-10-system-map-and-integration-health-design.md`

## Global Constraints

- Machine: every command below runs on the developer workstation, in the repository the task names.
- Branches: `feature/system-map` from `develop` in yggdrasil; `feature/internal-health` from `develop` in `heimdall-api`, `hermes-api` and `fortuna-api`; `feature/remove-health-screen` from `develop` in `heimdall-ui`.
- Merge one repository at a time and let its CI finish. Heimdall's and Fortuna's checks can be OOM-killed when run together.
- Contract stays version 2, and every addition to it is optional.
- Report `status` values: `Healthy`, `Degraded`, `Unhealthy`, `NotConfigured`.
- Report `reason`: only `timeout`, `connection refused`, `connection failed`, `connection reset`, `connection closed`, `name not resolved`, `host unreachable`, `tls error`, `invalid response`, `request failed`, or `HTTP <code>`.
- Integration wire statuses: `up`, `down`, `not_configured`, `stopped`, `unknown`.
- `details.state` wire values: `reported`, `unavailable`, `unsupported`, `not_checked`.
- Report fetch timeout 5 s. Fetch results under 10 s old are reused.
- Auto-refresh intervals: 1 (default), 5, 10, 15, 30 minutes. Auto-refresh is off by default.
- Icon names: `shield`, `mail`, `savings`, `construction`, `local_library`, `music_note`, `extension`, `person`, `business`, `park`, `cloud`.
- A health check must not send email, write data or spend a paid quota.
- A status is never conveyed by colour alone.
- Do not touch the `Environment` / `EnvironmentsController` names in the console: there they mean a saved host connection. `HostEnvironment` is a development/production environment.

## Review Focus

1. A report whose `reason` holds exception text or a connection string: the reason is dropped and never reaches the console (Task 2).
2. A console pointed at a status API without `POST /api/status/refresh` (404/405): the map still loads, with no failure banner (Task 5).
3. A response with no `icon`, `integrations` or `details`, from an older status API: neutral icons, no lines, no crash (Task 8).
4. The remembered environment no longer exists on the host, or the host has none: the selector falls back to the last environment, or the map shows the empty state (Task 7).
5. The user switches host while the details fetch is in flight: the late answer is discarded and never drawn over the new host (Task 6).

---

## Part 1: yggdrasil

### Task 1: Catalog and contract

**Files:**
- Modify: `docs/status-api.md`, `docs/catalog.md`, `catalog.yaml` (comments on the new keys only; values come in Task 15)
- Modify: `status/src/Yggdrasil.Status/Catalog/Catalog.cs`, `status/src/Yggdrasil.Status/Catalog/CatalogLoader.cs`, `scripts/catalog.py`
- Test: `status/tests/Yggdrasil.Status.Tests/CatalogLoaderTests.cs`, `scripts/test_catalog.py`

**Interfaces:**
- Produces: `SystemDefinition(..., string? Icon = null)`; `ApplicationDefinition(..., Uri? DetailedHealth = null, IReadOnlyList<IntegrationDefinition>? Integrations = null)`; `record IntegrationDefinition(string? System, string? External, string Check)`; `ProbeTarget.DetailedHealth : Uri?`, qualified exactly as `Health`.

- [ ] **Step 1: Update `docs/status-api.md`** with every contract addition in the spec's "Contract" list, the "Rules" and integration status tables, and `POST /api/status/refresh`; remove "polls it every 30 s (fixed)". Add the three keys to `docs/catalog.md`.

- [ ] **Step 2: Write the failing tests** in `CatalogLoaderTests`:

```csharp
GivenIconAndIntegrations_WhenParsed_ThenTheyAreOnTheDefinitions  // Icon == "shield"; Integrations[0] == new("hermes", null, "Hermes")
GivenUnknownIcon_WhenParsed_ThenListsAnError                      // message contains "icon 'rocket'"
GivenIntegrationWithSystemAndExternal_WhenParsed_ThenListsAnError // exactly one of system, external
GivenIntegrationToItsOwnSystem_WhenParsed_ThenListsAnError
GivenIntegrationToUnknownSystem_WhenParsed_ThenListsAnError
GivenIntegrationWithoutCheck_WhenParsed_ThenListsAnError
GivenRelativeDetailedHealth_WhenParsed_ThenListsAnError
GivenDetailedHealth_WhenTargetInEnvironment_ThenHostIsQualified   // http://heimdall-api.production:9464/internal/health
GivenNoNewKeys_WhenParsed_ThenAllAreNull
```

Mirror the icon, integration and URL cases in `scripts/test_catalog.py`, so a catalog one validator accepts the other accepts too.

- [ ] **Step 3: Run to verify they fail**

Run: `dotnet test status --filter CatalogLoaderTests` and, from `scripts/`, `python -m unittest test_catalog`
Expected: the new tests FAIL.

- [ ] **Step 4: Implement** the records, `ProbeTarget.DetailedHealth`, validation in `CatalogLoader.ValidateApplication` and `Validate` (integration targets checked after all systems are read), and the same rules in `catalog.py`. Keep the icon list in one constant per language.

- [ ] **Step 5: Run to verify they pass.** Same commands. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs status scripts catalog.yaml
git commit -m "feat: catalog icons, detailed health and integrations"
```

### Task 2: Fetch and sanitise one report

**Files:**
- Create: `status/src/Yggdrasil.Status/Probing/DetailedHealthClient.cs`
- Modify: `status/src/Yggdrasil.Status/Model/StatusModel.cs`, `status/src/Yggdrasil.Status/Probing/HealthProber.cs` (make `Describe` `internal static`), `status/src/Yggdrasil.Status/Program.cs`
- Test: `status/tests/Yggdrasil.Status.Tests/ClientTests.cs`

**Interfaces:**
- Produces: `record ApplicationDetails(string State, DateTimeOffset? CheckedAt, string? Reason, string? Status, IReadOnlyList<ReportedService> Services)`; `record ReportedService(string Name, string Status, string? Reason)`; `static class DetailsStates { Reported, Unavailable, Unsupported, NotChecked }` holding the wire strings; `DetailedHealthClient.FetchAsync(Uri url, CancellationToken ct) : Task<ApplicationDetails>`, which never throws.

- [ ] **Step 1: Write the failing tests** (use `FakeProbes`, extended to answer a body):

```csharp
GivenHealthyReport_WhenFetched_ThenReported            // State reported, Status "Healthy", Services carries names
GivenUnhealthyReportWith503_WhenFetched_ThenReported   // 503 with a body is an answer
Given404_WhenFetched_ThenUnsupported
Given401_WhenFetched_ThenUnavailableWithHttpReason     // Reason "HTTP 401"
GivenConnectionRefused_WhenFetched_ThenUnavailable     // Reason "connection refused"
GivenSlowAnswer_WhenFetched_ThenUnavailableTimeout     // Reason "timeout"
GivenNotJson_WhenFetched_ThenUnavailableInvalidResponse
GivenJsonWithoutServices_WhenFetched_ThenUnavailableInvalidResponse
GivenUnknownStatusWord_WhenFetched_ThenUnavailableInvalidResponse
GivenReasonOutsideVocabulary_WhenFetched_ThenReasonIsDropped // "Npgsql: Host=db;Password=x" => Reason null, entry kept
GivenHttp500Reason_WhenFetched_ThenKept                      // "HTTP 500" matches
```

- [ ] **Step 2: Run to verify they fail**: `dotnet test status --filter ClientTests`. Expected: FAIL.

- [ ] **Step 3: Implement `DetailedHealthClient`** as a typed client registered like `HealthProber`: no redirects, `MaxResponseContentBufferSize` 64 KiB, its own 5 s timeout, reasons from `HealthProber.Describe`. Extra JSON fields are ignored.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: fetch an application's detailed health"`

### Task 3: Rules and the snapshot

**Files:**
- Modify: `status/src/Yggdrasil.Status/Model/StatusRules.cs`, `Model/StatusModel.cs`, `Model/StatusSnapshot.cs`
- Test: `status/tests/Yggdrasil.Status.Tests/StatusRulesTests.cs`, `SnapshotBuilderTests.cs`

**Interfaces:**
- Consumes: Task 1's definitions, Task 2's records.
- Produces:
  - `enum IntegrationLevel { Up, Down, NotConfigured, Stopped, Unknown }`
  - `record IntegrationStatus(string From, string? System, string? External, string Check, IntegrationLevel Status, string? Reason)`
  - `StatusRules.ForIntegration(StatusLevel caller, StatusLevel? target, ApplicationDetails? details, string check) : IntegrationLevel`
  - `StatusRules.WithDetails(StatusLevel liveness, ApplicationDetails? details, IReadOnlySet<string> stoppedChecks) : StatusLevel`
  - Added trailing, defaulted members: `SystemStatus.Icon`, `SystemEnvironmentStatus.OnDemand` and `.Integrations`, `ApplicationStatus.Details`.
  - `SnapshotBuilder.Build(observations, generatedAt, IReadOnlyDictionary<string, ApplicationDetails>? details = null)`, keyed by `ProbeTarget.Key`.

- [ ] **Step 1: Write the failing tests**, one per row of the spec's tables:

```csharp
// ForIntegration, first match wins
(Stopped, Up, Healthy)              => Stopped   // caller stopped
(Up, Stopped, Unhealthy)            => Stopped   // target stopped
(Up, NotDeployed, Unhealthy)        => Stopped
(Up, Up, unavailable)               => Unknown
(Up, Up, reported without the check)=> Unknown
(Up, Up, null)                      => Unknown   // nothing fetched yet
(Up, Up, NotConfigured)             => NotConfigured
(Up, Up, Healthy)                   => Up
(Up, Up, Degraded)                  => Down
(Up, Up, Unhealthy)                 => Down
(Up, null /* external */, Healthy)  => Up

// WithDetails
(Up, Healthy)                       => Up
(Up, Degraded)                      => Degraded
(Up, Unhealthy)                     => Degraded
(Up, unavailable)                   => Degraded
(Up, unsupported)                   => Up
(Up, not_checked or null)           => Up
(Down, Healthy)                     => Down      // liveness precedes
(Stopped, Unhealthy)                => Stopped
(Up, Degraded where every non-healthy entry is in stoppedChecks) => Up
(Up, Unhealthy with Database failing and Hermes in stoppedChecks) => Degraded
```

`SnapshotBuilderTests`: `GivenRunningCallerAndStoppedTarget_ThenLineStoppedAndCallerUp`; `GivenTargetNotInEnvironment_ThenNoIntegrationEntry`; `GivenAnyOnDemandApplication_ThenSystemEnvironmentIsOnDemand`; `GivenNoDetailedHealth_ThenDetailsNull`; `GivenDetailedHealthNotFetched_ThenStateNotChecked`.

- [ ] **Step 2: Run to verify they fail**: `dotnet test status --filter "StatusRulesTests|SnapshotBuilderTests"`. Expected: FAIL.

- [ ] **Step 3: Implement.** In `Build`, compute each system's per-environment status from liveness alone first, and pass those as the `target` statuses. Details cannot turn anything into `stopped` or `not_deployed`, so this has no cycle. Then apply `WithDetails` and roll up as today.

- [ ] **Step 4: Run to verify they pass.** Also run all of `dotnet test status`, since record shapes changed. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: derive integration status and apply reports"`

### Task 4: The refresh endpoint

**Files:**
- Create: `status/src/Yggdrasil.Status/DetailedHealthRefresher.cs`
- Modify: `status/src/Yggdrasil.Status/StatusRefresher.cs`, `Api/Endpoints.cs`, `Program.cs`
- Test: `status/tests/Yggdrasil.Status.Tests/ApiTests.cs`

**Interfaces:**
- Consumes: `DetailedHealthClient.FetchAsync`, `SnapshotBuilder.Build(..., details)`.
- Produces: `DetailedHealthRefresher.RefreshAsync() : Task` (single-flight, 10 s reuse, not tied to any request's cancellation); `StatusRefresher.Republish()` rebuilds the snapshot from the last observations; `POST /api/status/refresh`.

- [ ] **Step 1: Write the failing tests:**

```csharp
GivenNoToken_WhenRefreshPosted_Then401
GivenNotReady_WhenRefreshPosted_Then503WithRetryAfter
GivenReportingApi_WhenRefreshPosted_ThenBodyHasDetails
GivenNoRefreshPosted_ThenNoReportFetchedAfterSeveralRefreshes   // no background loop
GivenStoppedApplication_WhenRefreshPosted_ThenItsReportNotFetched
GivenTwoConcurrentPosts_ThenOneFetchPerApplication
GivenPostWithin10Seconds_ThenNoNewFetch                         // FixedTime
GivenPostAfter10Seconds_ThenFetchesAgain
GivenAllowedOrigin_WhenPreflightPost_ThenAllowed
GivenRefreshAlreadyRan_WhenGetStatus_ThenDetailsAreThere
```

- [ ] **Step 2: Run to verify they fail**: `dotnet test status --filter ApiTests`. Expected: FAIL.

- [ ] **Step 3: Implement.** Fetch for targets with `DetailedHealth` that are neither `Stopped` nor `NotDeployed` by last observation. Add `HttpMethods.Post` to the CORS policy. Leave the 15 s loop alone, except that it passes the stored details to `Build`.

- [ ] **Step 4: Run to verify they pass**: `dotnet test status`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: refresh detailed health on request"`

### Task 5: Console data layer

**Files:**
- Modify: `console/lib/features/status/domain/status.dart`, `data/status_source.dart`, `data/status_repository.dart`
- Test: `console/test/features/status/domain/status_test.dart`, `console/test/features/status/data/status_data_test.dart`, `console/test/helpers.dart`

**Interfaces:**
- Produces:
  - `SystemStatus.icon : String?`; `SystemEnvironment.onDemand : bool`, `.integrations : List<IntegrationStatus>`; `ApplicationStatus.details : ApplicationDetails?`
  - `enum IntegrationHealth { up, down, notConfigured, stopped, unknown }` (unrecognised wire value is `unknown`)
  - `class IntegrationStatus { from, system, external, check, status, reason }`
  - `enum DetailsState { reported, unavailable, unsupported, notChecked }`
  - `class ApplicationDetails { state, checkedAt, reason, status, services }`, `class ReportedService { name, status, reason }`
  - `StatusSource.refreshDetails(Environment, String?) : Future<HostStatus?>`, where `null` means this status API has no such endpoint
  - `StatusRepository.refreshDetails(Environment, String?) : Future<StatusReport>`

- [ ] **Step 1: Write the failing tests:**

```dart
'reads icon, onDemand, integrations and details'
'a response without the new fields still parses'         // icon null, integrations empty, details null, onDemand false
'an unrecognised integration status is unknown'
'refreshDetails posts with the bearer token'             // POST <base>/api/status/refresh
'refreshDetails answers null on 404 and on 405'
'refreshDetails throws unauthorized on 401'
'the demo source answers refreshDetails from the fixture'
'repository keeps the last snapshot when refreshDetails answers null, with no failure'
```

- [ ] **Step 2: Run to verify they fail**: from `console/`, `flutter test test/features/status`. Expected: FAIL.

- [ ] **Step 3: Implement.** Use a 15 s timeout on the POST (5 s of fetching plus margin). Share request handling with `fetch`. Extend `FakeStatusSource` with `refreshDetails`.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: console reads details and integrations"`

### Task 6: Refresh behaviour

**Files:**
- Create: `console/lib/features/status/data/refresh_settings_store.dart`, `console/lib/features/status/presentation/refresh_settings.dart`
- Modify: `console/lib/features/status/presentation/status_controller.dart`, `overview_screen.dart`, `console/lib/core/config/app_config.dart`, `console/lib/main.dart`
- Test: `console/test/features/status/presentation/status_controller_test.dart`, `overview_screen_test.dart`, `console/test/core/format_and_config_test.dart`

**Interfaces:**
- Produces: `const autoRefreshIntervals = <Duration>[1, 5, 10, 15, 30 minutes]`; `class RefreshSettings { bool enabled; Duration interval }` defaulting to off, 1 minute; `refreshSettingsProvider`; `RefreshSettingsStore` with preferences and in-memory implementations; `StatusController.refresh()` now does GET, then POST.

- [ ] **Step 1: Write the failing tests:**

```dart
'loads once on start: one fetch and one refreshDetails'
'does not load again as time passes with auto-refresh off'      // pump 10 min; calls unchanged
'Refresh now loads again'
'changing environment does not load'
'with auto-refresh on, loads every minute by default'
'each listed interval is honoured'                              // 1, 5, 10, 15, 30
'auto-refresh pauses while hidden and loads on return'
'with auto-refresh off, returning from hidden does not load'
'closing the hosts screen does not load with auto-refresh off'
'settings survive a restart'                                    // via the store
'a details answer for the previous host is discarded'           // Review Focus 5
'a failed details refresh keeps the map'
'the one-shot retry still runs after a starting answer'
```

- [ ] **Step 2: Run to verify they fail**: `flutter test test/features/status/presentation`. Expected: FAIL.

- [ ] **Step 3: Implement.** Replace `_poll`'s fixed interval with the settings, running the timer only while `enabled`. Remove `AppConfig.refreshInterval`. Draw from the GET at once, and apply the POST's answer when it arrives, guarded by the same host check `refresh()` has now.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: load once, refresh now and auto-refresh"`

### Task 7: Environment selector and refresh controls

**Files:**
- Create: `console/lib/features/status/presentation/widgets/environment_selector.dart`, `widgets/refresh_controls.dart`
- Modify: `overview_screen.dart`, `console/lib/features/status/domain/status_logic.dart`, `data/refresh_settings_store.dart`, `status_controller.dart`
- Test: `console/test/features/status/presentation/overview_screen_test.dart`, `console/test/features/status/domain/status_logic_test.dart`

**Interfaces:**
- Produces: `selectedHostEnvironmentProvider` (remembered per saved host through `RefreshSettingsStore.readEnvironmentFor(hostId)` / `writeEnvironmentFor(hostId, id)`); `resolveHostEnvironment(HostStatus, String? remembered) : HostEnvironment?` (remembered if present, else the last); `environmentSummaryLine(HostStatus, String environmentId) : String` such as `1 integration failing`.

- [ ] **Step 1: Write the failing tests:**

```dart
'the selector lists the host\'s environments with a status mark'
'the first time, the last environment is selected'
'the choice is remembered per host'
'a remembered environment that is gone falls back to the last'   // Review Focus 4
'no environments shows the empty state'                          // Review Focus 4
'narrow screens get a dropdown'
'summary counts failing integrations in that environment only'
'summary ignores stopped and not-configured integrations'
'the auto-refresh menu offers 1, 5, 10, 15 and 30 minutes'
'updated time is visible beside Refresh now'
'the saved-host switcher is still offered with two hosts'
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement** the selector following `_EnvironmentSwitcher`'s segmented-or-dropdown rule, and the status mark with `StatusVisual`'s icon shape.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: environment selector"`

### Task 8: The map

**Files:**
- Create: `console/lib/features/status/presentation/map/map_layout.dart`, `map/system_map.dart`, `map/system_icons.dart`
- Modify: `overview_screen.dart`, `layout.dart`
- Test: `console/test/features/status/presentation/map/map_layout_test.dart`, `system_map_test.dart`

**Interfaces:**
- Consumes: Task 5's `SystemEnvironment.integrations`, Task 7's selected environment.
- Produces:
  - `int mapColumnsFor(double width)`: 3 below `Breakpoints.tablet`, 4 below `Breakpoints.desktop`, else 5
  - `sealed class MapNode` with `SystemNode(id)` and `ExternalNode(name)`
  - `MapLayout computeMapLayout(HostStatus host, int columns)`, with `MapLayout.cellOf(MapNode) : (int row, int column)`
  - `double distanceToSegment(Offset p, Offset a, Offset b)`
  - `IconData systemIcon(String? name)`, neutral `Icons.hub` for unknown or null
  - `SystemMap(host, environmentId, onOpenSystem)`

- [ ] **Step 1: Write the failing tests:**

```dart
// map_layout_test
'a chain of integrations occupies adjacent cells at every column count'  // Fortuna-Heimdall-Hermes-Mailgun; 3, 4, 5
'layout depends on the host, not the selected environment'
'unconnected systems follow, in catalog order'
'an external appears once however many callers it has'
'empty rows collapse only when every cell is empty'
'distanceToSegment measures to the nearest point'

// system_map_test
'draws only systems deployed in the environment, in their fixed cells'
'each line pill reads OK, Failing, Unknown, Stopped or Not configured'
'a stopped system is not counted or drawn as a problem'
'an on-demand system shows the on demand label, running or stopped'
'an always-on system shows no on demand label'
'an external is captioned external and has no status'
'an unknown or missing icon name shows the neutral icon'
'a response with no icon, integrations or details draws icons and no lines' // Review Focus 3
'every system and line has a semantics label'                               // "Heimdall to Hermes: failing, connection refused"
'a tap near a line opens the caller'                                        // within 12 px
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement `computeMapLayout`.** Spec leaves placement open; use this:

Walk the integration graph across all environments, taking connected components in catalog order, depth-first from each component's first caller. Append nodes to one list, then unconnected systems in catalog order. Place the list along a boustrophedon path (row 0 left to right, row 1 right to left, and so on), so consecutive nodes are always adjacent.

Draw lines with `CustomPaint` beneath the icon widgets, clipped to the circles, and position the pill at each line's midpoint.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: system map with integration lines"`

### Task 9: System panel, and retiring the cards

**Files:**
- Create: `console/lib/features/status/presentation/widgets/system_panel.dart`
- Modify: `overview_screen.dart`, `widgets/application_detail.dart`, `status_logic.dart`, `status_controller.dart`
- Delete: `widgets/system_card.dart` (keep `describeProbe`, moved to `application_detail.dart`)
- Test: `console/test/features/status/presentation/overview_screen_test.dart`, `describe_probe_test.dart`

**Interfaces:**
- Consumes: `SystemMap.onOpenSystem`.
- Produces: `showSystemPanel(context, {system, environment, host})`, a side panel from `Breakpoints.tablet` up and a bottom sheet below it.

- [ ] **Step 1: Write the failing tests:**

```dart
'the panel lists applications with status, version and deploy time'
'an application row opens the existing application detail'
'reported services show status, reason and checked time'
'an unavailable report says it could not get more details, with the reason'
    // "Could not get more details. The detailed health endpoint did not answer: connection refused."
'integrations of an unavailable report read Not checked'
'an unsupported report says this version does not report detailed health'
'no detailedHealth means no Reported by section'
'a stopped dependency reads stopped, not failing'
'the panel shows on demand when it applies'
'a phone gets a bottom sheet, a computer a side panel'
```

Delete the tests that only exercised the card grid, the per-environment chips and "Problems only".

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** Remove `problemsOnlyProvider`, `expandedSystemsProvider`, `visibleSystems`, `systemColumnsFor`, `_SystemGrid`, and anything else left unused.

- [ ] **Step 4: Run the whole suite**: `flutter analyze` and `flutter test`. Expected: no issues, PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: system panel replaces the card grid"`

### Task 10: Demo data and documentation

**Files:**
- Modify: `console/assets/demo/status.json`, `console/tool/mock_status_server.dart`, `console/test/fixtures/contract_example.json`, `console/README.md`, `README.md`, `CHANGELOG.md`
- Test: `console/test/features/status/data/status_data_test.dart`

- [ ] **Step 1: Write the failing test** `'the demo shows a failing integration and an API that does not answer'`: in the fixture's production, Heimdall to Hermes is `down` with `connection refused`, and one API's details have state `unavailable`.

- [ ] **Step 2: Run to verify it fails.** Expected: FAIL.

- [ ] **Step 3: Update the fixture and mock server** (the mock answers `POST /api/status/refresh`), and the README sentence describing the console.

- [ ] **Step 4: Run** `flutter test` and `dotnet test status`, and from `scripts/`, `python -m unittest discover -s . -v`. Expected: PASS.

- [ ] **Step 5: Run the console against the mock server and confirm** selector, map, panel, refresh controls and on-demand label, at desktop and phone widths.

- [ ] **Step 6: Commit**: `git commit -m "docs: demo and docs for the system map"`

## Part 2: heimdall-ui

### Task 11: Remove the Health screen

**Files:**
- Delete: `lib/features/health/presentation/health_screen.dart`, `health_controller.dart`, and their tests under `test/features/health/presentation/`
- Modify: `lib/app/router.dart`, `lib/app/route_access.dart`, `lib/app/session_scoped_state.dart`, `lib/features/home/presentation/destinations.dart`, `lib/features/home/presentation/widgets/admin_dashboard.dart`, `lib/features/health/data/health_repository_impl.dart`, `lib/features/health/domain/health_repository.dart`, `lib/features/health/domain/health.dart`, `api/heimdall.json`, `packages/heimdall_api_client/` (generated)
- Test: `test/features/health/data/health_repository_impl_test.dart`, router and dashboard tests that name `/health`

- [ ] **Step 1: Update the tests:** `/health` is no longer a route; no destination or dashboard tile links to it; `ping()` still works.

- [ ] **Step 2: Run to verify they fail**: `flutter test`. Expected: the updated tests FAIL.

- [ ] **Step 3: Remove the screen and `detailed()`.** Delete the `/api/healthcheck/detailed` path and any schema only it uses from this repository's own `api/heimdall.json`, then run `dart run tool/generate_api_client.dart`. The drift check compares against this repository's own copy, so this merges independently of Heimdall.

- [ ] **Step 4: Run** `flutter analyze`, `flutter test` and `ci/api-client-drift.sh`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: remove the health screen"`

## Part 3: the three APIs

Each API follows the report contract in the spec. Merge Hermes, then Heimdall, then Fortuna, one at a time.

### Task 12: hermes-api

**Files:**
- Create: `src/ArturRios.Hermes.WebApi/Platform/InternalHealthEndpoint.cs`, `Platform/HealthReason.cs`
- Modify: `Platform/MetricsSetup.cs`, `Emails/EmailRegistration.cs`, `docs/requirements/Operations & Infrastructure Document.md`, `CHANGELOG.md`
- Test: `tests/ArturRios.Hermes.WebApi.Tests/Unit/Platform/InternalHealthEndpointTests.cs`

**Interfaces:**
- Produces: `InternalHealthEndpoint.Route = "/internal/health"`; `HealthReason.From(Exception) : string`, which returns only the global vocabulary.

- [ ] **Step 1: Write the failing tests:**

```csharp
GivenMailgunReachable_WhenRequestedOnMetricsPort_ThenHealthy200        // services[0] Name "Mailgun"
GivenMailgunAnswers500_ThenStillHealthy                                // reachability only
GivenMailgunRefused_ThenDegraded200WithReason                          // "connection refused"
GivenMailgunNotConfigured_ThenNotConfiguredAndNoCallMade
GivenApplicationPort_WhenRequested_Then404                             // never on 8080
GivenOtherPathOnMetricsPort_Then404
GivenAnyFailure_ThenNoEmailIsSent
```

- [ ] **Step 2: Run to verify they fail**: `dotnet test --filter InternalHealthEndpointTests`. Expected: FAIL.

- [ ] **Step 3: Implement.** The Mailgun check is an unauthenticated `GET https://api.mailgun.net/`, where any HTTP answer is reachable: no key, no quota. Serve the route in `MetricsSetup.Use`, before the branch that closes the port.

- [ ] **Step 4: Run** `ci/test.sh`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: internal detailed health"`

### Task 13: heimdall-api

**Files:**
- Create: `src/Presentation/ArturRios.Heimdall.WebApi/Monitoring/InternalHealthEndpoint.cs`, `Monitoring/HermesHealthCheck.cs`, `Monitoring/HealthReason.cs`
- Modify: `Metrics/MetricsConfiguration.cs`, `Metrics/MetricsOptions.cs`, `Registration/EdgePipeline.cs`, `Registration/WebApiServiceCollectionExtensions.cs`, `Controllers/HealthCheckController.cs`, `src/Application/ArturRios.Heimdall.Query/HealthChecks/IServiceHealthCheck.cs`, `HealthChecks/DatabaseHealthCheck.cs`, `HealthChecks/HealthStatuses.cs`, `docs/openapi/heimdall.json`, `api-client/http/health.http`, `docs/requirements/Operations & Infrastructure Document.md`, `docs/requirements/Testing Specification Document.md`, `CHANGELOG.md`
- Delete: `Query/Input/DetailedHealthQuery.cs`, `Query/Handlers/GetDetailedHealthQueryHandler.cs`, `Query/Output/HealthCheckOutput.cs`, `Query/Output/ServiceHealthOutput.cs`, `api-client/bruno/Health/Detailed health.bru`, `tests/Application/ArturRios.Heimdall.Query.Tests/GetDetailedHealthQueryHandlerTests.cs`
- Test: `tests/Presentation/ArturRios.Heimdall.WebApi.Tests/InternalHealthTests.cs`, `HealthCheckTests.cs`, `tests/Application/ArturRios.Heimdall.Query.Tests/DatabaseHealthCheckTests.cs`

**Interfaces:**
- Produces: `IServiceHealthCheck { string ServiceName; bool IsRequired; Task<ServiceHealthResult> CheckAsync(CancellationToken) }`; `record ServiceHealthResult(string Status, string? Reason = null)`; `HealthStatuses` gains `Degraded`, `NotConfigured`. `DatabaseHealthCheck.IsHealthyAsync` stays for `/healthcheck/ready`.

- [ ] **Step 1: Write the failing tests** (a test middleware sets `Connection.LocalPort`):

```csharp
GivenAllHealthy_WhenRequestedOnMetricsPort_Then200Healthy            // bare JSON, no DataOutput envelope
GivenHermesDown_Then200DegradedWithReason                            // Hermes is not required
GivenDatabaseDown_Then503Unhealthy
GivenHermesNotConfigured_ThenNotConfiguredAndNoCallMade
GivenApplicationPort_WhenInternalHealthRequested_Then404
GivenAnyOtherPathOnMetricsPort_Then404                               // e.g. /api/auth/login: port closed
GivenMetricsPort_WhenScraped_ThenStillServed
GivenAdminRoute_WhenRequestedAnyAuth_Then404                         // /api/healthcheck/detailed and /healthcheck/detailed
GivenRootHealthcheckAndReady_WhenRequested_ThenStillServed
```

- [ ] **Step 2: Run to verify they fail**: `dotnet test src/ArturRios.Heimdall.sln --filter "InternalHealthTests|HealthCheckTests"`. Expected: FAIL.

- [ ] **Step 3: Implement.** Narrow the `EdgePipeline` rewrite to `(?i)^healthcheck(/ready)?/?$`. `HermesHealthCheck` calls `GET <HermesBaseUrl>/healthcheck` with a 3 s timeout, is not required, and returns `NotConfigured` when `HermesConfigured` is false. Regenerate `docs/openapi/heimdall.json` as the repository's `check` job does.

- [ ] **Step 4: Run** the repository's `ci/test.sh` alone, nothing else heavy alongside it. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: internal detailed health, remove the admin route"`

### Task 14: fortuna-api

**Files:**
- Create: `src/Presentation/ArturRios.Fortuna.WebApi/Observability/InternalHealthEndpoint.cs`, `Services/HeimdallOperationalHealthCheck.cs`, `Services/HealthReason.cs`
- Modify: `Observability/PrometheusMetrics.cs`, `Services/OperationalHealthChecks.cs`, `Extensions/FortunaIntegrationsServiceCollectionExtensions.cs`, `src/Application/ArturRios.Fortuna.Shared/Health/OperationalHealth.cs`, `Output/OperationalHealthOutput.cs`, `docs/requirements/Operations & Infrastructure Document.md`, `CHANGELOG.md`
- Test: `tests/Presentation/ArturRios.Fortuna.WebApi.Tests/InternalHealthTests.cs`

**Interfaces:**
- Produces: `OperationalHealthCheckResult` gains a trailing `string? Reason = null`.

- [ ] **Step 1: Write the failing tests:**

```csharp
GivenHeimdallAnswers2xx_ThenHeimdallHealthy
GivenHeimdallAnswers503_ThenDegradedWithHttpReason                 // "HTTP 503"
GivenHeimdallRefused_ThenDegraded200WithReason
GivenLocalAuthEnabled_ThenHeimdallNotConfiguredAndNoCallMade
GivenDatabaseDown_ThenUnhealthy503
GivenInternalRoute_ThenBodyMatchesTheContract                      // status, services[].name/status/reason
GivenApplicationPort_WhenInternalHealthRequested_Then404
GivenOtherPathOnMetricsPort_Then404
GivenAdminRoute_ThenStillRequiresTheRoleAndListsHeimdall
```

- [ ] **Step 2: Run to verify they fail**: `dotnet test --filter InternalHealthTests`. Expected: FAIL.

- [ ] **Step 3: Implement.** The check uses the `HeimdallJwks.HttpClientName` client against `options.HeimdallJwksUri`, with a 3 s timeout, is not required, and succeeds only on a 2xx. Open the route in `UsePrometheusMetrics` before the 404 branch. Keep the `Aggregator` and `ExchangeRateSource` checks as they are.

- [ ] **Step 4: Run** the repository's `ci/test.sh` alone. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: internal detailed health"`

## Part 4: Turning it on

### Task 15: Catalog entries

**Files:**
- Modify: `catalog.yaml`, `CHANGELOG.md`
- Test: `scripts/test_catalog.py` (the repository's own catalog validates)

- [ ] **Step 1: Add** `icon` to every system (spec's list), and to the three APIs:

```yaml
heimdall-api: detailedHealth http://heimdall-api:9464/internal/health; integrations [{system: hermes, check: Hermes}]
hermes-api:   detailedHealth http://hermes-api:9464/internal/health;   integrations [{external: Mailgun, check: Mailgun}]
fortuna-api:  detailedHealth http://fortuna-api:9464/internal/health;  integrations [{system: heimdall, check: Heimdall}]
```

- [ ] **Step 2: Run** from `scripts/`, `python -m unittest test_catalog`, and `dotnet test status`. Expected: PASS.

- [ ] **Step 3: Commit**: `git commit -m "feat: declare icons and integrations"`

- [ ] **Step 4: After each API is deployed, confirm on the Linux Application Server** that yggdrasil shows reports, not "does not report detailed health", by clicking Refresh now in the console.
