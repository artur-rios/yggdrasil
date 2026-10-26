# A server-rendered Next.js site, built in a pinned Node image and run by `node server.js` (Next's standalone
# output) as the image's non-root `node` user. Copy this file to the repository's root as `Dockerfile` (README.md
# next to it says what else to copy and how to use it).
#
# The build arguments are the stack file's (templates/stacks/site.yml), which scripts/deploy.sh feeds:
#   GIT_COMMIT, YGG_ENVIRONMENT   the commit and the environment this image is built for. They replace
#                    VERCEL_GIT_COMMIT_SHA and VERCEL_ENV. They are set when the app builds (as process.env.* and
#                    NEXT_PUBLIC_GIT_COMMIT / NEXT_PUBLIC_YGG_ENVIRONMENT for the browser's code) and stay set in
#                    the running container, for server code that reads them per request. Public values: never
#                    pass a secret as a build argument; the runtime's secrets are the application's env file.
# PUBLIC_HOST and PUBLIC_PATH (the stack passes them too) are not used: the app is served at its host's root.
#
# The repository needs a package-lock.json (`npm ci`), `output: 'standalone'` in next.config.js, a `build` script
# that runs `next build`, and a /healthz route (app/healthz/route.js in this template: copy it into the app).
FROM node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=
ENV NEXT_TELEMETRY_DISABLED=1 \
    GIT_COMMIT=${GIT_COMMIT} \
    YGG_ENVIRONMENT=${YGG_ENVIRONMENT} \
    NEXT_PUBLIC_GIT_COMMIT=${GIT_COMMIT} \
    NEXT_PUBLIC_YGG_ENVIRONMENT=${YGG_ENVIRONMENT}
# An app without a public/ directory still has one to copy below.
RUN mkdir -p public && npm run build

FROM node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53

ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=
# HOSTNAME: Docker sets it to the container's id, which Next would then listen on; 0.0.0.0 is every interface.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=8080 \
    HOSTNAME=0.0.0.0 \
    GIT_COMMIT=${GIT_COMMIT} \
    YGG_ENVIRONMENT=${YGG_ENVIRONMENT}

WORKDIR /app
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public

LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      yggdrasil.built-for="${YGG_ENVIRONMENT}"

USER node
EXPOSE 8080
# wget is busybox's, in the image; --spider asks without downloading. start-period gives Next time to boot.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD wget -q --spider http://127.0.0.1:8080/healthz || exit 1
CMD ["node", "server.js"]
