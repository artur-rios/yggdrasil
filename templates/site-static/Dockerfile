# A site that is already built (HTML, CSS, images in a directory), served by static-web-server. Copy this file to
# the repository's root as `Dockerfile` (README.md next to it says what else to copy and how to use it).
#
# The build arguments are the stack file's (templates/stacks/site.yml), which scripts/deploy.sh feeds:
#   SITE_DIR         the directory of the repository that is the site (default public). Only this directory is
#                    served: the repository's other files (Dockerfile, yggdrasil.yml, .git) never are.
#   PUBLIC_PATH      where under the host the site lives (/docs-name), empty for the host's root. The files are
#                    put under it, since the proxy does not strip it.
#   GIT_COMMIT, YGG_ENVIRONMENT   the commit and the environment this image is built for. A static site has
#                    nothing to compile them into: they are labels of the image.
# PUBLIC_HOST (the stack passes it too) is not used here, and an unused build argument is not an error.
#
# The server runs as a numeric non-root user and has no shell, so nothing is run in this image: the files are
# only copied. Port 8080. /healthz answers 200 from a file of its own. There is no HEALTHCHECK instruction,
# for the same reason (no shell, and no client in the image to probe with): the status API probes /healthz.
FROM joseluisq/static-web-server:2.44.1@sha256:94618e3d17802265a21926217e6f9e7832b311b6fbf5933684001739fafed06a

ARG SITE_DIR=public
ARG PUBLIC_PATH=
ARG GIT_COMMIT=
ARG YGG_ENVIRONMENT=

COPY ${SITE_DIR}/ /public${PUBLIC_PATH}/
COPY <<EOF /public/healthz
ok
EOF

# An address the site does not have is a 404 (its 404.html if it has one, else the server's own page).
# Hidden files (.git...) are never served: that is the server's default.
ENV SERVER_PORT=8080 \
    SERVER_ROOT=/public \
    SERVER_ERROR_PAGE_404=/public${PUBLIC_PATH}/404.html

LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      yggdrasil.built-for="${YGG_ENVIRONMENT}"

USER 65532:65532
EXPOSE 8080
