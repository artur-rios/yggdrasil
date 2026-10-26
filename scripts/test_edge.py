"""Tests for the edge configuration: Traefik's file middlewares and the routers the stacks' labels add.

    python3 -m unittest discover -s scripts
"""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACKS = ROOT / "stacks"
MIDDLEWARES = ROOT / "platform" / "traefik" / "dynamic" / "middlewares.yml"
PLATFORM = ROOT / "platform" / "compose.yml"
EXAMPLES = ROOT / "docs" / "examples" / "docker-desktop-and-vps" / "env"
UI_STACKS = ("heimdall-ui.proxy.yml", "fortuna-ui.proxy.yml")
API_STACKS = ("heimdall-api.proxy.yml", "fortuna-api.proxy.yml")

AUTH = "traefik.http.routers.heimdall-api-${YGG_ENVIRONMENT}-auth"
AUTH_LIMIT = "traefik.http.middlewares.heimdall-api-${YGG_ENVIRONMENT}-auth-limit"
PUBLIC_HOST = "${PUBLIC_HOST:?set PUBLIC_HOST in the env file}"
UI_HOST = "${UI_HOST:?set UI_HOST in the env file}"
# Every method but the safe ones: Traefik's Method matcher is case-sensitive, while ASP.NET routes a
# lowercase `post` to [HttpPost], so Method(`POST`) would let `post` skip the limit.
SAFE_METHODS_EXCLUDED = " && !Method(`GET`) && !Method(`HEAD`) && !Method(`OPTIONS`)"
AUTH_RULE = (f"(Host(`{PUBLIC_HOST}`) || Host(`{UI_HOST}`))"
             f" && PathRegexp(`(?i)^/api/auth/`){SAFE_METHODS_EXCLUDED}")
HOSTS = {"PUBLIC_HOST": "heimdall-api.example.com", "UI_HOST": "heimdall.example.com"}


class ComposeLoader(yaml.SafeLoader):
    """Compose's merge tags (!reset, !override) read as their plain values."""


for _tag in ("!reset", "!override"):
    ComposeLoader.add_constructor(_tag, lambda loader, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)))


def labels(stack):
    """The label list of the overlay's routed service, the only one with labels (an API's database has none)."""
    services = yaml.load((STACKS / stack).read_text(), Loader=ComposeLoader)["services"]
    labelled = [service for service in services.values() if "labels" in service]
    assert len(labelled) == 1, f"{stack} has {len(labelled)} labelled services"
    return labelled[0]["labels"]


def rule(stack, router):
    prefix = f"traefik.http.routers.{router}.rule="
    values = [label[len(prefix):] for label in labels(stack) if label.startswith(prefix)]
    assert len(values) == 1, f"{stack}: {len(values)} rule labels for {router}"
    return values[0]


PLACEHOLDER = re.compile(r"[$][{][A-Z_]+[^}]*[}]")


def substituted(value, hosts=None):
    """The rule as Traefik sees it: each ${NAME} or ${NAME:?message} replaced by its host."""
    hosts = HOSTS if hosts is None else hosts
    value = PLACEHOLDER.sub(lambda match: hosts[re.match(r"[$][{]([A-Z_]+)", match[0])[1]], value)
    assert "${" not in value, value
    return value


def placeholders(value):
    return set(PLACEHOLDER.findall(value))


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
        for part in ("PathRegexp(`(?i)^/api/auth/`)", f"Host(`{PUBLIC_HOST}`)", f"Host(`{UI_HOST}`)"):
            self.assertIn(part, auth)
        same_origin = rule("heimdall-api.proxy.yml", "heimdall-api-${YGG_ENVIRONMENT}-same-origin")
        ui = rule("heimdall-ui.proxy.yml", "heimdall-ui-${YGG_ENVIRONMENT}")
        # Traefik ranks routers without a priority by rule length: the longest rule wins.
        self.assertGreater(len(substituted(auth)), len(substituted(same_origin)))
        self.assertGreater(len(substituted(same_origin)), len(substituted(ui)))

    def test_auth_rule_outranks_same_origin_and_ui_for_any_hosts(self):
        # With every host empty, the fixed text alone keeps the order; and each rule holds every
        # host placeholder of the one below it, so real host names add at least as much above.
        auth = rule("heimdall-api.proxy.yml", "heimdall-api-${YGG_ENVIRONMENT}-auth")
        same_origin = rule("heimdall-api.proxy.yml", "heimdall-api-${YGG_ENVIRONMENT}-same-origin")
        ui = rule("heimdall-ui.proxy.yml", "heimdall-ui-${YGG_ENVIRONMENT}")
        empty = {name: "" for name in HOSTS}
        self.assertGreater(len(substituted(auth, empty)), len(substituted(same_origin, empty)))
        self.assertGreater(len(substituted(same_origin, empty)), len(substituted(ui, empty)))
        self.assertTrue(placeholders(ui))
        self.assertLessEqual(placeholders(same_origin), placeholders(auth))
        self.assertLessEqual(placeholders(ui), placeholders(same_origin))

    def test_get_routes_bypass_auth_limit(self):
        auth = rule("heimdall-api.proxy.yml", "heimdall-api-${YGG_ENVIRONMENT}-auth")
        self.assertTrue(auth.endswith(SAFE_METHODS_EXCLUDED), auth)
        self.assertNotIn("Method(`POST`)", auth)

    def test_given_an_api_overlay_when_read_then_its_database_is_private_to_the_project(self):
        for stack in API_STACKS:
            with self.subTest(stack=stack):
                overlay = yaml.load((STACKS / stack).read_text(), Loader=ComposeLoader)
                db, api = overlay["services"]["db"], overlay["services"]["api"]
                # On the project's own network only: no shared network, no port, no router.
                self.assertNotIn("networks", db)
                self.assertNotIn("ports", db)
                self.assertNotIn("labels", db)
                self.assertIn("default", api["networks"])
                self.assertEqual(api["depends_on"], {"db": {"condition": "service_healthy"}})
                self.assertEqual(overlay["volumes"], {"db": None})
                self.assertEqual(db["volumes"], ["db:/var/lib/postgresql"])

    def test_given_an_api_overlay_when_read_then_its_database_image_is_pinned(self):
        images = {yaml.load((STACKS / stack).read_text(), Loader=ComposeLoader)["services"]["db"]["image"]
                  for stack in API_STACKS}
        self.assertEqual(len(images), 1, images)
        (image,) = images
        # The major version the data was written by, pinned by digest: never a floating tag.
        self.assertRegex(image, r"^postgres:18\.\d+@sha256:[0-9a-f]{64}$")

    def test_given_an_api_overlay_when_read_then_its_database_needs_the_superuser_password(self):
        for stack in API_STACKS:
            with self.subTest(stack=stack):
                db = yaml.load((STACKS / stack).read_text(), Loader=ComposeLoader)["services"]["db"]
                self.assertEqual(db["environment"],
                                 {"POSTGRES_PASSWORD": "${DB_SUPERUSER_PASSWORD:?set DB_SUPERUSER_PASSWORD in the env file}"})
                # Over TCP: the socket answers during the image's first initialisation too.
                self.assertEqual(db["healthcheck"]["test"],
                                 ["CMD", "pg_isready", "--username", "postgres", "--host", "127.0.0.1"])

    def test_given_the_api_examples_when_read_then_they_reach_the_database_container(self):
        for env in ("development", "homologation", "production"):
            for app in ("heimdall-api", "fortuna-api"):
                with self.subTest(env=env, app=app):
                    example = (EXAMPLES / env / f"{app}.env.example").read_text().splitlines()
                    self.assertEqual([line for line in example if line.startswith("DB_HOST=")], ["DB_HOST=db"])
                    self.assertEqual([line for line in example if line.startswith("DB_SUPERUSER_PASSWORD=")],
                                     ["DB_SUPERUSER_PASSWORD="])

    def test_fortuna_examples_use_internal_heimdall(self):
        for env in ("development", "homologation", "production"):
            with self.subTest(env=env):
                example = (EXAMPLES / env / "fortuna-api.env.example").read_text().splitlines()
                urls = [line for line in example if line.startswith("FORTUNA_HEIMDALL_BASE_URL=")]
                self.assertEqual(urls, [f"FORTUNA_HEIMDALL_BASE_URL=http://heimdall-api.{env}:8080/"])

    def test_fortuna_examples_have_the_keys_3_0_requires(self):
        jwks = {env: f"http://heimdall-api.{env}:8080/.well-known/jwks.json"
                for env in ("development", "homologation", "production")}
        jwks["local"] = "http://host.docker.internal:8080/.well-known/jwks.json"
        for env, url in jwks.items():
            with self.subTest(env=env):
                example = (EXAMPLES / env / "fortuna-api.env.example").read_text().splitlines()
                self.assertEqual([line for line in example if line.startswith("FORTUNA_HEIMDALL_JWKS_URL=")],
                                 [f"FORTUNA_HEIMDALL_JWKS_URL={url}"])
                # A placeholder only: never a real secret in an example.
                self.assertEqual([line for line in example if line.startswith("FORTUNA_LOCAL_TOKEN_SECRET=")],
                                 ["FORTUNA_LOCAL_TOKEN_SECRET="])


if __name__ == "__main__":
    unittest.main()
