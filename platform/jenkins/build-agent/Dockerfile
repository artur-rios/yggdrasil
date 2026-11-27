# The build agent of a service user (ci, release): an inbound (WebSocket) Jenkins agent plus git, the
# Docker CLI and buildx talking to the user's engine through the mounted socket, curl/jq, and Python
# with PyYAML for the repositories' own scripts; xz for the Flutter archives the ci agent's toolchain cache
# unpacks (scripts/toolchains.sh). Modelled on the deploy agent's image
# (platform/jenkins/agent/Dockerfile), without Compose's secrets tooling: a build runs a check, it does
# not deploy.
FROM jenkins/inbound-agent:3396.ve02b_5cde31b_2-5-jdk25@sha256:f4199977fecfc539e8e79158e962531845f241d8f7b0b32ee5fc1369f7144c16

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl git gnupg jq python3 python3-yaml xz-utils \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends docker-ce-cli docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*
USER jenkins
