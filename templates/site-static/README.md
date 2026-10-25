# Static site

A site that is already built, in a directory (`public/` by default), served by
[static-web-server](https://static-web-server.net/). For a plain HTML/CSS repository, or a site another tool builds
before it is committed.

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
| `PUBLIC_PATH` | `YGG_PUBLIC_PATH`, the catalog's `path` (empty for a host's root) | the repository path of a Pages site |
| `SITE_DIR` | not passed: add it under `build.args` in the stack file to serve another directory | |

Build arguments are public, they end up in the image. A secret is the application's variable in the host's variables
store, read when the container runs, never a build argument. A static site has nothing to compile `GIT_COMMIT` and
`YGG_ENVIRONMENT` into, so they are labels of the image.

## What the image does

- Serves on port **8080** as a non-root user (`65532`), with static-web-server, pinned by version and digest, the only
  thing in the image: it has no shell.
- Answers **`/healthz`** with 200 from a file of its own, at the root. The status API probes
  `http://<application-id>.<environment>:8080/healthz`, so set the catalog's `health` to that. The image has no
  `HEALTHCHECK`: with no shell or client in it there is nothing to probe with.
- Serves `SITE_DIR` only, under `PUBLIC_PATH`, so `Dockerfile`, `yggdrasil.yml` and `.git` never are served: a site at
  `docs.artur-rios.tech/name` has its `index.html` at `/name/`. `/about` finds `about.html`, and a directory finds its
  `index.html`.
- An address the site does not have is a 404 (its `404.html` if it has one).

The `site` check of `yggdrasil.yml` only proves `public/index.html` exists. List it in the application's `checks` in
the catalog, or delete it.
