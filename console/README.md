# console

The yggdrasil deploy console: a Flutter app (web, Android and Windows) that shows the status of every system in an environment and, expanded, of each of its applications. It reads the status API described in [`docs/status-api.md`](../docs/status-api.md), polling `GET /api/status` every 30 s (fixed) while the overview is visible. Requests time out after 10 s.

## Environments and tokens

An environment is a name and the base URL of its status API (the console requests `<url>/api/status`). They are managed on the **Environments** screen (gear icon):

- The list and the selected environment are kept in `shared_preferences`; each token in `flutter_secure_storage` (Keystore on Android, DPAPI-encrypted for the Windows user on Windows, WebCrypto-encrypted storage on the web).
- Until a list is saved, the defaults apply: on the web, the page's own origin (Traefik routes `/api/` on the console's host to the status API); plus any compiled in with `--dart-define=YGGDRASIL_ENVIRONMENTS='[{"name":"production","url":"https://yggdrasil.example.com"}]'`. Never put a token there: the bundle is public.
  - The first successful answer from the page's own origin names that environment after the response's `environment` field **and saves the list**. From then on only the saved list is used: environments compiled into a later build don't appear by themselves; add them on the Environments screen.
  - Compiled-in entries with an invalid name or URL, or a duplicate URL, are skipped silently, and invalid JSON gives none.
  - On a platform host, the compiled-in list is `YGGDRASIL_CONSOLE_ENVIRONMENTS` in `platform.env`; `scripts/platform.sh up` rebuilds the console with it.
- A `401` opens the token prompt for that environment. A `503` with `Retry-After` (the API's first refresh is not done yet) is shown as "starting" and retried after the given delay, clamped to 1–60 s (5 s if it isn't a number). A `503` without `Retry-After` is an error.
- Polling pauses while the Environments screen is open; the console refreshes as soon as you return.
- **Offline demo** (Environments screen, or the empty state) shows `assets/demo/status.json`, the contract example expanded to every status and kind, without any network.

## Development

```bash
flutter pub get
flutter analyze && dart format --set-exit-if-changed lib test tool && flutter test

# Against a mock status API (serves the demo fixture, CORS, bearer token):
dart run tool/mock_status_server.dart --token mock-status-token-0123456789abcdef
flutter run -d chrome          # then add http://localhost:8090 with that token

# Or build and serve the web bundle same-origin with the mock, as behind Traefik:
flutter build web --release --no-web-resources-cdn
dart run tool/mock_status_server.dart --web build/web   # open http://localhost:8090
```

The mock's options: `--port` (default 8090), `--token` (default `mock-status-token-0123456789abcdef`), `--fixture` (the JSON it serves), `--web` (a web build to serve same-origin) and `--starting-seconds 10`, which makes it answer `503` + `Retry-After: 5` for its first 10 s. `python tool/generate_icons.py` regenerates the launcher icons (needs Pillow). CI checks the formatting of `lib` and `test`.

Android builds only allow HTTPS (`usesCleartextTraffic="false"`), so point the app at a real host, not the mock. The Environments form still accepts `http://`, but on Android such an environment always fails with "The status API could not be reached". The Windows app allows either scheme, so it can use the mock and a development status API too.

## Windows

The same app, packaged as a normal Windows installer: a per-user install by default (no administrator rights; the installer also offers an install for all users), a Start menu entry, an optional desktop shortcut, in-place upgrades and an uninstaller. On first start it has no environment: add one on the **Environments** screen (gear icon: name, URL `https://yggdrasil.<DOMAIN>`, and that host's `YGGDRASIL_STATUS_TOKEN`), then switch between environments from the overview, exactly as on Android.

```powershell
flutter build windows --release --build-name=0.2.0
foreach ($dll in "msvcp140.dll","vcruntime140.dll","vcruntime140_1.dll") { Copy-Item "$env:WINDIR\System32\$dll" build\windows\x64\runner\Release }
iscc /DAppVersion=0.2.0 windows\installer\yggdrasil.iss   # -> build\windows\installer\yggdrasil-console-0.2.0-setup.exe
```

- `--build-name` and `/DAppVersion`: the version shown in the app and the installer; keep them equal.
- The `foreach` line copies the Visual C++ runtime next to `yggdrasil.exe`, as CI does, so the installed app runs on machines without it.
- `iscc` is the [Inno Setup](https://jrsoftware.org/isinfo.php) compiler.

- Building needs Visual Studio with the "Desktop development with C++" workload, which must include the ATL component because `flutter_secure_storage` uses it. Flutter also requires Windows **Developer Mode** to build apps that use plugins, because it links them with symlinks.
- CI builds the installer (artifact `yggdrasil-console-windows`) and the APK (artifact `yggdrasil-console-android`) on every push and pull request to `main` and `develop`.
- `release.yml` runs on every `v*` tag (or by hand, with an existing tag) and attaches `yggdrasil-console-<version>-setup.exe` and `yggdrasil-console-<version>.apk` to that GitHub release, `<version>` being the tag without `v`.
- The installer ships whatever is in `build\windows\x64\runner\Release`, including the Visual C++ runtime DLLs copied there above.
- The window opens at 1280×800 and can shrink to the phone layout, down to 360 px wide.
- Neither the executable nor the installer is code-signed, so SmartScreen warns on first run ("More info" → "Run anyway"). Signing needs a certificate: add a `SignTool` line to `yggdrasil.iss` once you have one.

## Container

```bash
docker build -t yggdrasil-console console/
docker build -t yggdrasil-console --build-arg 'YGGDRASIL_ENVIRONMENTS=[...]' console/
```

nginx (unprivileged, port 8080) serves the static build with an SPA fallback and `/healthz`; it proxies nothing.
