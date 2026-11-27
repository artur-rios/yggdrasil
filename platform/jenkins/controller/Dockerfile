# Jenkins controller, configured entirely by Configuration as Code: casc.yaml and the files next to it
# that scripts/platform.sh picks for the installation's git server (casc.forgejo.yaml or casc.github.yaml,
# casc.github-app.yaml, casc.github-read.yaml, casc.nuget.yaml): no setup wizard, no click-configured state
# worth backing up beyond build history. Rebuilding this image and restarting is how Jenkins is upgraded or
# reconfigured.
FROM jenkins/jenkins:2.580.3-lts-jdk25@sha256:4d85f6cf5a8939647921564952d12a5a761f32a3bc520c2f4e5c3cafa7ea660f

# When `platform.sh update-plugins` last fetched the plugins (compose.yml passes it; platform.sh keeps it in
# <secrets>/jenkins-plugins-refreshed). The plugins are resolved to their latest compatible versions, but Docker
# reuses the layer below while nothing above it changes: a new value is what makes it fetch them again, and every
# `up` passing the same value is what keeps it on that layer rather than back on an older one.
ARG PLUGINS_REFRESHED=
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
