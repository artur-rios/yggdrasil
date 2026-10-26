# A Hugo site, built in a pinned Hugo image and served by static-web-server. Copy this file to the repository's
# root as `Dockerfile` (README.md next to it says what else to copy and how to use it).
#
# The build arguments are the stack file's (templates/stacks/site.yml), which scripts/deploy.sh feeds:
#   PUBLIC_HOST, PUBLIC_PATH   the address the site is served at: Hugo's baseURL is https://<host><path>/ (just
#                    <path>/ when there is no host, as on a machine without a domain). The proxy does not strip
#                    the path, so the files are put under it too. Leave the baseURL of hugo.toml alone: this
#                    overrides it.
#   HUGO_DIR         the directory of the repository where Hugo's site is (default ., docs for a library's).
#   GIT_COMMIT, YGG_ENVIRONMENT   the commit and the environment this image is built for. Templates read them as
#                    {{ getenv "HUGO_GIT_COMMIT" }} and {{ getenv "HUGO_YGG_ENVIRONMENT" }}: Hugo's getenv only
#                    allows names that start with HUGO_.
#
# A theme installed with npm (Docsy: @docsy/theme in package.json, themesDir = node_modules) is installed with
# `npm ci` when HUGO_DIR has a package-lock.json. The image has Hugo extended, Node and npm, no Go and no git:
# a theme as a Hugo module, or enableGitInfo, needs the "go-git" variant of the image.
FROM hugomods/hugo:debian-node-lts-0.165.0@sha256:eef6a12572c8cf3fea72fb034606f3e6a0821368682139378608eabc94e67c87 AS build

WORKDIR /src
COPY . .

ARG HUGO_DIR=.
ARG PUBLIC_HOST=
ARG PUBLIC_PATH=
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=
ENV HUGO_GIT_COMMIT=${GIT_COMMIT} \
    HUGO_YGG_ENVIRONMENT=${YGG_ENVIRONMENT}

RUN set -eu; \
    cd "$HUGO_DIR"; \
    if [ -f package-lock.json ]; then npm ci; fi; \
    if [ -n "$PUBLIC_HOST" ]; then base="https://$PUBLIC_HOST$PUBLIC_PATH/"; else base="$PUBLIC_PATH/"; fi; \
    hugo --environment production --minify --gc --baseURL "$base" --destination /out

FROM joseluisq/static-web-server:2.44.1@sha256:94618e3d17802265a21926217e6f9e7832b311b6fbf5933684001739fafed06a

# Declared again: an argument lives in the stage that declares it.
ARG PUBLIC_PATH=
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=

# The server has no shell, so nothing is run here: the site is copied under its path, and /healthz is a file of
# its own at the root. Port 8080, a numeric non-root user.
COPY --from=build /out/ /public${PUBLIC_PATH}/
COPY <<EOF /public/healthz
ok
EOF

# Hugo writes 404.html at the site's root (under the path): an address the site does not have is a 404.
ENV SERVER_PORT=8080 \
    SERVER_ROOT=/public \
    SERVER_ERROR_PAGE_404=/public${PUBLIC_PATH}/404.html

LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      yggdrasil.built-for="${YGG_ENVIRONMENT}"

USER 65532:65532
EXPOSE 8080
