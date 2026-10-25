# Next.js server

A server-rendered [Next.js](https://nextjs.org/) site: built in a pinned Node image and run with `node server.js`
(Next's standalone output), as the image's non-root `node` user, on port 8080. The site is served at its host's root.

## Files to copy

| From this template | To the repository |
|---|---|
| `Dockerfile` | `Dockerfile` |
| `yggdrasil.yml` | `yggdrasil.yml` |
| `app/healthz/route.js` | `app/healthz/route.js` (`src/app/healthz/route.js` when the app uses `src/`) |

The application's stack files are made in yggdrasil, not in the repository: copy `templates/stacks/site.yml` to
`stacks/<application-id>.yml` and one proxy overlay to `stacks/<application-id>.proxy.yml`, and replace
`APPLICATION_ID` with the application's id from `catalog.yaml`. Which overlay:

- `site.proxy.yml`: the site has its own host;
- `site-apex-redirect.proxy.yml`: the site is on `www.` and the bare domain redirects to it (`www.artur-rios.tech`;
  `apex: true` in the catalog, which gives the bare domain's name, `YGG_APEX_HOST`).

A server-rendered site does not live under a path (`site-path.proxy.yml`): `basePath` would also move `/healthz`.
Hand the application's env file to the container with `env_file: ${APP_ENV_FILE}` in the stack file when the server
needs secrets.

## Build arguments

`site.yml` passes them to the build; `scripts/deploy.sh` exports the values.

| Argument | Comes from | Replaces |
|---|---|---|
| `GIT_COMMIT` | the whole SHA of the commit being deployed | `VERCEL_GIT_COMMIT_SHA` |
| `YGG_ENVIRONMENT` | the environment being deployed to (`production`, `homologation`, ...) | `VERCEL_ENV` (its values were `production`, `preview` and `development`) |

Build arguments are public, they end up in the image. A secret is the application's variable in the host's variables
store (an API key, a database URL), read when the container runs, never a build argument. `PUBLIC_HOST` and
`PUBLIC_PATH`, which the stack passes too, are not used.

## The app

- Needs a `package-lock.json` (the build runs `npm ci`), a `build` script that runs `next build`, and
  `output: 'standalone'` in `next.config.js` (the image copies `.next/standalone`).
- `GIT_COMMIT` and `YGG_ENVIRONMENT` are available while the app builds and in the running container:
  `process.env.GIT_COMMIT` and `process.env.YGG_ENVIRONMENT` in server code (read per request), and
  `NEXT_PUBLIC_GIT_COMMIT` and `NEXT_PUBLIC_YGG_ENVIRONMENT` for code that runs in the browser (inlined at build time).
  Replace `VERCEL_GIT_COMMIT_SHA` and `VERCEL_ENV` with them.

## What the image does

- Serves on port **8080** as the non-root `node` user. `HOSTNAME=0.0.0.0` is set, since Docker sets `HOSTNAME` to the
  container's id and Next would listen on that.
- **`/healthz`** is the route in `app/healthz/route.js`, which you copy into the app. The status API probes
  `http://<application-id>.<environment>:8080/healthz`, so set the catalog's `health` to that. The image's
  `HEALTHCHECK` asks it with busybox's `wget`.

The `build` check of `yggdrasil.yml` builds the site on a pull request. List it, and a lint or test check, in the
application's `checks` in the catalog.
