# console

The yggdrasil deploy console: a Flutter app (web, Android and Windows) that shows, for a yggdrasil host, the status of every environment it runs and of every system in them and, expanded, of each application. It reads the status API described in [`docs/status-api.md`](../docs/status-api.md), polling `GET /api/status` every 30 s (fixed) while the overview is visible. Requests time out after 10 s.

## The overview

- **Header**: the host's name (the response's `host`, e.g. `example.com`), its overall status, a summary (`3 systems · 3 environments`, plus a count of the applications that are down, degraded or unknown), and one chip per environment with its status, in catalog order, e.g. `Development · Stopped`, `Homologation · Stopped`, `Production · Up`.
- **System cards**: name, description and one chip per environment the system is deployed to (environment name + status), in the same order. Cards are sorted worst first by the system's overall status, then by name; **Problems only** keeps the systems whose overall status is down, degraded or unknown.
- **Expanded card**: one section per environment (name, status, an *on demand* hint for on-demand environments, and a summary such as `2 applications · all up`) listing its applications. Tapping an application opens its details (a bottom sheet on a phone, a dialog on wider screens), including the environment it is in.
- **Stopped** (pause icon, neutral grey-blue) is the normal state of an on-demand environment that is switched off (`scripts/ygg.sh env stop <environment>`): it is not a problem, does not count for *Problems only*, and sorts between *Up* and *Not deployed*. A stopped application is not probed.
- The platform system (Traefik, the status API, the console, ...) runs once per host and appears in every environment with the same result.
- A host whose status API still answers with the v1 contract (one environment per host) is shown as a host with that one environment, named after it.
- One column on a phone, two from 600 dp, three from 1200 dp; the environment chips wrap on narrow screens.

## Hosts and tokens

A host is a name and the base URL of its status API (the console requests `<url>/api/status`); one host shows every environment it runs. Hosts are managed on the **Hosts** screen (gear icon): **Add host** asks for a name, the URL `https://yggdrasil.<DOMAIN>` and that host's `YGGDRASIL_STATUS_TOKEN`. With several hosts, the overview has a host switcher.

- The list and the selected host are kept in `shared_preferences`; each token in `flutter_secure_storage` (Keystore on Android, DPAPI-encrypted for the Windows user on Windows, WebCrypto-encrypted storage on the web).
- Until a list is saved, the defaults apply: on the web, the page's own origin (Traefik routes `/api/` on the console's host to the status API); plus any compiled in with `--dart-define=YGGDRASIL_ENVIRONMENTS='[{"name":"vps","url":"https://yggdrasil.example.com"}]'` (a list of hosts; the variable keeps its old name). Never put a token there: the bundle is public.
  - The first successful answer from the page's own origin names that host after the response's `host` (from a v1 status API: its `environmentName`, else its `environment` id) **and saves the list**. From then on only the saved list is used: hosts compiled into a later build don't appear by themselves; add them on the Hosts screen.
  - Compiled-in entries with an invalid name or URL, or a duplicate URL, are skipped silently, and invalid JSON gives none.
  - On a platform host, the compiled-in list is `YGGDRASIL_CONSOLE_ENVIRONMENTS` in `platform.env`; `scripts/platform.sh up` rebuilds the console with it.
- A `401` opens the token prompt for that host. A `503` with `Retry-After` (the API's first refresh is not done yet) is shown as "starting" and retried after the given delay, clamped to 1–60 s (5 s if it isn't a number). A `503` without `Retry-After` is an error.
- Polling pauses while the Hosts screen is open; the console refreshes as soon as you return.
- **Offline demo** (Hosts screen, or the empty state) shows `assets/demo/status.json` without any network: the default setup on `example.com`, with development and homologation on demand and stopped, production up, and the heimdall, fortuna and yggdrasil systems.

## Android: HTTPS only

Android builds only allow HTTPS (`usesCleartextTraffic="false"`), so point the app at a real host. The Hosts form still accepts `http://`, but on Android such a host always fails with "The status API could not be reached". The Windows app allows either scheme.

## Windows

The same app, packaged as a normal Windows installer, `yggdrasil-console-<version>-setup.exe`: a per-user install by default (no administrator rights; the installer also offers an install for all users), a Start menu entry, an optional desktop shortcut, in-place upgrades and an uninstaller. On first start it has no host: add one on the **Hosts** screen (gear icon: name, URL `https://yggdrasil.<DOMAIN>`, and that host's `YGGDRASIL_STATUS_TOKEN`), then switch between hosts from the overview, exactly as on Android.

- The window opens at 1280×800 and can shrink to the phone layout, down to 360 px wide.
- Neither the executable nor the installer is code-signed, so SmartScreen warns on first run ("More info" → "Run anyway").

## Getting it

The Windows installer and the APK are attached to every `v*` release of this repository by `.github/workflows/release.yml`, `<version>` being the tag without `v`. On a platform host the web console runs in a container (below), built by `scripts/platform.sh up`.

Building, testing, running it against a mock status API and building the Windows installer are described in [CONTRIBUTING.md](../CONTRIBUTING.md#console).

## Container

```bash
docker build -t yggdrasil-console console/
docker build -t yggdrasil-console --build-arg 'YGGDRASIL_ENVIRONMENTS=[...]' console/
```

nginx (unprivileged, port 8080) serves the static build with an SPA fallback and `/healthz`; it proxies nothing.
