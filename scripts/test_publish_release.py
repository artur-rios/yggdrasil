"""Tests for scripts/publish-release.sh, against a fake forge.sh and github.sh.   python3 -m unittest discover -s scripts

The fake forge.sh (YGG_FORGE_SH) is a small git server that keeps its tags, releases and release files in a
state directory, one subdirectory per server type, so a test can look at what each server holds and run the
script again on what a failed run left. It tells the servers apart the way the real one does, by FORGE_TYPE
in the environment. Every call is appended to calls.log as "<type> <command> <arguments>".

The fake github.sh (YGG_GITHUB_SH) stands in for the push mirror: `wait-ref` copies the Forgejo tag to GitHub
when there is one (the mirror pushes it), and exits 2, as the real one does on timeout, when there is not.

Failures are injected with FAKE_FAIL="<type> <command>" (that call exits 1 with a message), and a file that
comes back short from an upload with FAKE_SHORT_UPLOAD="<file name>".
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "publish-release.sh"

FAKE_FORGE = textwrap.dedent(r"""
    #!/usr/bin/env bash
    set -euo pipefail
    type=${FORGE_TYPE:?}
    dir=$FAKE_STATE/$type
    mkdir -p "$dir/tags" "$dir/releases"
    echo "$type $*" >>"$FAKE_STATE/calls.log"
    command=$1
    shift
    if [[ "${FAKE_FAIL:-}" == "$type $command" ]]; then
      echo "forge: $command failed: the fake server said no" >&2
      exit 1
    fi
    case "$command" in
      tag-sha)
        [[ -f "$dir/tags/$2" ]] || { echo "forge: no tag" >&2; exit 4; }
        cat "$dir/tags/$2"
        ;;
      create-tag)
        [[ ! -f "$dir/tags/$2" ]] || { echo "forge: create-tag failed: exists" >&2; exit 1; }
        echo "$3" >"$dir/tags/$2"
        ;;
      find-release)
        for release in "$dir"/releases/*/; do
          [[ -d "$release" && "$(cat "$release/tag")" == "$2" ]] || continue
          printf '%s\thttps://%s.example/%s/releases/%s\t%s\n' "$(basename "$release")" "$type" "$1" "$2" "$(cat "$release/draft")"
          exit 0
        done
        echo "forge: no release for $2" >&2
        exit 4
        ;;
      create-release)
        repo=$1 tag=$2 sha=$3 title=$4
        shift 4
        draft=false notes=""
        while (($#)); do
          case "$1" in --draft) draft=true ;; --notes-file) notes=$2; shift ;; esac
          shift
        done
        id=$(($(ls "$dir/releases" | wc -l) + 1))
        mkdir -p "$dir/releases/$id/assets"
        echo "$tag" >"$dir/releases/$id/tag"
        echo "$draft" >"$dir/releases/$id/draft"
        echo "$sha" >"$dir/releases/$id/sha"
        echo "$title" >"$dir/releases/$id/title"
        cp "$notes" "$dir/releases/$id/notes"
        printf '%s\thttps://%s.example/%s/releases/%s\n' "$id" "$type" "$repo" "$tag"
        ;;
      release-files)
        for file in "$dir/releases/$2/assets"/*; do
          [[ -f "$file" ]] || continue
          printf '%s\t%s\n' "$(basename "$file")" "$(cat "$file")"
        done
        ;;
      upload-release-file)
        name=$(basename "$3")
        size=$(wc -c <"$3" | tr -d ' ')
        [[ "${FAKE_SHORT_UPLOAD:-}" != "$name" ]] || size=$((size - 1))
        echo "$size" >"$dir/releases/$2/assets/$name"
        echo "https://$type.example/download/$name"
        ;;
      delete-release-file)
        [[ -f "$dir/releases/$2/assets/$3" ]] || { echo "forge: release $2 has no file named $3" >&2; exit 4; }
        rm "$dir/releases/$2/assets/$3"
        ;;
      publish-release)
        echo false >"$dir/releases/$2/draft"
        printf 'https://%s.example/%s/releases/%s\n' "$type" "$1" "$(cat "$dir/releases/$2/tag")"
        ;;
      *) echo "fake forge: unexpected $command" >&2; exit 2 ;;
    esac
""").lstrip()

FAKE_GITHUB = textwrap.dedent(r"""
    #!/usr/bin/env bash
    echo "github.sh $*" >>"$FAKE_STATE/calls.log"
    [[ "$1" == wait-ref ]] || { echo "fake github: unexpected $1" >&2; exit 3; }
    tag=${3#refs/tags/}
    if [[ -f "$FAKE_STATE/forgejo/tags/$tag" ]]; then
      mkdir -p "$FAKE_STATE/github/tags"
      cp "$FAKE_STATE/forgejo/tags/$tag" "$FAKE_STATE/github/tags/$tag"
      exit 0
    fi
    echo "github: $3 is not on GitHub after $4s" >&2
    exit 2
""").lstrip()

SHA = "a" * 40
OTHER = "b" * 40


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="publish-release-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.state = self.temp / "state"
        self.state.mkdir()
        self.forge_sh = self.temp / "forge.sh"
        self.forge_sh.write_text(FAKE_FORGE)
        self.github_sh = self.temp / "github.sh"
        self.github_sh.write_text(FAKE_GITHUB)
        self.files = self.temp / "files"
        self.files.mkdir()
        (self.files / "app.zip").write_bytes(b"zip bytes")
        (self.files / "my app.exe").write_bytes(b"windows bytes!")
        self.notes = self.temp / "notes.md"
        self.notes.write_text("- the thing\n")
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("FORGE_", "GITHUB_", "FAKE_", "YGG_")) and k != "GH_TOKEN"}
        self.env = dict(base, FORGE_TYPE="forgejo", YGG_FORGE_SH=str(self.forge_sh), YGG_GITHUB_SH=str(self.github_sh),
                        FAKE_STATE=str(self.state), GITHUB_TAG_WAIT="7")
        self.mirror_env = dict(self.env, GH_TOKEN="app-token", GITHUB_OWNER="octo")

    def run_script(self, *arguments, env=None):
        return subprocess.run(["bash", str(SCRIPT), *arguments], env=env or self.env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)

    def publish(self, *extra, env=None, files=None):
        return self.run_script("app", "v1.4.0", SHA, "v1.4.0", str(self.notes), str(files or self.files), *extra, env=env)

    def calls(self):
        path = self.state / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def calls_of(self, prefix):
        """The calls that start with a server type, or with a type and a command."""
        return [line for line in self.calls() if line.startswith(prefix + " ") or line == prefix]

    def release(self, kind, number=1):
        return self.state / kind / "releases" / str(number)

    def attached(self, kind, number=1):
        assets = self.release(kind, number) / "assets"
        return {f.name: f.read_text().strip() for f in assets.iterdir()} if assets.exists() else {}

    def draft(self, kind, number=1):
        return (self.release(kind, number) / "draft").read_text().strip()

    def tag(self, kind, sha):
        (self.state / kind / "tags").mkdir(parents=True, exist_ok=True)
        (self.state / kind / "tags" / "v1.4.0").write_text(sha + "\n")

    def test_given_files_when_published_then_both_drafts_get_them_and_github_is_published_last(self):
        result = self.publish("--mirror-releases", env=self.mirror_env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "https://forgejo.example/app/releases/v1.4.0\nhttps://github.example/app/releases/v1.4.0\n")
        expected = {"app.zip": "9", "my app.exe": "14"}
        for kind in ("forgejo", "github"):
            self.assertEqual(self.attached(kind), expected, kind)
            self.assertEqual(self.draft(kind), "false", kind)
            self.assertEqual((self.state / kind / "tags" / "v1.4.0").read_text().strip(), SHA)
            self.assertEqual((self.release(kind) / "notes").read_text(), "- the thing\n")
            self.assertEqual((self.release(kind) / "sha").read_text().strip(), SHA)
        calls = self.calls()
        publishes = [n for n, line in enumerate(calls) if " publish-release " in line]
        self.assertEqual([calls[n].split()[0] for n in publishes], ["forgejo", "github"])
        # Nothing is published before both drafts are complete, and GitHub's tag is waited for, not made.
        self.assertGreater(publishes[0], max(n for n, line in enumerate(calls) if " upload-release-file " in line))
        self.assertIn("github.sh wait-ref app refs/tags/v1.4.0 7", calls)
        self.assertNotIn("github create-tag", " ".join(calls))
        self.assertEqual(len([c for c in calls if c.startswith("forgejo create-tag")]), 1)

    def test_given_an_upload_that_comes_back_short_when_published_then_nothing_is_published(self):
        env = dict(self.mirror_env, FAKE_SHORT_UPLOAD="my app.exe")
        result = self.publish("--mirror-releases", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("publish-release: checking the files of Forgejo failed: my app.exe has 13 there, 14 bytes here",
                      result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.draft("forgejo"), "true")
        self.assertFalse((self.state / "github").exists(), "GitHub is not touched after Forgejo's draft failed")
        self.assertEqual([c for c in self.calls() if " publish-release " in c], [])

    def test_given_a_short_upload_to_github_when_published_then_forgejo_is_not_published_either(self):
        # Only GitHub's copy is short: the fake is told by the server type through the file name's size.
        env = dict(self.mirror_env, FAKE_SHORT_UPLOAD="app.zip")
        script = self.forge_sh.read_text().replace(
            '[[ "${FAKE_SHORT_UPLOAD:-}" != "$name" ]] || size=$((size - 1))',
            '[[ "${FAKE_SHORT_UPLOAD:-}" != "$name" || "$type" != github ]] || size=$((size - 1))')
        self.forge_sh.write_text(script)
        result = self.publish("--mirror-releases", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("checking the files of GitHub failed", result.stderr)
        self.assertEqual((self.draft("forgejo"), self.draft("github")), ("true", "true"))
        self.assertEqual([c for c in self.calls() if " publish-release " in c], [])

    def test_given_a_prerelease_tag_when_published_then_both_servers_create_it_from_that_tag(self):
        # forge.sh create-release decides prerelease from the tag it is given (test_forge.py has the rule), on
        # the git server and on the GitHub copy alike: so both servers' drafts must be created with the
        # release's own tag, unchanged.
        tag = "v1.5.0-rc.1"
        result = self.run_script("app", tag, SHA, tag, str(self.notes), str(self.files), "--mirror-releases",
                                 env=self.mirror_env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        created = [line for line in self.calls() if " create-release " in line]
        self.assertEqual([line.split()[:5] for line in created],
                         [["forgejo", "create-release", "app", tag, SHA], ["github", "create-release", "app", tag, SHA]])

    def test_given_replace_files_and_a_draft_left_by_a_failure_when_published_again_then_its_files_are_uploaded_again(self):
        # With release.finalize the manifest signed in this run describes this run's bytes: a draft's file of the
        # same name and size from an earlier run (a rebuilt Windows file, say) must not be kept. Every file the
        # draft already has is deleted and uploaded again, on each draft, before anything is published.
        failing = dict(self.mirror_env, FAKE_FAIL="github upload-release-file")
        first = self.publish("--mirror-releases", "--replace-files", env=failing)
        self.assertEqual(first.returncode, 1)
        self.assertEqual(self.attached("forgejo"), {"app.zip": "9", "my app.exe": "14"})
        self.assertEqual(self.calls_of("forgejo delete-release-file"), [], "a new draft has nothing to replace")

        second = self.publish("--mirror-releases", "--replace-files", env=self.mirror_env)
        self.assertEqual((second.returncode, second.stderr), (0, ""))
        calls = self.calls()
        deleted = [c for c in calls if " delete-release-file " in c]
        self.assertEqual(sorted(deleted), ["forgejo delete-release-file app 1 app.zip",
                                           "forgejo delete-release-file app 1 my app.exe"])
        # Each file deleted, then uploaded again: two uploads in the first run, two in this one.
        self.assertEqual(len([c for c in calls if c.startswith("forgejo upload-release-file")]), 4)
        for name in ("app.zip", "my app.exe"):
            delete = calls.index(f"forgejo delete-release-file app 1 {name}")
            upload = max(n for n, c in enumerate(calls) if c.startswith("forgejo upload-release-file") and c.endswith(name))
            self.assertLess(delete, upload, name)
        self.assertLess(max(n for n, c in enumerate(calls) if " delete-release-file " in c),
                        min(n for n, c in enumerate(calls) if " publish-release " in c))
        for kind in ("forgejo", "github"):
            self.assertEqual(self.attached(kind), {"app.zip": "9", "my app.exe": "14"}, kind)
            self.assertEqual(self.draft(kind), "false", kind)

    def test_given_replace_files_and_a_published_release_when_published_again_then_nothing_is_replaced(self):
        # A published release is public and complete: it is never changed, only checked by name and size.
        self.assertEqual(self.publish("--mirror-releases", env=self.mirror_env).returncode, 0)
        again = self.publish("--mirror-releases", "--replace-files", env=self.mirror_env)
        self.assertEqual((again.returncode, again.stderr), (0, ""))
        self.assertEqual([c for c in self.calls() if " delete-release-file " in c], [])
        self.assertEqual(len([c for c in self.calls() if " upload-release-file " in c]), 4, "the first run's only")

    def test_given_replace_files_with_attach_when_run_then_it_is_a_usage_error(self):
        # attach is for releases without release.finalize (actions-release refuses the others).
        result = self.run_script("attach", "app", "v1.4.0", str(self.files), "--replace-files")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_given_a_rerun_after_a_failure_when_published_then_existing_tag_and_drafts_are_reused(self):
        failing = dict(self.mirror_env, FAKE_FAIL="github upload-release-file")
        first = self.publish("--mirror-releases", env=failing)
        self.assertEqual(first.returncode, 1)
        self.assertIn("publish-release: uploading app.zip to GitHub failed: forge: upload-release-file failed", first.stderr)
        self.assertEqual(self.draft("forgejo"), "true")
        self.assertEqual(len(self.calls_of("forgejo upload-release-file")), 2)

        second = self.publish("--mirror-releases", env=self.mirror_env)
        self.assertEqual((second.returncode, second.stderr), (0, ""))
        calls = self.calls()
        # One tag, one draft per server, and Forgejo's files are not uploaded again.
        self.assertEqual(len([c for c in calls if c.startswith("forgejo create-tag")]), 1)
        self.assertEqual(len([c for c in calls if " create-release " in c]), 2)
        self.assertEqual(len([c for c in calls if c.startswith("forgejo upload-release-file")]), 2)
        self.assertEqual(len([c for c in calls if c.startswith("github upload-release-file")]), 2 + 1)
        self.assertEqual((self.draft("forgejo"), self.draft("github")), ("false", "false"))
        self.assertEqual(second.stdout.count("\n"), 2)

    def test_given_a_rerun_after_forgejo_was_published_when_published_then_only_github_is_published(self):
        failing = dict(self.mirror_env, FAKE_FAIL="github publish-release")
        first = self.publish("--mirror-releases", env=failing)
        self.assertEqual(first.returncode, 1)
        self.assertIn("publishing the release on GitHub failed", first.stderr)
        self.assertEqual((self.draft("forgejo"), self.draft("github")), ("false", "true"))
        second = self.publish("--mirror-releases", env=self.mirror_env)
        self.assertEqual((second.returncode, second.stderr), (0, ""))
        self.assertEqual(second.stdout, "https://forgejo.example/app/releases/v1.4.0\nhttps://github.example/app/releases/v1.4.0\n")
        self.assertEqual(len([c for c in self.calls() if c.startswith("forgejo publish-release")]), 1)
        self.assertEqual(self.draft("github"), "false")

    def test_given_a_tag_on_another_commit_when_published_then_it_is_refused(self):
        self.tag("forgejo", OTHER)
        result = self.publish()
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn(f"publish-release: checking tag v1.4.0 on Forgejo failed: it points to {OTHER}, not {SHA}", result.stderr)
        self.assertFalse((self.state / "forgejo" / "releases" / "1").exists())

    def test_given_a_github_tag_on_another_commit_when_published_then_it_is_refused_and_drafts_stay(self):
        # The mirror pushed the tag, but not what Forgejo has.
        self.tag("forgejo", SHA)
        self.tag("github", OTHER)
        script = self.github_sh.read_text().replace('cp "$FAKE_STATE/forgejo/tags/$tag" "$FAKE_STATE/github/tags/$tag"', "true")
        self.github_sh.write_text(script)
        result = self.publish("--mirror-releases", env=self.mirror_env)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"checking tag v1.4.0 on GitHub failed: it points to {OTHER}, not {SHA}", result.stderr)
        self.assertEqual(self.draft("forgejo"), "true")
        self.assertEqual([c for c in self.calls() if " publish-release " in c], [])

    def test_given_the_tag_never_reaching_github_when_published_then_it_fails_and_drafts_stay(self):
        script = self.github_sh.read_text().replace(
            'if [[ -f "$FAKE_STATE/forgejo/tags/$tag" ]]; then', 'if false; then')
        self.github_sh.write_text(script)
        result = self.publish("--mirror-releases", env=self.mirror_env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("publish-release: waiting for tag v1.4.0 on GitHub failed: github: refs/tags/v1.4.0 is not on GitHub after 7s",
                      result.stderr)
        self.assertEqual(self.draft("forgejo"), "true")
        self.assertEqual([c for c in self.calls() if " publish-release " in c], [])

    def test_given_no_mirror_releases_when_published_then_github_is_not_called(self):
        result = self.publish()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "https://forgejo.example/app/releases/v1.4.0\n")
        self.assertEqual(self.calls_of("github"), [])
        self.assertEqual(self.calls_of("github.sh"), [])
        self.assertFalse((self.state / "github").exists())

    def test_given_mirror_releases_without_github_credentials_when_published_then_it_exits_2_before_any_call(self):
        for missing in ("GH_TOKEN", "GITHUB_OWNER"):
            env = {k: v for k, v in self.mirror_env.items() if k != missing}
            result = self.publish("--mirror-releases", env=env)
            self.assertEqual(result.returncode, 2, missing)
            self.assertIn("--mirror-releases needs GH_TOKEN and GITHUB_OWNER", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_given_mirror_releases_on_a_github_installation_when_published_then_it_is_ignored(self):
        env = dict(self.mirror_env, FORGE_TYPE="github")
        result = self.publish("--mirror-releases", env=env)
        self.assertEqual((result.returncode, result.stdout), (0, "https://github.example/app/releases/v1.4.0\n"))
        self.assertIn("ignored", result.stderr)
        self.assertEqual(self.calls_of("forgejo"), [])

    def test_given_the_github_api_url_when_mirroring_then_forge_sh_is_pointed_at_github(self):
        script = self.forge_sh.read_text().replace(
            'echo "$type $*" >>"$FAKE_STATE/calls.log"',
            'echo "$type $*" >>"$FAKE_STATE/calls.log"\n'
            'echo "$type ${FORGE_API_URL:-} ${FORGE_OWNER:-} ${FORGE_TOKEN:-}" >>"$FAKE_STATE/env.log"')
        self.forge_sh.write_text(script)
        env = dict(self.mirror_env, GITHUB_API_URL="https://ghe.example/api/v3", FORGE_API_URL="https://forgejo.example/api/v1",
                   FORGE_OWNER="forgejo-owner", FORGE_TOKEN="forgejo-token")
        result = self.publish("--mirror-releases", env=env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        lines = set((self.state / "env.log").read_text().splitlines())
        self.assertEqual(lines, {"forgejo https://forgejo.example/api/v1 forgejo-owner forgejo-token",
                                 "github https://ghe.example/api/v3 octo app-token"})

    def test_given_missing_windows_files_when_published_then_the_notes_say_so(self):
        result = self.publish("--missing", "app-setup.exe,app.msi", "--mirror-releases", env=self.mirror_env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        for kind in ("forgejo", "github"):
            notes = (self.release(kind) / "notes").read_text()
            self.assertEqual(notes, "- the thing\n\n\n## Missing files\n\n"
                                    "These files could not be built and are not attached to this release:\n\n"
                                    "- app-setup.exe\n- app.msi\n", kind)
        # The caller's notes file is not changed.
        self.assertEqual(self.notes.read_text(), "- the thing\n")

    def test_given_a_file_of_the_same_name_and_another_size_when_published_then_it_stops_naming_the_file(self):
        self.tag("forgejo", SHA)
        (self.state / "forgejo" / "releases" / "1" / "assets").mkdir(parents=True)
        for name, content in (("tag", "v1.4.0"), ("draft", "true")):
            (self.state / "forgejo" / "releases" / "1" / name).write_text(content)
        (self.state / "forgejo" / "releases" / "1" / "assets" / "app.zip").write_text("3\n")
        result = self.publish()
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("publish-release: uploading app.zip to Forgejo failed", result.stderr)
        self.assertIn("3 bytes, not 9", result.stderr)
        self.assertEqual(self.calls_of("forgejo upload-release-file"), [])

    def test_given_files_in_subdirectories_when_published_then_they_are_uploaded_by_name(self):
        nested = self.files / "windows-build"
        nested.mkdir()
        (nested / "setup.exe").write_bytes(b"123")
        result = self.publish()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.attached("forgejo"), {"app.zip": "9", "my app.exe": "14", "setup.exe": "3"})

    def test_given_two_files_of_the_same_name_when_published_then_it_fails_before_any_call(self):
        nested = self.files / "other"
        nested.mkdir()
        (nested / "app.zip").write_bytes(b"1")
        result = self.publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("two files are named app.zip", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_given_no_files_when_published_then_a_plain_release_is_made(self):
        empty = self.temp / "empty"
        empty.mkdir()
        result = self.publish(files=empty)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.attached("forgejo"), {})
        self.assertEqual(self.draft("forgejo"), "false")

    def test_given_forge_sh_failing_when_a_step_runs_then_the_message_names_it_and_the_exit_is_1(self):
        for command, what in (("create-tag", "creating tag v1.4.0 on Forgejo"),
                              ("create-release", "creating the draft release on Forgejo"),
                              ("publish-release", "publishing the release on Forgejo")):
            shutil.rmtree(self.state)
            self.state.mkdir()
            result = self.publish(env=dict(self.env, FAKE_FAIL=f"forgejo {command}"))
            self.assertEqual(result.returncode, 1, command)
            self.assertIn(f"publish-release: {what} failed: forge: {command} failed: the fake server said no", result.stderr)

    def test_given_attach_when_run_then_files_reach_the_published_release(self):
        first = self.temp / "first"
        first.mkdir()
        (first / "app.zip").write_bytes(b"zip bytes")
        self.assertEqual(self.publish("--mirror-releases", env=self.mirror_env, files=first).returncode, 0)
        self.assertEqual(self.draft("forgejo"), "false")
        later = self.temp / "later"
        later.mkdir()
        (later / "app.zip").write_bytes(b"zip bytes")
        (later / "setup.exe").write_bytes(b"1234")
        before = len(self.calls())
        result = self.run_script("attach", "app", "v1.4.0", str(later), "--mirror-releases", env=self.mirror_env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "https://forgejo.example/app/releases/v1.4.0\nhttps://github.example/app/releases/v1.4.0\n")
        for kind in ("forgejo", "github"):
            self.assertEqual(self.attached(kind), {"app.zip": "9", "setup.exe": "4"}, kind)
            self.assertEqual(self.draft(kind), "false", kind)
        new_calls = self.calls()[before:]
        # Only the file that was not there is uploaded, and no tag, release or publish is touched.
        self.assertEqual(sorted(c.split()[:2] + [c.split()[-1].rsplit("/", 1)[-1]] for c in new_calls if " upload-release-file " in c),
                         [["forgejo", "upload-release-file", "setup.exe"], ["github", "upload-release-file", "setup.exe"]])
        self.assertEqual([c for c in new_calls if any(w in c for w in (" create-", " publish-release", "wait-ref"))], [])

    def test_given_attach_while_the_github_copy_is_pending_when_run_then_forgejo_gets_the_files_and_it_exits_5(self):
        # After a skip the release is published on Forgejo only: GitHub has no release of the tag until the
        # release-resume job makes it. attach puts the files on Forgejo's and says so (exit 5), without
        # touching GitHub beyond the look.
        first = self.temp / "first"
        first.mkdir()
        (first / "app.zip").write_bytes(b"zip bytes")
        self.assertEqual(self.publish(files=first).returncode, 0)
        later = self.temp / "later"
        later.mkdir()
        (later / "setup.exe").write_bytes(b"1234")
        before = len(self.calls())
        result = self.run_script("attach", "app", "v1.4.0", str(later), "--mirror-releases", env=self.mirror_env)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(result.stdout, "https://forgejo.example/app/releases/v1.4.0\n")
        self.assertIn("publish-release: GitHub has no release of v1.4.0 yet (its copy is pending): the files are on "
                      "Forgejo's release only", result.stderr)
        self.assertEqual(self.attached("forgejo"), {"app.zip": "9", "setup.exe": "4"})
        self.assertEqual([c for c in self.calls()[before:] if c.startswith("github ")], ["github find-release app v1.4.0"])

    def test_given_attach_when_the_github_lookup_fails_then_nothing_is_uploaded(self):
        self.assertEqual(self.publish().returncode, 0)
        extra = self.temp / "extra"
        extra.mkdir()
        (extra / "setup.exe").write_bytes(b"1234")
        before = len(self.calls())
        result = self.run_script("attach", "app", "v1.4.0", str(extra), "--mirror-releases",
                                 env=dict(self.mirror_env, FAKE_FAIL="github find-release"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("looking for the release of v1.4.0 on GitHub failed", result.stderr)
        self.assertEqual([c for c in self.calls()[before:] if " upload-release-file " in c], [])

    def test_given_attach_and_no_release_when_run_then_it_fails(self):
        result = self.run_script("attach", "app", "v1.4.0", str(self.files))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("publish-release: looking for the release of v1.4.0 on Forgejo failed: there is none", result.stderr)

    def test_given_attach_with_a_short_upload_when_run_then_it_fails(self):
        self.assertEqual(self.publish().returncode, 0)
        extra = self.temp / "extra"
        extra.mkdir()
        (extra / "setup.exe").write_bytes(b"1234")
        result = self.run_script("attach", "app", "v1.4.0", str(extra), env=dict(self.env, FAKE_SHORT_UPLOAD="setup.exe"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("setup.exe has 3 there, 4 bytes here", result.stderr)

    def test_given_a_wrong_command_line_when_run_then_the_usage_is_shown_and_it_exits_2(self):
        for arguments in ((), ("app", "v1.4.0"), ("attach", "app"), ("app", "v1.4.0", SHA, "t", str(self.notes), str(self.files), "--bogus"),
                          ("attach", "app", "v1.4.0", str(self.files), "--missing", "x")):
            result = self.run_script(*arguments)
            self.assertEqual((result.returncode, result.stdout), (2, ""), arguments)
            self.assertIn("usage: publish-release.sh", result.stderr)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
