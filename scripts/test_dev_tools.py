"""Tests for the dev user's own commands (platform/dev): ci-check and dev-toolchains.

`ygg install --role dev` copies both into dev's ~/.local/bin (scripts/machine.sh, step dev-tools, which
test_machine.py covers). These run the scripts themselves, in a temporary home: ci-check against a fake
scripts/run-check.sh that writes down how it was called, and dev-toolchains against a fake curl that serves
small archives and checksum files made by the tests, so nothing is downloaded.
"""

import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest

DEV = pathlib.Path(__file__).resolve().parent.parent / "platform" / "dev"
CI_CHECK = DEV / "ci-check"
DEV_TOOLCHAINS = DEV / "dev-toolchains"


def fake(directory, name, body):
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body).lstrip())
    path.chmod(0o755)
    return path


class CiCheckTests(unittest.TestCase):
    YML = textwrap.dedent("""\
        checks:
          lint:
            image: example/lint:1@sha256:aaaa
            run: ci/lint.sh
          tests:
            image: example/tests:2
            run: ci/tests.sh --all
            docker: true
        """)

    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="ygg-ci-check-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        # uv runs the script's Python with PyYAML: the tests' own python3 has it.
        fake(self.bin, "uv", """
            while (($#)) && [[ "$1" != python3 ]]; do shift; done
            shift
            exec python3 "$@"
        """)
        fake(self.bin, "ip", 'echo "1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 1000"\n')
        # A yggdrasil checkout whose run-check.sh writes down its arguments and the Testcontainers settings.
        self.ygg = self.temp / "yggdrasil"
        (self.ygg / "scripts").mkdir(parents=True)
        self.record = self.temp / "run-check"
        fake(self.ygg / "scripts", "run-check.sh", f"""
            {{
              printf '%s\\n' "$@"
              echo "socket=${{YGG_DOCKER_SOCKET:-}}"
              echo "host=${{TESTCONTAINERS_HOST_OVERRIDE:-}}"
            }} >"{self.record}"
            exit 7
        """)
        self.repo = self.temp / "repo"
        (self.repo / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "yggdrasil.yml").write_text(self.YML)
        self.env = {
            **{k: v for k, v in os.environ.items() if k not in ("YGG_DOCKER_SOCKET", "TESTCONTAINERS_HOST_OVERRIDE")},
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.temp / "home"),
            "YGG_CHECKOUT": str(self.ygg),
            "XDG_RUNTIME_DIR": "/run/user/1005",
        }

    def ci_check(self, *arguments, cwd=None, **env):
        return subprocess.run(["bash", str(CI_CHECK), *arguments], cwd=cwd or self.repo / "sub",
                              env={**self.env, **env}, capture_output=True, text=True)

    def recorded(self):
        return self.record.read_text().splitlines()

    def test_given_list_when_run_then_it_prints_every_check_with_what_it_runs(self):
        result = self.ci_check("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["lint", "tests"])
        self.assertIn("ci/lint.sh", lines[0])
        self.assertNotIn("(docker)", lines[0])
        self.assertIn("(docker) ci/tests.sh --all", lines[1])
        self.assertFalse(self.record.exists())

    def test_given_a_check_when_run_then_run_check_gets_its_image_the_repositorys_root_and_command(self):
        result = self.ci_check("lint")
        # The check's own exit status.
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.recorded(), [
            "lint", "example/lint:1@sha256:aaaa", str(self.repo), "--", "sh", "-c", "ci/lint.sh",
            "socket=", "host=",
        ])
        # Never the toolchain cache: that is the ci agent's.
        self.assertNotIn("--toolchains", self.recorded())

    def test_given_a_docker_check_when_run_then_it_gets_dev_s_socket_and_the_hosts_address(self):
        self.ci_check("tests")
        self.assertEqual(self.recorded(), [
            "tests", "example/tests:2", str(self.repo), "--docker", "--", "sh", "-c", "ci/tests.sh --all",
            "socket=/run/user/1005/docker.sock", "host=10.0.0.5",
        ])

    def test_given_a_host_override_when_a_docker_check_is_run_then_it_is_kept(self):
        self.ci_check("tests", TESTCONTAINERS_HOST_OVERRIDE="192.0.2.9")
        self.assertIn("host=192.0.2.9", self.recorded())

    def test_given_a_repository_directory_when_run_then_that_repository_is_checked(self):
        result = self.ci_check("lint", str(self.repo / "sub"), cwd=self.temp)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.recorded()[2], str(self.repo))

    def test_given_an_unknown_check_when_run_then_it_names_the_checks_there_are(self):
        result = self.ci_check("nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no check 'nope'", result.stderr)
        self.assertIn("lint, tests", result.stderr)
        self.assertFalse(self.record.exists())

    def test_given_a_repository_without_a_pipeline_file_when_run_then_it_says_so(self):
        (self.repo / "yggdrasil.yml").unlink()
        result = self.ci_check("lint")
        self.assertEqual(result.returncode, 2)
        self.assertIn("has no yggdrasil.yml", result.stderr)

    def test_given_no_check_when_run_then_it_prints_its_usage(self):
        result = self.ci_check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: ci-check", result.stderr)

    def test_given_no_yggdrasil_checkout_when_run_then_it_says_where_it_looked(self):
        result = self.ci_check("lint", YGG_CHECKOUT=str(self.temp / "nowhere"))
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(self.temp / "nowhere"), result.stderr)


def url_file(url):
    """The fake curl's file for a URL: the URL with every character but [A-Za-z0-9._-] made _."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", url)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def archive(members, mode):
    """A tar archive (mode w:gz or w:xz) of {name: script text}, every member executable."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class DevToolchainsTests(unittest.TestCase):
    GO = "1.26.10"
    HUGO = "0.165.0"
    NODE = "v24.11.0"
    FLUTTER = "3.50.0"
    FLUTTER_URL = ("https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/"
                   f"flutter_linux_{FLUTTER}-stable.tar.xz")
    UV = "uv-x86_64-unknown-linux-gnu.tar.gz"
    UV_URL = f"https://github.com/astral-sh/uv/releases/latest/download/{UV}"
    RUSTUP_URL = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"

    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="ygg-dev-toolchains-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.home = self.temp / "home"
        self.home.mkdir()
        (self.home / ".profile").write_text("# ~/.profile\n")
        self.urls = self.temp / "urls"
        self.urls.mkdir()
        self.log = self.temp / "calls"
        self.log.write_text("")
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        # curl: the file of the URL (to -o's file, else to stdout); fails like an HTTP error without one.
        fake(bin_dir, "curl", f"""
            out="" url=""
            while (($#)); do
              case $1 in
                -o | --proto) [[ "$1" == -o ]] && out=$2; shift 2 ;;
                https://*) url=$1; shift ;;
                *) shift ;;
              esac
            done
            echo "curl $url" >>"{self.log}"
            file="{self.urls}/$(printf '%s' "$url" | tr -c 'A-Za-z0-9._-' _)"
            [[ -f "$file" ]] || exit 22
            if [[ -n "$out" ]]; then cp "$file" "$out"; else cat "$file"; fi
        """)
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.home)}
        self.serve_everything()
        self.flutter_installed(self.FLUTTER)

    # ---- What the fake internet serves ---------------------------------------------------------------

    def serve(self, url, data):
        (self.urls / url_file(url)).write_bytes(data if isinstance(data, bytes) else data.encode())

    def serve_everything(self):
        log = self.log
        # rustup-init: a script that makes cargo and writes down how it was called.
        rustup = textwrap.dedent(f"""\
            #!/bin/sh
            echo "rustup-init $*" >>"{log}"
            mkdir -p "$HOME/.cargo/bin"
            printf '#!/bin/sh\\n' >"$HOME/.cargo/bin/cargo"
            chmod 755 "$HOME/.cargo/bin/cargo"
            """).encode()
        self.serve(self.RUSTUP_URL, rustup)
        self.serve(self.RUSTUP_URL + ".sha256", f"{sha256(rustup)} *./rustup-init\n")

        go_file = f"go{self.GO}.linux-amd64.tar.gz"
        go = archive({"go/bin/go": f"#!/bin/sh\necho 'go version go{self.GO} linux/amd64'\n"}, "w:gz")
        self.serve(f"https://go.dev/dl/{go_file}", go)
        self.serve("https://go.dev/dl/?mode=json&include=all", json.dumps([
            {"version": "go1.27.0", "files": [{"filename": "go1.27.0.linux-amd64.tar.gz", "sha256": "0" * 64}]},
            {"version": f"go{self.GO}", "files": [{"filename": go_file, "sha256": sha256(go)}]},
        ]))

        hugo_file = f"hugo_extended_{self.HUGO}_linux-amd64.tar.gz"
        hugo = archive({"hugo": f"#!/bin/sh\necho 'hugo v{self.HUGO}-abcdef+extended linux/amd64'\n",
                        "README.md": "readme\n"}, "w:gz")
        base = f"https://github.com/gohugoio/hugo/releases/download/v{self.HUGO}"
        self.serve(f"{base}/{hugo_file}", hugo)
        self.serve(f"{base}/hugo_{self.HUGO}_checksums.txt",
                   f"{'1' * 64}  hugo_{self.HUGO}_linux-amd64.tar.gz\n{sha256(hugo)}  {hugo_file}\n")

        node_dir = f"node-{self.NODE}-linux-x64"
        node = archive({f"{node_dir}/bin/node": f"#!/bin/sh\necho {self.NODE}\n"}, "w:xz")
        self.serve("https://nodejs.org/dist/index.json", json.dumps([
            {"version": "v25.2.0", "lts": False}, {"version": self.NODE, "lts": "Krypton"},
            {"version": "v22.20.0", "lts": "Jod"},
        ]))
        self.serve(f"https://nodejs.org/dist/{self.NODE}/{node_dir}.tar.xz", node)
        self.serve(f"https://nodejs.org/dist/{self.NODE}/SHASUMS256.txt", f"{sha256(node)}  {node_dir}.tar.xz\n")

        uv_script = f'#!/bin/sh\necho "uv $*" >>"{log}"\n'
        uv = archive({"uv-x86_64-unknown-linux-gnu/uv": uv_script,
                      "uv-x86_64-unknown-linux-gnu/uvx": uv_script}, "w:gz")
        self.serve(self.UV_URL, uv)
        self.serve(self.UV_URL + ".sha256", f"{sha256(uv)}  {self.UV}\n")

    def flutter_installed(self, version):
        """A Flutter SDK in ~/sdk/flutter, as unpacked and first run: its bin/flutter and its version file."""
        flutter = self.home / "sdk" / "flutter"
        shutil.rmtree(flutter, ignore_errors=True)
        (flutter / "bin" / "cache").mkdir(parents=True)
        (flutter / "bin" / "flutter").write_text("#!/bin/sh\n")
        (flutter / "bin" / "flutter").chmod(0o755)
        (flutter / "bin" / "cache" / "flutter.version.json").write_text(
            json.dumps({"frameworkVersion": version, "channel": "stable"}))

    # ---- Running it ------------------------------------------------------------------------------------

    def run_it(self):
        return subprocess.run(["bash", str(DEV_TOOLCHAINS)], env=self.env, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines()

    def fetched(self):
        return [line.split(" ", 1)[1] for line in self.calls() if line.startswith("curl ")]

    def output_of(self, path):
        return subprocess.run([str(self.home / path)], capture_output=True, text=True).stdout.strip()

    def test_given_a_fresh_home_when_run_then_every_toolchain_is_installed_and_on_the_path(self):
        result = self.run_it()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.access(self.home / ".cargo/bin/cargo", os.X_OK))
        self.assertIn(f"go{self.GO} ", self.output_of("sdk/go/bin/go"))
        self.assertIn(f"v{self.HUGO}-", self.output_of("sdk/hugo/hugo"))
        self.assertFalse((self.home / "sdk/hugo/README.md").exists())
        self.assertEqual(self.output_of("sdk/node/bin/node"), self.NODE)
        for name in ("uv", "uvx"):
            self.assertTrue(os.access(self.home / ".local/bin" / name, os.X_OK), name)
        calls = self.calls()
        # rustup with the components alexandria-api's lint runs, and without touching the profile itself.
        rustup = next(line for line in calls if line.startswith("rustup-init "))
        for part in ("-y", "--no-modify-path", "--profile minimal", "-c clippy,rustfmt"):
            self.assertIn(part, rustup)
        self.assertIn("uv tool install --quiet shellcheck-py", calls)
        # The pinned Flutter was there already: not fetched again.
        self.assertNotIn(self.FLUTTER_URL, self.fetched())
        profile = (self.home / ".profile").read_text()
        self.assertTrue(profile.startswith("# ~/.profile\n"))
        self.assertEqual(profile.count("# >>> dev sdk >>>"), 1)
        self.assertEqual(profile.count("# <<< dev sdk <<<"), 1)
        for part in ("$HOME/sdk/flutter/bin", "$HOME/.cargo/bin", "$HOME/sdk/go/bin", "$HOME/sdk/hugo",
                     "$HOME/sdk/node/bin", "GOTOOLCHAIN=local"):
            self.assertIn(part, profile)

    def test_given_everything_installed_when_run_again_then_no_archive_is_fetched_and_the_profile_is_kept(self):
        self.assertEqual(self.run_it().returncode, 0)
        profile = (self.home / ".profile").read_text()
        self.log.write_text("")
        result = self.run_it()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Only Node's release list, to learn which release is the newest LTS.
        self.assertEqual(self.fetched(), ["https://nodejs.org/dist/index.json"])
        self.assertFalse([line for line in self.calls() if line.startswith("rustup-init")])
        self.assertEqual((self.home / ".profile").read_text(), profile)

    def test_given_an_archive_that_does_not_match_its_checksum_when_run_then_it_stops_and_unpacks_nothing(self):
        url = f"https://go.dev/dl/go{self.GO}.linux-amd64.tar.gz"
        self.serve(url, archive({"go/bin/go": "#!/bin/sh\necho tampered\n"}, "w:gz"))
        result = self.run_it()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr)
        self.assertFalse((self.home / "sdk" / "go").exists())
        self.assertNotIn("dev sdk", (self.home / ".profile").read_text())

    def test_given_a_rustup_init_that_does_not_match_its_checksum_when_run_then_it_is_never_run(self):
        self.serve(self.RUSTUP_URL + ".sha256", f"{'2' * 64} *./rustup-init\n")
        result = self.run_it()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse([line for line in self.calls() if line.startswith("rustup-init")])
        self.assertFalse((self.home / ".cargo").exists())

    def test_given_a_uv_archive_that_does_not_match_its_checksum_when_run_then_uv_is_not_installed(self):
        self.serve(self.UV_URL + ".sha256", f"{'3' * 64}  {self.UV}\n")
        result = self.run_it()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".local/bin/uv").exists())

    def test_given_no_flutter_when_run_then_the_pinned_archive_is_fetched_and_refused_when_it_is_not_the_pin(self):
        shutil.rmtree(self.home / "sdk" / "flutter")
        self.serve(self.FLUTTER_URL, b"not the pinned archive")
        result = self.run_it()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.FLUTTER_URL, self.fetched())
        self.assertIn("checksum", result.stderr)
        self.assertFalse((self.home / "sdk" / "flutter").exists())

    def test_given_another_flutter_version_when_run_then_the_pin_is_fetched_and_the_old_sdk_stays_until_it_checks_out(self):
        self.flutter_installed("3.44.0")
        self.serve(self.FLUTTER_URL, b"not the pinned archive")
        result = self.run_it()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.FLUTTER_URL, self.fetched())
        version = json.loads((self.home / "sdk/flutter/bin/cache/flutter.version.json").read_text())
        self.assertEqual(version["frameworkVersion"], "3.44.0")

    def test_given_the_pins_when_read_then_flutter_is_the_one_the_repositories_pin(self):
        text = DEV_TOOLCHAINS.read_text()
        self.assertIn(f"FLUTTER_VERSION={self.FLUTTER}\n", text)
        self.assertIn("FLUTTER_SHA256=6ea2d806fd57269ae783e6788e2a81fc26eaad782f1e0d0ff38623bb765ff9f5\n", text)
        self.assertIn(f"GO_VERSION={self.GO}\n", text)
        self.assertIn(f"HUGO_VERSION={self.HUGO}\n", text)


if __name__ == "__main__":
    unittest.main()
