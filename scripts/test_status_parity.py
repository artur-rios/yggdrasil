"""The status API must report the address scripts/catalog.py routes.   python3 -m unittest discover -s scripts

An application's public address is worked out twice: scripts/catalog.py (public-host and public-path:
what deploy.sh hands the stack file, so what Traefik answers to) and the status API (what the console
links to). If they drift, the console shows a link that does not open. Neither implementation can run
the other -- the status API is built and tested in a .NET SDK container, which has no Python -- so both
read one fixture catalog and must produce one list of addresses, kept beside it:

  status/tests/Yggdrasil.Status.Tests/fixtures/public-addresses.yaml          the catalog
  status/tests/Yggdrasil.Status.Tests/fixtures/public-addresses.expected.txt  application, environment, address

This test checks catalog.py against that list; PublicAddressTests (status/tests) checks the status API
against the same list. The list is the only thing they share, so a change to either rule is a change
to the list, and fails the other side until it follows.
"""

import contextlib
import io
import pathlib
import unittest
from unittest import mock

import catalog as c

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "status" / "tests" / "Yggdrasil.Status.Tests" / "fixtures"
DOMAIN = "example.com"


def expected_addresses():
    """(application, environment, address or None) for every line of the list."""
    rows = []
    for line in (FIXTURES / "public-addresses.expected.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            application, environment, address = line.split()
            rows.append((application, environment, None if address == "-" else address))
    return rows


def cli(value, *argv):
    """What `catalog.py <argv>` prints for `value`: the way deploy.sh asks, not the functions behind it."""
    out = io.StringIO()
    out.reconfigure = lambda **_: None  # main() asks stdout for plain LF line endings
    with mock.patch.object(c, "load", return_value=value), contextlib.redirect_stdout(out):
        status = c.main(list(argv))
    assert status == 0, argv
    return out.getvalue().strip()


class StatusParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = c.load(FIXTURES / "public-addresses.yaml")

    def test_given_the_fixture_catalog_when_validated_then_it_is_valid(self):
        self.assertEqual(c.validate(self.catalog), [])

    def test_given_the_fixture_when_catalog_py_forms_each_address_then_it_is_the_one_the_status_api_must_report(self):
        for application, environment, expected in expected_addresses():
            with self.subTest(application=application, environment=environment):
                host = cli(self.catalog, "public-host", application, environment, DOMAIN)
                path = cli(self.catalog, "public-path", application)
                # Nothing without a host: a path alone is not an address. Exactly the status API's rule.
                self.assertEqual(f"https://{host}{path}" if host else None, expected)

    def test_given_the_list_when_read_then_it_covers_every_application_of_the_fixture(self):
        # An application added to the fixture without a line in the list would be compared with nothing.
        listed = {application for application, _, _ in expected_addresses()}
        self.assertEqual(listed, {app["id"] for system in self.catalog["systems"] for app in system["applications"]})


if __name__ == "__main__":
    unittest.main()
