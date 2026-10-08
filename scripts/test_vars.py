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
import unittest.mock
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
        patcher = unittest.mock.patch.dict(os.environ, {"USER": "tester"})  # for in-process stores
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SUDO_USER", None)
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


class ResolveTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        with self.store() as s:
            s.set("env:development", "DB_HOST", "host.docker.internal", None, "set")
            s.set("env:development", "LOCALE", "pt-BR", None, "set")
            s.set("app:heimdall-api", "LOCALE", "en-US", None, "set")
            s.set("app:heimdall-api@development", "HEIMDALL_AUTH_TOKEN_SECRET", "s" * 40, None, "set")
            s.set("app:heimdall-api@development", "LOCALE", "es-ES", None, "set")
            s.set("app:fortuna-api@development", "FORTUNA_AUTH_TOKEN_SECRET",
                  "${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}", None, "set")

    def test_given_three_layers_when_resolved_then_the_most_specific_wins(self):
        with self.store(readonly=True) as s:
            resolved = s.resolve("heimdall-api", "development")
        self.assertEqual(resolved["LOCALE"][:2], ("es-ES", "app@env"))
        self.assertEqual(resolved["DB_HOST"][:2], ("host.docker.internal", "env"))
        with self.store(readonly=True) as s:
            self.assertEqual(s.resolve("fortuna-api", "homologation"), {})

    def test_given_an_application_layer_only_when_resolved_then_it_beats_the_environment(self):
        with self.store() as s:
            s.unset("app:heimdall-api@development", "LOCALE", "unset")
            self.assertEqual(s.resolve("heimdall-api", "development")["LOCALE"][:2], ("en-US", "app"))

    def test_given_a_reference_when_resolved_then_the_target_value_in_the_same_environment(self):
        with self.store(readonly=True) as s:
            value, origin, secret = s.resolve("fortuna-api", "development")["FORTUNA_AUTH_TOKEN_SECRET"]
        self.assertEqual(value, "s" * 40)
        self.assertEqual(origin, "ref → heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET")
        self.assertTrue(secret)

    def test_given_a_reference_to_a_missing_key_when_resolved_then_it_fails_naming_it(self):
        with self.store() as s:
            s.set("app:fortuna-api@development", "X", "${ref:heimdall-api:NOPE}", None, "set")
            with self.assertRaises(v.VarsError) as caught:
                s.resolve("fortuna-api", "development")
        self.assertIn("heimdall-api:NOPE", str(caught.exception))

    def test_given_a_reference_to_a_reference_when_resolved_then_it_fails(self):
        with self.store() as s:
            s.set("app:heimdall-api@development", "Y", "${ref:fortuna-api:FORTUNA_AUTH_TOKEN_SECRET}", None, "set")
            with self.assertRaises(v.VarsError) as caught:
                s.resolve("heimdall-api", "development")
        self.assertIn("reference to a reference", str(caught.exception))

    def test_given_a_reference_in_a_platform_or_environment_scope_when_set_then_it_is_refused(self):
        with self.store() as s:
            for scope in ("platform", "env:development"):
                with self.assertRaises(v.VarsError, msg=scope):
                    s.set(scope, "X", "${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}", None, "set")


class RenderTests(StoreTestCase):
    def test_given_values_with_shell_and_compose_characters_when_rendered_then_single_quoted_sorted_lines(self):
        self.assertEqual(v.render_lines({"B": "a $b #c", "A": "$2y$05$x=y"}), "A='$2y$05$x=y'\nB='a $b #c'\n")

    def test_given_render_when_run_then_resolved_lines_on_stdout(self):
        self.cli("set", "@development", "DB_HOST=host.docker.internal")
        self.cli("set", "heimdall-api@development", "PUBLIC_HOST=heimdall-api-dev.example.com")
        result = self.cli("render", "heimdall-api", "development")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "DB_HOST='host.docker.internal'\nPUBLIC_HOST='heimdall-api-dev.example.com'\n")

    def test_given_render_platform_when_run_then_platform_or_acme_lines(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        self.cli("set", "platform:acme", "CF_DNS_API_TOKEN=abc")
        self.assertEqual(self.cli("render-platform").stdout, "DOMAIN='example.com'\n")
        self.assertEqual(self.cli("render-platform", "--acme").stdout, "CF_DNS_API_TOKEN='abc'\n")

    def test_given_list_resolved_then_each_key_with_its_origin_and_secrets_masked(self):
        self.cli("set", "@development", "DB_HOST=host.docker.internal")
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl")
        listed = self.cli("list", "heimdall-api@development", "--resolved").stdout
        self.assertIn("DB_HOST=host.docker.internal  (env)", listed)
        self.assertIn("DB_PASSWORD=••••••kl  (app@env)", listed)

    def test_given_export_then_revealed_env_file_text(self):
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl")
        self.assertEqual(self.cli("export", "heimdall-api@development").stdout, "DB_PASSWORD='abcdefghijkl'\n")

    @unittest.skipUnless(shutil.which("docker"), "needs the docker CLI")
    def test_given_a_rendered_file_when_compose_reads_it_then_every_value_is_verbatim(self):
        values = {"A": "$2y$05$abc", "B": "x #not a comment", "C": "p=q", "D": "${NOT_INTERPOLATED}"}
        env_file = self.dir / "rendered.env"
        env_file.write_text(v.render_lines(values))
        compose = self.dir / "compose.yml"
        compose.write_text("services:\n  s:\n    image: busybox\n    env_file: [rendered.env]\n")
        out = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--format", "json"],
                             capture_output=True, text=True, cwd=self.dir)
        if out.returncode != 0:
            self.skipTest(out.stderr)
        import json
        # `config` prints the model re-escaped for interpolation: a literal $ comes back as $$.
        shown = {key: value.replace("$$", "$") for key, value in json.loads(out.stdout)["services"]["s"]["environment"].items()}
        self.assertEqual(shown, values)


class HistoryTests(StoreTestCase):
    def test_given_changes_when_listed_then_newest_first_with_actor_and_values(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            s.set("platform", "DOMAIN", "b.example.com", None, "set")
            s.unset("platform", "DOMAIN", "unset")
            rows = s.history("platform", "DOMAIN", 10)
        self.assertEqual([(r["old"], r["new"], r["command"]) for r in rows],
                         [("b.example.com", None, "unset"), ("a.example.com", "b.example.com", "set"),
                          (None, "a.example.com", "set")])
        self.assertEqual(rows[0]["actor"], "tester")

    def test_given_a_change_when_rolled_back_then_the_value_before_it_returns_and_it_is_recorded(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            s.set("platform", "DOMAIN", "b.example.com", None, "set")
            change = s.history("platform", "DOMAIN", 1)[0]["id"]
            s.rollback(change, force=False)
            self.assertEqual(s.get("platform", "DOMAIN")[0], "a.example.com")
            self.assertEqual(s.history("platform", "DOMAIN", 1)[0]["command"], f"rollback {change}")

    def test_given_a_creation_when_rolled_back_then_the_key_is_deleted(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            s.rollback(s.history("platform", "DOMAIN", 1)[0]["id"], force=False)
            self.assertIsNone(s.get("platform", "DOMAIN"))

    def test_given_a_later_change_when_rolling_back_an_earlier_one_then_it_needs_force(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "a.example.com", None, "set")
            first = s.history("platform", "DOMAIN", 1)[0]["id"]
            s.set("platform", "DOMAIN", "b.example.com", None, "set")
            with self.assertRaises(v.VarsError):
                s.rollback(first, force=False)
            s.rollback(first, force=True)
            self.assertIsNone(s.get("platform", "DOMAIN"))

    def test_given_history_on_the_cli_then_secret_values_are_masked_unless_revealed(self):
        self.cli("set", "platform", "GRAFANA_ADMIN_PASSWORD=abcdefghijkl")
        self.assertIn("••••••kl", self.cli("history").stdout)
        self.assertNotIn("abcdefghijkl", self.cli("history").stdout)
        self.assertIn("abcdefghijkl", self.cli("history", "--reveal").stdout)

    def test_given_sudo_when_changing_then_the_sudo_user_is_the_actor(self):
        self.env["SUDO_USER"] = "admin-person"
        self.cli("set", "platform", "DOMAIN=example.com")
        self.assertIn("admin-person", self.cli("history").stdout)


class EnvTextTests(unittest.TestCase):
    def test_given_env_file_text_when_parsed_then_compose_semantics(self):
        text = ("# comment\n\nexport A=plain\nB='lit $x #y'\nC=\"q \\\"x\\\" \\\\\"\n"
                "D=value # trailing comment\nE=\n")
        self.assertEqual(v.parse_env_text(text),
                         {"A": "plain", "B": "lit $x #y", "C": 'q "x" \\', "D": "value", "E": ""})

    def test_given_dollar_dollar_unquoted_or_double_quoted_when_parsed_then_one_dollar_as_compose_reads_it(self):
        self.assertEqual(v.parse_env_text('A=ab$$cd\nB="x$$y $$$$"\nC=\'ab$$cd\'\n'),
                         {"A": "ab$cd", "B": "x$y $$", "C": "ab$$cd"})

    def test_given_a_single_quoted_dollar_when_parsed_then_it_stays_literal(self):
        self.assertEqual(v.parse_env_text("A='$X ${Y} $'\n"), {"A": "$X ${Y} $"})

    def test_given_interpolation_unquoted_or_double_quoted_when_parsed_then_it_fails_naming_line_and_key(self):
        for text in ("A=1\nB=ab$X\n", "A=1\nB=${X}\n", 'A=1\nB="${X:-d}"\n', "A=1\nB=a$$$X\n", "A=1\nB=a$\n"):
            with self.assertRaises(v.VarsError, msg=text) as caught:
                v.parse_env_text(text)
            self.assertIn("line 2", str(caught.exception), text)
            self.assertIn("B", str(caught.exception), text)

    def test_given_a_line_without_equals_when_parsed_then_it_fails_naming_the_line(self):
        with self.assertRaises(v.VarsError) as caught:
            v.parse_env_text("A=1\nnot a variable\n")
        self.assertIn("line 2", str(caught.exception))


class EditTests(StoreTestCase):
    def editor(self, sed_script):
        script = self.dir / "editor.sh"
        script.write_text(f"#!/bin/sh\nsed -i '{sed_script}' \"$1\"\n")
        script.chmod(0o755)
        self.env["EDITOR"] = str(script)

    def test_given_masked_secrets_left_untouched_when_edited_then_they_keep_their_values(self):
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl", "DB_HOST=old")
        self.editor("s/^DB_HOST=.*/DB_HOST=new/")
        result = self.cli("edit", "heimdall-api@development")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli("get", "heimdall-api@development", "DB_PASSWORD", "--reveal").stdout, "abcdefghijkl\n")
        self.assertEqual(self.cli("get", "heimdall-api@development", "DB_HOST").stdout, "new\n")

    def test_given_a_line_deleted_when_edited_then_the_key_is_unset(self):
        self.cli("set", "heimdall-api@development", "DB_PASSWORD=abcdefghijkl", "DB_HOST=old")
        self.editor("/^DB_PASSWORD=/d")
        self.cli("edit", "heimdall-api@development")
        self.assertEqual(self.cli("get", "heimdall-api@development", "DB_PASSWORD").returncode, 1)
        self.assertIn("edit", self.cli("history").stdout)

    def test_given_values_with_hash_and_quote_like_text_when_edited_untouched_then_they_are_unchanged(self):
        self.cli("set", "heimdall-api@development", "A=x #y", 'B="q"')
        self.editor("s/^$//")
        self.cli("edit", "heimdall-api@development")
        self.assertEqual(self.cli("get", "heimdall-api@development", "A").stdout, "x #y\n")
        self.assertEqual(self.cli("get", "heimdall-api@development", "B").stdout, '"q"\n')

    def test_given_a_bad_value_in_the_middle_when_applied_then_nothing_changes_and_nothing_is_recorded(self):
        with self.store() as s:
            s.set("platform", "A", "1", None, "set")
            s.set("platform", "C", "3", None, "set")
            before = s.history(None, None, 100)
            previous = {"A": ("1", False), "C": ("3", False)}
            with self.assertRaises(v.VarsError) as caught:
                s.apply("platform", "A=10\nB=it's\nC=30\n", previous, "edit")
            self.assertIn("B", str(caught.exception))
            with self.assertRaises(v.VarsError):
                s.apply("platform", "A=10\nB=${ref:heimdall-api:X}\n", previous, "edit")
            self.assertEqual(s.items("platform"), [("A", "1", False), ("C", "3", False)])
            self.assertEqual(s.history(None, None, 100), before)

    def test_given_an_invalid_edit_when_saved_then_the_scope_is_unchanged_and_the_text_is_kept(self):
        self.cli("set", "platform", "A=1")
        self.editor("s/^A=.*/A=10\\nB=it\\x27s/")
        result = self.cli("edit", "platform")
        self.assertEqual(result.returncode, 1)
        saved = pathlib.Path(result.stderr.split("your text is saved in ")[1].strip())
        self.addCleanup(saved.unlink)
        self.assertIn("B=it's", saved.read_text())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cli("get", "platform", "A").stdout, "1\n")


class ImportTests(StoreTestCase):
    def tree(self):
        (self.dir / "platform.env").write_text("ENVIRONMENTS=development,homologation\nDOMAIN=example.com\n")
        (self.dir / "acme.env").write_text("CF_DNS_API_TOKEN=abc\n")
        for env in ("development", "homologation"):
            (self.dir / env).mkdir()
            (self.dir / env / "heimdall-api.env").write_text(
                f"DB_HOST=host.docker.internal\nLOCALE=pt-BR\nDB_NAME=heimdall_{env}\n")
            (self.dir / env / "fortuna-api.env").write_text(f"DB_HOST=host.docker.internal\nFORTUNA_X={env}\n")

    def test_given_a_file_when_imported_then_existing_keys_are_kept_unless_replace(self):
        with self.store() as s:
            s.set("platform", "DOMAIN", "kept.example.com", None, "set")
            set_keys, kept = s.import_text("platform", "DOMAIN=new.example.com\nA=1\n", False, "import")
            self.assertEqual((set_keys, kept), (["A"], ["DOMAIN"]))
            self.assertEqual(s.get("platform", "DOMAIN")[0], "kept.example.com")
            s.import_text("platform", "DOMAIN=new.example.com\n", True, "import")
            self.assertEqual(s.get("platform", "DOMAIN")[0], "new.example.com")

    def test_given_a_secrets_tree_when_imported_all_then_every_file_lands_and_is_renamed(self):
        self.tree()
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "no")
            self.assertEqual(s.get("platform", "DOMAIN")[0], "example.com")
            self.assertEqual(s.get("platform:acme", "CF_DNS_API_TOKEN")[0], "abc")
            self.assertEqual(s.get("app:heimdall-api@homologation", "DB_NAME")[0], "heimdall_homologation")
        self.assertTrue((self.dir / "platform.env.imported").exists())
        self.assertTrue((self.dir / "development" / "heimdall-api.env.imported").exists())
        self.assertFalse((self.dir / "development" / "heimdall-api.env").exists())

    def test_given_move_up_yes_when_imported_all_then_shared_values_move_to_the_wider_layer(self):
        self.tree()
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "yes")
            # identical in every environment of heimdall-api -> application layer
            self.assertEqual(s.get("app:heimdall-api", "LOCALE")[0], "pt-BR")
            self.assertIsNone(s.get("app:heimdall-api@development", "LOCALE"))
            # identical in every application of an environment -> environment layer
            self.assertEqual(s.get("env:development", "DB_HOST")[0], "host.docker.internal")
            self.assertIsNone(s.get("app:fortuna-api@development", "DB_HOST"))
            # what resolves is unchanged
            self.assertEqual(s.resolve("heimdall-api", "development")["DB_NAME"][0], "heimdall_development")
            self.assertEqual(s.resolve("fortuna-api", "homologation")["DB_HOST"][0], "host.docker.internal")

    def test_given_import_all_run_twice_then_the_second_run_imports_nothing_and_renames_nothing(self):
        self.tree()
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "no")
            before = len(s.history(None, None, 10**6))
            report = v.import_all(s, self.dir, v.load_catalog(), "no")
            self.assertEqual(len(s.history(None, None, 10**6)), before)
        self.assertIn("nothing to import", " ".join(report))

    def test_given_dollars_in_the_secrets_tree_when_imported_all_then_values_are_what_compose_read(self):
        self.tree()
        (self.dir / "platform.env").write_text("ENVIRONMENTS=development\nHASH=$$2y$$05$$abc\nQ='$$kept'\n")
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "no")
            self.assertEqual(s.get("platform", "HASH")[0], "$2y$05$abc")
            self.assertEqual(s.get("platform", "Q")[0], "$$kept")
            rendered = v.render_lines({k: val for k, val, _ in s.items("platform")})
        self.assertIn("HASH='$2y$05$abc'", rendered)

    def test_given_interpolation_in_the_secrets_tree_when_imported_all_then_nothing_is_imported_or_renamed(self):
        self.tree()
        (self.dir / "development" / "heimdall-api.env").write_text("DB_PASSWORD=ab$OTHER\n")
        with self.store() as s:
            with self.assertRaises(v.VarsError) as caught:
                v.import_all(s, self.dir, v.load_catalog(), "no")
            self.assertEqual(s.history(None, None, 10), [])
        self.assertIn("heimdall-api.env: line 1: DB_PASSWORD", str(caught.exception))
        self.assertEqual(list(self.dir.rglob("*.imported")), [])

    def test_given_an_env_file_of_an_unknown_application_when_imported_all_then_it_is_left_alone(self):
        self.tree()
        (self.dir / "development" / "unknown-app.env").write_text("A=1\n")
        with self.store() as s:
            report = v.import_all(s, self.dir, v.load_catalog(), "no")
        self.assertTrue((self.dir / "development" / "unknown-app.env").exists())
        self.assertIn("unknown-app", " ".join(report))


class MoveUpScopeTests(StoreTestCase):
    tree = ImportTests.tree

    def test_given_a_value_set_earlier_when_imported_all_then_untouched_scopes_are_not_moved(self):
        self.tree()
        with self.store() as s:
            for env in ("local", "production"):
                s.set(f"app:heimdall-api@{env}", "KEEP", "same", None, "set")
            v.import_all(s, self.dir, v.load_catalog(), "yes")
            self.assertEqual(s.get("app:heimdall-api@local", "KEEP")[0], "same")
            self.assertIsNone(s.get("app:heimdall-api", "KEEP"))

    def test_given_a_secret_flag_when_moved_up_then_the_flag_survives(self):
        self.tree()
        with self.store() as s:
            v.import_all(s, self.dir, v.load_catalog(), "no")
            s.set("app:heimdall-api@development", "LOCALE", "pt-BR", True, "set")
            (self.dir / "development" / "heimdall-api.env").write_text("LOCALE=pt-BR\n")
            (self.dir / "homologation" / "heimdall-api.env").write_text("LOCALE=pt-BR\n")
            v.import_all(s, self.dir, v.load_catalog(), "yes")
            self.assertEqual(s.get("app:heimdall-api", "LOCALE"), ("pt-BR", True))

    def test_given_an_app_layer_value_when_moving_to_the_environment_then_the_key_is_skipped(self):
        self.tree()
        with self.store() as s:
            s.set("app:heimdall-api", "DB_HOST", "other", None, "set")
            v.import_all(s, self.dir, v.load_catalog(), "yes")
            self.assertEqual(s.resolve("heimdall-api", "development")["DB_HOST"][0], "host.docker.internal")
            self.assertIsNone(s.get("env:development", "DB_HOST"))

    def test_given_a_move_when_it_fails_midway_then_nothing_is_half_moved(self):
        with self.store() as s:
            s.set("app:heimdall-api@development", "A", "1", None, "set")
            s.set("app:fortuna-api@development", "A", "1", None, "set")
            with self.assertRaises(v.VarsError):
                s.move_up("A", "has a\nnewline", False, ["app:heimdall-api@development"], "env:development", "x")
            self.assertEqual(s.get("app:heimdall-api@development", "A")[0], "1")


class BackupCheckTests(StoreTestCase):
    def test_given_a_backup_when_restored_then_values_read_with_its_key(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        target = self.dir / "backups"
        result = self.cli("backup", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        database = next(target.glob("vars-*.db"))
        key = next(target.glob("vars-*.key"))
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        restored = self.dir / "restored"
        restored.mkdir()
        shutil.copy(database, restored / "vars.db")
        shutil.copy(key, restored / "vars.key")
        with v.Store.open(restored, readonly=True) as s:
            self.assertEqual(s.get("platform", "DOMAIN")[0], "example.com")

    def test_given_a_healthy_store_when_checked_then_exit_0(self):
        self.cli("set", "heimdall-api@development", "A=1")
        result = self.cli("check")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_given_a_broken_reference_when_checked_then_exit_1_naming_it(self):
        self.cli("set", "fortuna-api@development", "X=${ref:heimdall-api:NOPE}")
        result = self.cli("check", "fortuna-api", "development")
        self.assertEqual(result.returncode, 1)
        self.assertIn("heimdall-api:NOPE", result.stdout + result.stderr)

    def test_given_an_undecryptable_value_when_checked_then_exit_1(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        conn = sqlite3.connect(self.dir / "vars.db")
        conn.execute("UPDATE variables SET value=? WHERE key='DOMAIN'", (b"garbage",))
        conn.commit()
        conn.close()
        result = self.cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("DOMAIN", result.stdout + result.stderr)

    def test_given_a_corrupted_file_when_checked_then_exit_1(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        data = bytearray((self.dir / "vars.db").read_bytes())
        data[100:4096] = b"\xff" * (4096 - 100)
        (self.dir / "vars.db").write_bytes(bytes(data))
        self.assertEqual(self.cli("check").returncode, 1)

    def test_given_a_value_of_an_unknown_application_when_checked_then_a_warning_not_an_error(self):
        conn = sqlite3.connect(self.dir / "vars.db")
        with self.store() as s:
            s.set("app:gone-app@development", "A", "1", None, "set")
        conn.close()
        result = self.cli("check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("gone-app", result.stdout + result.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_given_a_read_only_directory_when_reading_then_it_works_and_writes_nothing(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        os.chmod(self.dir, 0o555)
        self.addCleanup(os.chmod, self.dir, 0o755)
        self.assertEqual(self.cli("get", "platform", "DOMAIN").stdout, "example.com\n")
        self.assertEqual(self.cli("render-platform").returncode, 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith("vars.db")), ["vars.db"])

    def test_given_a_writer_holding_a_transaction_when_reading_read_only_then_the_reader_waits_and_succeeds(self):
        self.cli("set", "platform", "DOMAIN=example.com")
        writer = sqlite3.connect(self.dir / "vars.db")
        writer.execute("BEGIN IMMEDIATE")
        try:
            result = self.cli("get", "platform", "DOMAIN")
        finally:
            writer.rollback()
            writer.close()
        self.assertEqual(result.stdout, "example.com\n")


class CheckScopeTests(StoreTestCase):
    """A full check is strict for what this machine runs, a warning for the catalog's other pairs."""

    def setUp(self):
        super().setUp()
        self.cli("set", "platform", "ENVIRONMENTS=development")
        self.cli("set", "heimdall-api@development", "HEIMDALL_AUTH_TOKEN_SECRET=" + "s" * 40)
        # The application layer applies in every environment, local included, where nothing is set.
        self.cli("set", "fortuna-api", "FORTUNA_AUTH_TOKEN_SECRET=${ref:heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET}")

    def test_given_an_app_layer_reference_unresolved_in_an_unused_environment_when_checked_then_a_warning(self):
        result = self.cli("check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("warning: fortuna-api@local FORTUNA_AUTH_TOKEN_SECRET -> heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET",
                      result.stdout)

    def test_given_a_host_environment_where_the_reference_fails_when_checked_then_an_error(self):
        self.cli("set", "platform", "ENVIRONMENTS=development,production")
        result = self.cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: fortuna-api@production", result.stdout)

    def test_given_a_configured_pair_outside_the_host_environments_when_checked_then_an_error(self):
        self.cli("set", "heimdall-api@homologation", "X=${ref:fortuna-api:NOPE}")
        result = self.cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: heimdall-api@homologation X -> fortuna-api:NOPE", result.stdout)

    def test_given_a_deploy_check_of_an_unconfigured_pair_then_it_stays_strict(self):
        result = self.cli("check", "fortuna-api", "local")
        self.assertEqual(result.returncode, 1)
        self.assertIn("heimdall-api:HEIMDALL_AUTH_TOKEN_SECRET", result.stdout)

    def test_given_a_broken_application_value_when_checking_the_platform_then_it_passes(self):
        self.cli("set", "heimdall-api@homologation", "X=${ref:fortuna-api:NOPE}")
        self.cli("set", "platform:acme", "CF_DNS_API_TOKEN=abc")
        result = self.cli("check", "--platform")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_given_an_undecryptable_platform_value_when_checking_the_platform_then_it_fails(self):
        self.cli("set", "platform:acme", "CF_DNS_API_TOKEN=abc")
        self.garble("CF_DNS_API_TOKEN")
        result = self.cli("check", "--platform")
        self.assertEqual(result.returncode, 1)
        self.assertIn("CF_DNS_API_TOKEN", result.stdout)

    def test_given_an_undecryptable_application_value_when_checking_usable_then_it_passes(self):
        self.garble("HEIMDALL_AUTH_TOKEN_SECRET")
        self.assertEqual(self.cli("check", "--usable").returncode, 0)
        self.assertEqual(self.cli("check").returncode, 1)

    def test_given_a_wrong_key_when_checking_usable_then_it_fails_naming_the_key(self):
        (self.dir / "vars.key").write_text(v.Fernet.generate_key().decode() + "\n")
        result = self.cli("check", "--usable")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vars.key", result.stderr)

    def test_given_platform_or_usable_with_an_application_when_checked_then_a_usage_error(self):
        for flag in ("--platform", "--usable"):
            self.assertNotEqual(self.cli("check", flag, "fortuna-api", "local").returncode, 0, flag)
        self.assertNotEqual(self.cli("check", "--platform", "--usable").returncode, 0)

    def garble(self, key):
        conn = sqlite3.connect(self.dir / "vars.db")
        conn.execute("UPDATE variables SET value=? WHERE key=?", (b"garbage", key))
        conn.commit()
        conn.close()


class BackupUmaskTests(StoreTestCase):
    def test_given_a_backup_when_done_then_the_umask_is_restored_and_files_are_private(self):
        old = os.umask(0o022)
        try:
            database, key = v.backup(self.dir, self.dir / "b")
            self.assertEqual(os.umask(0o022), 0o022)
        finally:
            os.umask(old)
        self.assertEqual((database.stat().st_mode & 0o777, key.stat().st_mode & 0o777), (0o600, 0o600))
