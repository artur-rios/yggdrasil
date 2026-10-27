"""Tests for the Forgejo service of platform/compose.yml.

    python3 -m unittest discover -s scripts

Static, like test_socket_proxies.py: they read compose.yml, they do not start a container. Forgejo
holds the source of every repository, so what matters here is what it is allowed to reach (nothing of
the host: no Docker socket), who can sign up (nobody) and what an anonymous visitor can read (nothing).
"""

import pathlib
import re
import unittest

try:
    import yaml
except ImportError:  # CI installs it; these tests need it.
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "platform" / "compose.yml"


def forgejo():
    return yaml.safe_load(COMPOSE.read_text())["services"]["forgejo"]


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class ForgejoServiceTests(unittest.TestCase):
    def test_given_the_forgejo_service_when_read_then_it_is_only_in_the_forge_profile(self):
        self.assertEqual(forgejo()["profiles"], ["forge"])

    def test_given_the_forgejo_service_when_read_then_registration_is_closed_and_sign_in_is_required(self):
        environment = forgejo()["environment"]
        self.assertEqual(environment["FORGEJO__service__DISABLE_REGISTRATION"], "true")
        self.assertEqual(environment["FORGEJO__service__REQUIRE_SIGNIN_VIEW"], "true")

    def test_given_the_forgejo_service_when_read_then_webhooks_may_reach_jenkins_internally(self):
        # Forgejo refuses webhook targets on private addresses unless the host list allows them: `external`
        # keeps the public https://jenkins.<DOMAIN> working (docs/setup.md step 12), and `jenkins` -- the
        # controller's Compose service name -- makes http://jenkins:8080 on the shared network work too.
        hosts = forgejo()["environment"]["FORGEJO__webhook__ALLOWED_HOST_LIST"].split(",")
        self.assertEqual(hosts, ["external", "jenkins"])
        services = yaml.safe_load(COMPOSE.read_text())["services"]
        self.assertIn("jenkins", services)

    def test_given_the_forgejo_service_when_read_then_forgejo_actions_is_off(self):
        # Jenkins runs every check. With Actions on, Forgejo also runs a repository's .github/workflows
        # (when it has no .forgejo/workflows), and with no runner those jobs wait on every pull request.
        self.assertEqual(forgejo()["environment"]["FORGEJO__actions__ENABLED"], "false")

    def test_given_the_forgejo_service_when_read_then_ssh_is_published_on_2222_by_default(self):
        service = forgejo()
        self.assertIn("${FORGEJO_SSH_PORT:-2222}:2222", service["ports"])
        environment = service["environment"]
        self.assertEqual(environment["FORGEJO__server__SSH_PORT"], "${FORGEJO_SSH_PORT:-2222}")
        self.assertEqual(environment["FORGEJO__server__SSH_LISTEN_PORT"], "2222")
        self.assertEqual(environment["FORGEJO__server__START_SSH_SERVER"], "true")

    def test_given_the_forgejo_service_when_read_then_its_image_is_the_rootless_variant_pinned_by_digest(self):
        self.assertRegex(forgejo()["image"], r"forgejo/forgejo:\d+\.\d+\.\d+-rootless@sha256:[0-9a-f]{64}$")

    def test_given_the_forgejo_service_when_read_then_it_never_mounts_the_docker_socket(self):
        service = forgejo()
        for volume in service.get("volumes", []):
            self.assertNotRegex(str(volume), r"docker\.sock|DOCKER_SOCKET")
        self.assertNotIn("docker", " ".join(service.get("networks", [])))

    def test_given_the_forgejo_service_when_read_then_traefik_routes_git_to_its_web_port(self):
        labels = forgejo()["labels"]
        self.assertEqual(labels["traefik.http.routers.forgejo.rule"], "Host(`git.${DOMAIN}`)")
        self.assertEqual(labels["traefik.http.routers.forgejo.entrypoints"], "websecure")
        self.assertEqual(labels["traefik.http.routers.forgejo.middlewares"], "secure-headers@file")
        self.assertEqual(labels["traefik.http.services.forgejo.loadbalancer.server.port"], "3000")
        self.assertEqual(forgejo()["networks"], ["edge"])

    def test_given_the_forgejo_service_when_read_then_it_keeps_its_data_and_configuration_in_a_named_volume(self):
        # The rootless image keeps app.ini under /var/lib/gitea (custom/conf), not in /etc/gitea.
        self.assertIn("forgejo-data:/var/lib/gitea", forgejo()["volumes"])
        self.assertIn("forgejo-data", yaml.safe_load(COMPOSE.read_text())["volumes"])


if __name__ == "__main__":
    unittest.main()
