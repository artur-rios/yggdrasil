# Hugo site

A [Hugo](https://gohugo.io/) site, built in a pinned `hugomods/hugo` image (Hugo extended, Node and npm) and served
by [static-web-server](https://static-web-server.net/). A theme installed with npm (Docsy) is installed with
`npm ci` when there is a `package-lock.json`.

## Files to copy

| From this template | To the repository |
|---|---|
| `Dockerfile` | `Dockerfile` |
| `yggdrasil.yml` | `yggdrasil.yml` |

The application's stack files are made in yggdrasil, not in the repository: copy `templates/stacks/site.yml` to
`stacks/<application-id>.yml` and one proxy overlay to `stacks/<application-id>.proxy.yml`, and replace
`APPLICATION_ID` with the application's id from `catalog.yaml`. Which overlay:

- `site.proxy.yml`: the site has its own host (`blog.artur-rios.tech`);
- `site-path.proxy.yml`: the site lives under a path of a shared host (`docs.artur-rios.tech/<repository>`; give the
  application a `path` in the catalog);
- `site-apex-redirect.proxy.yml`: the site is on `www.` and the bare domain redirects to it (`apex: true` in the
  catalog, which gives the bare domain's name, `YGG_APEX_HOST`).

## Build arguments

`site.yml` passes them to the build; `scripts/deploy.sh` exports the values.

| Argument | Comes from | Replaces |
|---|---|---|
| `GIT_COMMIT` | the whole SHA of the commit being deployed | `VERCEL_GIT_COMMIT_SHA` |
| `YGG_ENVIRONMENT` | the environment being deployed to (`production`, `homologation`, ...) | `VERCEL_ENV` (its values were `production`, `preview` and `development`) |
| `PUBLIC_HOST` | `YGG_PUBLIC_HOST`, the catalog's host and domain | the site's own URL |
| `PUBLIC_PATH` | `YGG_PUBLIC_PATH`, the catalog's `path` (empty for a host's root) | the repository path of a Pages site |
| `HUGO_DIR` | not passed: add it under `build.args` in the stack file when Hugo's site is not the repository's root | |

Build arguments are public, they end up in the image. A secret is the application's variable in the host's variables
store, read when the container runs, never a build argument.

## What the image does

- Builds with `--baseURL https://<PUBLIC_HOST><PUBLIC_PATH>/` (just `<PUBLIC_PATH>/` without a host), so leave
  `baseURL` in `hugo.toml` alone, and `--environment production`. Templates read the commit and the environment as
  `{{ getenv "HUGO_GIT_COMMIT" }}` and `{{ getenv "HUGO_YGG_ENVIRONMENT" }}`: Hugo's `getenv` only allows names that
  start with `HUGO_`.
- Has no Go and no git: a theme as a Hugo module, or `enableGitInfo`, needs the `go-git` variant of the image (and
  a new pin).
- Serves on port **8080** as a non-root user (`65532`), with static-web-server, pinned by version and digest, the only
  thing in the final image: it has no shell.
- Answers **`/healthz`** with 200 from a file of its own, at the root. The status API probes
  `http://<application-id>.<environment>:8080/healthz`, so set the catalog's `health` to that. The image has no
  `HEALTHCHECK`: with no shell or client in it there is nothing to probe with.
- Puts the site under `PUBLIC_PATH`: the proxy does not strip it. An address the site does not have is a 404 (Hugo's
  `404.html`).

The `build` check of `yggdrasil.yml` builds the site on a pull request. List it in the application's `checks` in the
catalog.
