# Jenkins controller, configured entirely by Configuration as Code: casc.yaml and the files next to it
# that scripts/platform.sh picks for the installation's git server (casc.forgejo.yaml or casc.github.yaml,
# casc.github-app.yaml, casc.github-read.yaml, casc.nuget.yaml): no setup wizard, no click-configured state
# worth backing up beyond build history. Rebuilding this image and restarting is how Jenkins is upgraded or
# reconfigured.
FROM jenkins/jenkins:2.580.2-lts-jdk21@sha256:74401de6a6f92e1afe1b02d1c8e3d0c33f263bfb5275dd8813f9150355f2367b

COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
