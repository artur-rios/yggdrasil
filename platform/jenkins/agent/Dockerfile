# The deploy agent: an inbound (WebSocket) Jenkins agent plus the Docker CLI and Compose plugin,
# talking to the host's engine through the mounted socket, and curl/jq for the GitHub API calls in
# scripts/github.sh.
FROM jenkins/inbound-agent:latest-jdk21

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl gnupg jq \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*
USER jenkins
