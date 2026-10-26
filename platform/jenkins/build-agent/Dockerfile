# The build agent of a service user (ci, release): an inbound (WebSocket) Jenkins agent plus git, the
# Docker CLI and buildx talking to the user's engine through the mounted socket, curl/jq, and Python
# with PyYAML for the repositories' own scripts. Modelled on the deploy agent's image
# (platform/jenkins/agent/Dockerfile), without Compose's secrets tooling: a build runs a check, it does
# not deploy.
FROM jenkins/inbound-agent:3391.va_37fa_a_305d6d-6-jdk21@sha256:c93e68c1b1a9e5c4cb8e459dfd0f7fc63b6dc86b47f12883b8231c85d2e4ca4d

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl git gnupg jq python3 python3-yaml \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends docker-ce-cli docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*
USER jenkins
