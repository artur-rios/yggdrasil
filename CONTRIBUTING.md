# Contributing

This guide is for changing yggdrasil itself. Installing and running it is in the [README](./README.md) and
[docs/setup.md](docs/setup.md).

The repository lives at [git.techcraftsmen.net/artur-rios/yggdrasil](https://git.techcraftsmen.net/artur-rios/yggdrasil):
clone it from there and open pull requests there.
[github.com/artur-rios/yggdrasil](https://github.com/artur-rios/yggdrasil) is a read-only mirror: it runs the GitHub
Actions jobs below, and takes no pushes or pull requests.

```bash
git clone https://git.techcraftsmen.net/artur-rios/yggdrasil.git
```

## Prerequisites

What CI uses for each part of the repository:

| Part | Needs |
|---|---|
| Status API (`status/`) | [.NET SDK 10.0](https://dotnet.microsoft.com/download), Docker to build its image |
| Console (`console/`) | [Flutter](https://docs.flutter.dev/get-started/install) 3.47.4 (stable channel); Java 17 for the Android APK |
| Console for Windows | Visual Studio with the "Desktop development with C++" workload, including the ATL component (`flutter_secure_storage` uses it), Windows **Developer Mode** (Flutter links plugins with symlinks), and [Inno Setup](https://jrsoftware.org/isinfo.php) 6 for the installer |
| Platform (`scripts/`, `github/`, `platform/`, `stacks/`, `catalog.yaml`) | Python 3 with PyYAML (`pip install pyyaml`; Ubuntu: `apt install python3-yaml`), ShellCheck, Docker with Compose |

## Build and test

Jenkins runs the following as the checks of [`yggdrasil.yml`](yggdrasil.yml), `status`, `console` and `platform`
(`ci/status.sh`, `ci/console.sh`, `ci/platform.sh`, each in its pinned image), on every pull request and every push
to `develop`, `main` and `release/*`. The Windows console is built by the GitHub Actions job `Console (Windows)`
(`.github/workflows/ci.yml`) on the mirror, and Jenkins copies its result. Run the part you changed before opening a
pull request, or its script in its image, as Jenkins does ([The pipeline](#the-pipeline)):

```bash
bash scripts/run-check.sh console <the console image in yggdrasil.yml> "$PWD" -- sh -c 'ci/console.sh'
```

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

How the console behaves at run time (hosts and their environments, tokens, the offline demo, the container image) is
described in [console/README.md](console/README.md).

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
python3 scripts/run_tests.py
python3 scripts/catalog.py validate
```

`scripts/run_tests.py` runs the same tests as `python3 -m unittest discover -s scripts -v`, in parallel: a few
minutes instead of twenty (`-j` sets the number of workers; `-j 1` runs them one after another). Run a single
module or test with unittest as usual (`python3 -m unittest test_machine` from `scripts/`).

CI also validates `platform/compose.yml` and every stack in `stacks/` with `docker compose config`, using dummy values
for each variable they require. Traefik's configuration is rendered first, as `platform.sh` does
(`scripts/traefik_config.py`), because `compose.yml` requires that file.

`scripts/test_socket_proxies.py` checks the Docker socket proxies' allowlists in `platform/compose.yml`. With Docker,
CI also runs the proxies, then Traefik and Alloy through them, against a fake Docker API; locally:

```bash
YGG_DOCKER_TESTS=1 python3 -m unittest discover -s scripts -p test_socket_proxies.py -v
```

It creates an internal network and a few `ygg-sp-test-*` containers, removes them, and never mounts the real Docker
socket. When Traefik or Alloy is upgraded, run it: a request the new version makes that its proxy refuses fails it.

`scripts/test_forge.py` and `scripts/test_rules.py` check `scripts/forge.sh` and `scripts/rules.py` against fake
servers. `scripts/test_forgejo_live.py` checks that the fakes are right, against a real Forgejo: it starts the
image `platform/compose.yml` pins in one `ygg-forgejo-test-*` container (SQLite, a free local port), makes the
users and repositories, applies the rules and tries them (a developer cannot merge a `Jenkinsfile` change, push
`release/*` or create, move or delete a `v*` tag; a `rules: basic` repository refuses force pushes and deletions;
the owner can still merge past failing checks). One test pins a known gap instead: the Jenkins user, which creates
the `v*` tags, can also move and delete them (docs/setup.md, step 12). With Docker, bash, jq and git:

```bash
YGG_FORGEJO_TESTS=1 python3 -m unittest discover -s scripts -p test_forgejo_live.py -v
```

It takes about 20 seconds and removes its container at the end, also when it fails. CI runs it (check `platform`;
there Forgejo joins the check's own container's network, `YGG_FORGEJO_TEST_NETWORK`, since the engine's published
ports are out of a check's reach).
When Forgejo is upgraded, run it: a changed answer of the API fails it.

### The pipeline

A repository that takes part in the pipeline writes a `yggdrasil.yml` ([docs/catalog.md](docs/catalog.md#repository-pipeline-file))
and, usually, a `ci/` folder of scripts for its `run` lines, so that a check is the same thing in Jenkins and on
your machine. The pieces of the pipeline in this repository are tested with the platform suite above:
`test_pipeline_config.py` (the file and the plan of the checks), `test_pipeline_library.py` (static, and it pins
the library's security rules: which agent may bind which credential, that no value reaches a shell through
Groovy interpolation), `test_jenkins_config.py` (the Configuration as Code files, the Job DSL, `agents.groovy`),
`test_branch_policy.py`, `test_run_check.py`, `test_publish_release.py`, `test_release_notes.py`,
`test_nuget_push.py` and `test_renovate.py`.

To run what Jenkins runs, from a checkout of the repository in question and of this one:

```bash
# The file read and checked against the catalog (YGG_CATALOG points it at another catalog.yaml); exits 1 on errors
python3 scripts/pipeline_config.py describe <job-id> <repository-checkout>
# One check, in its image, as the CI agent runs it (the workspace is an absolute path)
bash scripts/run-check.sh unit mcr.microsoft.com/dotnet/sdk:10.0 "$PWD" -- sh -c 'ci/test.sh'
# The branching model, on a branch of the checkout (needs origin/develop, origin/main and the tags fetched)
bash scripts/branch-policy.sh feature/x develop "$(git rev-parse HEAD)"
```

`run-check.sh` runs the command as root in the container, as in Jenkins, so files it writes into the checkout
may belong to root. Add `--docker` (and `YGG_DOCKER_SOCKET`, the host path of your engine's socket) for a check
that starts containers.

The library, `jenkins/library/vars/yggdrasilPipeline.groovy`, runs on Jenkins only; its header describes the
flow it implements. A change to it reaches an installation by a release of this repository and a new
`YGGDRASIL_LIBRARY_VERSION` ([docs/setup.md](docs/setup.md#how-a-build-runs)).

### Live suites

Two suites run the real thing in containers. Both are skipped unless their variable is set, need Docker, and are
not part of the platform suite:

```bash
YGG_FORGEJO_TESTS=1 python3 -m unittest discover -s scripts -p test_forgejo_live.py -v   # forge.sh and rules.py against Forgejo, ~20 s (above)
YGG_JENKINS_TESTS=1 python3 -m unittest discover -s scripts -p test_jenkins_live.py -v   # the pipeline against Jenkins and Forgejo, minutes
```

`test_jenkins_live.py` builds the Jenkins controller and the build agent from `platform/jenkins`, starts Forgejo,
the controller, the `ci`, `release` and deploy agents and a local NuGet feed, and runs pull requests, pushes,
releases (with the skip and `release-resume`), owner tags and a NuGet push through the library. It needs the
engine to see this machine's paths as they are (a native Linux engine, or WSL's own, not a VM that translates
them for its CLI) and takes many minutes: the GitHub Actions workflow `.github/workflows/pipeline-live.yml` runs it on
the mirror, only when it is started by hand (`workflow_dispatch`); it is not a check. GitHub is replaced by a test double there, and the engine is not rootless: **the
GitHub paths and rootless Docker are not proven by it**. When Jenkins, a plugin or the Forgejo image is
upgraded, run it.

## Branching and pull requests

This repository follows the same branching model it enforces on application repositories (see
[Branches and releases](README.md#branches-and-releases)). `develop` is the integration branch and the base for all
new work; `main` only holds released code.

The **branch policy** ([`scripts/branch-policy.sh`](scripts/branch-policy.sh), status `branch-policy`), which Jenkins
runs on every pull request into `develop` or `main`, as for every repository:

- Into `develop` go only `feature/<name>` and `fix/<name>` branches (and Renovate's `renovate/...`) cut from
  `develop`. `<name>` starts with a lowercase letter or digit, then lowercase letters, digits, `.`, `_` or `-`. A branch
  that contains a release merge commit from `main` was cut from `main` and is rejected.
- Into `main` go only `release/<major>.<minor>.<patch>` branches that are snapshots of `develop`: every commit on them
  is already on `develop`, and the version has no `v<version>` tag yet. A release carries no changes of its own; a fix
  goes to `develop` through a `fix/` branch and a new release is cut.

The branch and tag protections [`scripts/rules.py`](scripts/rules.py) applies to every catalog repository hold this
in place (this one is the catalog's `yggdrasil` entry: `release: owner-tag`, `rules: release`):

| Branch or tag | Rules |
|---|---|
| `develop` | No deletion or force push; changes only through a pull request (squash or merge commit, no approval required) whose checks pass: `branch-policy`, `status`, `console`, `platform` and `Console (Windows)` |
| `main` | The same without `Console (Windows)` (GitHub Actions checks are required on `develop` only), merge commit only, merged by the owner. No `deploy/<environment>` status: Jenkins does not deploy or merge this repository |
| `v*` tags | Created only by the owner ([docs/setup.md, step 12](docs/setup.md#12-apply-the-branch-rules) has Forgejo's accepted gap; on the GitHub mirror nobody can move or delete them) |

The owner (repository admin) can merge past failing checks, for emergencies (**Merge anyway** on Forgejo).
`develop` is the default branch, and head branches are deleted when their pull request merges.

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
  repositories must follow (their `yggdrasil.yml`, branch policy or rules, `templates/application/`); data in the
  platform's volumes that has to be migrated.
- **Minor**: something new that an existing installation can ignore: an optional catalog field or variable, a
  command, a console feature, an added status API field.
- **Patch**: a fix that asks nothing of the operator.

yggdrasil is still at 0.x. SemVer allows anything to change before 1.0.0; this repository applies it as follows until
then: a change that would need a major version increments the **minor** version, and its CHANGELOG entry says what an
operator has to do. 0.3.0, which replaced the fixed development, homologation and production environments with the
catalog's, was such a minor increment. Everything else increments the patch.

There is no version file to bump. The version is the release branch's name (`release/<version>`) and the tag
`v<version>` the owner pushes; the CHANGELOG heading records it. The release build passes the tag, without its `v`,
to `flutter build --build-name` (`ci/release-build.sh` for the APK, `.github/workflows/release-windows.yml` for
Windows) and to the installer's `/DAppVersion`, so the released Windows installer, its executable and the APK carry
it.

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

3. Once the branch policy and the checks pass, merge it with a merge commit.
4. Tag the merge commit on `main`, **annotated**, and push the tag:

   ```bash
   git switch main && git pull
   git tag -a v<version> -m v<version>
   git push origin v<version>
   ```

   Only the owner can create `v*` tags. Jenkins builds the tag on the release agent
   ([Owner tags and NuGet](docs/setup.md#owner-tags-and-nuget)): it checks that the tagged commit is on `main`, builds
   the Android APK (`release.build`, `ci/release-build.sh`), has the mirror's GitHub Actions build the Windows
   installer (`release.windows`, `.github/workflows/release-windows.yml`, dispatched on the tag), and publishes the
   release with both files and the version's CHANGELOG section as its notes, on Forgejo and then on the GitHub mirror.
   A tag that was not built (a lightweight tag on an old commit, say) is finished with the `release-resume` job
   ([docs/setup.md](docs/setup.md#finishing-a-release-that-stopped)).

`main` is not merged back into `develop`: the release merge commits stay on `main`, and the branch policy rejects any
branch into `develop` that contains one.

Hosts pick up a release as described in the README's [Day to day](README.md#day-to-day) section ("Update a host's
platform or catalog").
