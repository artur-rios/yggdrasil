"""Tests for the edge configuration: Traefik's file middlewares and the routers the stacks' labels add.

    python3 -m unittest discover -s scripts
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACKS = ROOT / "stacks"
MIDDLEWARES = ROOT / "platform" / "traefik" / "dynamic" / "middlewares.yml"
PLATFORM = ROOT / "platform" / "compose.yml"
EXAMPLES = ROOT / "docs" / "examples" / "docker-desktop-and-vps" / "env"
UI_STACKS = ("heimdall-ui.proxy.yml", "fortuna-ui.proxy.yml")

AUTH = "traefik.http.routers.heimdall-api-${YGG_ENVIRONMENT}-auth"
AUTH_LIMIT = "traefik.http.middlewares.heimdall-api-${YGG_ENVIRONMENT}-auth-limit"
AUTH_RULE = ("(Host(`${PUBLIC_HOST}`) || Host(`${UI_HOST}`))"
             " && PathRegexp(`(?i)^/api/auth/`) && Method(`POST`)")
HOSTS = {"PUBLIC_HOST": "heimdall-api.example.com", "UI_HOST": "heimdall.example.com"}


class ComposeLoader(yaml.SafeLoader):
    """Compose's merge tags (!reset, !override) read as their plain values."""


for _tag in ("!reset", "!override"):
    ComposeLoader.add_constructor(_tag, lambda loader, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)))


def labels(stack):
    """The label list of the overlay's only service."""
    services = yaml.load((STACKS / stack).read_text(), Loader=ComposeLoader)["services"]
    assert len(services) == 1, f"{stack} has {len(services)} services"
    (service,) = services.values()
    return service["labels"]


def rule(stack, router):
    prefix = f"traefik.http.routers.{router}.rule="
    values = [label[len(prefix):] for label in labels(stack) if label.startswith(prefix)]
    assert len(values) == 1, f"{stack}: {len(values)} rule labels for {router}"
    return values[0]


def substituted(value):
    """The rule as Traefik sees it: ${NAME:?message} and ${NAME} replaced by the example hosts."""
    for name, host in HOSTS.items():
        value = value.replace(f"${{{name}:?set {name} in the env file}}", host)
        value = value.replace(f"${{{name}}}", host)
    assert "${" not in value, value
    return value


def middlewares():
    return yaml.safe_load(MIDDLEWARES.read_text())["http"]["middlewares"]


class EdgeTests(unittest.TestCase):
    def test_ui_headers_match_shared_headers_without_referrer_policy(self):
        shared = middlewares()["secure-headers"]["headers"]
        ui = middlewares()["secure-headers-ui"]["headers"]
        self.assertNotIn("referrerPolicy", ui)
        self.assertEqual(ui, {k: v for k, v in shared.items() if k != "referrerPolicy"})
        self.assertEqual(shared["referrerPolicy"], "strict-origin-when-cross-origin")

    def test_ui_routers_use_ui_headers(self):
        for stack in UI_STACKS:
            with self.subTest(stack=stack):
                chains = [label for label in labels(stack) if ".middlewares=" in label]
                self.assertTrue(chains)
                for label in chains:
                    self.assertTrue(label.endswith(".middlewares=secure-headers-ui@file"), label)

    def test_other_routers_keep_shared_headers(self):
        stacks = sorted(path.name for path in STACKS.glob("*.proxy.yml") if path.name not in UI_STACKS)
        self.assertIn("heimdall-api.proxy.yml", stacks)
        for stack in stacks:
            for label in labels(stack):
                if ".middlewares=" in label:
                    with self.subTest(stack=stack, label=label):
                        self.assertIn("secure-headers@file", label.split("=", 1)[1].split(","))
        platform = yaml.safe_load(PLATFORM.read_text())["services"]
        chains = [(name, key, value) for name, service in platform.items()
                  for key, value in (service.get("labels") or {}).items() if key.endswith(".middlewares")]
        self.assertTrue(chains)
        for name, key, value in chains:
            with self.subTest(service=name, label=key):
                self.assertIn("secure-headers@file", value.split(","))

    def test_heimdall_auth_router(self):
        stack = labels("heimdall-api.proxy.yml")
        for label in (
                f"{AUTH}.rule={AUTH_RULE}",
                f"{AUTH}.entrypoints=websecure",
                f"{AUTH}.middlewares=secure-headers@file,heimdall-api-${{YGG_ENVIRONMENT}}-auth-limit",
                f"{AUTH}.service=heimdall-api-${{YGG_ENVIRONMENT}}",
                f"{AUTH_LIMIT}.ratelimit.average=10",
                f"{AUTH_LIMIT}.ratelimit.period=1m",
                f"{AUTH_LIMIT}.ratelimit.burst=10",
                f"{AUTH_LIMIT}.ratelimit.sourcecriterion.ipstrategy.ipv6subnet=56"):
            with self.subTest(label=label):
                self.assertIn(label, stack)
        for label in stack:
            with self.subTest(label=label):
                self.assertNotIn("requesthost", label.lower())
                self.assertFalse(label.startswith(f"{AUTH}.priority"), label)

    def test_auth_rule_is_case_insensitive_and_covers_both_hosts(self):
        auth = rule("heimdall-api.proxy.yml", "heimdall-api-${YGG_ENVIRONMENT}-auth")
        for part in ("PathRegexp(`(?i)^/api/auth/`)", "Host(`${PUBLIC_HOST}`)", "Host(`${UI_HOST}`)"):
            self.assertIn(part, auth)
        same_origin = rule("heimdall-api.proxy.yml", "heimdall-api-${YGG_ENVIRONMENT}-same-origin")
        ui = rule("heimdall-ui.proxy.yml", "heimdall-ui-${YGG_ENVIRONMENT}")
        # Traefik ranks routers without a priority by rule length: the longest rule wins.
        self.assertGreater(len(substituted(auth)), len(substituted(same_origin)))
        self.assertGreater(len(substituted(same_origin)), len(substituted(ui)))

    def test_get_routes_bypass_auth_limit(self):
        auth = rule("heimdall-api.proxy.yml", "heimdall-api-${YGG_ENVIRONMENT}-auth")
        self.assertTrue(auth.endswith(" && Method(`POST`)"), auth)

    def test_fortuna_examples_use_internal_heimdall(self):
        for env in ("development", "homologation", "production"):
            with self.subTest(env=env):
                example = (EXAMPLES / env / "fortuna-api.env.example").read_text().splitlines()
                urls = [line for line in example if line.startswith("FORTUNA_HEIMDALL_BASE_URL=")]
                self.assertEqual(urls, [f"FORTUNA_HEIMDALL_BASE_URL=http://heimdall-api.{env}:8080/"])


if __name__ == "__main__":
    unittest.main()
