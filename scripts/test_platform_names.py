"""Tests for the public names of the platform's own web pages in platform/compose.yml.

    python3 -m unittest discover -s scripts

Static, like test_forgejo_service.py: they read compose.yml, they do not start a container. The apps
machine and the development machine both run Traefik and Grafana, and `*.DOMAIN` points at the apps
machine, so the development machine adds PLATFORM_HOST_SUFFIX (`-dev`) to its dashboard and Grafana
names (traefik-dev.DOMAIN, grafana-dev.DOMAIN) rather than claiming the apps machine's.
"""

import pathlib
import unittest

try:
    import yaml
except ImportError:  # CI installs it; these tests need it.
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "platform" / "compose.yml"


def service(name):
    return yaml.safe_load(COMPOSE.read_text())["services"][name]


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class PlatformNamesTests(unittest.TestCase):
    def test_given_the_traefik_dashboard_when_read_then_its_host_carries_the_suffix(self):
        rule = service("traefik")["labels"]["traefik.http.routers.dashboard.rule"]
        self.assertEqual(rule, "Host(`traefik${PLATFORM_HOST_SUFFIX:-}.${DOMAIN}`)")

    def test_given_grafana_when_read_then_its_router_host_carries_the_suffix(self):
        rule = service("grafana")["labels"]["traefik.http.routers.grafana.rule"]
        self.assertEqual(rule, "Host(`grafana${PLATFORM_HOST_SUFFIX:-}.${DOMAIN}`)")

    def test_given_grafana_when_read_then_its_root_url_carries_the_suffix(self):
        url = service("grafana")["environment"]["GF_SERVER_ROOT_URL"]
        self.assertEqual(url, "https://grafana${PLATFORM_HOST_SUFFIX:-}.${DOMAIN}/")

    def test_given_the_suffix_when_read_then_it_defaults_to_nothing(self):
        # An empty default keeps the apps machine's names (traefik.DOMAIN, grafana.DOMAIN) as they were.
        text = COMPOSE.read_text()
        self.assertNotIn("PLATFORM_HOST_SUFFIX}", text)
        self.assertNotIn("PLATFORM_HOST_SUFFIX:?", text)


if __name__ == "__main__":
    unittest.main()
