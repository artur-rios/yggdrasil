# Vite app

A [Vite](https://vite.dev/) single-page app, built in a pinned Node image and served by
[static-web-server](https://static-web-server.net/).

## Files to copy

| From this template | To the repository |
|---|---|
| `Dockerfile` | `Dockerfile` |
| `yggdrasil.yml` | `yggdrasil.yml` |

The application's stack files are made in yggdrasil, not in the repository: copy `templates/stacks/site.yml` to
`stacks/<application-id>.yml` and one proxy overlay to `stacks/<application-id>.proxy.yml`, and replace
`APPLICATION_ID` with the application's id from `catalog.yaml`. Which overlay:

- `site.proxy.yml`: the app has its own host (`placeholder.artur-rios.tech`);
- `site-path.proxy.yml`: the app lives under a path of a shared host (give the application a `path` in the catalog);
- `site-apex-redirect.proxy.yml`: the app is on `www.` and the bare domain redirects to it (`apex: true` in the
  catalog, and `APEX_HOST` in the application's variables).

## Build arguments

`site.yml` passes them to the build; `scripts/deploy.sh` exports the values.

| Argument | Comes from | Replaces |
|---|---|---|
| `GIT_COMMIT` | the whole SHA of the commit being deployed | `VERCEL_GIT_COMMIT_SHA` |
| `YGG_ENVIRONMENT` | the environment being deployed to (`production`, `homologation`, ...) | `VERCEL_ENV` (its values were `production`, `preview` and `development`) |
| `PUBLIC_PATH` | `YGG_PUBLIC_PATH`, the catalog's `path` (empty for a host's root) | Vite's `base` in `vercel.json` or the config |

Build arguments are public, they end up in the image. A secret is the application's variable in the host's variables
store, read when the container runs, never a build argument.

## The app

- Needs a `package-lock.json` (the build runs `npm ci`) and a `build` script that runs `vite build`: the Dockerfile runs
  `npm run build -- --base=<PUBLIC_PATH>/`, so `--base` must reach `vite`.
- Replace `process.env.VERCEL_GIT_COMMIT_SHA` and `VERCEL_ENV` in the code with `import.meta.env.VITE_GIT_COMMIT` and
  `import.meta.env.VITE_YGG_ENVIRONMENT` (Vite only exposes variables that start with `VITE_`); `index.html` can
  use `%VITE_GIT_COMMIT%`.

## What the image does

- Serves on port **8080** as a non-root user (`65532`), with static-web-server, pinned by version and digest, the only
  thing in the final image: it has no shell.
- Answers **`/healthz`** with 200 from a file of its own, at the root. The status API probes
  `http://<application-id>.<environment>:8080/healthz`, so set the catalog's `health` to that. The image has no
  `HEALTHCHECK`: with no shell or client in it there is nothing to probe with.
- Puts `dist/` under `PUBLIC_PATH`: the proxy does not strip it. Any address under the path that is not a file answers
  the app's `index.html` with 200, for the client router: a deep link or a reload works. A mistyped asset address
  does too, as on any single-page app host.

The `build` check of `yggdrasil.yml` builds the app on a pull request. List it, and a lint or test check, in the
application's `checks` in the catalog.
