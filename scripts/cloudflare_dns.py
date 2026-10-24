#!/usr/bin/env python3
"""Applies the DNS records of both domains through Cloudflare's API (docs/hosts.md, "DNS records").

    CF_DNS_API_TOKEN=... python3 scripts/cloudflare_dns.py [--dry-run] \\
        --apps <ipv4> [--apps6 <ipv6>] --dev <ipv4> [--dev6 <ipv6>] [--skip-caa <zone>]...

The owner runs it once when building the two machines and again at the cutover, to point the domains
at the new apps machine; run with the old apps address it is the rollback. It makes, on each of
techcraftsmen.net and artur-rios.tech:

  A (and AAAA)   the bare domain and `*`     the apps machine   proxied (orange cloud)
  CAA            the bare domain             0 issue "letsencrypt.org; validationmethods=dns-01"

and, on techcraftsmen.net only, A (and AAAA) `git`, `jenkins`, `traefik-dev` and `grafana-dev` pointing at
the dev machine, DNS only (grey cloud: git over SSH and large pushes cannot pass the proxy). An AAAA record
is made only when --apps6 / --dev6 is given.

Every record it makes carries the comment `yggdrasil`; that comment is how it knows a record is its own.

  * A record with the comment is created if missing, updated in place if its address, proxy flag or CAA
    data differ, and left alone if right (nothing is sent). That is the cutover and the rollback.
  * A record of the same name and type WITHOUT the comment was made by hand: it is reported and left as it
    is, and the run exits 1. So is a CNAME at a name where an A, AAAA or CAA record is wanted (Cloudflare
    allows none beside a CNAME). Change or delete those yourself, then run again.
  * Nothing is ever deleted. An AAAA record with the comment at a name whose address was not given (no
    --apps6 / --dev6) is stale: after a move to an IPv4-only address it would keep sending IPv6 visitors to
    the old machine. It is reported ("delete it by hand or pass --apps6") and the run exits 1.
  * Every other record of the zone is not looked at. Note that a specific record wins over the wildcard:
    an old `www` or `blog` CNAME (to Vercel, say) keeps answering until you delete it.

--skip-caa <zone> (repeatable, one of the two zones) leaves that zone's CAA record out: it is neither created,
updated nor reported, and the run says "CAA skipped on <zone> (--skip-caa)". It is for a zone whose names still
get certificates another way: artur-rios.tech, while Vercel serves www. and blog. with certificates it renews
through Let's Encrypt over HTTP-01, which the CAA's validationmethods=dns-01 would refuse. Drop the flag once
those sites have moved.

--dry-run still reads from Cloudflare (so it needs the token) but sends no write; it prints what a real
run would change. The token (CF_DNS_API_TOKEN, the name acme.env uses) goes only in the Authorization
header (rules.Api), never in a process argument, a URL, an error or the output. It needs Zone > Zone >
Read and Zone > DNS > Edit on both zones, as the Traefik token does.

Exit status: 0 done, 1 something was reported or failed, 2 usage (bad address, no token).
"""

import argparse
import ipaddress
import os
import pathlib
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import rules  # noqa: E402  (scripts/rules.py: Api, ApiError)

API_BASE = "https://api.cloudflare.com"
ZONES = ("techcraftsmen.net", "artur-rios.tech")
DEV_ZONE = "techcraftsmen.net"
DEV_NAMES = ("git", "jenkins", "traefik-dev", "grafana-dev")
COMMENT = "yggdrasil"
CAA_DATA = {"flags": 0, "tag": "issue", "value": "letsencrypt.org; validationmethods=dns-01"}
PER_PAGE = 100


# --- What the records should be -----------------------------------------------------------------------

def _address_records(zone, name, v4, v6, proxied):
    """The A record at zone/name and, only when `v6` is given, its AAAA record."""
    fqdn = zone if name == "@" else f"{name}.{zone}"
    records = [{"zone": zone, "name": fqdn, "type": "A", "content": v4, "proxied": proxied,
                "comment": COMMENT, "ttl": 1}]
    if v6:
        records.append({"zone": zone, "name": fqdn, "type": "AAAA", "content": v6, "proxied": proxied,
                        "comment": COMMENT, "ttl": 1})
    return records


def desired_records(apps, apps6, dev, dev6, skip_caa=()):
    """Every record the script makes, as dicts: zone, name (the full name), type, content (or `data` for
    CAA), proxied (not for CAA), comment, ttl (1 is Cloudflare's "automatic"; a proxied record is always
    automatic). The addresses are strings; apps6 and dev6 may be empty or None: then no AAAA record is wanted.
    `skip_caa`: the zones whose CAA record is left out (--skip-caa)."""
    records = []
    for zone in ZONES:
        records += _address_records(zone, "@", apps, apps6, True)
        records += _address_records(zone, "*", apps, apps6, True)
        if zone == DEV_ZONE:
            for name in DEV_NAMES:
                records += _address_records(zone, name, dev, dev6, False)
        if zone not in skip_caa:
            records.append({"zone": zone, "name": zone, "type": "CAA", "data": dict(CAA_DATA),
                            "comment": COMMENT, "ttl": 1})
    return records


def _show(record):
    """A wanted record's value for a message: `1.2.3.4 (proxied)`, `2001:db8::1 (DNS only)`, or the CAA data."""
    if record["type"] == "CAA":
        data = record["data"]
        return f'{data["flags"]} {data["tag"]} "{data["value"]}"'
    return f'{record["content"]} ({"proxied" if record["proxied"] else "DNS only"})'


def _same_address(left, right):
    """Equal as addresses (Cloudflare may answer an IPv6 address written another way), else as text."""
    try:
        return ipaddress.ip_address(left) == ipaddress.ip_address(right)
    except ValueError:
        return left == right


def _differences(existing, want):
    """What differs between a record Cloudflare has and the one wanted, as text; empty when it is right.
    The TTL is not compared: a proxied record's is always automatic, and a hand-tuned one is the owner's."""
    differences = []
    if want["type"] == "CAA":
        have = existing.get("data") or {}
        for key in ("flags", "tag", "value"):
            if have.get(key) != want["data"][key]:
                differences.append(f'data.{key} {have.get(key)!r} -> {want["data"][key]!r}')
        return differences
    if not _same_address(str(existing.get("content", "")), want["content"]):
        differences.append(f'address {existing.get("content")} -> {want["content"]}')
    if bool(existing.get("proxied")) != want["proxied"]:
        differences.append("proxied " + ("on" if want["proxied"] else "off"))
    return differences


def _body(want):
    """What is sent to Cloudflare for a record: the wanted fields without the zone."""
    return {key: value for key, value in want.items() if key != "zone"}


# --- Cloudflare ---------------------------------------------------------------------------------------

class ZoneError(rules.ApiError):
    """The zone could not be looked up (not there, or the token may not read it): nothing in it can be tried."""


class Cloudflare:
    """The few calls the script needs. Cloudflare sometimes answers `{"success": false, "errors": [...]}`
    with HTTP 200, so every answer is checked; that is an error like any non-2xx one (rules.ApiError).
    The token lives only in the api object's Authorization header, so it cannot reach an error."""

    ZONES_PATH = "/client/v4/zones"

    def __init__(self, api):
        self.api = api
        self._zones = {}

    def _call(self, method, path, body=None):
        answer = self.api.request(method, path, body)
        if not isinstance(answer, dict) or answer.get("success") is False:
            errors = answer.get("errors") if isinstance(answer, dict) else None
            text = "; ".join(f'{e.get("code", "?")}: {e.get("message", "")}' if isinstance(e, dict) else str(e)
                             for e in errors or []) or "no success in the answer"
            raise rules.ApiError(method, path.split("?")[0], 200, text)
        return answer

    def zone_id(self, zone):
        if zone not in self._zones:
            query = urllib.parse.urlencode({"name": zone})
            try:
                found = self._call("GET", f"{self.ZONES_PATH}?{query}").get("result") or []
            except rules.ApiError as error:
                raise ZoneError(error.method, f"{self.ZONES_PATH} (zone {zone})", error.status, error.message) from None
            if not found:
                raise ZoneError("GET", self.ZONES_PATH, 200,
                                f"zone {zone} not found: does the token have access to it?")
            self._zones[zone] = found[0]["id"]
        return self._zones[zone]

    def records(self, zone, name, type_):
        """Every record of that name and type (all pages)."""
        found, page = [], 1
        while True:
            query = urllib.parse.urlencode({"name": name, "type": type_, "page": page, "per_page": PER_PAGE})
            answer = self._call("GET", f"{self.ZONES_PATH}/{self.zone_id(zone)}/dns_records?{query}")
            found += answer.get("result") or []
            pages = (answer.get("result_info") or {}).get("total_pages") or 1
            if page >= pages:
                return found
            page += 1

    def create(self, zone, want):
        self._call("POST", f"{self.ZONES_PATH}/{self.zone_id(zone)}/dns_records", _body(want))

    def update(self, zone, record_id, want):
        self._call("PUT", f"{self.ZONES_PATH}/{self.zone_id(zone)}/dns_records/{record_id}", _body(want))


# --- Applying -----------------------------------------------------------------------------------------

def _apply_one(cloudflare, want, dry_run, report):
    """Brings one wanted record about. Returns "ok" (already right), "changed" or "reported" (a problem went
    to `report`, whether or not something was also changed)."""
    zone, name, type_ = want["zone"], want["name"], want["type"]
    label = f"{type_} {name}"

    # Cloudflare allows no other record beside a CNAME, whatever made it: we never make one.
    cnames = cloudflare.records(zone, name, "CNAME")
    if cnames:
        report(f"{label}: a CNAME to {cnames[0].get('content')} is at this name and Cloudflare allows "
               "none beside it; made by hand, left as it is: change it or delete it yourself")
        return "reported"

    existing = cloudflare.records(zone, name, type_)
    ours = [r for r in existing if r.get("comment") == COMMENT]
    by_hand = [r for r in existing if r.get("comment") != COMMENT]
    status = "ok"
    for record in by_hand:
        shown = record.get("content") if type_ != "CAA" else "CAA data"
        report(f"{label} ({shown}): made by hand; change it or delete it yourself")
        status = "reported"
    if by_hand and not ours:
        return status  # a second record beside it would only split the traffic

    verb = "would " if dry_run else ""
    if not ours:
        print(f"{verb}create {label}: {_show(want)}")
        if not dry_run:
            cloudflare.create(zone, want)
        return status if status == "reported" else "changed"

    # Update the one that is wrong (or leave the one that is right). Any other record of ours would keep
    # answering beside it: reported, because deleting is the owner's call.
    right = [r for r in ours if not _differences(r, want)]
    keep = right[0] if right else ours[0]
    for extra in ours:
        if extra is not keep:
            report(f"{label} ({extra.get('content')}): a second record made by yggdrasil answers beside "
                   "the right one; delete it by hand")
            status = "reported"
    differences = _differences(keep, want)
    if differences:
        print(f"{verb}update {label}: {', '.join(differences)}")
        if not dry_run:
            cloudflare.update(zone, keep["id"], want)
        return status if status == "reported" else "changed"
    return status


def _stale_ipv6(cloudflare, wanted, report):
    """Reports (never deletes) an AAAA record of ours at a name whose IPv6 address was not given."""
    with_ipv6 = {(w["zone"], w["name"]) for w in wanted if w["type"] == "AAAA"}
    for want in wanted:
        if want["type"] != "A" or (want["zone"], want["name"]) in with_ipv6:
            continue
        for record in cloudflare.records(want["zone"], want["name"], "AAAA"):
            if record.get("comment") == COMMENT:
                flag = "--apps6" if want["proxied"] else "--dev6"
                report(f"AAAA {want['name']} ({record.get('content')}): stale (no address given): "
                       f"delete it by hand or pass {flag}")


def apply(cloudflare, wanted, dry_run=False):
    """Applies the wanted records; prints what changes (or would); returns the problems found, as text."""
    problems = []
    unchanged = 0
    skipped_zones = set()

    for want in wanted:
        if want["zone"] in skipped_zones:
            continue
        try:
            if _apply_one(cloudflare, want, dry_run, problems.append) == "ok":
                unchanged += 1
        except rules.ApiError as error:
            problems.append(str(error))
            if isinstance(error, ZoneError):  # the zone cannot be looked up: nothing more to try in it
                skipped_zones.add(want["zone"])
    try:
        _stale_ipv6(cloudflare, [w for w in wanted if w["zone"] not in skipped_zones], problems.append)
    except rules.ApiError as error:
        problems.append(str(error))
    print(f"{unchanged} of {len(wanted)} records already right")
    return problems


# --- The command --------------------------------------------------------------------------------------

def _address(version):
    def parse(text):
        try:
            return str(ipaddress.IPv4Address(text) if version == 4 else ipaddress.IPv6Address(text))
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not an IPv{version} address") from None
    return parse


def _parser():
    parser = argparse.ArgumentParser(
        prog="cloudflare_dns.py",
        description="Apply the DNS records of techcraftsmen.net and artur-rios.tech through Cloudflare "
                    "(token: CF_DNS_API_TOKEN).")
    parser.add_argument("--dry-run", action="store_true", help="print the changes, send none")
    parser.add_argument("--apps", required=True, type=_address(4), metavar="IPV4", help="the apps machine")
    parser.add_argument("--apps6", type=_address(6), metavar="IPV6", help="the apps machine, to make AAAA records")
    parser.add_argument("--dev", required=True, type=_address(4), metavar="IPV4", help="the dev machine")
    parser.add_argument("--dev6", type=_address(6), metavar="IPV6", help="the dev machine, to make AAAA records")
    parser.add_argument("--skip-caa", action="append", default=[], choices=ZONES, metavar="ZONE",
                        help="leave this zone's CAA record out (repeatable): neither created, updated nor reported")
    return parser


def main(argv=None, environ=None, api=None):
    """Runs the command; returns the exit status. `api` stands in for rules.Api (tests)."""
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(argv)
    except SystemExit as stop:  # argparse: --help is 0, a bad argument 2
        return stop.code if isinstance(stop.code, int) else 2
    token = environ.get("CF_DNS_API_TOKEN", "").strip()
    if not token:
        print("cloudflare_dns.py: no token: set CF_DNS_API_TOKEN (the one acme.env holds)", file=sys.stderr)
        return 2
    wanted = desired_records(args.apps, args.apps6, args.dev, args.dev6, skip_caa=args.skip_caa)
    for zone in dict.fromkeys(args.skip_caa):
        print(f"CAA skipped on {zone} (--skip-caa)")
    cloudflare = Cloudflare(api or rules.Api(API_BASE, token, "Bearer"))
    problems = apply(cloudflare, wanted, dry_run=args.dry_run)
    if problems:
        print(f"{len(problems)} to see to:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
