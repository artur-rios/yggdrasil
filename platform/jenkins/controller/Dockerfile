# Jenkins controller, configured entirely by Configuration as Code: casc.yaml and the files next to it
# that scripts/platform.sh picks for the installation's git server (casc.forgejo.yaml or casc.github.yaml,
# casc.github-app.yaml, casc.github-read.yaml, casc.nuget.yaml): no setup wizard, no click-configured state
# worth backing up beyond build history. Rebuilding this image and restarting is how Jenkins is upgraded or
# reconfigured.
FROM jenkins/jenkins:2.580.1-lts-jdk21@sha256:a660310e39ade10631f774bacd5219de767dfd08947ea5c32a739b5e5bb382c1

COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
