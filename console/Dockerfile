# syntax=docker/dockerfile:1
# check=skip=FromPlatformFlagConstDisallowed
#
# Production image for the Flutter web build of the yggdrasil console.
#
#   docker build -t yggdrasil-console console/
#   docker build -t yggdrasil-console \
#     --build-arg 'YGGDRASIL_ENVIRONMENTS=[{"name":"vps","url":"https://yggdrasil.example.com"}]' \
#     console/
#
# YGGDRASIL_ENVIRONMENTS (a list of hosts; the name predates the console
# calling them hosts) is compiled into main.dart.js and is readable by anyone
# who loads the page: names and URLs only, never a token. Without it the
# console shows the host it is served from (its own origin), whose status API
# Traefik routes under /api/ on the same host.

# ---------------------------------------------------------------------------
# Build stage: a pinned Flutter SDK compiles the web bundle.
#
# The SDK comes from Flutter's official release archive, which is x64-only;
# the output is platform-independent static files, so this stage always runs
# as amd64.
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 debian:trixie-slim AS build

# FLUTTER_SHA256 is the archive's checksum from
# https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json;
# override it together with FLUTTER_VERSION, or pass it empty to skip the check.
ARG FLUTTER_VERSION=3.47.4
ARG FLUTTER_SHA256=5b45f0ceda99b9bebdc873e7e69f6450aeb4c30f454b505e2e62fc9255a907d3

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git unzip xz-utils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --shell /bin/bash flutter \
 && install -d -o flutter -g flutter /opt/flutter

USER flutter
ENV CI=true \
    FLUTTER_ROOT=/opt/flutter \
    PUB_CACHE=/home/flutter/.pub-cache \
    PATH=/opt/flutter/bin:/opt/flutter/bin/cache/dart-sdk/bin:$PATH

RUN curl -fsSL -o /tmp/flutter.tar.xz \
      "https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz" \
 && if [ -n "$FLUTTER_SHA256" ]; then echo "$FLUTTER_SHA256  /tmp/flutter.tar.xz" | sha256sum -c -; fi \
 && tar -xJf /tmp/flutter.tar.xz -C /opt \
 && rm /tmp/flutter.tar.xz \
 && flutter config --no-analytics --no-cli-animations \
 && dart --disable-analytics \
 && flutter precache --web \
 && flutter --version

WORKDIR /home/flutter/app

# Dependencies first, so source-only changes reuse the resolved packages.
COPY --chown=flutter:flutter pubspec.yaml pubspec.lock ./
RUN flutter pub get --enforce-lockfile

COPY --chown=flutter:flutter . .

# Read by lib/core/config/app_config.dart. Optional: a JSON list of
# {"name", "url"} hosts offered when the user has saved no hosts of their own.
ARG YGGDRASIL_ENVIRONMENTS=

# --no-web-resources-cdn bundles CanvasKit and fonts into build/web so the
# running app never fetches from gstatic.com.
RUN flutter build web --release --no-web-resources-cdn \
      "--dart-define=YGGDRASIL_ENVIRONMENTS=${YGGDRASIL_ENVIRONMENTS}"

# ---------------------------------------------------------------------------
# Runtime stage: static files behind unprivileged nginx on port 8080.
# ---------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /home/flutter/app/build/web /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
