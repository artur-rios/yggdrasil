# .NET library and its docs site

For each library: the checks (format, build, test categories, tests; the coverage report), the NuGet packing, and the
docs site's image. It replaces the library's `run-tests.yml`, `build-docs-and-coverage-report.yml` and
`publish-package.yml`.

## Files to copy

| From this template | To the library |
|---|---|
| `ci/test.sh`, `ci/coverage.sh`, `ci/pack.sh`, `ci/branch-policy.sh` | the same paths (executable) |
| `yggdrasil.yml` | `yggdrasil.yml` |
| `Dockerfile`, `.dockerignore` | the same paths (the docs site's image, and what its build context leaves out) |
| `.gitattributes` | `.gitattributes`, or its lines added to the one the library has (`*.sh`, `Dockerfile` and `yggdrasil.yml` are checked out with LF, on Windows too) |

Replace `<Name>` in `SOLUTION=src/ArturRios.<Name>.sln` at the top of `ci/test.sh`, `ci/coverage.sh` and `ci/pack.sh`; a
script refuses to run while the placeholder is there. Add `coverage/`, `coverage-report/` and `artifacts/` to
`.gitignore`. `ci/branch-policy.sh` has no placeholder: it reads `SOLUTION` from `ci/pack.sh`.

`ci/branch-policy.sh` and the `branchPolicy` section of `yggdrasil.yml` are for a single-package library whose old
`branch-policy.yml` had a "Validate release version" step; any other library deletes both. A pull request from
`release/x.y.z` into `main` then fails unless every packed project's `<Version>` is `x.y.z`.

## Two catalog entries, one file

The library is a `repositories` entry; its docs site is an application whose `repository` is the library:

```yaml
repositories:
  - id: dotnet-util
    release: owner-tag
    tags: "[0-9]*"
    checks: [test, coverage]
    credentials: [reportgenerator-license]
# ... and in a system's applications:
      - id: dotnet-util-docs
        repository: dotnet-util
        checks: []
        credentials: [reportgenerator-license]   # its deploy runs coverage, which binds it
        domain: artur-rios.tech
        host: docs                 # docs.artur-rios.tech/dotnet-util
        path: /dotnet-util
```

Each job runs only its entry's checks. The docs job runs `coverage` as well when it deploys, because
`deploy.inputs: [coverage]` names it: the check's archive, `coverage-report/`, is copied into the build context's root
by `scripts/deploy.sh`, and the `Dockerfile` puts it into the Hugo site (`docs/coverage-report`, which `docs/hugo.toml`
mounts as `static/coverage-report`). The docs application's stack files are `templates/stacks/site.yml` and
`site-path.proxy.yml`, with `APPLICATION_ID` replaced by its id.

## The checks

- **`test`** (`ci/test.sh`): `dotnet restore`, `dotnet format --verify-no-changes`, `dotnet build`, a check that every test
  has a `Category` trait (`Unit` or `Functional`), then the tests of each category, with the `.trx` results kept under
  `artifacts/test-results/`. Functional tests that use Testcontainers need `docker: true` on the check.
- **`coverage`** (`ci/coverage.sh`): the tests with coverage collected, and ReportGenerator's HTML report in
  `coverage-report/`. With `REPORTGENERATOR_LICENSE` (the Jenkins credential `reportgenerator-license`, in the catalog's
  `credentials`) it makes the PRO report. Without it, which is the case on every `renovate/*` build, it makes the free
  report and says so in the log. ReportGenerator runs with `-verbosity:Warning`: at its default verbosity it prints
  its arguments, the license's first 6 characters among them, and Jenkins masks only the whole string.
- **`publish.nuget`** (`ci/pack.sh`): on the owner's version tag, packs into `artifacts/`; the release agent pushes
  `artifacts/*.nupkg` with the NuGet key, which the packing never sees. With `RELEASE_VERSION` set (the tag without a
  `v`; `N.N.N` or `N.N.N-pre`, anything else stops the script), each package must be named
  `<PackageId>.<RELEASE_VERSION>.nupkg` for a packable project of the solution, as the old workflow's
  tag-against-`<Version>` check did. A library that
  versions each package apart tags `<package>@<version>` (catalog `tags: "*@[0-9]*"`): `RELEASE_VERSION` is what follows
  the `@`, and its `ci/pack.sh` must pack only the package `RELEASE_TAG` names, since the others keep their own
  versions.

## The docs site

The image builds `docs/` with Hugo (extended, Node and npm, pinned), `npm ci` for the Docsy theme, and serves it with
static-web-server on port 8080 as a non-root user. `/healthz` answers 200, from a file of its own: the image has no
shell, so no `HEALTHCHECK`.

Build arguments, passed by `templates/stacks/site.yml`:

| Argument | Comes from |
|---|---|
| `GIT_COMMIT`, `YGG_ENVIRONMENT` | the commit and the environment being deployed (`{{ getenv "HUGO_GIT_COMMIT" }}` and `{{ getenv "HUGO_YGG_ENVIRONMENT" }}` in a Hugo template) |
| `PUBLIC_HOST`, `PUBLIC_PATH` | `YGG_PUBLIC_HOST` and `YGG_PUBLIC_PATH`: Hugo builds with `--baseURL https://<host><path>/`, so leave the `baseURL` of `docs/hugo.toml` alone |
| `DOCS_DIR` | not passed; `docs` unless the stack file says otherwise |

The site is served under `PUBLIC_PATH`, `/dotnet-util`, since the proxy does not strip it. Replace the library's
`github.io` addresses (README, `PackageProjectUrl`) with `https://docs.artur-rios.tech/<library>/`.
