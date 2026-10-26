# Node.js service

A Node.js HTTP service with its own server (github-readme-stats): `npm ci --omit=dev` in a pinned Node image, run with
`node server.js` as the image's non-root `node` user, on port 8080. It answers on its host's root.

## Files to copy

| From this template | To the repository |
|---|---|
| `Dockerfile` | `Dockerfile` |
| `yggdrasil.yml` | `yggdrasil.yml` |

The application's stack files are made in yggdrasil, not in the repository: copy `templates/stacks/site.yml` to
`stacks/<application-id>.yml` and `templates/stacks/site.proxy.yml` to `stacks/<application-id>.proxy.yml`, and replace
`APPLICATION_ID` with the application's id from `catalog.yaml`. Add `env_file: ${APP_ENV_FILE}` to the service in
`stacks/<application-id>.yml` to hand the application's variables to the container (a read-only GitHub token, say):
a secret is the application's variable in the host's variables store, read when the container runs, never a build
argument.

## Build arguments

`site.yml` passes them to the build; `scripts/deploy.sh` exports the values.

| Argument | Comes from | Replaces |
|---|---|---|
| `GIT_COMMIT` | the whole SHA of the commit being deployed | `VERCEL_GIT_COMMIT_SHA` |
| `YGG_ENVIRONMENT` | the environment being deployed to (`production`, `homologation`, ...) | `VERCEL_ENV` (its values were `production`, `preview` and `development`) |

They are public, they end up in the image, and they are set as environment variables of the same names in the running
container. `PUBLIC_HOST` and `PUBLIC_PATH`, which the stack passes too, are not used.

## The service

- Needs a `package-lock.json` (`npm ci --omit=dev`), and listens on `process.env.PORT` (`8080` in the image).
- Answers **`GET /healthz`** with 200. The status API probes `http://<application-id>.<environment>:8080/healthz`, so
  set the catalog's `health` to that. The image's `HEALTHCHECK` asks it with busybox's `wget`.
- The entry file is `server.js`; change the last line of the Dockerfile for another one.
- Add a `.dockerignore` with `.git`, `node_modules` and the like, so the build context stays small and the host's
  `node_modules` never replaces the image's.

The `test` check of `yggdrasil.yml` runs `npm test` on a pull request. List it in the application's `checks` in the
catalog.
