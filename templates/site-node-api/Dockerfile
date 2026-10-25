# A Node.js HTTP service (github-readme-stats, an API with no framework to build), run by `node` as the image's
# non-root `node` user. Copy this file to the repository's root as `Dockerfile` (README.md next to it says what
# else to copy and how to use it).
#
# The build arguments are the stack file's (templates/stacks/site.yml), which scripts/deploy.sh feeds:
#   GIT_COMMIT, YGG_ENVIRONMENT   the commit and the environment this image is built for, set as the environment
#                    variables of the same names in the running container (a service reports them, or logs
#                    them). Public values: never pass a secret as a build argument; the service's secrets are
#                    the application's env file (APP_ENV_FILE), read at run time.
# PUBLIC_HOST and PUBLIC_PATH (the stack passes them too) are not used: the service listens on PORT 8080 and
# answers on its host's root.
#
# The repository needs a package-lock.json (`npm ci --omit=dev`: only the dependencies, not the dev ones) and a
# server that listens on process.env.PORT (8080) and answers GET /healthz with 200. Change the last line if the
# entry file is not server.js.
FROM node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS deps

WORKDIR /app
COPY package.json package-lock.json ./
# node_modules is made even when there are no dependencies to install, so the COPY below always has it.
RUN npm ci --omit=dev && mkdir -p node_modules

FROM node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53

ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=
ENV NODE_ENV=production \
    PORT=8080 \
    GIT_COMMIT=${GIT_COMMIT} \
    YGG_ENVIRONMENT=${YGG_ENVIRONMENT}

WORKDIR /app
COPY --from=deps --chown=node:node /app/node_modules ./node_modules
COPY --chown=node:node . .

LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      yggdrasil.built-for="${YGG_ENVIRONMENT}"

USER node
EXPOSE 8080
# wget is busybox's, in the image; --spider asks without downloading.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q --spider http://127.0.0.1:8080/healthz || exit 1
CMD ["node", "server.js"]
