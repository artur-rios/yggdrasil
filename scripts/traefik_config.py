#!/usr/bin/env python3
"""Renders Traefik's static configuration (traefik.yml) from the platform's settings.

    python3 scripts/traefik_config.py [--cloudflare-ips FILE]

The settings are read from the environment (scripts/platform.sh sources the env file and exports
them) and the YAML goes to stdout. Exit 2, with a message on stderr, on a settings error.

    DOMAIN             required. The host's base domain
    EXTRA_DOMAINS      comma-separated, optional. More domains this host serves, each with its own
                       wildcard certificate
    ACME_EMAIL         required. The Let's Encrypt account's contact
    ACME_DNS_PROVIDER  required. The DNS-01 provider's code
    ACME_CA_SERVER     default: Let's Encrypt production
    ACME_DNS_RESOLVERS default: 1.1.1.1:53,8.8.8.8:53
    CLOUDFLARE_PROXIED true or false (default false). Then --cloudflare-ips names the file with
                       Cloudflare's ranges, one CIDR per line (written by `ygg install`)

This used to be a list of command flags in platform/compose.yml; a file can hold the list of
domains, which flags could only index by hand.

Needs PyYAML (pip install pyyaml; Ubuntu: apt install python3-yaml).
"""

import argparse
import os
import re
import sys
from collections.abc import Mapping

import yaml

# The catalog's host-label rules (HOST_LABEL in scripts/catalog.py), joined by dots, ending in a
# top-level domain: what a wildcard certificate can be issued for.
DOMAIN = re.compile(r"^([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}$")

DEFAULT_CA_SERVER = "https://acme-v02.api.letsencrypt.org/directory"
DEFAULT_RESOLVERS = "1.1.1.1:53,8.8.8.8:53"


class SettingsError(ValueError):
    """A setting is missing or wrong; the message says which and what to do."""


def required(settings: Mapping[str, str], name: str) -> str:
    value = settings.get(name, "").strip()
    if not value:
        raise SettingsError(f"{name} must be set in the platform's settings")
    return value


def split(value: str) -> list[str]:
    """A comma-separated list: whitespace stripped, empty entries ignored."""
    return [item.strip() for item in value.split(",") if item.strip()]


def domains(settings: Mapping[str, str]) -> list[str]:
    main = required(settings, "DOMAIN")
    if not DOMAIN.match(main):
        raise SettingsError(f"DOMAIN '{main}' is not a domain name (e.g. example.com)")
    extra = split(settings.get("EXTRA_DOMAINS", ""))
    for domain in extra:
        if not DOMAIN.match(domain):
            raise SettingsError(f"EXTRA_DOMAINS has '{domain}', which is not a domain name "
                                "(e.g. artur-rios.tech; no wildcard, no scheme)")
    return [main, *extra]


def proxied(settings: Mapping[str, str]) -> bool:
    value = settings.get("CLOUDFLARE_PROXIED", "").strip() or "false"
    if value not in ("true", "false"):
        raise SettingsError(f"CLOUDFLARE_PROXIED is '{value}': it must be true or false")
    return value == "true"


def render(settings: Mapping[str, str], cloudflare_ips: list[str] | None) -> dict:
    """The static configuration as a dict. cloudflare_ips are Cloudflare's ranges; they are used
    (and required) only when CLOUDFLARE_PROXIED is true."""
    hosts = domains(settings)
    email = required(settings, "ACME_EMAIL")
    provider = required(settings, "ACME_DNS_PROVIDER")
    ca_server = settings.get("ACME_CA_SERVER", "").strip() or DEFAULT_CA_SERVER
    resolvers = split(settings.get("ACME_DNS_RESOLVERS", "") or DEFAULT_RESOLVERS)

    web = {
        "address": ":80",
        "http": {"redirections": {"entryPoint": {"to": "websecure", "scheme": "https"}}},
    }
    websecure = {
        "address": ":443",
        "http": {
            "tls": {
                "certResolver": "letsencrypt",
                # No tls.options here: the options named `default` in traefik/dynamic apply to every
                # router by themselves, and have no provider namespace -- `default@file` is an
                # unknown name, and Traefik then builds no HTTPS router at all.
                # One wildcard certificate per domain instead of one per host name: it covers every
                # environment of the host too, as their host names differ by hostSuffix
                # (heimdall-dev.DOMAIN), not by a sub-domain. It is what makes DNS-01 worth it -- and
                # a host behind NAT can never answer an HTTP-01 challenge anyway.
                "domains": [{"main": host, "sans": [f"*.{host}"]} for host in hosts],
            },
        },
    }
    if proxied(settings):
        if not cloudflare_ips:
            raise SettingsError("CLOUDFLARE_PROXIED is true but there is no <secrets>/cloudflare-ips "
                                "(Cloudflare's ranges, one per line): run `ygg install` to write it")
        # Behind Cloudflare every connection comes from one of its addresses, and the client's own is
        # in X-Forwarded-For. Trust that header from Cloudflare's ranges only: from anyone else it
        # would let a client choose the address Traefik logs and rate-limits.
        for entry_point in (web, websecure):
            entry_point["forwardedHeaders"] = {"trustedIPs": list(cloudflare_ips)}

    return {
        "log": {"format": "json", "level": "INFO"},
        "accessLog": {"format": "json"},
        # /ping on the internal metrics port, where the status API probes it; the container health
        # check (traefik healthcheck) uses the same entry point.
        "ping": {"entryPoint": "metrics"},
        "providers": {
            "docker": {
                # Through traefik-docker-proxy, never the socket itself: Traefik faces the internet.
                "endpoint": "tcp://traefik-docker-proxy:2375",
                # Nothing is routed unless it opts in with traefik.enable=true.
                "exposedByDefault": False,
                "network": "edge",
            },
            "file": {"directory": "/etc/traefik/dynamic", "watch": True},
        },
        "entryPoints": {
            "web": web,
            "websecure": websecure,
            "metrics": {"address": ":8082"},
        },
        "certificatesResolvers": {
            "letsencrypt": {
                "acme": {
                    "email": email,
                    "storage": "/letsencrypt/acme.json",
                    # Point at https://acme-staging-v02.api.letsencrypt.org/directory while trying
                    # things out: the production Let's Encrypt service rate-limits failed validations
                    # per hour.
                    "caServer": ca_server,
                    "dnsChallenge": {
                        # Any provider Traefik supports
                        # (https://doc.traefik.io/traefik/https/acme/#providers): its code here, and
                        # its credentials in acme.env next to platform.env.
                        "provider": provider,
                        # Check propagation against public resolvers, not the host's, which on a home
                        # network may cache the old answer for longer than the challenge waits.
                        "resolvers": resolvers,
                    },
                },
            },
        },
        "metrics": {"prometheus": {"entryPoint": "metrics", "addRoutersLabels": True}},
        "api": {"dashboard": True},
    }


def read_ranges(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render Traefik's static configuration.")
    parser.add_argument("--cloudflare-ips", metavar="FILE",
                        help="Cloudflare's ranges, one CIDR per line (used when CLOUDFLARE_PROXIED=true)")
    args = parser.parse_args(argv)
    try:
        ranges = read_ranges(args.cloudflare_ips) if args.cloudflare_ips else None
        config = render(os.environ, ranges)
    except (SettingsError, OSError) as error:
        print(f"traefik_config: {error}", file=sys.stderr)
        return 2
    sys.stdout.write(yaml.safe_dump(config, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
