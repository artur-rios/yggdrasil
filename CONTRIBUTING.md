# Contributing

This guide is for changing yggdrasil itself. Installing and running it is in the [README](./README.md) and
[docs/setup.md](docs/setup.md).

## Prerequisites

What CI uses for each part of the repository:

| Part | Needs |
|---|---|
| Status API (`status/`) | [.NET SDK 10.0](https://dotnet.microsoft.com/download), Docker to build its image |
| Console (`console/`) | [Flutter](https://docs.flutter.dev/get-started/install) 3.47.4 (stable channel); Java 17 for the Android APK |
| Console for Windows | Visual Studio with the "Desktop development with C++" workload, including the ATL component (`flutter_secure_storage` uses it), Windows **Developer Mode** (Flutter links plugins with symlinks), and [Inno Setup](https://jrsoftware.org/isinfo.php) 6 for the installer |
| Platform (`scripts/`, `github/`, `platform/`, `stacks/`, `catalog.yaml`) | Python 3 with PyYAML (`pip install pyyaml`; Ubuntu: `apt install python3-yaml`), ShellCheck, Docker with Compose |

## Build and test

CI (`.github/workflows/ci.yml`) runs all of the following on every push and pull request to `develop` and `main`. Run
the part you changed before opening a pull request.

### Status API

```bash
dotnet test status --configuration Release
docker build --tag yggdrasil-status:ci status
```

### Console

```bash
cd console
flutter pub get
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter build web --release --no-web-resources-cdn
flutter build apk --release
```

CI checks the formatting of `lib` and `test`; format `tool` too when you change it
(`dart format lib test tool`). `python tool/generate_icons.py` regenerates the launcher icons (needs Pillow).

How the console behaves at run time (environments, tokens, the offline demo, the container image) is described in
[console/README.md](console/README.md).

#### Against a mock status API

`tool/mock_status_server.dart` serves the demo fixture with CORS and a bearer token, so the console can run without a
platform host:

```bash
cd console
dart run tool/mock_status_server.dart --token mock-status-token-0123456789abcdef
flutter run -d chrome          # then add http://localhost:8090 with that token

# Or build and serve the web bundle same-origin with the mock, as behind Traefik:
flutter build web --release --no-web-resources-cdn
dart run tool/mock_status_server.dart --web build/web   # open http://localhost:8090
```

The mock's options: `--port` (default 8090), `--token` (default `mock-status-token-0123456789abcdef`), `--fixture`
(the JSON it serves), `--web` (a web build to serve same-origin) and `--starting-seconds 10`, which makes it answer
`503` + `Retry-After: 5` for its first 10 s.

Android builds only allow HTTPS, so the Android app cannot use the mock
([console/README.md](console/README.md#android-https-only)). The Windows app allows either scheme, so it can use the
mock and a development status API too.

#### Windows installer

On Windows, from `console/`, with the prerequisites above:

```powershell
flutter build windows --release --build-name=0.4.0
foreach ($dll in "msvcp140.dll","vcruntime140.dll","vcruntime140_1.dll") { Copy-Item "$env:WINDIR\System32\$dll" build\windows\x64\runner\Release }
iscc /DAppVersion=0.4.0 windows\installer\yggdrasil.iss   # -> build\windows\installer\yggdrasil-console-0.4.0-setup.exe
```

- `--build-name` sets the version in `yggdrasil.exe`'s file properties; `/DAppVersion` is the installer's version (shown
  in Windows' installed apps list) and its file name. Keep them equal. Neither is shown inside the app.
- The `foreach` line copies the Visual C++ runtime next to `yggdrasil.exe`, as CI does, so the installed app runs on
  machines without it. The installer ships whatever is in `build\windows\x64\runner\Release`, those DLLs included.
- `iscc` is the [Inno Setup](https://jrsoftware.org/isinfo.php) compiler. Never change the `AppId` in
  `yggdrasil.iss`: it is what lets a new version upgrade an installed one in place.
- Neither the executable nor the installer is code-signed. Signing needs a certificate: add a `SignTool` line to
  `yggdrasil.iss` once there is one.

CI builds the installer (artifact `yggdrasil-console-windows`) and the APK (artifact `yggdrasil-console-android`) on
every run; see [Versioning](#versioning) for the version they carry.

### Platform

```bash
shellcheck scripts/*.sh
python3 -m unittest discover -s scripts -v
python3 scripts/catalog.py validate
```

CI also validates `platform/compose.yml` and every stack in `stacks/` with `docker compose config`, using dummy values
for each variable they require.

`scripts/test_socket_proxies.py` checks the Docker socket proxies' allowlists in `platform/compose.yml`. With Docker,
CI also runs the proxies, then Traefik and Alloy through them, against a fake Docker API; locally:

```bash
YGG_DOCKER_TESTS=1 python3 -m unittest scripts.test_socket_proxies -v
```

It creates an internal network and a few `ygg-sp-test-*` containers, removes them, and never mounts the real Docker
socket. When Traefik or Alloy is upgraded, run it: a request the new version makes that its proxy refuses fails it.

## Branching and pull requests

This repository follows the same branching model it enforces on application repositories (see
[Branches and releases](README.md#branches-and-releases)). `develop` is the integration branch and the base for all
new work; `main` only holds released code.

The **Branch Policy** workflow (`.github/workflows/branch-policy.yml`, check `branch-policy`) runs on every pull
request into `develop` or `main`:

- Into `develop` go only `feature/<name>` and `fix/<name>` branches (`dependabot/...` is also accepted) cut from
  `develop`. `<name>` starts with a lowercase letter or digit, then lowercase letters, digits, `.`, `_` or `-`. A branch
  that contains a release merge commit from `main` was cut from `main` and is rejected.
- Into `main` go only `release/<major>.<minor>.<patch>` branches that are snapshots of `develop`: every commit on them
  is already on `develop`, and the version has no `v<version>` tag yet. A release carries no changes of its own; a fix
  goes to `develop` through a `fix/` branch and a new release is cut.

Repository rulesets, the same ones [`github/rulesets.py`](github/rulesets.py) applies to application repositories,
hold this in place:

| Ruleset | Rules |
|---|---|
| `develop` | No deletion or force push; changes only through a pull request (squash or merge commit, no approval required) whose checks pass: `branch-policy`, `Status API`, `Console`, `Console (Windows)` and `Platform` |
| `main` | The same, merge commit only. No `deploy/<environment>` status: Jenkins does not deploy or merge this repository |
| `release tags` | `v*` tags can't be created, moved or deleted, except by the repository owner |

The owner (repository admin role) can bypass all three, for emergencies. `develop` is the default branch, and head
branches are deleted when their pull request merges.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) with a lowercase subject, e.g.
`feat: add a host helper menu for ubuntu hosts` or `fix: accept a release branch github already deleted`.

Record every change an operator of yggdrasil would notice under `## [Unreleased]` in [CHANGELOG.md](./CHANGELOG.md),
in the same pull request that makes it.

## Versioning

yggdrasil follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). One version covers the whole
repository (platform, scripts, catalog format, Jenkins library, application templates, status API and console),
because a host runs a checkout of one release of all of it. For yggdrasil, the parts of a version mean:

- **Major**: an existing installation stops working, or needs its operator to act, after updating. For example: a
  `catalog.yaml` field removed, renamed or given a new meaning; a new required variable in `platform.env`, `acme.env`
  or an application env file; a stack file convention that existing `stacks/` files no longer meet; a change to the
  [status API contract](docs/status-api.md) that a console of the previous release can't read; a change application
  repositories must follow (their `Jenkinsfile`, Branch Policy or rules, `templates/application/`); data in the
  platform's volumes that has to be migrated.
- **Minor**: something new that an existing installation can ignore: an optional catalog field or variable, a
  command, a console feature, an added status API field.
- **Patch**: a fix that asks nothing of the operator.

yggdrasil is still at 0.x. SemVer allows anything to change before 1.0.0; this repository applies it as follows until
then: a change that would need a major version increments the **minor** version, and its CHANGELOG entry says what an
operator has to do. 0.3.0, which replaced the fixed development, homologation and production environments with the
catalog's, was such a minor increment. Everything else increments the patch.

There is no version file to bump. The version is the release branch's name (`release/<version>`) and the tag
`v<version>` the GitHub release creates; the CHANGELOG heading records it. `release.yml` passes the tag, without its
`v`, to `flutter build --build-name` and to the installer's `/DAppVersion`, so the released Windows installer, its
executable and the APK carry it.

`version: 1.0.0+1` in `console/pubspec.yaml` is not the release version, and is not bumped for a release:

- Release builds override its name with `--build-name`, but its build number (`+1`) still applies: every APK has
  `versionCode` 1, and `yggdrasil.exe`'s file version has a fourth part `1` (0.4.0.1 for 0.4.0).
- Builds without `--build-name` use it as it is: CI's Windows installer artifact is `yggdrasil-console-1.0.0-setup.exe`,
  and the web console each host builds from `console/Dockerfile` is 1.0.0. Nothing in the console displays its own
  version.

## Releasing

1. Since a release branch carries no commits of its own, finalize the changelog on `develop` first: in a `feature/` or
   `fix/` branch, rename `## [Unreleased]` in [CHANGELOG.md](./CHANGELOG.md) to `## [<version>] - <yyyy-mm-dd>` above
   a fresh, empty `## [Unreleased]`, update the compare links at the bottom, and merge it into `develop`.
2. Cut the release branch from an up-to-date `develop`, push it and open a pull request into `main`:

   ```bash
   git switch develop && git pull && git switch -c release/<version> && git push -u origin release/<version>
   ```

3. Once the Branch Policy and CI checks pass, merge it with a merge commit.
4. Create the GitHub release `v<version>` on `main`, which creates the tag:

   ```bash
   gh release create v<version> --target main --generate-notes
   ```

   Only the owner can create `v*` tags (the `release tags` ruleset). Pushing the tag runs
   `.github/workflows/release.yml`, which first checks that the tagged commit is on `main` (job `Tag is on main`),
   then builds the console's Windows installer and Android APK and attaches them to that release. It can also be run
   by hand for an existing tag.

`main` is not merged back into `develop`: the release merge commits stay on `main`, and the Branch Policy rejects any
branch into `develop` that contains one.

Hosts pick up a release as described in the README's [Day to day](README.md#day-to-day) section ("Update a host's
platform or catalog").
