# Jenkins controller, configured entirely by casc.yaml (Configuration as Code): no setup wizard, no
# click-configured state worth backing up beyond build history. Rebuilding this image and restarting
# is how Jenkins is upgraded or reconfigured.
FROM jenkins/jenkins:lts-jdk21

COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
