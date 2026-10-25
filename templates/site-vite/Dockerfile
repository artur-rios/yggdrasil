# A Vite single-page app, built in a pinned Node image and served by static-web-server. Copy this file to the
# repository's root as `Dockerfile` (README.md next to it says what else to copy and how to use it).
#
# The build arguments are the stack file's (templates/stacks/site.yml), which scripts/deploy.sh feeds:
#   PUBLIC_PATH      where under the host the app lives (/name), empty for the host's root: `vite build --base`
#                    gets it, and the files are put under it, since the proxy does not strip it.
#   GIT_COMMIT, YGG_ENVIRONMENT   the commit and the environment this image is built for, compiled into the app
#                    as import.meta.env.VITE_GIT_COMMIT and VITE_YGG_ENVIRONMENT (and %VITE_GIT_COMMIT% in
#                    index.html). They replace VERCEL_GIT_COMMIT_SHA and VERCEL_ENV. Public values: never pass a
#                    secret as a build argument.
# PUBLIC_HOST (the stack passes it too) is not used here, and an unused build argument is not an error.
#
# The repository needs a package-lock.json (`npm ci`) and a `build` script that runs `vite build` (extra
# arguments are appended, and `--base` must reach vite). The output is dist/, as Vite's default.
FROM node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ARG PUBLIC_PATH=
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=
ENV VITE_GIT_COMMIT=${GIT_COMMIT} \
    VITE_YGG_ENVIRONMENT=${YGG_ENVIRONMENT}
RUN npm run build -- --base="${PUBLIC_PATH}/"

FROM joseluisq/static-web-server:2.44.1@sha256:94618e3d17802265a21926217e6f9e7832b311b6fbf5933684001739fafed06a

# Declared again: an argument lives in the stage that declares it.
ARG PUBLIC_PATH=
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=

# The server has no shell, so nothing is run here: the build is copied under its path, and /healthz is a file of
# its own at the root. Port 8080, a numeric non-root user.
COPY --from=build /app/dist/ /public${PUBLIC_PATH}/
COPY <<EOF /public/healthz
ok
EOF

# A single-page app: any address under its path that is not a file is the app's page, for the client router to
# read (a deep link or a reload answers 200, not 404).
ENV SERVER_PORT=8080 \
    SERVER_ROOT=/public \
    SERVER_FALLBACK_PAGE=/public${PUBLIC_PATH}/index.html

LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      yggdrasil.built-for="${YGG_ENVIRONMENT}"

USER 65532:65532
EXPOSE 8080
