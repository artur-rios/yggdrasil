# console

The yggdrasil deploy console: a Flutter app (web and Android) that shows the status of every system in an environment and, expanded, of each of its applications. It reads the status API described in [`docs/status-api.md`](../docs/status-api.md), polling `GET /api/status` every 30 s while it is visible.

## Environments and tokens

An environment is a name and the base URL of its status API (the console requests `<url>/api/status`). They are managed on the **Environments** screen (gear icon):

- The list and the selected environment are kept in `shared_preferences`; each token in `flutter_secure_storage` (Keystore on Android, WebCrypto-encrypted storage on the web).
- Until a list is saved, the defaults apply: on the web, the page's own origin (Traefik routes `/api/` on the console's host to the status API), named after the `environment` field of its first response; plus any compiled in with `--dart-define=YGGDRASIL_ENVIRONMENTS='[{"name":"production","url":"https://yggdrasil.example.com"}]'`. Never put a token there: the bundle is public.
- A `401` opens the token prompt for that environment. A `503` with `Retry-After` (the API's first refresh is not done yet) is shown as "starting" and retried after the given delay.
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

`--starting-seconds 10` makes the mock answer `503` + `Retry-After: 5` for its first 10 s. `python tool/generate_icons.py` regenerates the launcher icons (needs Pillow).

Android builds only allow HTTPS (`usesCleartextTraffic="false"`), so point the app at a real host, not the mock.

## Container

```bash
docker build -t yggdrasil-console console/
docker build -t yggdrasil-console --build-arg 'YGGDRASIL_ENVIRONMENTS=[...]' console/
```

nginx (unprivileged, port 8080) serves the static build with an SPA fallback and `/healthz`; it proxies nothing.
