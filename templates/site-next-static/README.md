# Next.js static export

A [Next.js](https://nextjs.org/) site exported as static files (`output: 'export'`), built in a pinned Node image and
served by [static-web-server](https://static-web-server.net/).

## Files to copy

| From this template | To the repository |
|---|---|
| `Dockerfile` | `Dockerfile` |
| `yggdrasil.yml` | `yggdrasil.yml` |

The application's stack files are made in yggdrasil, not in the repository: copy `templates/stacks/site.yml` to
`stacks/<application-id>.yml` and one proxy overlay to `stacks/<application-id>.proxy.yml`, and replace
`APPLICATION_ID` with the application's id from `catalog.yaml`. Which overlay:

- `site.proxy.yml`: the site has its own host (`techcraftsmen.net`);
- `site-path.proxy.yml`: the site lives under a path of a shared host (give the application a `path` in the catalog);
- `site-apex-redirect.proxy.yml`: the site is on `www.` and the bare domain redirects to it (`apex: true` in the
  catalog, which gives the bare domain's name, `YGG_APEX_HOST`).

## Build arguments

`site.yml` passes them to the build; `scripts/deploy.sh` exports the values.

| Argument | Comes from | Replaces |
|---|---|---|
| `GIT_COMMIT` | the whole SHA of the commit being deployed | `VERCEL_GIT_COMMIT_SHA` |
| `YGG_ENVIRONMENT` | the environment being deployed to (`production`, `homologation`, ...) | `VERCEL_ENV` (its values were `production`, `preview` and `development`) |
| `PUBLIC_PATH` | `YGG_PUBLIC_PATH`, the catalog's `path` (empty for a host's root) | `basePath` in `vercel.json` or the config |

Build arguments are public, they end up in the image. A secret is the application's variable in the host's variables
store, read when the container runs, never a build argument.

## The app

- Needs a `package-lock.json` (the build runs `npm ci`) and a `build` script that runs `next build`.
- In `next.config.js`:

  ```js
  const publicPath = process.env.PUBLIC_PATH || '';
  module.exports = {
    output: 'export',
    basePath: publicPath || undefined,   // the proxy does not strip the path: the site is built for it
    trailingSlash: true,                 // /about is about/index.html (see below)
    images: { unoptimized: true },       // the image optimizer is a server feature
  };
  ```

- Replace `process.env.VERCEL_GIT_COMMIT_SHA` and `VERCEL_ENV` with `process.env.NEXT_PUBLIC_GIT_COMMIT` and
  `process.env.NEXT_PUBLIC_YGG_ENVIRONMENT`, which Next inlines into the pages and the bundle at build time. The build
  also sees `GIT_COMMIT`, `YGG_ENVIRONMENT` and `PUBLIC_PATH`, for `next.config.js`.

## What the image does

- Serves on port **8080** as a non-root user (`65532`), with static-web-server, pinned by version and digest, the only
  thing in the final image: it has no shell.
- Answers **`/healthz`** with 200 from a file of its own, at the root. The status API probes
  `http://<application-id>.<environment>:8080/healthz`, so set the catalog's `health` to that. The image has no
  `HEALTHCHECK`: with no shell or client in it there is nothing to probe with.
- Puts `out/` under `PUBLIC_PATH`: the proxy does not strip it. Keep `trailingSlash: true`: Next 16 exports `about.html`
  next to an `about/` directory of prefetch data, and the server redirects `/about` to `/about/` because of the
  directory, where there would be no `index.html` without it. With it, a page is `about/index.html`, `/about` redirects
  to `/about/`, and `/about/` answers 200. An address the site does not have is a 404 (the export's `404.html`).

The `build` check of `yggdrasil.yml` builds the site on a pull request. List it, and a lint or test check, in the
application's `checks` in the catalog.
