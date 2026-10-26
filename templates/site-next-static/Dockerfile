# A Next.js site exported as static files (`output: 'export'`), built in a pinned Node image and served by
# static-web-server. Copy this file to the repository's root as `Dockerfile` (README.md next to it says what else
# to copy and how to use it).
#
# The build arguments are the stack file's (templates/stacks/site.yml), which scripts/deploy.sh feeds:
#   PUBLIC_PATH      where under the host the site lives (/name), empty for the host's root. next.config.js reads
#                    it as process.env.PUBLIC_PATH for `basePath` (README.md has the lines), and the files are put
#                    under it, since the proxy does not strip it.
#   GIT_COMMIT, YGG_ENVIRONMENT   the commit and the environment this image is built for. They replace
#                    VERCEL_GIT_COMMIT_SHA and VERCEL_ENV: the code reads process.env.NEXT_PUBLIC_GIT_COMMIT and
#                    NEXT_PUBLIC_YGG_ENVIRONMENT (inlined into the pages and the bundle at build time), and
#                    next.config.js sees GIT_COMMIT and YGG_ENVIRONMENT. Public values: never pass a secret as a
#                    build argument.
# PUBLIC_HOST (the stack passes it too) is not used here, and an unused build argument is not an error.
#
# The repository needs a package-lock.json (`npm ci`), `output: 'export'` and `trailingSlash: true` in
# next.config.js (a page is about/index.html, which the server finds for /about/; without it the export has
# about.html next to an about/ directory, and the server redirects /about to a directory with no index.html) and
# a `build` script that runs `next build`. The output is out/.
FROM node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ARG PUBLIC_PATH=
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=
ENV NEXT_TELEMETRY_DISABLED=1 \
    PUBLIC_PATH=${PUBLIC_PATH} \
    GIT_COMMIT=${GIT_COMMIT} \
    YGG_ENVIRONMENT=${YGG_ENVIRONMENT} \
    NEXT_PUBLIC_GIT_COMMIT=${GIT_COMMIT} \
    NEXT_PUBLIC_YGG_ENVIRONMENT=${YGG_ENVIRONMENT}
RUN npm run build

FROM joseluisq/static-web-server:2.44.1@sha256:94618e3d17802265a21926217e6f9e7832b311b6fbf5933684001739fafed06a

# Declared again: an argument lives in the stage that declares it.
ARG PUBLIC_PATH=
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=

# The server has no shell, so nothing is run here: the export is copied under its path, and /healthz is a file of
# its own at the root. Port 8080, a numeric non-root user.
COPY --from=build /app/out/ /public${PUBLIC_PATH}/
COPY <<EOF /public/healthz
ok
EOF

# The export has about/index.html for /about/ and 404.html for an address the site does not have.
ENV SERVER_PORT=8080 \
    SERVER_ROOT=/public \
    SERVER_ERROR_PAGE_404=/public${PUBLIC_PATH}/404.html

LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      yggdrasil.built-for="${YGG_ENVIRONMENT}"

USER 65532:65532
EXPOSE 8080
