"""The pipeline end to end: a real Jenkins controller, its CI agent and Forgejo, in containers.

    YGG_JENKINS_TESTS=1 python3 -m unittest discover -s scripts -p test_jenkins_live.py -v

test_jenkins_config.py and test_pipeline_library.py read the Jenkins configuration and the library; this
runs them. It is the first place the configuration as code (platform/jenkins/controller/casc*.yaml), the
Job DSL, agents.groovy, the default Jenkinsfile and the library (jenkins/library) meet the plugins they
were written for, and the place their answers about the plugins are pinned.

What it starts, on a Docker network of its own, every container named ygg-pipeline-test-*-<run>:

  forgejo   the image platform/compose.yml pins, served over HTTPS (catalog.py accepts only an https
            forge URL) with a certificate of a throwaway CA the other containers trust; users
            artur-rios (the owner), jenkins (forge.jenkinsUser, the write token), jenkins-ci (the read
            token) and claude (a developer)
  jenkins   the controller built from platform/jenkins/controller, with the production casc files,
            Jenkinsfile.default and agents.groovy mounted as compose.yml mounts them, a test catalog,
            and casc.test.yaml (the one credential a check may bind). Booted twice on one
            JENKINS_HOME: first with the Forgejo-only set of casc files and a ci work directory that
            is wrong, then with the mirror set (casc.github-app.yaml, a dummy GitHub App key) and the
            right one, so the second boot proves the merge of both sets and the ci node's move
  ci        platform/jenkins/build-agent, as build-agent.yml runs it: root, the engine's socket, the
  release   work directory at the same path inside and on the host
  vps       the deploy agent of the test catalog's one environment (the build agent image: the
            yggdrasil repository on Forgejo replaces scripts/deploy.sh with a stand-in that prints its
            arguments, so a deploy is a line in the log)

The library is this checkout's jenkins/library, served from the Forgejo repository artur-rios/yggdrasil
at the tag v0.0.0-live (YGGDRASIL_LIBRARY_VERSION), next to this checkout's scripts/. rules.py applies
the protections and the webhooks to the controller (http://jenkins:8080/gitea-webhook/post), so pull
requests and pushes reach Jenkins the way they will in production.

Differences from production worth knowing when reading a result: the engine here is not rootless (the
agents' root is the machine's root, and a check's TESTCONTAINERS_HOST_OVERRIDE is the agent's gateway on
a plain bridge network, not a rootless network namespace); Jenkins is plain HTTP on the test network;
there is one engine for the agents and the checks' containers. The deploy agent runs the build agent's
image and deploy.sh is a stand-in. The engine must see this machine's paths as they are (the ci agent's
work directory is a temporary directory mounted at the same path, as in production): a native engine
(Linux, WSL's own dockerd), not one in a VM that translates paths only for its own CLI.

Skipped unless YGG_JENKINS_TESTS=1 (and Docker, git, openssl and bash are there). Building the controller
image needs the internet (the plugins); the images ygg-pipeline-test-controller:local and
ygg-pipeline-test-build-agent:local are kept as a build cache, everything else -- containers, the
network, the JENKINS_HOME volume, the CA-trusting images of the run -- is removed at the end, also
when the setup fails (class cleanups, and atexit for an interrupted run). YGG_JENKINS_KEEP=1 leaves
them running for a look after a failure (the run prints how to remove them). Every password, token and
key is a throwaway value of that run. CI runs it by hand only (job pipeline-live, workflow_dispatch).
"""

import atexit
import base64
import hashlib
import dataclasses
import http.cookiejar
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yaml

import catalog as catalog_module
import rules
from test_forgejo_live import CLAUDE, JENKINS, OWNER, ForgejoClient, docker, free_port, pinned_image

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER_DIR = ROOT / "platform" / "jenkins" / "controller"
PREFIX = "ygg-pipeline-test"
CONTROLLER_IMAGE = f"{PREFIX}-controller:local"
AGENT_IMAGE = f"{PREFIX}-build-agent:local"

JENKINS_CI = "jenkins-ci"
LIBRARY_VERSION = "v0.0.0-live"
WEBHOOK_SECRET = "a-throwaway-webhook-secret"
DB_PASSWORD = "a-throwaway-db-password"
FORGE_URL = "https://forgejo:3000"
JENKINS_URL = "http://jenkins:8080/"

# The repositories: pull requests (with yggdrasil.yml on their branches), pushes with yggdrasil.yml on
# develop, a repository that never has one, and an owner-tagged library.
APP, WEB, PLAIN, LIB = "shop-api", "shop-web", "plain-site", "ygg-lib"
# Pushes to develop whose check is slow when the commit carries a file named `slow`: the window in which
# develop moves on during a build.
WORKER = "shop-worker"

# A check's command whose output differs from its text: the shell trace (sh -x) prints the command, so
# the marker in the log proves the check ran, not that it was planned.
UNIT_RAN = "UNIT-42-RAN"
UNIT = {"image": "alpine:3", "run": 'echo "UNIT-$((40 + 2))-RAN"'}

HOSTILE_JENKINSFILE = """\
// A branch's own Jenkinsfile: Jenkins must never run it (casc.yaml, globalConfigFiles).
echo 'HOSTILE-JENKINSFILE'
node { sh 'echo HOSTILE-JENKINSFILE-SH' }
"""

DEPLOY_STUB = """\
#!/usr/bin/env bash
# test_jenkins_live.py's stand-in for scripts/deploy.sh: the pipeline's side of a deploy is what is
# under test, deploy.sh has its own tests.
echo "DEPLOY-STUB environment=$1 stack=$2 checkout=$3 version=$4 inputs=${DEPLOY_INPUTS_DIR:-}"
"""


def test_catalog():
    """The installation under test: one branch environment on the agent vps; shop-api requires the
    check unit and may bind shop-db-password; shop-web requires unit; plain-site has no checks; ygg-lib
    is tagged by its owner."""
    return {
        "owner": OWNER,
        "repository": "yggdrasil",
        "forge": {"type": "forgejo", "url": FORGE_URL, "jenkinsUser": JENKINS, "developers": [CLAUDE]},
        "environments": [{"id": "development", "name": "Development", "trigger": "branch", "branches": "develop",
                          "agent": "vps"}],
        "systems": [{"id": "shop", "name": "Shop", "applications": [
            {"id": APP, "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz", "checks": ["unit"],
             "credentials": ["shop-db-password"]},
            {"id": WEB, "name": "Web", "kind": "web", "health": "http://shop-web:8080/", "checks": ["unit"]},
            {"id": PLAIN, "name": "Site", "kind": "web", "health": "http://plain-site:8080/"},
            {"id": WORKER, "name": "Worker", "kind": "worker", "health": "http://shop-worker:8080/"},
        ]}],
        "repositories": [{"id": LIB, "release": "owner-tag", "tags": "v*"}],
    }


def pipeline_file(**checks):
    return yaml.safe_dump({"checks": checks}, sort_keys=False)


def _can_run():
    return (os.environ.get("YGG_JENKINS_TESTS") == "1" and os.name == "posix"
            and all(shutil.which(tool) for tool in ("docker", "bash", "git", "openssl")))


def openssl(*arguments, cwd):
    subprocess.run(["openssl", *arguments], cwd=cwd, check=True, capture_output=True, timeout=120)


def make_certificates(directory):
    """A throwaway CA and Forgejo's certificate from it (forgejo on the network, 127.0.0.1 for the
    tests); the GitHub App's dummy key (PKCS#8, as casc.github-app.yaml wants)."""
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "2",
            "-subj", "/CN=ygg-pipeline-test CA", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=directory)
    (directory / "forgejo.ext").write_text("subjectAltName=DNS:forgejo,IP:127.0.0.1\n"
                                           "extendedKeyUsage=serverAuth\n")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "forgejo.key", "-out", "forgejo.csr",
            "-subj", "/CN=forgejo", cwd=directory)
    openssl("x509", "-req", "-in", "forgejo.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "forgejo.crt", "-days", "2", "-extfile", "forgejo.ext", cwd=directory)
    openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", "github-app.pem", cwd=directory)
    for name in ("forgejo.key", "forgejo.crt", "ca.crt", "github-app.pem"):
        (directory / name).chmod(0o644)  # read by the containers' own users


@dataclasses.dataclass
class Build:
    job: str
    branch: str
    number: int
    result: str
    url: str
    console: str


class JenkinsApi:
    """The controller's REST API as the administrator: a session (Jenkins ties the CSRF crumb to it),
    the crumb on every POST, and the script console for what the REST API does not show."""

    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.crumb = None

    def request(self, method, path, data=None, content_type="application/x-www-form-urlencoded"):
        """(status, text) of a request; never raises for an HTTP status."""
        headers = {"Authorization": self.authorization}
        if method != "GET":
            if self.crumb is None:
                status, text = self.request("GET", "/crumbIssuer/api/json")
                if status != 200:
                    raise AssertionError(f"no CSRF crumb: {status} {text[:300]}")
                answer = json.loads(text)
                self.crumb = (answer["crumbRequestField"], answer["crumb"])
            headers[self.crumb[0]] = self.crumb[1]
            if data is not None:
                headers["Content-Type"] = content_type
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=60) as answer:
                return answer.status, answer.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as error:
            return 0, str(error)

    def json(self, path):
        status, text = self.request("GET", path)
        if status == 404:
            return None
        if status != 200:
            raise AssertionError(f"GET {path}: {status} {text[:500]}")
        return json.loads(text)

    def post(self, path, fields=None):
        status, text = self.request("POST", path, urllib.parse.urlencode(fields or {}).encode())
        if status not in (200, 201, 302):
            raise AssertionError(f"POST {path}: {status} {text[:500]}")
        return text

    def script(self, groovy):
        """The script console's output (println) of a Groovy script run as the administrator."""
        status, text = self.request("POST", "/scriptText", urllib.parse.urlencode({"script": groovy}).encode())
        if status != 200:
            raise AssertionError(f"script console: {status} {text[:1000]}")
        return text

    def script_json(self, groovy_expression):
        text = self.script(f"println(groovy.json.JsonOutput.toJson({groovy_expression}))")
        try:
            return json.loads(text)
        except ValueError:
            raise AssertionError(f"script console answered:\n{text[:3000]}") from None


def job_path(job, branch=None):
    path = f"/job/{urllib.parse.quote(job, safe='')}"
    if branch is not None:
        path += f"/job/{urllib.parse.quote(branch, safe='')}"
    return path


@unittest.skipUnless(_can_run(), "set YGG_JENKINS_TESTS=1 to run Jenkins, its agents and Forgejo in containers "
                                 "(needs Docker, git, openssl and bash on a POSIX system, and the internet to build "
                                 "the controller image)")
class JenkinsLiveTests(ForgejoClient, unittest.TestCase):
    """One installation for the whole class. Every test works on branches and pull requests of its
    own; the boot tests read what setUpClass recorded about the two boots."""

    @classmethod
    def setUpClass(cls):
        cls.run_id = uuid.uuid4().hex[:8]
        cls.names = {role: f"{PREFIX}-{role}-{cls.run_id}" for role in ("forgejo", "jenkins", "ci", "release", "vps")}
        cls.network = f"{PREFIX}-{cls.run_id}"
        cls.home_volume = f"{PREFIX}-jenkins-home-{cls.run_id}"
        cls.test_images = [f"{PREFIX}-controller:run-{cls.run_id}", f"{PREFIX}-build-agent:run-{cls.run_id}"]
        cls.temp = pathlib.Path(tempfile.mkdtemp(prefix=f"{PREFIX}-"))
        # Everything is removed by one idempotent function, both as a class cleanup (also when
        # setUpClass fails halfway) and at exit (an interrupted run).
        cls.addClassCleanup(cls.remove_everything)
        atexit.register(cls.remove_everything)

        cls.certificates = cls.temp / "certificates"
        cls.certificates.mkdir()
        make_certificates(cls.certificates)
        # The tests' own HTTPS calls (rules.Api, urllib) and git trust the CA as well as the machine's.
        bundle = cls.temp / "ca-bundle.crt"
        system = pathlib.Path("/etc/ssl/certs/ca-certificates.crt")
        bundle.write_text((system.read_text() if system.exists() else "") + (cls.certificates / "ca.crt").read_text())
        cls.previous_ssl_cert_file = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(bundle)
        cls.git_ca = cls.certificates / "ca.crt"

        docker("network", "create", cls.network)
        cls.build_images()
        cls.start_forgejo()
        cls.create_users()
        cls.push_yggdrasil()
        cls.write_controller_files()

        # Boot 1: the Forgejo-only set, and a ci work directory that is not the agent's.
        cls.ci_workdir = cls.temp / "agents" / "ci"
        cls.release_workdir = cls.temp / "agents" / "release"
        for directory in (cls.ci_workdir, cls.release_workdir):
            directory.mkdir(parents=True)
        cls.start_controller(casc=["casc.yaml", "casc.forgejo.yaml", "casc.test.yaml"],
                             ci_workdir=cls.temp / "agents" / "ci-before")
        cls.first_boot = cls.inspect_controller()
        docker("rm", "--force", cls.names["jenkins"], timeout=120)

        # Boot 2, on the same JENKINS_HOME: the mirror set, and the right ci work directory.
        cls.start_controller(casc=["casc.yaml", "casc.forgejo.yaml", "casc.github-app.yaml", "casc.test.yaml"],
                             ci_workdir=cls.ci_workdir)
        cls.second_boot = cls.inspect_controller()
        cls.start_agents()

        cls.create_repositories()
        cls.apply_rules()
        cls.first_builds = {}
        for job in (APP, WEB, PLAIN, LIB, WORKER):
            cls.jenkins.post(job_path(job) + "/build", {"delay": "0"})
        for job in (APP, WEB, PLAIN, LIB, WORKER):
            for branch in ("develop", "main"):
                cls.first_builds[(job, branch)] = cls.wait_for_build(job, branch, after=0, timeout=900)

    # --- Containers -------------------------------------------------------------------------------

    @classmethod
    def remove_everything(cls):
        if getattr(cls, "removed", False):
            return
        if os.environ.get("YGG_JENKINS_KEEP") == "1":
            cls.removed = True
            print(f"\nYGG_JENKINS_KEEP=1: left running. Remove with:\n  docker rm -f $(docker ps -aq --filter "
                  f"name={cls.run_id}); docker volume rm {cls.home_volume}; docker network rm {cls.network}\n"
                  f"  Jenkins: {getattr(cls, 'jenkins_url', '?')} (admin / {getattr(cls, 'admin_password', '?')}), "
                  f"Forgejo: {getattr(cls, 'url', '?')}, files: {cls.temp}")
            return
        cls.removed = True
        listed = docker("ps", "--all", "--filter", f"name={PREFIX}-", "--format", "{{.Names}}", check=False)
        ours = [name for name in listed.stdout.split() if name.startswith(PREFIX) and name.endswith(cls.run_id)]
        if ours:
            docker("rm", "--force", "--volumes", *ours, check=False, timeout=300)
        docker("volume", "rm", "--force", cls.home_volume, check=False)
        docker("network", "rm", cls.network, check=False)
        docker("image", "rm", "--force", *cls.test_images, check=False)
        if cls.temp.exists():
            # The agents wrote their work directories as root.
            docker("run", "--rm", f"--volume={cls.temp}:{cls.temp}", "alpine:3", "chown", "-R",
                   f"{os.getuid()}:{os.getgid()}", str(cls.temp), check=False)
            shutil.rmtree(cls.temp, ignore_errors=True)
        if cls.previous_ssl_cert_file is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = cls.previous_ssl_cert_file

    previous_ssl_cert_file = None

    @classmethod
    def build_images(cls):
        """The production images (cached between runs), then for this run the same with the CA trusted
        by Java (the controller's Gitea client) and by the system (git, curl)."""
        for image, context in ((CONTROLLER_IMAGE, "controller"), (AGENT_IMAGE, "build-agent")):
            docker("build", "--quiet", "--tag", image, str(ROOT / "platform" / "jenkins" / context), timeout=1800)
        shutil.copy(cls.certificates / "ca.crt", cls.temp / "ca.crt")
        for base, image in zip((CONTROLLER_IMAGE, AGENT_IMAGE), cls.test_images):
            user = "jenkins"
            (cls.temp / "Dockerfile").write_text(
                f"FROM {base}\nUSER root\nCOPY ca.crt /usr/local/share/ca-certificates/ygg-pipeline-test.crt\n"
                "RUN update-ca-certificates && keytool -importcert -noprompt -cacerts -storepass changeit "
                "-alias ygg-pipeline-test -file /usr/local/share/ca-certificates/ygg-pipeline-test.crt\n"
                f"USER {user}\n")
            docker("build", "--quiet", "--tag", image, "--file", str(cls.temp / "Dockerfile"), str(cls.temp), timeout=600)

    @classmethod
    def start_forgejo(cls):
        port = free_port()
        cls.url = f"https://127.0.0.1:{port}"
        cls.container = cls.names["forgejo"]
        docker("run", "--detach", f"--name={cls.container}", f"--network={cls.network}", "--network-alias=forgejo",
               f"--publish=127.0.0.1:{port}:3000", "--memory=1g",
               f"--volume={cls.certificates}:/certs:ro",
               "--env=FORGEJO__security__INSTALL_LOCK=true",
               "--env=FORGEJO__database__DB_TYPE=sqlite3",
               "--env=FORGEJO__server__PROTOCOL=https",
               "--env=FORGEJO__server__CERT_FILE=/certs/forgejo.crt",
               "--env=FORGEJO__server__KEY_FILE=/certs/forgejo.key",
               # The address the webhooks' payloads carry: what the Gitea plugin matches its sources by.
               f"--env=FORGEJO__server__ROOT_URL={FORGE_URL}/",
               "--env=FORGEJO__service__DISABLE_REGISTRATION=true",
               "--env=FORGEJO__service__REQUIRE_SIGNIN_VIEW=true",
               # Jenkins is on the same private network: Forgejo delivers only to external hosts by default.
               "--env=FORGEJO__webhook__ALLOWED_HOST_LIST=private",
               pinned_image())
        cls.wait_for_health()

    @classmethod
    def create_users(cls):
        cls.tokens = {}
        scopes = {JENKINS_CI: "read:repository"}
        for user in (OWNER, JENKINS, JENKINS_CI, CLAUDE):
            cls.admin_cli("create", "--username", user, "--password", f"throwaway-{uuid.uuid4().hex}",
                          "--email", f"{user}@example.com", *(["--admin"] if user == OWNER else []),
                          "--must-change-password=false")
            cls.tokens[user] = cls.admin_cli("generate-access-token", "--username", user, "--token-name", "tests",
                                             "--scopes", scopes.get(user, "all"), "--raw").strip()
        cls.api = {user: rules.Api(cls.url + "/api/v1", token, "token") for user, token in cls.tokens.items()}

    @classmethod
    def new_repository(cls, repo, readers=(JENKINS_CI,), writers=(JENKINS, CLAUDE), init=True):
        owner = cls.api[OWNER]
        owner.request("POST", "/user/repos", {"name": repo, "auto_init": init, "default_branch": "main",
                                              "private": True})
        for user in writers:
            owner.request("PUT", f"/repos/{OWNER}/{repo}/collaborators/{user}", {"permission": "write"})
        for user in readers:
            owner.request("PUT", f"/repos/{OWNER}/{repo}/collaborators/{user}", {"permission": "read"})

    @classmethod
    def push_yggdrasil(cls):
        """artur-rios/yggdrasil: this checkout's library and scripts (deploy.sh replaced by the stand-in)
        and the test catalog, on main and at the library's tag."""
        cls.new_repository("yggdrasil", writers=(), init=False)
        tree = cls.temp / "yggdrasil"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "scripts", tree / "scripts", ignore=ignore)
        shutil.copytree(ROOT / "jenkins", tree / "jenkins", ignore=ignore)
        (tree / "scripts" / "deploy.sh").write_text(DEPLOY_STUB)
        (tree / "catalog.yaml").write_text(yaml.safe_dump(test_catalog(), sort_keys=False))
        for arguments in (("init", "--quiet", "--initial-branch=main"), ("add", "--all"),
                          ("commit", "--quiet", "--message", "yggdrasil under test"),
                          ("tag", LIBRARY_VERSION),
                          ("push", "--quiet", f"{cls.url}/{OWNER}/yggdrasil.git", "main", LIBRARY_VERSION)):
            done = cls.git(OWNER, *arguments, cwd=tree)
            if done.returncode:
                raise AssertionError(f"git {arguments[0]}: {done.stderr}")

    @classmethod
    def write_controller_files(cls):
        files = cls.temp / "controller"
        files.mkdir()
        (files / "catalog.yaml").write_text(yaml.safe_dump(test_catalog(), sort_keys=False))
        # As env/platform.env.example says (printf '%s'): no trailing newline -- but the webhook secret,
        # which nothing authenticates with, has one, to see what ${readFile:} does with it.
        (files / "forge-write-token").write_text(cls.tokens[JENKINS])
        (files / "forge-read-token").write_text(cls.tokens[JENKINS_CI])
        (files / "forge-webhook-secret").write_text(WEBHOOK_SECRET + "\n")
        # The credential a check of shop-api may bind (the catalog's `credentials`), in a casc file of
        # its own: a third domainCredentials list for Configuration as Code to merge.
        (files / "casc.test.yaml").write_text(yaml.safe_dump({"credentials": {"system": {"domainCredentials": [
            {"credentials": [{"string": {"scope": "GLOBAL", "id": "shop-db-password", "secret": DB_PASSWORD}}]}]}}}))
        for name in os.listdir(files):
            (files / name).chmod(0o644)  # read by the controller's jenkins user
        cls.controller_files = files
        cls.admin_password = f"throwaway-{uuid.uuid4().hex}"

    @classmethod
    def start_controller(cls, casc, ci_workdir):
        """The controller as platform/compose.yml runs it (the same environment and mounts), on the
        run's JENKINS_HOME volume; waits until it has finished starting."""
        port = getattr(cls, "jenkins_port", None) or free_port()
        cls.jenkins_port = port
        cls.jenkins_url = f"http://127.0.0.1:{port}"
        mounts = [(CONTROLLER_DIR / name, f"/etc/jenkins/{name}")
                  for name in ("casc.yaml", "casc.forgejo.yaml", "casc.github.yaml", "casc.github-app.yaml",
                               "casc.github-read.yaml")]
        mounts += [
            (cls.controller_files / "casc.test.yaml", "/etc/jenkins/casc.test.yaml"),
            (ROOT / "jenkins" / "Jenkinsfile.default", "/etc/jenkins/Jenkinsfile.default"),
            (cls.controller_files / "catalog.yaml", "/etc/jenkins/catalog.yaml"),
            (CONTROLLER_DIR / "init.groovy.d" / "agents.groovy", "/var/jenkins_home/init.groovy.d/agents.groovy"),
            (cls.certificates / "github-app.pem", "/run/secrets/github-app.pem"),
            (pathlib.Path("/dev/null"), "/run/secrets/github-read.pem"),
            (cls.controller_files / "forge-write-token", "/run/secrets/forge-write-token"),
            (cls.controller_files / "forge-read-token", "/run/secrets/forge-read-token"),
            (cls.controller_files / "forge-webhook-secret", "/run/secrets/forge-webhook-secret"),
        ]
        environment = {
            "JAVA_OPTS": "-Djenkins.install.runSetupWizard=false",
            "CASC_JENKINS_CONFIG": ",".join(f"/etc/jenkins/{name}" for name in casc),
            "JENKINS_URL": JENKINS_URL,
            "JENKINS_ADMIN_ID": "admin",
            "JENKINS_ADMIN_PASSWORD": cls.admin_password,
            "GITHUB_OWNER": OWNER,
            "YGGDRASIL_REPOSITORY": "yggdrasil",
            "GITHUB_APP_ID": "123456",
            "GITHUB_READ_APP_ID": "",
            "CI_AGENT_WORKDIR": str(ci_workdir),
            "RELEASE_AGENT_WORKDIR": str(cls.release_workdir),
            "FORGE_URL": FORGE_URL,
            "FORGE_JENKINS_USER": JENKINS,
            "YGGDRASIL_LIBRARY_VERSION": LIBRARY_VERSION,
        }
        docker("run", "--detach", f"--name={cls.names['jenkins']}", f"--network={cls.network}",
               "--network-alias=jenkins", f"--publish=127.0.0.1:{port}:8080", "--memory=3g",
               *[f"--env={key}={value}" for key, value in environment.items()],
               *[f"--volume={source}:{target}:ro" for source, target in mounts],
               f"--volume={cls.home_volume}:/var/jenkins_home",
               cls.test_images[0])
        cls.jenkins = JenkinsApi(cls.jenkins_url, "admin", cls.admin_password)
        deadline = time.monotonic() + 600
        last = None
        while time.monotonic() < deadline:
            status, text = cls.jenkins.request("GET", "/whoAmI/api/json")
            last = (status, text[:500])
            if status == 200:
                try:
                    level = cls.jenkins.script("println(jenkins.model.Jenkins.get().initLevel.name())").strip()
                    last = level
                    if level == "COMPLETED":
                        return
                except AssertionError as error:
                    last = str(error)[:1000]
            running = docker("inspect", "--format", "{{.State.Running}}", cls.names["jenkins"], check=False)
            if running.stdout.strip() != "true":
                break
            time.sleep(3)
        raise AssertionError(f"Jenkins did not start (last answer: {last}):\n{cls.controller_log()}")

    @classmethod
    def controller_log(cls, lines=150):
        logs = docker("logs", f"--tail={lines}", cls.names["jenkins"], check=False)
        return logs.stdout + logs.stderr

    @classmethod
    def inspect_controller(cls):
        """What a boot made of the configuration: jobs and their source and factory, credentials, the
        Gitea servers, the library, the nodes, and the plugins' versions."""
        api = cls.jenkins
        return {
            "jobs": api.script_json(
                "jenkins.model.Jenkins.get().getAllItems(org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject)"
                ".collectEntries { [(it.fullName): [sources: it.SCMSources.collect { s -> s.getClass().name }, "
                "factory: it.projectFactory.getClass().name]] }"),
            "credentials": api.script_json(
                "com.cloudbees.plugins.credentials.CredentialsProvider.lookupCredentials("
                "com.cloudbees.plugins.credentials.common.StandardCredentials, jenkins.model.Jenkins.get(), "
                "hudson.security.ACL.SYSTEM, []).collectEntries { [(it.id): it.getClass().simpleName] }"),
            "webhook_secret_has_newline": api.script_json(
                "com.cloudbees.plugins.credentials.CredentialsProvider.lookupCredentials("
                "org.jenkinsci.plugins.plaincredentials.StringCredentials, jenkins.model.Jenkins.get(), "
                "hudson.security.ACL.SYSTEM, []).find { it.id == 'forge-webhook' }.secret.plainText.endsWith('\\n')"),
            "gitea_servers": api.script_json(
                "org.jenkinsci.plugin.gitea.servers.GiteaServers.get().servers.collect { "
                "[serverUrl: it.serverUrl, manageHooks: it.manageHooks, credentialsId: it.credentialsId] }"),
            "libraries": api.script_json(
                "org.jenkinsci.plugins.workflow.libs.GlobalLibraries.get().libraries.collect { "
                "[name: it.name, version: it.defaultVersion, scm: it.retriever.scm.getClass().name, "
                "remote: it.retriever.scm.remote, credentials: it.retriever.scm.credentialsId] }"),
            "nodes": api.script_json(
                "jenkins.model.Jenkins.get().nodes.collectEntries { [(it.nodeName): "
                "[remoteFS: it.remoteFS, executors: it.numExecutors, labels: it.labelString]] }"),
            "plugins": api.script_json(
                "jenkins.model.Jenkins.get().pluginManager.plugins.collectEntries { [(it.shortName): it.version] }"),
            "version": api.script("println(jenkins.model.Jenkins.VERSION)").strip(),
        }

    @classmethod
    def start_agents(cls):
        """ci and release as platform/build-agent.yml runs them, vps as a deploy agent; waits until all
        three are online."""
        socket_path = "/var/run/docker.sock"
        if os.environ.get("DOCKER_HOST", "").startswith("unix://"):
            socket_path = os.environ["DOCKER_HOST"][len("unix://"):]
        for name, workdir in (("ci", cls.ci_workdir), ("release", cls.release_workdir), ("vps", None)):
            secret = cls.jenkins.script(f"println(jenkins.model.Jenkins.get().getComputer('{name}').jnlpMac)").strip()
            arguments = ["run", "--detach", f"--name={cls.names[name]}", f"--network={cls.network}", "--init",
                         "--user=0:0", "--env=JENKINS_URL=http://jenkins:8080", "--env=JENKINS_WEB_SOCKET=true",
                         f"--env=JENKINS_AGENT_NAME={name}", f"--env=JENKINS_SECRET={secret}"]
            if workdir is None:
                arguments += ["--env=JENKINS_AGENT_WORKDIR=/home/jenkins/agent"]
            else:
                arguments += [f"--env=JENKINS_AGENT_WORKDIR={workdir}", f"--env=YGG_DOCKER_SOCKET={socket_path}",
                              f"--volume={socket_path}:/var/run/docker.sock", f"--volume={workdir}:{workdir}"]
            docker(*arguments, cls.test_images[1])
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            online = cls.jenkins.script_json(
                "['ci', 'release', 'vps'].collectEntries { [(it): jenkins.model.Jenkins.get().getComputer(it)?.online] }")
            if all(online.values()):
                return
            time.sleep(2)
        logs = docker("logs", "--tail=40", cls.names["ci"], check=False)
        raise AssertionError(f"agents not online: {online}\n{logs.stdout}{logs.stderr}")

    @classmethod
    def create_repositories(cls):
        """shop-api, shop-web, plain-site and ygg-lib: main and develop. shop-web has yggdrasil.yml on
        develop (unit, only for changes under src/)."""
        for repo in (APP, WEB, PLAIN, LIB, WORKER):
            cls.new_repository(repo)
            cls.api[OWNER].request("POST", f"/repos/{OWNER}/{repo}/branches",
                                   {"new_branch_name": "develop", "old_branch_name": "main"})
        cls.change_file(OWNER, WEB, "yggdrasil.yml",
                        pipeline_file(unit={**UNIT, "run": 'echo "WEB-UNIT-$((40 + 2))-RAN"', "paths": ["src/**"]}),
                        "develop")
        cls.change_file(OWNER, WORKER, "yggdrasil.yml", pipeline_file(unit={
            "image": "alpine:3",
            "run": 'if [ -f slow ]; then echo "SLOW-$((1 + 1))-CHECK"; sleep 60; fi; echo "WORKER-$((40 + 2))-RAN"'}),
            "develop")

    @classmethod
    def apply_rules(cls):
        webhook = {"url": JENKINS_URL + "gitea-webhook/post", "secret": WEBHOOK_SECRET}
        for r in rules.desired_rules(test_catalog()):
            lines = rules.apply_forgejo(cls.api[OWNER], OWNER, r, webhook, False)
            warnings = [line for line in lines if "warning" in line]
            if warnings:
                raise AssertionError(f"rules for {r.repo}: {warnings}")

    # --- Jenkins ----------------------------------------------------------------------------------

    @classmethod
    def builds(cls, job, branch):
        answer = cls.jenkins.json(job_path(job, branch) + "/api/json?tree=builds[number,result,building,url]")
        return [] if answer is None else answer["builds"]

    @classmethod
    def console(cls, job, branch, number):
        status, text = cls.jenkins.request("GET", f"{job_path(job, branch)}/{number}/consoleText")
        return text if status == 200 else ""

    @classmethod
    def wait_for_build(cls, job, branch, after, timeout=600):
        """The first build of job/branch numbered above `after`, once finished."""
        deadline = time.monotonic() + timeout
        found = None
        while time.monotonic() < deadline:
            newer = [b for b in cls.builds(job, branch) if b["number"] > after]
            if newer:
                found = min(newer, key=lambda b: b["number"])
                if not found["building"] and found["result"] is not None:
                    return Build(job, branch, found["number"], found["result"], found["url"],
                                 cls.console(job, branch, found["number"]))
            time.sleep(3)
        console = cls.console(job, branch, found["number"]) if found else "(no build)"
        queue = cls.jenkins.json("/queue/api/json?tree=items[why,task[name]]")
        raise AssertionError(f"{job}/{branch}: no finished build after #{after} in {timeout} s; queue: {queue}\n"
                             f"{console[-6000:]}")

    def last_number(self, job, branch):
        return max((b["number"] for b in self.builds(job, branch)), default=0)

    def rebuild(self, job, branch):
        before = self.last_number(job, branch)
        self.jenkins.post(job_path(job, branch) + "/buildWithParameters", {"DEPLOY_TO": ""})
        return self.wait_for_build(job, branch, after=before)

    # --- Forgejo ----------------------------------------------------------------------------------

    def branch_with(self, user, repo, branch, files, base="develop"):
        """A new branch from `base` with `files` ({path: text}) committed by `user`; its head."""
        sha = None
        for index, (path, text) in enumerate(files.items()):
            sha = self.change_file(user, repo, path, text, base if index == 0 else branch,
                                   new_branch=branch if index == 0 else None)
        return sha

    def land_on_develop(self, repo, name, files):
        """`files` on develop, the way the owner gets them there: a feature branch and a pull request
        merged past its checks (develop takes no direct commits, not even the owner's). The merge."""
        branch = f"feature/{name}"
        self.branch_with(OWNER, repo, branch, files)
        number = self.open_pull_request(OWNER, repo, branch, "develop")
        self.merge(OWNER, repo, number, force_merge=True)
        return self.head(repo, "develop")

    def pull_request_build(self, user, repo, branch, base="develop"):
        """Opens the pull request (the webhook tells Jenkins) and waits for its first build."""
        number = self.open_pull_request(user, repo, branch, base)
        return number, self.wait_for_build(repo, f"PR-{number}", after=0)

    def statuses(self, repo, sha):
        """Every status of the commit, oldest first: (context, state, creator, description, target)."""
        answer = self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/statuses/{sha}?sort=oldest&limit=50") or []
        return [(s["context"], s["status"], s["creator"]["login"], s["description"], s["target_url"]) for s in answer]

    def latest_statuses(self, repo, sha):
        return {status[0]: status for status in self.statuses(repo, sha)}

    def assert_no_secret(self, text, where):
        for user in (JENKINS, JENKINS_CI, OWNER, CLAUDE):
            self.assertNotIn(self.tokens[user], text, f"{user}'s token in {where}")

    # --- The two boots ----------------------------------------------------------------------------

    def test_given_the_forgejo_casc_set_when_booted_then_jobs_server_library_and_credentials_are_configured(self):
        boot = self.first_boot
        # Job DSL's giteaSCMSource and pipelineBranchDefaultsProjectFactory: one job per catalog entry,
        # each reading Forgejo and running the Jenkinsfile kept in Jenkins.
        self.assertEqual(set(boot["jobs"]), {APP, WEB, PLAIN, LIB, WORKER})
        for job, made in boot["jobs"].items():
            self.assertEqual(made, {"sources": ["org.jenkinsci.plugin.gitea.GiteaSCMSource"],
                                    "factory": "org.jenkinsci.plugins.pipeline.multibranch.defaults."
                                               "PipelineBranchDefaultsProjectFactory"}, job)
        # unclassified.giteaServers.servers: the server, managing no hooks.
        self.assertEqual(boot["gitea_servers"], [{"serverUrl": FORGE_URL, "manageHooks": False, "credentialsId": None}])
        # modernSCM with the git plugin's source (the Gitea one cannot fetch a tag version).
        self.assertEqual(boot["libraries"], [{"name": "yggdrasil", "version": LIBRARY_VERSION,
                                              "scm": "jenkins.plugins.git.GitSCMSource",
                                              "remote": f"{FORGE_URL}/{OWNER}/yggdrasil.git",
                                              "credentials": "forge-read"}])
        # Two domainCredentials lists (casc.forgejo.yaml, casc.test.yaml) merged; no GitHub App.
        self.assertEqual(boot["credentials"], {"forge-write": "UsernamePasswordCredentialsImpl",
                                               "forge-read": "UsernamePasswordCredentialsImpl",
                                               "forge-webhook": "StringCredentialsImpl",
                                               "shop-db-password": "StringCredentialsImpl"})

    def test_given_a_mirror_when_booted_then_the_github_app_joins_the_forge_credentials(self):
        # Three domainCredentials lists: casc.forgejo.yaml, casc.github-app.yaml and casc.test.yaml.
        self.assertEqual(self.second_boot["credentials"], {**self.first_boot["credentials"],
                                                           "github-app": "GitHubAppCredentials"})
        self.assertEqual(self.second_boot["jobs"], self.first_boot["jobs"])

    def test_given_a_secret_file_ending_in_a_newline_when_read_by_casc_then_the_newline_is_kept(self):
        # ${readFile:} keeps a trailing newline: env/platform.env.example's printf '%s' is needed.
        self.assertTrue(self.first_boot["webhook_secret_has_newline"])

    def test_given_another_ci_work_directory_when_the_controller_restarts_then_the_ci_node_moves(self):
        before, after = self.first_boot["nodes"], self.second_boot["nodes"]
        self.assertEqual(before["ci"], {"remoteFS": str(self.temp / "agents" / "ci-before"), "executors": 4,
                                        "labels": "ci"})
        self.assertEqual(after["ci"], {"remoteFS": str(self.ci_workdir), "executors": 4, "labels": "ci"})
        self.assertEqual(before["release"], after["release"])
        self.assertEqual(after["release"], {"remoteFS": str(self.release_workdir), "executors": 1, "labels": "release"})
        self.assertEqual(after["vps"], {"remoteFS": "/home/jenkins/agent", "executors": 2, "labels": "vps"})

    # --- Pull requests ----------------------------------------------------------------------------

    def test_given_a_feature_pull_request_when_built_then_branch_policy_and_checks_are_reported_by_jenkins(self):
        # The bound value's digest, not the value: the log is masked, the digest proves it arrived.
        check = {**UNIT, "run": UNIT["run"] + '; echo "DB_PASSWORD $(printf %s "$DB_PASSWORD" | sha256sum)"',
                 "credentials": {"DB_PASSWORD": "shop-db-password"}}
        sha = self.branch_with(CLAUDE, APP, "feature/reported", {"yggdrasil.yml": pipeline_file(unit=check)})
        number, build = self.pull_request_build(CLAUDE, APP, "feature/reported")
        self.assertEqual(build.result, "SUCCESS", build.console[-6000:])
        self.assertIn(UNIT_RAN, build.console)
        self.assertIn(f"DB_PASSWORD {hashlib.sha256(DB_PASSWORD.encode()).hexdigest()}", build.console)
        self.assertIn(f"Loading library yggdrasil@{LIBRARY_VERSION}", build.console)

        statuses = self.statuses(APP, sha)
        # pending first, then the result; by jenkins (forge-write), pointing at the build.
        for context in ("branch-policy", "unit"):
            mine = [s for s in statuses if s[0] == context]
            self.assertEqual([s[1] for s in mine], ["pending", "success"], statuses)
            for _, _, creator, _, target in mine:
                self.assertEqual((creator, target), (JENKINS, build.url))
        self.assertEqual(self.latest_statuses(APP, sha)["branch-policy"][3], "Follows the branching model")
        self.assert_no_secret(build.console, "the build log")
        self.assertNotIn(DB_PASSWORD, build.console)
        # Jenkins warns that reportStatus passes the token to httpRequest through an interpolated string:
        # what it stores of the step's arguments (the build's flow nodes) must still not hold it.
        stored = docker_tar(self.names["jenkins"], f"/var/jenkins_home/jobs/{APP}")
        self.assertIn(b"httpRequest", stored)
        self.assertNotIn(self.tokens[JENKINS].encode(), stored)

    def test_given_a_dependabot_branch_pull_request_when_built_then_branch_policy_fails(self):
        branch = "dependabot/npm_and_yarn/left-pad-1.3.0"
        sha = self.branch_with(CLAUDE, APP, branch, {"yggdrasil.yml": pipeline_file(unit=UNIT)})
        number, build = self.pull_request_build(CLAUDE, APP, branch)
        self.assertEqual(build.result, "FAILURE")
        self.assertIn("Failed: branch-policy.", build.console)
        latest = self.latest_statuses(APP, sha)
        self.assertEqual(latest["branch-policy"][1:3], ("failure", JENKINS))
        self.assertIn("must come from feature/<name> or fix/<name>", latest["branch-policy"][3])
        # The checks still run and report: the owner sees everything that is wrong at once.
        self.assertEqual(latest["unit"][1], "success")

    def test_given_claudes_branch_with_its_own_jenkinsfile_when_built_then_that_file_never_runs(self):
        sha = self.branch_with(CLAUDE, APP, "feature/own-jenkinsfile",
                               {"Jenkinsfile": HOSTILE_JENKINSFILE, "yggdrasil.yml": pipeline_file(unit=UNIT)})
        number, build = self.pull_request_build(CLAUDE, APP, "feature/own-jenkinsfile")
        self.assertNotIn("HOSTILE-JENKINSFILE", build.console)
        # What ran is the library (the Jenkinsfile kept in Jenkins), on the branch's commit.
        self.assertIn(f"Loading library yggdrasil@{LIBRARY_VERSION}", build.console)
        self.assertIn("(Read yggdrasil.yml)", build.console)
        self.assertIn(UNIT_RAN, build.console)
        self.assertEqual(build.result, "SUCCESS", build.console[-4000:])
        self.assertEqual(self.latest_statuses(APP, sha)["unit"][1], "success")

    def test_given_a_check_asking_for_an_unlisted_credential_when_built_then_the_build_fails_before_it_runs(self):
        check = {**UNIT, "credentials": {"DB_PASSWORD": "production-db-password"}}
        sha = self.branch_with(CLAUDE, APP, "feature/unlisted-credential", {"yggdrasil.yml": pipeline_file(unit=check)})
        number, build = self.pull_request_build(CLAUDE, APP, "feature/unlisted-credential")
        self.assertEqual(build.result, "FAILURE")
        self.assertIn("yggdrasil.yml cannot be used", build.console)
        self.assertIn("credential 'production-db-password' is not allowed for shop-api", build.console)
        self.assertNotIn(UNIT_RAN, build.console)
        self.assertNotIn("(Checks)", build.console)
        self.assertEqual(self.statuses(APP, sha), [])

    def test_given_a_forged_success_by_claude_when_built_then_the_check_still_runs(self):
        sha = self.branch_with(CLAUDE, APP, "feature/forged", {"yggdrasil.yml": pipeline_file(unit=UNIT)})
        self.api[CLAUDE].request("POST", f"/repos/{OWNER}/{APP}/statuses/{sha}",
                                 {"state": "success", "context": "unit", "description": "trust me"})
        number, build = self.pull_request_build(CLAUDE, APP, "feature/forged")
        self.assertEqual(build.result, "SUCCESS", build.console[-4000:])
        self.assertIn(f"Check unit: run (success by {CLAUDE}, not {JENKINS})", build.console)
        self.assertIn(UNIT_RAN, build.console)
        self.assertEqual(self.latest_statuses(APP, sha)["unit"][1:3], ("success", JENKINS))

    def test_given_a_commit_already_checked_by_jenkins_when_built_again_then_the_check_is_reused(self):
        sha = self.branch_with(CLAUDE, APP, "feature/reused", {"yggdrasil.yml": pipeline_file(unit=UNIT)})
        number, first = self.pull_request_build(CLAUDE, APP, "feature/reused")
        self.assertEqual(first.result, "SUCCESS", first.console[-4000:])
        self.assertIn(UNIT_RAN, first.console)

        second = self.rebuild(APP, f"PR-{number}")
        self.assertEqual(second.result, "SUCCESS", second.console[-4000:])
        self.assertIn(f"Check unit: reuse (reused: success by {JENKINS} on this commit)", second.console)
        self.assertNotIn(UNIT_RAN, second.console)
        statuses = self.statuses(APP, sha)
        # The branch policy is never reused: it ran again.
        self.assertEqual([s[1] for s in statuses if s[0] == "branch-policy"], ["pending", "success"] * 2)
        self.assertEqual([s[1] for s in statuses if s[0] == "unit"], ["pending", "success", "success"])
        self.assertEqual(self.latest_statuses(APP, sha)["unit"][3], f"reused: success by {JENKINS} on this commit")

    def test_given_a_check_with_paths_and_no_change_under_them_when_built_then_it_is_skipped(self):
        sha = self.branch_with(CLAUDE, APP, "feature/docs-only",
                               {"yggdrasil.yml": pipeline_file(unit={**UNIT, "paths": ["src/**"]}),
                                "docs/readme.md": "Only documentation.\n"})
        number, build = self.pull_request_build(CLAUDE, APP, "feature/docs-only")
        self.assertEqual(build.result, "SUCCESS", build.console[-4000:])
        self.assertIn("Check unit: skip (skipped: no changes under src/**)", build.console)
        self.assertNotIn(UNIT_RAN, build.console)
        self.assertEqual(self.latest_statuses(APP, sha)["unit"][1:4], ("success", JENKINS, "skipped: no changes under src/**"))

    def test_given_a_check_using_docker_when_built_then_it_can_start_a_container_on_the_ci_engine(self):
        # Testcontainers-style: start a container publishing a port, reach it through
        # TESTCONTAINERS_HOST_OVERRIDE (run-check.sh: the agent's default gateway).
        name = f"{PREFIX}-check-{self.run_id}"
        run = "\n".join([
            "set -e",
            f"docker run --detach --rm --name {name} --publish 8080 alpine:3 "
            "sh -c 'while true; do echo HELLO-FROM-A-CONTAINER | nc -l -p 8080; done' >/dev/null",
            f"port=$(docker port {name} 8080/tcp | head -n 1 | sed 's/.*://')",
            'echo "host=$TESTCONTAINERS_HOST_OVERRIDE port=$port"',
            "answer=",
            "for attempt in $(seq 1 30); do",
            '  answer=$(nc -w 2 "$TESTCONTAINERS_HOST_OVERRIDE" "$port" </dev/null || true)',
            '  [ -n "$answer" ] && break',
            "  sleep 1",
            "done",
            f"docker rm --force {name} >/dev/null",
            'echo "answer=$answer"',
            '[ "$answer" = HELLO-FROM-A-CONTAINER ]',
        ])
        files = {"yggdrasil.yml": pipeline_file(unit=UNIT, containers={"image": "docker:cli", "run": run, "docker": True})}
        sha = self.branch_with(CLAUDE, APP, "feature/containers", files)
        number, build = self.pull_request_build(CLAUDE, APP, "feature/containers")
        self.assertIn("answer=HELLO-FROM-A-CONTAINER", build.console)
        self.assertEqual(build.result, "SUCCESS", build.console[-6000:])
        self.assertEqual(self.latest_statuses(APP, sha)["containers"][1], "success")

    def test_given_the_ci_agent_when_a_check_runs_then_no_write_token_is_in_its_environment(self):
        marker = f"check-started-{uuid.uuid4().hex[:8]}"
        check = {"image": "alpine:3", "run": f"env > check-env.txt; touch {marker}; sleep 20", "archive": "check-env.txt"}
        sha = self.branch_with(CLAUDE, APP, "feature/environment", {"yggdrasil.yml": pipeline_file(unit=check)})
        number = self.open_pull_request(CLAUDE, APP, "feature/environment", "develop")

        # While the check runs: every process of the ci agent, and every file under its work directory.
        deadline = time.monotonic() + 600
        while not list(self.ci_workdir.glob(f"workspace/*/app/{marker}")):
            self.assertLess(time.monotonic(), deadline, "the check never started")
            time.sleep(1)
        environments = docker("exec", self.names["ci"], "sh", "-c",
                              "for f in /proc/[0-9]*/environ; do tr '\\0' '\\n' <$f; done 2>/dev/null").stdout
        self.assertIn("JENKINS_AGENT_NAME=ci", environments)  # what was read is the agent's
        files = docker_tar(self.names["ci"], str(self.ci_workdir))
        build = self.wait_for_build(APP, f"PR-{number}", after=0)
        self.assertEqual(build.result, "SUCCESS", build.console[-4000:])

        status, check_env = self.jenkins.request("GET", f"{job_path(APP, f'PR-{number}')}/1/artifact/check-env.txt")
        self.assertEqual(status, 200)
        self.assertIn("HOSTNAME=", check_env)
        self.assertNotIn(self.tokens[JENKINS], environments)
        self.assertNotIn(self.tokens[JENKINS].encode(), files)
        # Nor, in the check's own container, the read token (bound only while git fetches).
        self.assert_no_secret(check_env, "the check's environment")

    def test_given_a_newer_push_to_a_pull_request_when_built_then_the_older_build_is_aborted(self):
        check = {"image": "alpine:3",
                 "run": 'if [ -f fast ]; then echo "FAST-$((1 + 1))"; else echo "SLOW-$((1 + 1))-STARTED"; sleep 300; fi'}
        self.branch_with(CLAUDE, APP, "feature/replaced", {"yggdrasil.yml": pipeline_file(unit=check)})
        number = self.open_pull_request(CLAUDE, APP, "feature/replaced", "develop")
        branch = f"PR-{number}"
        deadline = time.monotonic() + 600
        while "SLOW-2-STARTED" not in self.console(APP, branch, 1):
            self.assertLess(time.monotonic(), deadline, "the first build never reached its check")
            time.sleep(2)
        self.change_file(CLAUDE, APP, "fast", "\n", "feature/replaced")
        second = self.wait_for_build(APP, branch, after=1)
        first = self.wait_for_build(APP, branch, after=0, timeout=60)
        # abortPrevious: the older build is interrupted, its check's container stopped with it (run-check.sh
        # runs it in the foreground), and the build is marked not built, not failed.
        self.assertEqual((first.number, first.result), (1, "NOT_BUILT"), first.console[-3000:])
        self.assertIn("Superseded by #2", first.console)
        running = docker("ps", "--no-trunc", "--format", "{{.Command}}").stdout
        self.assertNotIn("SLOW-$((1 + 1))-STARTED", running)
        self.assertEqual(second.result, "SUCCESS", second.console[-3000:])
        self.assertIn("FAST-2", second.console)

    # --- Pushes -----------------------------------------------------------------------------------

    def test_given_a_repository_without_a_pipeline_file_when_pushed_then_it_only_deploys_as_before(self):
        before = self.last_number(PLAIN, "develop")
        sha = self.land_on_develop(PLAIN, "index", {"index.html": "<h1>plain</h1>\n"})
        build = self.wait_for_build(PLAIN, "develop", after=before)
        self.assertEqual(build.result, "SUCCESS", build.console[-4000:])
        self.assertIn(f"DEPLOY-STUB environment=development stack={PLAIN} checkout=app version=develop-{sha[:7]} inputs=",
                      build.console)
        for stage in ("(Branch policy)", "(Checks)"):
            self.assertNotIn(stage, build.console)
        self.assertEqual(self.statuses(PLAIN, sha), [])

    def test_given_a_second_push_to_develop_when_built_then_changes_count_from_the_last_checked_commit(self):
        # The first develop build (setUpClass) had nothing to compare with: unit ran. A push of docs
        # only is compared with the commit that build checked (YGG_CHECKED_COMMIT, read back from it).
        first = self.first_builds[(WEB, "develop")]
        self.assertEqual(first.result, "SUCCESS", first.console[-4000:])
        self.assertIn("Check unit: run (no earlier result; changed files unknown)", first.console)
        self.assertIn("WEB-UNIT-42-RAN", first.console)

        before = self.last_number(WEB, "develop")
        self.land_on_develop(WEB, "notes", {"docs/notes.md": "notes\n"})
        docs = self.wait_for_build(WEB, "develop", after=before)
        self.assertEqual(docs.result, "SUCCESS", docs.console[-4000:])
        self.assertIn("Check unit: skip (skipped: no changes under src/**)", docs.console)
        self.assertIn("DEPLOY-STUB environment=development", docs.console)

        sha = self.land_on_develop(WEB, "app", {"src/app.py": "print('hi')\n"})
        code = self.wait_for_build(WEB, "develop", after=docs.number)
        self.assertIn("Check unit: run (no earlier result; changed files match)", code.console)
        self.assertIn("WEB-UNIT-42-RAN", code.console)
        self.assertEqual(self.latest_statuses(WEB, sha)["unit"][1:3], ("success", JENKINS))

    def test_given_develop_moving_on_during_a_build_when_it_deploys_then_only_the_checked_commit_is_deployed(self):
        # The checks ran on one commit; while they ran, another landed on develop. The deploy agent's
        # `checkout scm` must deploy the checked commit, or the deploy must refuse: never the newer
        # commit, which no check saw.
        before = self.last_number(WORKER, "develop")
        checked = self.land_on_develop(WORKER, "slow", {"slow": "slow\n"})
        deadline = time.monotonic() + 600
        while "SLOW-2-CHECK" not in self.console(WORKER, "develop", before + 1):
            self.assertLess(time.monotonic(), deadline, "the build never reached its slow check")
            time.sleep(2)
        moved = self.land_on_develop(WORKER, "moved", {"moved.txt": "after the check started\n"})
        self.assertNotEqual(moved, checked)
        build = self.wait_for_build(WORKER, "develop", after=before)
        self.assertEqual(build.number, before + 1)
        self.assertIn(f"Status unit on {checked[:7]}: success", build.console)
        self.assertNotIn(f"version=develop-{moved[:7]}", build.console)
        if build.result == "SUCCESS":
            self.assertIn(f"DEPLOY-STUB environment=development stack={WORKER} checkout=app version=develop-{checked[:7]}",
                          build.console)
        else:
            self.assertEqual(build.result, "FAILURE", build.console[-4000:])
            self.assertIn(f"The commit moved from {checked[:7]} to {moved[:7]} during the build", build.console)
            self.assertNotIn("DEPLOY-STUB", build.console)
        print(f"\n  develop moved during the build: {build.result} "
              f"({'checkout scm deployed the checked commit' if build.result == 'SUCCESS' else 'the guard refused'})")
        # The newer commit gets its own build, checked and deployed.
        newer = self.wait_for_build(WORKER, "develop", after=build.number)
        self.assertEqual(newer.result, "SUCCESS", newer.console[-4000:])
        self.assertIn(f"version=develop-{moved[:7]}", newer.console)

    # --- Owner tags --------------------------------------------------------------------------------

    def test_given_owner_tags_on_an_old_commit_when_scanned_then_only_an_annotated_one_is_young_enough(self):
        # buildTags atMostDays('1') (casc.yaml) builds a tag only while it is young, and the Gitea plugin
        # dates a tag as Forgejo does: a lightweight tag by its commit, an annotated one by its tagger.
        # So a lightweight tag pushed today on a commit five days old is "too old" and never built,
        # while an annotated tag on the same commit is built; a tag on a new commit is built either way.
        clone = self.clone(OWNER, LIB)
        five_days_ago = f"@{int(time.time()) - 5 * 86400} +0000"
        old = {"GIT_COMMITTER_DATE": five_days_ago, "GIT_AUTHOR_DATE": five_days_ago}
        for arguments, env in ((("checkout", "--quiet", "-b", "feature/tagged", "origin/develop"), None),
                               (("commit", "--quiet", "--allow-empty", "-m", "an old commit"), old),
                               (("tag", "v1.0.0"), None),
                               (("tag", "--annotate", "v1.0.1", "-m", "tagged today"), None),
                               (("commit", "--quiet", "--allow-empty", "-m", "a new commit"), None),
                               (("tag", "v1.1.0"), None),
                               (("push", "--quiet", "origin", "feature/tagged", "v1.0.0", "v1.0.1", "v1.1.0"), None)):
            done = self.git(OWNER, *arguments, cwd=clone, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
        # The webhook's events may have started already; a scan makes sure every tag was looked at.
        self.jenkins.post(job_path(LIB) + "/build", {"delay": "0"})
        for tag in ("v1.0.1", "v1.1.0"):
            built = self.wait_for_build(LIB, tag, after=0)
            self.assertIn(f"Nothing to do for the tag {tag}", built.console)
            self.assertEqual(built.result, "SUCCESS")
        deadline = time.monotonic() + 120
        scan = ""
        while "Finished:" not in scan or "Checking tag v1.0.0" not in scan:
            self.assertLess(time.monotonic(), deadline, scan)
            time.sleep(2)
            scan = self.jenkins.request("GET", job_path(LIB) + "/indexing/consoleText")[1]
        self.assertIn("No automatic build triggered for v1.0.0", scan)
        self.assertEqual(self.builds(LIB, "v1.0.0"), [], scan)

    # --- The library's own pieces -----------------------------------------------------------------

    def test_given_the_libraries_tool_runner_when_run_on_the_ci_agent_then_bash_gets_each_argument_intact(self):
        # TOOL (a #!/usr/bin/env bash script run by the sh step) is used on the release path and the
        # GitHub Actions look, which need GitHub; a probe job loads the pinned library and runs it.
        script = "\n".join([
            "@Library('yggdrasil') _",
            f"withEnv(['FORGE_TYPE=forgejo', 'FORGE_URL={FORGE_URL}', 'GITHUB_OWNER={OWNER}', "
            "'YGGDRASIL_REPOSITORY=yggdrasil']) {",
            "  node('ci') {",
            "    yggdrasilPipeline.checkoutTools()",
            "    int code = yggdrasilPipeline.toolStatus('branch-policy.sh', ['feature/probe', 'it\\'s $(id) `x`;', 'abc'], "
            "pwd() + '/tool.out')",
            "    echo \"TOOL-EXIT ${code}\"",
            "    echo 'TOOL-OUT ' + readFile('tool.out')",
            "  }",
            "}",
        ])
        name = f"probe-{uuid.uuid4().hex[:6]}"
        self.jenkins.script(
            "def job = jenkins.model.Jenkins.get().createProject(org.jenkinsci.plugins.workflow.job.WorkflowJob, "
            f"'{name}')\n"
            f"job.definition = new org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition({groovy_string(script)}, true)\n"
            "job.scheduleBuild2(0)\n")
        build = self.wait_for_build(name, None, after=0)
        self.assertIn("TOOL-EXIT 0", build.console)
        self.assertIn("TOOL-OUT Pull requests into 'it's $(id) `x`;' are not covered by the branching model",
                      build.console)
        self.assertEqual(build.result, "SUCCESS", build.console[-3000:])


def groovy_string(text):
    """`text` as a single-quoted Groovy literal: no interpolation; backslashes, quotes and newlines escaped."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def docker_tar(container, path):
    """Every file under `path` in the container, concatenated (a tar stream read whole)."""
    done = subprocess.run(["docker", "exec", container, "tar", "-C", path, "-cf", "-", "."], capture_output=True,
                          timeout=300)
    data = io.BytesIO(done.stdout)
    chunks = []
    with tarfile.open(fileobj=data) as archive:
        for member in archive:
            if member.isfile():
                chunks.append(archive.extractfile(member).read())
    return b"\n".join(chunks)


class CatalogTests(unittest.TestCase):
    def test_given_the_live_suites_catalog_when_validated_then_it_is_a_valid_catalog(self):
        self.assertEqual(catalog_module.validate(test_catalog()), [])


if __name__ == "__main__":
    unittest.main()
