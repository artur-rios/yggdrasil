# console

The yggdrasil deploy console: a Flutter app (web, Android and Windows) that shows the status of every system in an environment and, expanded, of each of its applications. It reads the status API described in [`docs/status-api.md`](../docs/status-api.md), polling `GET /api/status` every 30 s (fixed) while the overview is visible. Requests time out after 10 s.

## Environments and tokens

An environment is a name and the base URL of its status API (the console requests `<url>/api/status`). They are managed on the **Environments** screen (gear icon):

- The list and the selected environment are kept in `shared_preferences`; each token in `flutter_secure_storage` (Keystore on Android, DPAPI-encrypted for the Windows user on Windows, WebCrypto-encrypted storage on the web).
- Until a list is saved, the defaults apply: on the web, the page's own origin (Traefik routes `/api/` on the console's host to the status API); plus any compiled in with `--dart-define=YGGDRASIL_ENVIRONMENTS='[{"name":"production","url":"https://yggdrasil.example.com"}]'`. Never put a token there: the bundle is public.
  - The first successful answer from the page's own origin names that environment after the response's `environmentName` (its `environment` id, from a status API that predates the field) **and saves the list**. From then on only the saved list is used: environments compiled into a later build don't appear by themselves; add them on the Environments screen.
  - Compiled-in entries with an invalid name or URL, or a duplicate URL, are skipped silently, and invalid JSON gives none.
  - On a platform host, the compiled-in list is `YGGDRASIL_CONSOLE_ENVIRONMENTS` in `platform.env`; `scripts/platform.sh up` rebuilds the console with it.
- A `401` opens the token prompt for that environment. A `503` with `Retry-After` (the API's first refresh is not done yet) is shown as "starting" and retried after the given delay, clamped to 1–60 s (5 s if it isn't a number). A `503` without `Retry-After` is an error.
- Polling pauses while the Environments screen is open; the console refreshes as soon as you return.
- **Offline demo** (Environments screen, or the empty state) shows `assets/demo/status.json`, the contract example expanded to every status and kind, without any network.

## Android: HTTPS only

Android builds only allow HTTPS (`usesCleartextTraffic="false"`), so point the app at a real host. The Environments form still accepts `http://`, but on Android such an environment always fails with "The status API could not be reached". The Windows app allows either scheme.

## Windows

The same app, packaged as a normal Windows installer, `yggdrasil-console-<version>-setup.exe`: a per-user install by default (no administrator rights; the installer also offers an install for all users), a Start menu entry, an optional desktop shortcut, in-place upgrades and an uninstaller. On first start it has no environment: add one on the **Environments** screen (gear icon: name, URL `https://yggdrasil.<DOMAIN>`, and that host's `YGGDRASIL_STATUS_TOKEN`), then switch between environments from the overview, exactly as on Android.

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
