"""A restore of Forgejo from the nightly dump, on a scratch engine, against a real Forgejo.

    YGG_FORGEJO_TESTS=1 python3 -m unittest discover -s scripts -p test_backup_live.py -v

test_backup.py checks what scripts/backup.sh sends to docker against a fake one; docs/hosts.md
"Restoring" is a list of commands nothing had run. This proves the two together, the way a rebuilt
machine would live them:

  1. Forgejo 16.0.6 (the image platform/compose.yml pins) runs with the labels Compose puts on it, and
     holds a repository: two commits, a tag on the first, a published release on that tag with a file
     attached.
  2. The real `scripts/backup.sh run` dumps it (a scratch HOME, backup.conf and target; the item is
     just `forgejo`), exactly as the timer would at night.
  3. The machine is "lost": that container and its volume are removed.
  4. A fresh Forgejo starts on a new, empty volume (what `platform.sh up` gives a rebuilt machine) and
     docs/hosts.md's own commands restore it. They are not retyped here: the test reads the bash blocks
     of the "Forgejo, from a dump" section out of the document and runs them, changing only the volume
     name and the day to restore, and leaving out `platform.sh up` (it needs the whole platform, and
     the test has already done what it does). A step reworded in the document is a step run as worded.
  5. Through the owner's API token, made before the dump, the repository, the tag (on the same
     commit), the release and its file are all there, and `git clone` over HTTP gets the commit.

Skipped unless YGG_FORGEJO_TESTS=1 (and Docker, bash, jq, git, curl, rsync and flock are there; a
Docker socket that the test can name). The containers and volumes are named ygg-backup-test-* and are
removed at the end, also when the setup fails and when the run is interrupted (class cleanup and
atexit). No other container is touched; if one with the labels backup.sh looks for is already running
on the engine, the suite refuses to start rather than dump somebody's Forgejo.

The engine of a developer's WSL is rootful, and backup.sh refuses to run on one (require_rootless).
The fake `docker` test_backup.py puts on PATH has a counterpart here: a wrapper that passes every call
to the real docker, but answers the one question `docker info --format '{{json .SecurityOptions}}'`
as a rootless engine would. It changes nothing in backup.sh, and nothing else about the engine.
"""

import atexit
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
import urllib.parse
import urllib.request
import uuid

import rules
from test_forgejo_live import OWNER, ForgejoClient, _can_run, docker, free_port, pinned_image

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKUP_SH = ROOT / "scripts" / "backup.sh"
HOSTS_MD = ROOT / "docs" / "hosts.md"

PREFIX = "ygg-backup-test"
# What backup.sh finds Forgejo by (FORGEJO_PROJECT and FORGEJO_SERVICE in it): the labels Compose puts
# on the container. The test container carries them, as the platform's does.
LABELS = ("com.docker.compose.project=yggdrasil", "com.docker.compose.service=forgejo")

TAG, RELEASE_NOTES = "v1.0.0", "The first release.\n"
ATTACHMENT, ATTACHMENT_BYTES = "shop-api-1.0.0.zip", b"the release's bytes, kept in data/attachments"
README_AFTER = "A second commit, after the one the tag is on.\n"


def engine_socket():
    """The Docker socket backup.sh will be told about (DOCKER_HOST=unix://...): the environment's when it
    names one, else the usual root engine's. None when there is neither."""
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        return host[len("unix://"):]
    return "/var/run/docker.sock" if pathlib.Path("/var/run/docker.sock").exists() else None


def labelled_containers():
    """The running containers backup.sh would take for the platform's Forgejo."""
    done = docker("ps", "--quiet", *(f"--filter=label={label}" for label in LABELS))
    return done.stdout.split()


def restore_script():
    """docs/hosts.md "Restoring" -> "Forgejo, from a dump" as one bash script: its bash blocks, in the
    order they are written, as the user would paste them into one shell (the second needs `$forgejo`,
    which the first sets). Returns the script text with the document's own placeholders still in it."""
    text = HOSTS_MD.read_text(encoding="utf-8")
    start = text.index("**Forgejo, from a dump**")
    end = text.index("**A volume, from its tar**", start)
    blocks = re.findall(r"^[ ]*```bash\n(.*?)^[ ]*```", text[start:end], re.MULTILINE | re.DOTALL)
    if len(blocks) != 3:
        raise AssertionError(f"docs/hosts.md: expected 3 bash blocks under 'Forgejo, from a dump', found {len(blocks)}")
    return "\n".join(textwrap.dedent(block) for block in blocks)


def replaced_once(script, old, new):
    """`script` with `old` (a command or name the document is expected to have, once) changed to `new`."""
    if script.count(old) != 1:
        raise AssertionError(f"docs/hosts.md's restore steps were expected to have {old!r} once, "
                             f"found {script.count(old)}: the test adapts it for the scratch engine")
    return script.replace(old, new)


def multipart(name, filename, content):
    """(body, content type) of a multipart/form-data upload with one file field."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


@unittest.skipUnless(_can_run() and shutil.which("rsync") and shutil.which("flock") and engine_socket(),
                     "set YGG_FORGEJO_TESTS=1 to run a restore of Forgejo against a container (needs Docker "
                     "and its socket, bash, jq, git, curl, rsync and flock on a POSIX system)")
class ForgejoRestoreTests(ForgejoClient, unittest.TestCase):
    """One dump and one restore for the whole class (setUpClass); each test reads a different part of
    what came back, from the restored Forgejo."""

    @classmethod
    def setUpClass(cls):
        run_id = uuid.uuid4().hex[:8]
        cls.names = {"source": f"{PREFIX}-source-{run_id}", "restored": f"{PREFIX}-restored-{run_id}"}
        cls.volumes = {"source": f"{PREFIX}-data-{run_id}", "restored": f"{PREFIX}-restored-data-{run_id}"}
        cls.temp = pathlib.Path(tempfile.mkdtemp(prefix=f"{PREFIX}-"))
        # Idempotent, run both as a class cleanup (also when setUpClass fails halfway) and at exit (an
        # interrupted run): nothing outlives the suite.
        cls.addClassCleanup(cls.remove_everything)
        atexit.register(cls.remove_everything)

        # Nothing of the machine's own may be dumped: backup.sh takes the one container with the labels.
        running = labelled_containers()
        if running:
            raise unittest.SkipTest(f"a container with the platform's Forgejo labels is already running on this "
                                    f"engine ({' '.join(running)}): refusing to dump what is not the test's")

        cls.port = free_port()
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.start_forgejo("source")
        cls.create_content()
        cls.run_backup()
        # The machine is lost: the container and its data are gone, only ~/backups/<day>/forgejo.zip is left.
        docker("rm", "--force", "--volumes", cls.names["source"])
        docker("volume", "rm", "--force", cls.volumes["source"])
        # `platform.sh up` on the rebuilt machine: an empty Forgejo on a new volume ...
        cls.start_forgejo("restored")
        # ... and the document's steps.
        cls.restore_from_the_document()

        # The same owner token, made before the dump: it works only if the database came back.
        cls.api = {OWNER: rules.Api(cls.url + "/api/v1", cls.tokens[OWNER], "token")}

    @classmethod
    def remove_everything(cls):
        for role in ("source", "restored"):
            docker("rm", "--force", "--volumes", cls.names[role], check=False)
            docker("volume", "rm", "--force", cls.volumes[role], check=False)
        shutil.rmtree(cls.temp, ignore_errors=True)

    @classmethod
    def start_forgejo(cls, role):
        """Forgejo of the pinned image, with the platform's labels and settings (SQLite, nothing visible signed
        out, registration closed) on the role's own named volume, at the suite's port."""
        docker("run", "--detach", f"--name={cls.names[role]}", *(f"--label={label}" for label in LABELS),
               f"--publish=127.0.0.1:{cls.port}:3000", "--memory=1g",
               f"--volume={cls.volumes[role]}:/var/lib/gitea",
               "--env=FORGEJO__security__INSTALL_LOCK=true",
               "--env=FORGEJO__database__DB_TYPE=sqlite3",
               f"--env=FORGEJO__server__ROOT_URL={cls.url}/",
               "--env=FORGEJO__service__DISABLE_REGISTRATION=true",
               "--env=FORGEJO__service__REQUIRE_SIGNIN_VIEW=true",
               pinned_image())
        cls.container = cls.names[role]
        cls.wait_for_health()

    @classmethod
    def create_content(cls):
        """The owner, a token, and a private repository: the initial commit, a second one, the tag on it, a third
        commit (so that the tag is not simply the head), a published release on the tag and a file on it."""
        cls.admin_cli("create", "--username", OWNER, "--password", f"throwaway-{uuid.uuid4().hex}",
                      "--email", f"{OWNER}@example.com", "--admin", "--must-change-password=false")
        cls.tokens = {OWNER: cls.admin_cli("generate-access-token", "--username", OWNER, "--token-name", "tests",
                                           "--scopes", "all", "--raw").strip()}
        cls.api = {OWNER: rules.Api(cls.url + "/api/v1", cls.tokens[OWNER], "token")}
        owner = cls.api[OWNER]
        cls.repo = "shop-api"
        owner.request("POST", "/user/repos", {"name": cls.repo, "auto_init": True, "default_branch": "main",
                                              "private": True, "description": "a repository to restore"})
        cls.tagged = cls.change_file(OWNER, cls.repo, "hello.txt", "hello\n", "main")
        owner.request("POST", f"/repos/{OWNER}/{cls.repo}/tags", {"tag_name": TAG, "target": cls.tagged})
        cls.head_sha = cls.change_file(OWNER, cls.repo, "README.md", README_AFTER, "main")
        release = owner.request("POST", f"/repos/{OWNER}/{cls.repo}/releases",
                                {"tag_name": TAG, "target_commitish": cls.tagged, "name": TAG,
                                 "body": RELEASE_NOTES, "draft": False})
        cls.release_id = release["id"]
        body, content_type = multipart("attachment", ATTACHMENT, ATTACHMENT_BYTES)
        upload = urllib.request.Request(
            f"{cls.url}/api/v1/repos/{OWNER}/{cls.repo}/releases/{cls.release_id}/assets?"
            + urllib.parse.urlencode({"name": ATTACHMENT}), data=body, method="POST",
            headers={"Authorization": f"token {cls.tokens[OWNER]}", "Content-Type": content_type})
        with urllib.request.urlopen(upload, timeout=60):
            pass

    @classmethod
    def run_backup(cls):
        """scripts/backup.sh run, on a scratch HOME, with a backup.conf whose only item is `forgejo`, sending to
        a scratch directory (rsync copes with a local target: the ssh it is told about is never used)."""
        cls.home = cls.temp / "home"
        secrets = cls.temp / "secrets"
        target = cls.temp / "other-machine"
        shim = cls.temp / "bin"
        for directory in (cls.home, secrets, target, shim):
            directory.mkdir()
        (secrets / "backup.conf").write_text(f"BACKUP_TARGET={target}/\nBACKUP_KEEP=3\nforgejo\n")

        # The seam for a rootful engine (see the module's docstring): everything goes to the real docker.
        real_docker = shutil.which("docker")
        wrapper = shim / "docker"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "# Test-only: backup.sh insists on a rootless engine; this engine is not one.\n"
            "if [[ \"$*\" == 'info --format {{json .SecurityOptions}}' ]]; then\n"
            "  echo '[\"name=seccomp,profile=builtin\",\"name=rootless\"]'\n"
            "  exit 0\n"
            "fi\n"
            f"exec {real_docker} \"$@\"\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)

        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("YGG_", "BACKUP_", "XDG_RUNTIME_DIR"))}
        environment.update(PATH=f"{shim}{os.pathsep}{os.environ['PATH']}", HOME=str(cls.home),
                           YGG_SECRETS_DIR=str(secrets), DOCKER_HOST=f"unix://{engine_socket()}")
        done = subprocess.run(["bash", str(BACKUP_SH), "run"], env=environment, capture_output=True, text=True,
                              timeout=600, stdin=subprocess.DEVNULL)
        if done.returncode:
            raise AssertionError(f"backup.sh run failed ({done.returncode}):\n{done.stdout}{done.stderr}")
        cls.backup_output = done.stdout + done.stderr
        days = sorted(path for path in (cls.home / "backups").iterdir() if path.is_dir())
        if len(days) != 1:
            raise AssertionError(f"expected one day in {cls.home / 'backups'}, found {days}")
        cls.day = days[0]
        cls.sent_to_other_machine = target

    @classmethod
    def restore_from_the_document(cls):
        """docs/hosts.md's steps, as written, in one bash: the volume and the day are the only things changed
        (the document says `yggdrasil_forgejo-data` and `$HOME/restore/2026-10-22`), and `platform.sh up` is
        left out. `docker stop "$forgejo"` finds the container by Compose's labels, as it is written: the
        restored one is the only one that has them."""
        script = restore_script()
        script = replaced_once(script, "cd /opt/yggdrasil && scripts/platform.sh up",
                               "true  # platform.sh up: the test started the Forgejo it would have")
        script = replaced_once(script, "yggdrasil_forgejo-data:", f"{cls.volumes['restored']}:")
        script = replaced_once(script, '"$HOME/restore/2026-10-22"', f"'{cls.day}'")
        done = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True,
                              timeout=600, stdin=subprocess.DEVNULL, env={**os.environ, "HOME": str(cls.home)})
        if done.returncode:
            raise AssertionError(f"the restore steps of docs/hosts.md failed ({done.returncode}):\n{script}\n"
                                 f"{done.stdout}{done.stderr}")
        cls.restore_output = done.stdout
        cls.wait_for_health()

    def fetch(self, path_or_url):
        """The bytes at an API path (or a full URL) of the restored Forgejo, as the owner."""
        url = path_or_url if path_or_url.startswith("http") else f"{self.url}/api/v1{path_or_url}"
        request = urllib.request.Request(url, headers={"Authorization": f"token {self.tokens[OWNER]}"})
        with urllib.request.urlopen(request, timeout=30) as answer:
            return answer.read()

    # --- What backup.sh did ------------------------------------------------------------------------

    def test_given_a_running_forgejo_when_backup_runs_then_the_day_has_a_whole_dump_and_it_was_sent(self):
        self.assertIn("backup: forgejo", self.backup_output)
        self.assertIn("backup: done", self.backup_output)
        dump = self.day / "forgejo.zip"
        self.assertTrue(dump.is_file(), list(self.day.iterdir()))
        self.assertFalse((self.day / "forgejo.zip.partial").exists())
        self.assertGreater(dump.stat().st_size, 0)
        self.assertEqual((self.sent_to_other_machine / self.day.name / "forgejo.zip").stat().st_size,
                         dump.stat().st_size)

    # --- What came back ----------------------------------------------------------------------------

    def test_given_the_restore_steps_when_followed_then_forgejo_is_healthy_and_the_old_token_works(self):
        # The last step of the document prints the container's status.
        self.assertIn("Up", self.restore_output)
        who = self.api[OWNER].request("GET", "/user")
        self.assertEqual((who["login"], who["is_admin"]), (OWNER, True))

    def test_given_the_restore_when_read_then_the_repository_is_back(self):
        repo = self.api[OWNER].request("GET", f"/repos/{OWNER}/{self.repo}")
        self.assertEqual((repo["private"], repo["default_branch"], repo["description"]),
                         (True, "main", "a repository to restore"))
        self.assertEqual(self.head(self.repo, "main"), self.head_sha)
        self.assertEqual(self.fetch(f"/repos/{OWNER}/{self.repo}/raw/README.md?ref=main"), README_AFTER.encode())

    def test_given_the_restore_when_read_then_the_tag_points_to_the_same_commit(self):
        tag = self.api[OWNER].request("GET", f"/repos/{OWNER}/{self.repo}/tags/{TAG}")
        self.assertEqual(tag["commit"]["sha"], self.tagged)
        # The tag is on the second commit, not the head: a restore that re-made tags at the head would fail.
        self.assertNotEqual(self.tagged, self.head_sha)

    def test_given_the_restore_when_read_then_the_release_and_its_file_are_back(self):
        release = self.api[OWNER].request("GET", f"/repos/{OWNER}/{self.repo}/releases/tags/{TAG}")
        self.assertEqual((release["id"], release["name"], release["body"], release["draft"], release["tag_name"]),
                         (self.release_id, TAG, RELEASE_NOTES, False, TAG))
        assets = self.api[OWNER].request("GET", f"/repos/{OWNER}/{self.repo}/releases/{self.release_id}/assets")
        self.assertEqual([(a["name"], a["size"]) for a in assets], [(ATTACHMENT, len(ATTACHMENT_BYTES))])
        # The bytes themselves, from the data directory the dump carried (a nothing-signed-out server: a token).
        self.assertEqual(self.fetch(assets[0]["browser_download_url"]), ATTACHMENT_BYTES)

    def test_given_the_restore_when_cloned_over_http_then_the_commits_and_the_tag_come(self):
        clone = self.clone(OWNER, self.repo)
        self.assertEqual(self.git(OWNER, "rev-parse", "HEAD", cwd=clone).stdout.strip(), self.head_sha)
        self.assertEqual(self.git(OWNER, "rev-parse", f"{TAG}^{{commit}}", cwd=clone).stdout.strip(), self.tagged)
        self.assertEqual((clone / "hello.txt").read_text(), "hello\n")
        self.assertEqual((clone / "README.md").read_text(), README_AFTER)
        # Every commit: the initial one, hello.txt's and README.md's.
        self.assertEqual(self.git(OWNER, "rev-list", "--count", "HEAD", cwd=clone).stdout.strip(), "3")

    def test_given_the_restore_when_pushed_to_then_the_repositories_hooks_work(self):
        # A push runs the repository's hooks (hooks written into each repository by the Forgejo that made it,
        # calling the Forgejo binary and the keys of the data directory): they must work on the new volume.
        clone = self.clone(OWNER, self.repo)
        (clone / "after-restore.txt").write_text("pushed after the restore\n")
        self.assertEqual(self.git(OWNER, "add", ".", cwd=clone).returncode, 0)
        self.assertEqual(self.git(OWNER, "commit", "--quiet", "-m", "after the restore", cwd=clone).returncode, 0)
        pushed = self.git(OWNER, "push", "origin", "HEAD:refs/heads/after-restore", cwd=clone)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertTrue(self.branch_exists(self.repo, "after-restore"))


if __name__ == "__main__":
    unittest.main()
