"""Tests for scripts/traefik_config.py.   python3 -m unittest discover -s scripts"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

from traefik_config import SettingsError, render

SCRIPT = pathlib.Path(__file__).resolve().parent / "traefik_config.py"

SETTINGS = {"DOMAIN": "techcraftsmen.net", "ACME_EMAIL": "ops@techcraftsmen.net", "ACME_DNS_PROVIDER": "cloudflare"}


def tls_domains(config):
    return config["entryPoints"]["websecure"]["http"]["tls"]["domains"]


class RenderTests(unittest.TestCase):
    def test_given_one_domain_when_rendered_then_one_wildcard_certificate_domain(self):
        config = render(SETTINGS, None)
        self.assertEqual(tls_domains(config), [{"main": "techcraftsmen.net", "sans": ["*.techcraftsmen.net"]}])

    def test_given_extra_domains_when_rendered_then_each_domain_gets_its_own_entry_in_order(self):
        config = render({**SETTINGS, "EXTRA_DOMAINS": "artur-rios.tech, example.org"}, None)
        self.assertEqual([d["main"] for d in tls_domains(config)],
                         ["techcraftsmen.net", "artur-rios.tech", "example.org"])
        self.assertEqual(tls_domains(config)[1]["sans"], ["*.artur-rios.tech"])

    def test_given_empty_entries_among_extra_domains_when_rendered_then_they_are_ignored(self):
        config = render({**SETTINGS, "EXTRA_DOMAINS": " , artur-rios.tech,, "}, None)
        self.assertEqual([d["main"] for d in tls_domains(config)], ["techcraftsmen.net", "artur-rios.tech"])

    def test_given_cloudflare_proxied_when_rendered_then_both_entry_points_trust_only_its_ranges(self):
        config = render({**SETTINGS, "CLOUDFLARE_PROXIED": "true"}, ["173.245.48.0/20", "2400:cb00::/32"])
        for name in ("web", "websecure"):
            self.assertEqual(config["entryPoints"][name]["forwardedHeaders"]["trustedIPs"],
                             ["173.245.48.0/20", "2400:cb00::/32"])

    def test_given_not_proxied_when_rendered_then_no_forwarded_headers_are_trusted(self):
        self.assertNotIn("forwardedHeaders", render(SETTINGS, None)["entryPoints"]["websecure"])
        self.assertNotIn("forwardedHeaders", render(SETTINGS, None)["entryPoints"]["web"])

    def test_given_not_proxied_but_ranges_given_when_rendered_then_they_are_not_trusted(self):
        config = render({**SETTINGS, "CLOUDFLARE_PROXIED": "false"}, ["173.245.48.0/20"])
        self.assertNotIn("forwardedHeaders", config["entryPoints"]["websecure"])

    def test_given_cloudflare_proxied_without_its_ranges_when_rendered_then_it_is_refused(self):
        with self.assertRaisesRegex(SettingsError, "cloudflare-ips.*ygg install"):
            render({**SETTINGS, "CLOUDFLARE_PROXIED": "true"}, None)

    def test_given_cloudflare_proxied_with_an_empty_list_when_rendered_then_it_is_refused(self):
        with self.assertRaisesRegex(SettingsError, "cloudflare-ips"):
            render({**SETTINGS, "CLOUDFLARE_PROXIED": "true"}, [])

    def test_given_a_proxied_value_that_is_not_a_boolean_when_rendered_then_it_is_refused(self):
        with self.assertRaisesRegex(SettingsError, "CLOUDFLARE_PROXIED"):
            render({**SETTINGS, "CLOUDFLARE_PROXIED": "yes"}, None)

    def test_given_a_domain_that_is_not_a_hostname_when_rendered_then_it_is_refused(self):
        with self.assertRaisesRegex(SettingsError, "EXTRA_DOMAINS"):
            render({**SETTINGS, "EXTRA_DOMAINS": "*.bad"}, None)

    def test_given_a_main_domain_that_is_not_a_hostname_when_rendered_then_it_is_refused(self):
        with self.assertRaisesRegex(SettingsError, "DOMAIN"):
            render({**SETTINGS, "DOMAIN": "localhost"}, None)

    def test_given_a_missing_required_setting_when_rendered_then_it_is_refused_by_name(self):
        for name in ("DOMAIN", "ACME_EMAIL", "ACME_DNS_PROVIDER"):
            settings = {k: v for k, v in SETTINGS.items() if k != name}
            with self.assertRaisesRegex(SettingsError, name):
                render(settings, None)

    def test_given_the_defaults_when_rendered_then_acme_matches_todays_flags(self):
        acme = render(SETTINGS, None)["certificatesResolvers"]["letsencrypt"]["acme"]
        self.assertEqual(acme["caServer"], "https://acme-v02.api.letsencrypt.org/directory")
        self.assertEqual(acme["dnsChallenge"], {"provider": "cloudflare", "resolvers": ["1.1.1.1:53", "8.8.8.8:53"]})
        self.assertEqual(acme["email"], "ops@techcraftsmen.net")
        self.assertEqual(acme["storage"], "/letsencrypt/acme.json")

    def test_given_acme_overrides_when_rendered_then_they_replace_the_defaults(self):
        acme = render({**SETTINGS, "ACME_CA_SERVER": "https://staging.example/directory",
                       "ACME_DNS_RESOLVERS": "9.9.9.9:53"}, None)["certificatesResolvers"]["letsencrypt"]["acme"]
        self.assertEqual(acme["caServer"], "https://staging.example/directory")
        self.assertEqual(acme["dnsChallenge"]["resolvers"], ["9.9.9.9:53"])

    def test_given_the_defaults_when_rendered_then_everything_the_old_flags_set_is_there(self):
        config = render(SETTINGS, None)
        self.assertEqual(config["log"], {"format": "json", "level": "INFO"})
        self.assertEqual(config["accessLog"], {"format": "json"})
        self.assertEqual(config["ping"], {"entryPoint": "metrics"})
        docker = config["providers"]["docker"]
        self.assertEqual(docker["endpoint"], "tcp://traefik-docker-proxy:2375")
        self.assertIs(docker["exposedByDefault"], False)
        self.assertEqual(docker["network"], "edge")
        self.assertEqual(config["providers"]["file"], {"directory": "/etc/traefik/dynamic", "watch": True})
        entry_points = config["entryPoints"]
        self.assertEqual(entry_points["web"]["address"], ":80")
        self.assertEqual(entry_points["web"]["http"]["redirections"]["entryPoint"],
                         {"to": "websecure", "scheme": "https"})
        self.assertEqual(entry_points["websecure"]["address"], ":443")
        self.assertEqual(entry_points["websecure"]["http"]["tls"]["certResolver"], "letsencrypt")
        self.assertEqual(entry_points["metrics"]["address"], ":8082")
        self.assertEqual(config["metrics"]["prometheus"], {"entryPoint": "metrics", "addRoutersLabels": True})
        self.assertEqual(config["api"], {"dashboard": True})

    def test_given_the_rendered_config_when_dumped_and_loaded_then_it_is_plain_yaml(self):
        config = render({**SETTINGS, "EXTRA_DOMAINS": "artur-rios.tech"}, None)
        self.assertEqual(yaml.safe_load(yaml.safe_dump(config, sort_keys=False)), config)


class CommandLineTests(unittest.TestCase):
    def run_cli(self, settings, *arguments):
        env = {k: v for k, v in os.environ.items() if k not in SETTINGS and not k.startswith(("ACME_", "EXTRA_", "CLOUDFLARE_"))}
        env.update(settings)
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=env, capture_output=True, text=True)

    def test_given_settings_in_the_environment_when_run_then_it_prints_the_yaml(self):
        result = self.run_cli({**SETTINGS, "EXTRA_DOMAINS": "artur-rios.tech"})
        self.assertEqual(result.returncode, 0, result.stderr)
        config = yaml.safe_load(result.stdout)
        self.assertEqual([d["main"] for d in tls_domains(config)], ["techcraftsmen.net", "artur-rios.tech"])

    def test_given_a_cloudflare_ips_file_when_proxied_then_its_lines_are_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            ranges = pathlib.Path(directory) / "cloudflare-ips"
            ranges.write_text("173.245.48.0/20\n\n2400:cb00::/32\n")
            result = self.run_cli({**SETTINGS, "CLOUDFLARE_PROXIED": "true"}, "--cloudflare-ips", str(ranges))
        self.assertEqual(result.returncode, 0, result.stderr)
        config = yaml.safe_load(result.stdout)
        self.assertEqual(config["entryPoints"]["web"]["forwardedHeaders"]["trustedIPs"],
                         ["173.245.48.0/20", "2400:cb00::/32"])

    def test_given_a_settings_error_when_run_then_exit_2_with_a_message(self):
        result = self.run_cli({**SETTINGS, "CLOUDFLARE_PROXIED": "true"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("cloudflare-ips", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
