"""Tests for scripts/cloudflare_dns.py (the DNS records of both domains).   python3 -m unittest discover -s scripts"""

import contextlib
import io
import unittest
import urllib.parse

import cloudflare_dns
import rules

APPS, APPS6 = "203.0.113.10", "2001:db8::10"
DEV, DEV6 = "203.0.113.20", "2001:db8::20"
TOKEN = "cf-secret-token-123"
CAA = {"flags": 0, "tag": "issue", "value": "letsencrypt.org; validationmethods=dns-01"}
ENV = {"CF_DNS_API_TOKEN": TOKEN}
ARGS = ["--apps", APPS, "--dev", DEV]


def wanted_by_key(**kwargs):
    """The desired records keyed by (type, full name)."""
    records = cloudflare_dns.desired_records(**{"apps": APPS, "apps6": None, "dev": DEV, "dev6": None, **kwargs})
    return {(r["type"], r["name"]): r for r in records}


class FakeCloudflare:
    """Stands in for rules.Api, answering like Cloudflare does: zones by name, DNS records filtered by name
    and type in pages of at most `page_size`, `result_info.total_pages`, and (with `fail`) an HTTP-200
    answer that says `success: false`. Keeps the records, so applying twice shows what the second run
    does."""

    def __init__(self, page_size=100, fail=None):
        self.calls = []
        self.zones = {z: f"zone-{i}" for i, z in enumerate(cloudflare_dns.ZONES)}
        self.records = {zone_id: [] for zone_id in self.zones.values()}
        self.page_size = page_size
        self.refuse_zones = False  # answer every zone lookup with HTTP 403
        self.fail = fail  # a (method, path-prefix) whose answer is {"success": false}
        self.next_id = 1

    def add(self, zone, name, type_, content=None, proxied=False, comment="", data=None):
        record = {"id": f"rec-{self.next_id}", "name": name, "type": type_, "comment": comment,
                  "ttl": 1, "proxied": proxied, "zone_name": zone}
        if data is not None:
            record["data"] = data
        else:
            record["content"] = content
        self.next_id += 1
        self.records[self.zones[zone]].append(record)
        return record

    def seed_right(self, apps6=None, dev6=None):
        """Records exactly as a first run with these addresses leaves them."""
        for want in cloudflare_dns.desired_records(APPS, apps6, DEV, dev6):
            self.add(want["zone"], want["name"], want["type"], want.get("content"), want.get("proxied", False),
                     want["comment"], want.get("data"))

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.fail and method == self.fail[0] and path.startswith(self.fail[1]):
            return {"success": False, "errors": [{"code": 9109, "message": "Invalid access token"}],
                    "result": None}
        if self.refuse_zones and path.startswith("/client/v4/zones?"):
            raise rules.ApiError(method, path, 403, "Forbidden")
        url = urllib.parse.urlparse(path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        parts = url.path.split("/")
        if parts[-1] == "zones":
            found = [{"id": i, "name": n} for n, i in self.zones.items() if n == query["name"]]
            return {"success": True, "errors": [], "result": found}
        zone_id = parts[4]
        if method == "GET":
            matching = [r for r in self.records[zone_id]
                        if r["name"] == query["name"] and r["type"] == query["type"]]
            page, size = int(query["page"]), min(int(query["per_page"]), self.page_size)
            total = max(1, -(-len(matching) // size))
            return {"success": True, "errors": [], "result": matching[(page - 1) * size:page * size],
                    "result_info": {"page": page, "per_page": size, "total_pages": total}}
        if method == "POST":
            record = {"id": f"rec-{self.next_id}", "proxied": False, **body}
            self.next_id += 1
            self.records[zone_id].append(record)
            return {"success": True, "errors": [], "result": record}
        if method == "PUT":
            for record in self.records[zone_id]:
                if record["id"] == parts[-1]:
                    keep = {"id": record["id"]}
                    record.clear()
                    record.update({"proxied": False, **body, **keep})
                    return {"success": True, "errors": [], "result": record}
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def find(self, zone, name, type_):
        return [r for r in self.records[self.zones[zone]] if r["name"] == name and r["type"] == type_]


def run(fake, argv=ARGS, environ=ENV):
    """main() with the fake: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = cloudflare_dns.main(argv, environ=environ, api=fake)
    return status, out.getvalue(), err.getvalue()


class DesiredRecordsTests(unittest.TestCase):
    def test_given_both_addresses_when_desired_then_apps_names_are_proxied_and_dev_names_are_not(self):
        wanted = wanted_by_key(apps6=APPS6, dev6=DEV6)
        for zone in ("techcraftsmen.net", "artur-rios.tech"):
            for name in (zone, f"*.{zone}"):
                for type_, address in (("A", APPS), ("AAAA", APPS6)):
                    self.assertEqual(wanted[(type_, name)]["content"], address)
                    self.assertTrue(wanted[(type_, name)]["proxied"], f"{type_} {name}")
        for host in ("git", "jenkins", "traefik-dev", "grafana-dev"):
            for type_, address in (("A", DEV), ("AAAA", DEV6)):
                record = wanted[(type_, f"{host}.techcraftsmen.net")]
                self.assertEqual(record["content"], address)
                self.assertFalse(record["proxied"], f"{type_} {host}")

    def test_given_the_second_zone_when_desired_then_it_has_no_dev_names(self):
        wanted = wanted_by_key(apps6=APPS6, dev6=DEV6)
        names = {name for _, name in wanted if name.endswith("artur-rios.tech")}
        self.assertEqual(names, {"artur-rios.tech", "*.artur-rios.tech"})

    def test_given_no_ipv6_addresses_when_desired_then_there_are_no_aaaa_records(self):
        self.assertEqual({t for t, _ in wanted_by_key()}, {"A", "CAA"})

    def test_given_only_the_apps_ipv6_when_desired_then_only_apps_names_get_aaaa(self):
        names = {n for t, n in wanted_by_key(apps6=APPS6) if t == "AAAA"}
        self.assertEqual(names, {"techcraftsmen.net", "*.techcraftsmen.net", "artur-rios.tech", "*.artur-rios.tech"})

    def test_given_the_zones_when_desired_then_each_has_one_caa_record_on_the_bare_domain(self):
        wanted = wanted_by_key()
        for zone in ("techcraftsmen.net", "artur-rios.tech"):
            record = wanted[("CAA", zone)]
            self.assertEqual(record["data"], CAA)
            self.assertNotIn("content", record)
        self.assertEqual(len([k for k in wanted if k[0] == "CAA"]), 2)

    def test_given_any_record_when_desired_then_it_carries_the_yggdrasil_comment(self):
        for record in cloudflare_dns.desired_records(APPS, APPS6, DEV, DEV6):
            self.assertEqual(record["comment"], "yggdrasil")


class ApplyTests(unittest.TestCase):
    def test_given_empty_zones_when_applied_then_every_record_is_created_with_the_comment(self):
        fake = FakeCloudflare()
        status, out, _ = run(fake, ARGS + ["--apps6", APPS6, "--dev6", DEV6])
        self.assertEqual(status, 0)
        want = cloudflare_dns.desired_records(APPS, APPS6, DEV, DEV6)
        self.assertEqual(len(fake.writes()), len(want))
        self.assertTrue(all(m == "POST" for m, _, _ in fake.writes()))
        sent = fake.find("techcraftsmen.net", "git.techcraftsmen.net", "A")[0]
        self.assertEqual((sent["content"], sent["proxied"], sent["comment"]), (DEV, False, "yggdrasil"))
        caa = fake.find("artur-rios.tech", "artur-rios.tech", "CAA")[0]
        self.assertEqual(caa["data"], CAA)
        self.assertIn("create A *.techcraftsmen.net", out)

    def test_given_records_already_right_when_applied_then_nothing_is_sent(self):
        fake = FakeCloudflare()
        fake.seed_right(APPS6, DEV6)
        status, out, err = run(fake, ARGS + ["--apps6", APPS6, "--dev6", DEV6])
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(fake.writes(), [])
        self.assertNotIn("create", out)
        self.assertNotIn("update", out)

    def test_given_a_first_run_when_run_again_then_the_second_run_sends_nothing(self):
        fake = FakeCloudflare()
        run(fake)
        fake.calls.clear()
        self.assertEqual(run(fake)[0], 0)
        self.assertEqual(fake.writes(), [])

    def test_given_a_record_made_by_hand_when_applied_then_it_is_reported_and_left(self):
        fake = FakeCloudflare()
        fake.add("artur-rios.tech", "artur-rios.tech", "A", "76.76.21.21", proxied=False, comment="")
        status, _, err = run(fake)
        self.assertEqual(status, 1)
        self.assertIn("A artur-rios.tech", err)
        self.assertIn("made by hand; change it or delete it yourself", err)
        # left as it is, and no second A record made beside it
        by_hand = fake.find("artur-rios.tech", "artur-rios.tech", "A")
        self.assertEqual([(r["content"], r["comment"]) for r in by_hand], [("76.76.21.21", "")])
        self.assertFalse([w for w in fake.writes() if "artur-rios.tech" in str(w[2].get("name")) and
                          w[2]["type"] == "A" and w[2]["name"] == "artur-rios.tech"])
        # everything else was still done
        self.assertTrue(fake.find("artur-rios.tech", "*.artur-rios.tech", "A"))
        self.assertTrue(fake.find("techcraftsmen.net", "techcraftsmen.net", "A"))

    def test_given_a_record_with_another_comment_when_applied_then_it_counts_as_made_by_hand(self):
        fake = FakeCloudflare()
        fake.add("techcraftsmen.net", "git.techcraftsmen.net", "A", "198.51.100.1", comment="yggdrasil-old")
        status, _, err = run(fake)
        self.assertEqual(status, 1)
        self.assertIn("git.techcraftsmen.net", err)
        self.assertEqual(fake.find("techcraftsmen.net", "git.techcraftsmen.net", "A")[0]["content"], "198.51.100.1")

    def test_given_a_cname_where_an_a_record_is_wanted_when_applied_then_it_is_reported_and_left(self):
        fake = FakeCloudflare()
        fake.add("artur-rios.tech", "*.artur-rios.tech", "CNAME", "cname.vercel-dns.com", comment="")
        status, _, err = run(fake)
        self.assertEqual(status, 1)
        self.assertIn("CNAME", err)
        self.assertIn("*.artur-rios.tech", err)
        self.assertEqual(fake.find("artur-rios.tech", "*.artur-rios.tech", "A"), [])
        self.assertEqual(len(fake.find("artur-rios.tech", "*.artur-rios.tech", "CNAME")), 1)
        self.assertFalse([w for w in fake.writes() if w[0] != "POST"])

    def test_given_a_cutover_when_applied_with_a_new_apps_address_then_owned_records_are_updated_in_place(self):
        fake = FakeCloudflare()
        fake.seed_right()
        ids = {r["id"] for z in fake.records.values() for r in z}
        status, out, _ = run(fake, ["--apps", "198.51.100.77", "--dev", DEV])
        self.assertEqual(status, 0)
        methods = {m for m, _, _ in fake.writes()}
        self.assertEqual(methods, {"PUT"})
        self.assertEqual(len(fake.writes()), 4)  # the bare domain and `*`, on both zones
        self.assertEqual({r["id"] for z in fake.records.values() for r in z}, ids)
        self.assertEqual(fake.find("techcraftsmen.net", "*.techcraftsmen.net", "A")[0]["content"], "198.51.100.77")
        self.assertEqual(fake.find("techcraftsmen.net", "git.techcraftsmen.net", "A")[0]["content"], DEV)
        self.assertIn("address", out)

    def test_given_a_cutover_then_a_rollback_when_applied_then_the_old_address_is_back(self):
        fake = FakeCloudflare()
        fake.seed_right()
        run(fake, ["--apps", "198.51.100.77", "--dev", DEV])
        self.assertEqual(run(fake, ARGS)[0], 0)
        self.assertEqual(fake.find("artur-rios.tech", "artur-rios.tech", "A")[0]["content"], APPS)

    def test_given_an_owned_record_with_the_wrong_proxy_flag_when_applied_then_it_is_updated(self):
        fake = FakeCloudflare()
        fake.seed_right()
        fake.find("techcraftsmen.net", "jenkins.techcraftsmen.net", "A")[0]["proxied"] = True
        status, _, _ = run(fake)
        self.assertEqual(status, 0)
        self.assertEqual(len(fake.writes()), 1)
        self.assertFalse(fake.find("techcraftsmen.net", "jenkins.techcraftsmen.net", "A")[0]["proxied"])

    def test_given_an_owned_caa_record_with_other_data_when_applied_then_it_is_updated(self):
        fake = FakeCloudflare()
        fake.seed_right()
        fake.find("techcraftsmen.net", "techcraftsmen.net", "CAA")[0]["data"] = {**CAA, "value": "other.example"}
        status, _, _ = run(fake)
        self.assertEqual(status, 0)
        self.assertEqual(fake.find("techcraftsmen.net", "techcraftsmen.net", "CAA")[0]["data"], CAA)

    def test_given_an_ipv6_address_written_differently_when_applied_then_nothing_is_sent(self):
        fake = FakeCloudflare()
        fake.seed_right(APPS6, DEV6)
        fake.find("artur-rios.tech", "artur-rios.tech", "AAAA")[0]["content"] = "2001:0db8:0:0:0:0:0:10"
        self.assertEqual(run(fake, ARGS + ["--apps6", APPS6, "--dev6", DEV6])[0], 0)
        self.assertEqual(fake.writes(), [])

    def test_given_an_aaaa_record_of_ours_and_no_ipv6_address_when_applied_then_it_is_reported_stale_and_kept(self):
        fake = FakeCloudflare()
        fake.seed_right(APPS6, DEV6)
        status, _, err = run(fake)  # no --apps6 / --dev6
        self.assertEqual(status, 1)
        self.assertIn("AAAA artur-rios.tech", err)
        self.assertIn("stale (no address given): delete it by hand or pass --apps6", err)
        self.assertIn("AAAA git.techcraftsmen.net", err)
        self.assertIn("--dev6", err)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(len(fake.find("artur-rios.tech", "artur-rios.tech", "AAAA")), 1)

    def test_given_an_aaaa_record_made_by_hand_and_no_ipv6_address_when_applied_then_it_is_not_ours_to_mention(self):
        fake = FakeCloudflare()
        fake.seed_right()
        fake.add("artur-rios.tech", "artur-rios.tech", "AAAA", "2001:db8::99", comment="")
        status, out, err = run(fake)
        self.assertEqual((status, err), (0, ""))
        self.assertNotIn("AAAA", out)

    def test_given_an_ipv6_address_when_applied_then_the_aaaa_records_are_created(self):
        fake = FakeCloudflare()
        fake.seed_right()
        status, _, _ = run(fake, ARGS + ["--apps6", APPS6])
        self.assertEqual(status, 0)
        self.assertEqual(fake.find("artur-rios.tech", "*.artur-rios.tech", "AAAA")[0]["content"], APPS6)
        self.assertEqual(fake.find("techcraftsmen.net", "git.techcraftsmen.net", "AAAA"), [])

    def test_given_any_run_when_applied_then_nothing_is_ever_deleted(self):
        fake = FakeCloudflare()
        fake.seed_right(APPS6, DEV6)
        fake.add("techcraftsmen.net", "git.techcraftsmen.net", "A", "198.51.100.1", comment="")
        run(fake, ["--apps", "198.51.100.77", "--dev", "198.51.100.88"])
        self.assertNotIn("DELETE", {m for m, _, _ in fake.calls})

    def test_given_records_on_several_pages_when_applied_then_every_page_is_read(self):
        fake = FakeCloudflare(page_size=2)
        for i in range(3):
            fake.add("techcraftsmen.net", "git.techcraftsmen.net", "A", f"198.51.100.{i}", comment="")
        # the one made by yggdrasil is on the second page
        fake.add("techcraftsmen.net", "git.techcraftsmen.net", "A", DEV, comment="yggdrasil")
        status, _, err = run(fake)
        self.assertEqual(status, 1)  # the hand-made ones on page one
        self.assertEqual(err.count("A git.techcraftsmen.net"), 3)
        self.assertFalse([w for w in fake.writes() if w[2]["name"] == "git.techcraftsmen.net"])
        self.assertTrue([c for c in fake.calls if "page=2" in c[1]])

    def test_given_a_second_owned_record_at_a_name_when_applied_then_it_is_reported_and_kept(self):
        fake = FakeCloudflare()
        fake.seed_right()
        fake.add("techcraftsmen.net", "git.techcraftsmen.net", "A", "198.51.100.1", comment="yggdrasil")
        status, _, err = run(fake)
        self.assertEqual(status, 1)
        self.assertIn("second record", err)
        self.assertEqual(len(fake.find("techcraftsmen.net", "git.techcraftsmen.net", "A")), 2)
        self.assertEqual(fake.writes(), [])


class DryRunTests(unittest.TestCase):
    def test_given_a_dry_run_when_applied_then_only_the_changes_are_printed(self):
        fake = FakeCloudflare()
        fake.seed_right()
        fake.find("techcraftsmen.net", "git.techcraftsmen.net", "A")[0]["content"] = "198.51.100.1"
        fake.records[fake.zones["artur-rios.tech"]] = [
            r for r in fake.records[fake.zones["artur-rios.tech"]] if r["name"] != "*.artur-rios.tech"]
        status, out, err = run(fake, ["--dry-run"] + ARGS)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(fake.writes(), [])
        lines = [line for line in out.splitlines() if line.startswith("would ")]
        self.assertEqual(len(lines), 2)
        self.assertIn("would update A git.techcraftsmen.net", out)
        self.assertIn(f"198.51.100.1 -> {DEV}", out)
        self.assertIn("would create A *.artur-rios.tech", out)
        self.assertNotIn("jenkins", out)  # a record already right is not mentioned
        self.assertNotIn("traefik-dev", out)

    def test_given_a_dry_run_and_a_record_by_hand_when_applied_then_it_is_still_reported(self):
        fake = FakeCloudflare()
        fake.add("artur-rios.tech", "artur-rios.tech", "A", "76.76.21.21", comment="")
        status, _, err = run(fake, ["--dry-run"] + ARGS)
        self.assertEqual(status, 1)
        self.assertIn("made by hand", err)
        self.assertEqual(fake.writes(), [])


class SkipCaaTests(unittest.TestCase):
    """--skip-caa <zone>: that zone's CAA is left out of the desired set, so nothing about it is created,
    updated or reported (artur-rios.tech while Vercel renews www. and blog. over HTTP-01)."""

    SKIP = ARGS + ["--skip-caa", "artur-rios.tech"]

    def test_given_a_skipped_zone_when_desired_then_only_the_other_zone_has_a_caa(self):
        wanted = wanted_by_key(skip_caa=("artur-rios.tech",))
        self.assertNotIn(("CAA", "artur-rios.tech"), wanted)
        self.assertIn(("CAA", "techcraftsmen.net"), wanted)
        self.assertIn(("A", "artur-rios.tech"), wanted)  # only the CAA goes

    def test_given_empty_zones_when_run_with_skip_caa_then_that_caa_is_not_created_and_the_skip_is_said(self):
        fake = FakeCloudflare()
        status, out, err = run(fake, self.SKIP)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(fake.find("artur-rios.tech", "artur-rios.tech", "CAA"), [])
        self.assertEqual(len(fake.find("techcraftsmen.net", "techcraftsmen.net", "CAA")), 1)
        self.assertIn("CAA skipped on artur-rios.tech (--skip-caa)", out.splitlines())
        self.assertIn("0 of 9 records already right", out)

    def test_given_a_caa_made_by_hand_on_a_skipped_zone_when_run_then_it_is_not_reported(self):
        fake = FakeCloudflare()
        fake.seed_right()
        fake.find("artur-rios.tech", "artur-rios.tech", "CAA")[0]["comment"] = ""  # made by hand
        status, out, err = run(fake, self.SKIP)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(fake.writes(), [])
        self.assertNotIn("made by hand", out + err)

    def test_given_an_owned_caa_with_other_data_on_a_skipped_zone_when_run_then_it_is_not_updated(self):
        fake = FakeCloudflare()
        fake.seed_right()
        fake.find("artur-rios.tech", "artur-rios.tech", "CAA")[0]["data"] = {"flags": 0, "tag": "issue",
                                                                           "value": "letsencrypt.org"}
        status, out, err = run(fake, self.SKIP)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(fake.writes(), [])
        self.assertNotIn("update CAA", out)

    def test_given_the_zone_twice_and_the_other_once_when_run_then_each_skip_is_said_once(self):
        fake = FakeCloudflare()
        status, out, _ = run(fake, ARGS + ["--skip-caa", "artur-rios.tech", "--skip-caa", "artur-rios.tech",
                                           "--skip-caa", "techcraftsmen.net"])
        self.assertEqual(status, 0)
        self.assertEqual(out.count("CAA skipped on artur-rios.tech (--skip-caa)"), 1)
        self.assertEqual(out.count("CAA skipped on techcraftsmen.net (--skip-caa)"), 1)
        self.assertFalse([c for c in fake.writes() if c[2] and c[2].get("type") == "CAA"])

    def test_given_a_dry_run_with_skip_caa_when_run_then_the_skip_is_said_and_nothing_is_sent(self):
        fake = FakeCloudflare()
        status, out, _ = run(fake, ["--dry-run"] + self.SKIP)
        self.assertEqual(status, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("CAA skipped on artur-rios.tech (--skip-caa)", out)
        self.assertNotIn("would create CAA artur-rios.tech", out)
        self.assertIn("would create CAA techcraftsmen.net", out)

    def test_given_a_zone_that_is_not_ours_when_skipped_then_it_exits_2(self):
        fake = FakeCloudflare()
        status, _, err = run(fake, ARGS + ["--skip-caa", "example.com"])
        self.assertEqual(status, 2)
        self.assertIn("--skip-caa", err)
        self.assertEqual(fake.calls, [])


class Unused:
    """An api that fails the test if it is used: the commands that must stop before any request."""

    def request(self, *args, **kwargs):
        raise AssertionError("a request was sent")


class CommandTests(unittest.TestCase):
    def test_given_no_token_when_run_then_it_exits_2(self):
        status, _, err = run(Unused(), ARGS, environ={})
        self.assertEqual(status, 2)
        self.assertIn("CF_DNS_API_TOKEN", err)

    def test_given_a_blank_token_when_run_then_it_exits_2(self):
        self.assertEqual(run(Unused(), ARGS, environ={"CF_DNS_API_TOKEN": "  "})[0], 2)

    def test_given_a_bad_address_when_run_then_it_exits_2(self):
        for argv in (["--apps", "not-an-ip", "--dev", DEV],
                     ["--apps", APPS6, "--dev", DEV],                       # IPv6 where IPv4 is wanted
                     ["--apps", APPS, "--apps6", APPS, "--dev", DEV],       # IPv4 where IPv6 is wanted
                     ["--apps", APPS, "--dev", DEV, "--dev6", "2001:db8::zz"]):
            fake = FakeCloudflare()
            self.assertEqual(run(fake, argv)[0], 2, argv)
            self.assertEqual(fake.calls, [])

    def test_given_a_missing_address_when_run_then_it_exits_2(self):
        self.assertEqual(run(FakeCloudflare(), ["--apps", APPS])[0], 2)

    def test_given_success_false_with_http_200_when_applied_then_it_is_an_error_and_exit_1(self):
        fake = FakeCloudflare(fail=("POST", "/client/v4/zones/zone-0/dns_records"))
        status, _, err = run(fake)
        self.assertEqual(status, 1)
        self.assertIn("9109: Invalid access token", err)
        self.assertNotIn(TOKEN, err)
        # the other zone was still done
        self.assertTrue(fake.find("artur-rios.tech", "artur-rios.tech", "A"))

    def test_given_a_zone_the_token_cannot_see_when_applied_then_it_is_reported_and_skipped(self):
        fake = FakeCloudflare()
        del fake.zones["artur-rios.tech"]
        status, _, err = run(fake)
        self.assertEqual(status, 1)
        self.assertIn("zone artur-rios.tech not found", err)
        self.assertTrue(fake.find("techcraftsmen.net", "techcraftsmen.net", "A"))

    def test_given_a_token_refused_on_the_zone_lookup_when_applied_then_each_zone_is_reported_once(self):
        fake = FakeCloudflare()
        fake.refuse_zones = True
        status, _, err = run(fake)
        self.assertEqual(status, 1)
        self.assertEqual(err.count("403 Forbidden"), 2)
        self.assertEqual(len(fake.calls), 2)  # one lookup per zone, then nothing more
        self.assertNotIn(TOKEN, err)

    def test_given_the_real_client_when_built_then_the_token_is_only_in_the_authorization_header(self):
        api = rules.Api(cloudflare_dns.API_BASE, TOKEN, "Bearer")
        self.assertEqual(api._headers["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, api.base)


if __name__ == "__main__":
    unittest.main()
