"""Tests for scripts/vars.py, the variables store.   python3 -m unittest discover -s scripts"""

import io
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import vars as v

SCRIPT = pathlib.Path(__file__).resolve().parent / "vars.py"


class StoreTestCase(unittest.TestCase):
    """A fresh secrets directory with an initialised store; the catalog is the repository's own."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="vars-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.env = dict(os.environ, YGG_SECRETS_DIR=str(self.dir), USER="tester")
        self.env.pop("SUDO_USER", None)
        v.init(self.dir, confirm=lambda prompt: "saved")

    def store(self, readonly=False):
        return v.Store.open(self.dir, readonly=readonly)

    def cli(self, *arguments, input=None):
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.env,
                              capture_output=True, text=True, input=input)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="vars-init-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_given_an_empty_directory_when_initialised_then_database_and_key_exist_with_mode_0640(self):
        v.init(self.dir, confirm=lambda prompt: "saved")
        for name in ("vars.db", "vars.key"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o640, name)

    def test_given_the_key_not_confirmed_when_initialised_then_nothing_is_left_behind(self):
        with self.assertRaises(v.VarsError):
            v.init(self.dir, confirm=lambda prompt: "no")
        self.assertFalse((self.dir / "vars.db").exists())
        self.assertFalse((self.dir / "vars.key").exists())

    def test_given_an_existing_store_when_initialised_again_then_it_is_refused(self):
        v.init(self.dir, confirm=lambda prompt: "saved")
        with self.assertRaises(v.VarsError):
            v.init(self.dir, confirm=lambda prompt: "saved")


class ScopeTests(unittest.TestCase):
    def test_given_each_spelling_when_parsed_then_the_stored_form(self):
        cat = {"environments": [{"id": "development"}], "systems": [{"applications": [{"id": "heimdall-api"}]}]}
        self.assertEqual(v.parse_scope("platform", cat), "platform")
        self.assertEqual(v.parse_scope("platform:acme", cat), "platform:acme")
        self.assertEqual(v.parse_scope("@development", cat), "env:development")
        self.assertEqual(v.parse_scope("heimdall-api", cat), "app:heimdall-api")
        self.assertEqual(v.parse_scope("heimdall-api@development", cat), "app:heimdall-api@development")

    def test_given_an_unknown_application_or_environment_when_parsed_then_it_is_refused(self):
        cat = {"environments": [{"id": "development"}], "systems": [{"applications": [{"id": "heimdall-api"}]}]}
        for text in ("nope", "@nope", "heimdall-api@nope", "nope@development", "", "a@b@c"):
            with self.assertRaises(v.VarsError, msg=text):
                v.parse_scope(text, cat)

    def test_given_a_stored_scope_when_shown_then_the_cli_spelling(self):
        for text in ("platform", "platform:acme", "@development", "heimdall-api", "heimdall-api@development"):
            self.assertEqual(v.show_scope(v.parse_scope(text, None)), text)


class ValueRuleTests(unittest.TestCase):
    def test_given_names_when_classified_then_secret_ones_are_detected(self):
        for name in ("DB_PASSWORD", "HEIMDALL_AUTH_TOKEN_SECRET", "HEIMDALL_AUTH_TOKEN_SECRET_PREVIOUS",
                     "MAILGUN_API_KEY", "CF_DNS_API_TOKEN", "FORTUNA_DATA_CONNECTIONSTRING"):
            self.assertTrue(v.is_secret_name(name), name)
        for name in ("DB_HOST", "PUBLIC_HOST", "KEYBOARD_LAYOUT", "ASPNETCORE_ENVIRONMENT"):
            self.assertFalse(v.is_secret_name(name), name)

    def test_given_values_when_masked_then_only_long_ones_show_their_last_two_characters(self):
        self.assertEqual(v.mask("abcdefghijkl"), "••••••kl")
        self.assertEqual(v.mask("short"), "••••••")

    def test_given_unsafe_values_or_keys_when_validated_then_they_are_refused(self):
        for value in ("it's", "a\nb", "a\rb", " lead", "trail "):
            with self.assertRaises(v.VarsError, msg=repr(value)):
                v.validate_value(value)
        for value in ("", "a b", "x#y", "a $b", "p=q", "$2y$05$abc"):
            v.validate_value(value)
        for key in ("1A", "A-B", "", "A B"):
            with self.assertRaises(v.VarsError, msg=key):
                v.validate_key(key)


class SetGetTests(StoreTestCase):
    def test_given_a_value_when_set_then_get_returns_it_and_the_file_does_not_contain_it(self):
        with self.store() as s:
            s.set("app:heimdall-api@development", "DB_PASSWORD", "hunter2-very-secret", None, "set")
            self.assertEqual(s.get("app:heimdall-api@development", "DB_PASSWORD"), ("hunter2-very-secret", True))
        self.assertNotIn(b"hunter2-very-secret", (self.dir / "vars.db").read_bytes())

    def test_given_a_secret_override_when_set_then_the_flag_follows_it(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "example.com", True, "set")
            s.set("platform", "GRAFANA_ADMIN_PASSWORD", "x" * 16, False, "set")
            self.assertEqual(s.get("platform", "DOMAIN"), ("example.com", True))
            self.assertEqual(s.get("platform", "GRAFANA_ADMIN_PASSWORD"), ("x" * 16, False))

    def test_given_a_key_when_unset_then_it_is_gone_and_a_second_unset_fails(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "example.com", None, "set")
            s.unset("platform", "DOMAIN", "unset")
            self.assertIsNone(s.get("platform", "DOMAIN"))
            with self.assertRaises(v.VarsError):
                s.unset("platform", "DOMAIN", "unset")

    def test_given_a_wrong_key_file_when_reading_then_it_fails_naming_the_key_file(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "example.com", None, "set")
        (self.dir / "vars.key").write_text(v.Fernet.generate_key().decode() + "\n")
        for readonly in (True, False):
            with self.assertRaises(v.VarsError) as caught:
                self.store(readonly=readonly)
            self.assertIn("vars.key", str(caught.exception))

    def test_given_a_wrong_key_file_when_setting_a_new_key_then_it_fails_naming_the_key_file(self):
        (self.dir / "vars.key").write_text(v.Fernet.generate_key().decode() + "\n")
        with self.assertRaises(v.VarsError) as caught:
            with self.store() as s:
                s.set("platform", "NEW", "1", None, "set")
        self.assertIn("vars.key", str(caught.exception))

    def test_given_no_key_file_when_opening_then_it_fails_naming_the_key_file(self):
        (self.dir / "vars.key").unlink()
        with self.assertRaises(v.VarsError) as caught:
            self.store()
        self.assertIn("vars.key", str(caught.exception))


class CliBasicsTests(StoreTestCase):
    def test_given_set_then_get_and_list_mask_secrets_unless_revealed(self):
        self.assertEqual(self.cli("set", "platform", "DOMAIN=example.com", "GRAFANA_ADMIN_PASSWORD=abcdefghijkl").returncode, 0)
        self.assertEqual(self.cli("get", "platform", "DOMAIN").stdout, "example.com\n")
        self.assertEqual(self.cli("get", "platform", "GRAFANA_ADMIN_PASSWORD").stdout, "••••••kl\n")
        self.assertEqual(self.cli("get", "platform", "GRAFANA_ADMIN_PASSWORD", "--reveal").stdout, "abcdefghijkl\n")
        listed = self.cli("list", "platform").stdout
        self.assertIn("DOMAIN=example.com", listed)
        self.assertIn("GRAFANA_ADMIN_PASSWORD=••••••kl", listed)

    def test_given_a_dash_value_when_set_then_it_is_read_from_the_prompt(self):
        result = self.cli("set", "platform", "GRAFANA_ADMIN_PASSWORD=-", input="from-stdin-value\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli("get", "platform", "GRAFANA_ADMIN_PASSWORD", "--reveal").stdout, "from-stdin-value\n")

    def test_given_an_absent_key_when_get_then_exit_1(self):
        result = self.cli("get", "platform", "NOPE")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vars:", result.stderr)

    def test_given_a_wrong_key_when_set_then_exit_1_naming_the_key_file(self):
        (self.dir / "vars.key").write_text(v.Fernet.generate_key().decode() + "\n")
        result = self.cli("set", "platform", "NEW=1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vars:", result.stderr)
        self.assertIn("vars.key", result.stderr)

    def test_given_list_keys_then_only_names(self):
        self.cli("set", "platform", "A=1", "B=2")
        self.assertEqual(self.cli("list", "platform", "--keys").stdout, "A\nB\n")
