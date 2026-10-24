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
  baget     a local NuGet feed (BaGet, pinned), on the engine's default bridge and published on its
            gateway, over HTTPS with a certificate of the same CA: the release agent's YGG_NUGET_SOURCE
            (and YGG_NUGET_CA_FILE), so an owner-tagged library's tag build pushes there, with the
            controller's nuget-api-key (casc.nuget.yaml, its file mounted) as the feed's key

The library is this checkout's jenkins/library, served from the Forgejo repository artur-rios/yggdrasil
at the tag v0.0.0-live (YGGDRASIL_LIBRARY_VERSION), next to this checkout's scripts/. rules.py applies
the protections and the webhooks to the controller (http://jenkins:8080/gitea-webhook/post), so pull
requests and pushes reach Jenkins the way they will in production.

Releases: the test catalog has a `production` environment (trigger: release, on vps, which is therefore
the publish agent) and a GitHub mirror (mirror.releases off: there is no GitHub to copy releases to). No
GitHub is reachable, so GitHub is a test double: GITHUB_DOUBLE, a script in this file, is YGG_GITHUB_SH on
the vps and release agents only (the library and publish-release.sh run it instead of scripts/github.sh)
and answers actions-status, wait-ref, dispatch, run-status and download-artifacts from a state directory
the tests write, logging every call with the agent that made it and whether a GitHub token was bound. The
github-app credential (a GitHubAppCredentials, which would ask GitHub for a token) is replaced, after the
boots were inspected, by a plain username/password one under the same id. The skip is answered through
Jenkins' REST API (the input's proceedEmpty), and the actions-release and release-resume jobs (casc.yaml,
their scripts mounted as compose.yml mounts them) are started with buildWithParameters; the tag-check job
(its daily trigger read back from the boot) with a plain build.

Owner tags: the owner pushes annotated tags (git tag -a: a lightweight tag on a commit older than a day is
never built, see the tag-age test) to ygg-tool (release files) and dotnet-lib (a NuGet package, packed with
the .NET SDK image nuget-push.sh pins), on main and off it.

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
# Releases (the production environment): a plain release; release files built, from "GitHub" and
# validated; an application whose GitHub Actions check never answers (the skip, then actions-release); a
# release whose validation fails.
RELEASE, DESKTOP, TOOLS, BROKEN = "shop-release", "shop-desktop", "shop-tools", "shop-broken"
# A release that fails after its merge (its release build binds a credential Jenkins does not have yet),
# then finished by the release-resume job.
RESUME = "shop-resume"
# A release merged but never tagged (its tag was taken while it waited for approval), then tagged and
# finished by the release-resume job given its merge commit.
UNTAGGED = "shop-untagged"
# Owner-tagged repositories (tag builds): one releasing files, one library publishing NuGet packages to a
# local feed (BaGet).
OWNED, LIBRARY = "ygg-tool", "dotnet-lib"
# An owner-tagged repository declaring both release files and NuGet packages, whose validation fails: the
# packages must never be packed or pushed.
BOTH = "ygg-both"
MIRROR_OWNER = "artur-rios-mirror"
GITHUB_TOKEN = "a-throwaway-github-token"
SKIP_QUESTION = "GitHub Actions checks didn't finish. Deploy without them?"
KEPT = "kept: release branches are protected on Forgejo; the owner deletes them"

# The GitHub test double (YGG_GITHUB_SH on the vps and release agents), mounted at /opt/github-double with
# its state: state/<repo>/actions (an exit code; none: GitHub does not answer), state/<repo>/run
# (success, failure or pending; default success), state/<repo>/artifacts/<artifact>/<files>. Every call is
# logged to state/calls.log with the agent, whether GH_TOKEN was bound, and GITHUB_OWNER.
GITHUB_DOUBLE = """\
#!/usr/bin/env bash
# test_jenkins_live.py's stand-in for scripts/github.sh: no GitHub is reachable from the test network.
set -euo pipefail
state=/opt/github-double/state
command=${1:-}
shift || true
token=unset
if [[ -n "${GH_TOKEN:-}" ]]; then token=set; fi
printf '%s agent=%s token=%s owner=%s args=%s\\n' "$command" "${JENKINS_AGENT_NAME:-?}" "$token" "${GITHUB_OWNER:-}" "$*" \\
  >>"$state/calls.log"
repo=${1:-}
case "$command" in
  actions-status)
    if [[ ! -f "$state/$repo/actions" ]]; then
      echo "github: GitHub did not answer about the checks (test double)" >&2
      exit 2
    fi
    exit "$(cat "$state/$repo/actions")"
    ;;
  wait-ref) exit 0 ;;
  dispatch) echo 4242 ;;
  run-status)
    result=$(cat "$state/$repo/run" 2>/dev/null || echo success)
    echo "$result"
    case "$result" in success) exit 0 ;; failure) exit 1 ;; *) exit 3 ;; esac
    ;;
  download-artifacts)
    mkdir -p "$3"
    cp -R "$state/$repo/artifacts/." "$3/"
    ;;
  *)
    echo "github double: $command is not faked" >&2
    exit 2
    ;;
esac
"""

# The release applications' yggdrasil.yml. The markers are computed by the shell, so the log shows them
# only when the command ran (the trace shows the command's text).
DESKTOP_PIPELINE = {"release": {
    "build": {"image": "alpine:3",
              "run": 'mkdir -p dist && printf "shop-desktop %s\\n" "$RELEASE_VERSION" >"dist/shop-desktop-$RELEASE_VERSION.tar.gz"'
                     ' && sha256sum dist/*.tar.gz >dist/shop-desktop.sha256 && echo stray >dist/notes.txt'
                     ' && echo "BUILD-KEY $(printf %s "$SIGNING_KEY" | sha256sum | cut -c1-64)"',
              "files": ["dist/*.tar.gz", "dist/*.sha256"],
              "credentials": {"SIGNING_KEY": "shop-db-password"}},
    "windows": {"workflow": "release-windows.yml", "files": ["*/*.msi"]},
    "validate": {"image": "alpine:3",
                 "run": 'test -f "$RELEASE_FILES/windows/shop-desktop-windows/shop-desktop-setup.msi"'
                        ' && echo "VALIDATED-$(find "$RELEASE_FILES" -type f | wc -l | tr -d " ")-FILES"'
                        # Then it tries to change what is released: a file added and one rewritten, in
                        # its copy and in the gathered directory itself (the whole workspace is mounted).
                        ' && echo extra >"$RELEASE_FILES/build/dist/extra.txt"'
                        ' && echo tampered >"$RELEASE_FILES/build/dist/shop-desktop-$RELEASE_VERSION.tar.gz"'
                        ' && echo extra >../release-files/build/dist/extra-direct.txt'
                        ' && echo tampered >../release-files/build/dist/shop-desktop.sha256'
                        ' && echo "TAMPERED-$((1 + 1))"'},
}}
TOOLS_PIPELINE = {"release": {
    "build": {"image": "alpine:3", "run": "mkdir -p dist && echo tools >dist/shop-tools.tar.gz",
              "files": ["dist/shop-tools.tar.gz"]},
    "windows": {"workflow": "release-windows.yml", "files": ["*/*.msi"]},
}}
BROKEN_PIPELINE = {"release": {
    "build": {"image": "alpine:3", "run": "mkdir -p dist && echo broken >dist/shop-broken.tar.gz",
              "files": ["dist/*.tar.gz"]},
    "validate": {"image": "alpine:3", "run": 'echo "VALIDATION-$((1 + 2))-FAILED"; exit 3'},
}}
RESUME_KEY = "a-throwaway-resume-key"
RESUME_PIPELINE = {"release": {
    "build": {"image": "alpine:3",
              "run": 'mkdir -p dist && printf "resumed %s\\n" "$RELEASE_VERSION" >dist/shop-resume.tar.gz'
                     ' && echo "RESUME-KEY $(printf %s "$RESUME_KEY" | sha256sum | cut -c1-64)"',
              "files": ["dist/*.tar.gz"], "credentials": {"RESUME_KEY": "shop-resume-key"}},
}}

# The owner-tagged repositories' yggdrasil.yml, on main. The library packs with the .NET SDK image
# nuget-push.sh pushes with (one pull), and says whether the key was in its environment (it must not be).
SDK_IMAGE = re.search(r"^IMAGE='([^']+)'$", (ROOT / "scripts" / "nuget-push.sh").read_text(encoding="utf-8"), re.M).group(1)
OWNED_PIPELINE = {"release": {
    "build": {"image": "alpine:3",
              "run": 'mkdir -p dist && printf "ygg-tool %s\\n" "$RELEASE_VERSION" >"dist/ygg-tool-$RELEASE_VERSION.tar.gz"'
                     ' && echo "TOOL-BUILT-$((1 + 1))"',
              "files": ["dist/*.tar.gz"]},
}}
LIBRARY_PIPELINE = {"publish": {"nuget": {
    "image": SDK_IMAGE,
    "run": 'dotnet pack src/Lib/Lib.csproj -c Release -o out -p:Version="$RELEASE_VERSION"'
           ' && echo "PACK-SAW-KEY-$(env | grep -c NUGET_API_KEY)"',
    "packages": "out/*.nupkg"}}}
BOTH_PIPELINE = {
    "release": {"build": {"image": "alpine:3", "run": "mkdir -p dist && echo both >dist/ygg-both.tar.gz",
                          "files": ["dist/*.tar.gz"]},
                "validate": {"image": "alpine:3", "run": 'echo "BOTH-VALIDATION-$((1 + 2))-FAILED"; exit 3'}},
    "publish": {"nuget": {"image": SDK_IMAGE, "run": 'echo "BOTH-PACKED-$((1 + 1))"', "packages": "out/*.nupkg"}},
}
LIBRARY_FILES = {
    "src/Lib/Lib.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
                          "<PackageId>Ygg.Live.Lib</PackageId><Authors>ygg</Authors></PropertyGroup></Project>\n",
    "src/Lib/Lib.cs": "namespace Ygg.Live.Lib;\npublic static class Answer { public static int Value => 42; }\n",
}
PACKAGE_ID = "Ygg.Live.Lib"
# The local NuGet feed, over HTTPS (dotnet refuses to push to an http source, and nuget-push.sh too) with a
# certificate of the run's CA, the key a throwaway one.
BAGET_IMAGE = "loicsharma/baget:0.4.0-preview2@sha256:7ed7bdc3d29bb5145d69d1ca73b7c5fd4a34f72dd8b2810c907a6703b172b8ba"
NUGET_KEY = "a-throwaway-nuget-api-key"

# A check's command whose output differs from its text: the shell trace (sh -x) prints the command, so
# the marker in the log proves the check ran, not that it was planned.
UNIT_RAN = "UNIT-42-RAN"
UNIT = {"image": "alpine:3", "run": 'echo "UNIT-$((40 + 2))-RAN"'}
# shop-release's unit check archives a file its release deploys with (deploy.inputs).
RELEASE_PIPELINE = {"checks": {"unit": {**UNIT, "run": UNIT["run"] + " && mkdir -p out && echo input >out/input.txt",
                                        "archive": "out/**"}},
                    "deploy": {"inputs": ["unit"]}}

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
# The owner's data next to it (stacks/ from main, while this script is the library's version).
echo "DEPLOY-OWNER-DATA $(cat "$(dirname "$0")/../stacks/live-marker.yml" 2>/dev/null || echo none)"
# What the image build would get from deploy.inputs: each file's first line.
if [ -n "${DEPLOY_INPUTS_DIR:-}" ]; then
  find "$DEPLOY_INPUTS_DIR" -type f | sort | while read -r file; do echo "DEPLOY-INPUT $(head -n 1 "$file")"; done
fi
"""


def test_catalog():
    """The installation under test: a branch environment and a release one (its checksTimeout short, for
    the skip), both on the agent vps, and a GitHub mirror without release copies; shop-api requires the
    check unit and may bind shop-db-password; shop-web requires unit; plain-site has no checks and only
    development (it does not release); ygg-lib, ygg-tool and dotnet-lib are tagged by their owner; the
    release applications (shop-desktop may bind shop-db-password for its release build only, through
    releaseCredentials; shop-tools requires
    a GitHub Actions check; shop-resume's release build binds a credential added only later)."""
    return {
        "owner": OWNER,
        "repository": "yggdrasil",
        "forge": {"type": "forgejo", "url": FORGE_URL, "jenkinsUser": JENKINS, "developers": [CLAUDE]},
        "mirror": {"github": MIRROR_OWNER, "releases": False},
        "environments": [{"id": "development", "name": "Development", "trigger": "branch", "branches": "develop",
                          "agent": "vps"},
                         {"id": "production", "name": "Production", "trigger": "release", "agent": "vps",
                          "checksTimeout": 20}],
        "systems": [{"id": "shop", "name": "Shop", "applications": [
            {"id": APP, "name": "API", "kind": "api", "health": "http://shop-api:8080/healthz", "checks": ["unit"],
             "credentials": ["shop-db-password"]},
            {"id": WEB, "name": "Web", "kind": "web", "health": "http://shop-web:8080/", "checks": ["unit"]},
            # Development only: an application that does not release (release-resume refuses it).
            {"id": PLAIN, "name": "Site", "kind": "web", "health": "http://plain-site:8080/",
             "environments": {"development": {}}},
            {"id": WORKER, "name": "Worker", "kind": "worker", "health": "http://shop-worker:8080/"},
            {"id": RELEASE, "name": "Release", "kind": "web", "health": "http://shop-release:8080/", "checks": ["unit"]},
            {"id": DESKTOP, "name": "Desktop", "kind": "web", "health": "http://shop-desktop:8080/",
             "releaseCredentials": ["shop-db-password"]},
            {"id": TOOLS, "name": "Tools", "kind": "web", "health": "http://shop-tools:8080/",
             "actionsChecks": ["Console (Windows)"]},
            {"id": BROKEN, "name": "Broken", "kind": "web", "health": "http://shop-broken:8080/"},
            {"id": RESUME, "name": "Resume", "kind": "web", "health": "http://shop-resume:8080/",
             "releaseCredentials": ["shop-resume-key"]},
            # Production asks before it deploys, which is when the test takes the release's tag.
            {"id": UNTAGGED, "name": "Untagged", "kind": "web", "health": "http://shop-untagged:8080/",
             "environments": {"development": {}, "production": {"approval": True}}},
        ]}],
        "repositories": [{"id": LIB, "release": "owner-tag", "tags": "v*"},
                         {"id": OWNED, "release": "owner-tag", "tags": "v*"},
                         {"id": LIBRARY, "release": "owner-tag", "tags": "[0-9]*"},
                         {"id": BOTH, "release": "owner-tag", "tags": "v*"}],
    }


RELEASE_APPLICATIONS = {RELEASE: RELEASE_PIPELINE, DESKTOP: DESKTOP_PIPELINE, TOOLS: TOOLS_PIPELINE,
                        BROKEN: BROKEN_PIPELINE, RESUME: RESUME_PIPELINE, UNTAGGED: {"checks": {"unit": UNIT}}}
# The owner-tagged repositories with a yggdrasil.yml on main (and develop, cut from it), and their files.
OWNER_TAGGED = {OWNED: {"yggdrasil.yml": yaml.safe_dump(OWNED_PIPELINE, sort_keys=False)},
                LIBRARY: {"yggdrasil.yml": yaml.safe_dump(LIBRARY_PIPELINE, sort_keys=False), **LIBRARY_FILES},
                BOTH: {"yggdrasil.yml": yaml.safe_dump(BOTH_PIPELINE, sort_keys=False)}}


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


def make_feed_certificate(directory, address):
    """The local NuGet feed's certificate from the same CA, for its address on the engine's default bridge
    (where nuget-push.sh's container reaches it), as the PKCS#12 file Kestrel reads."""
    (directory / "baget.ext").write_text(f"subjectAltName=IP:{address}\nextendedKeyUsage=serverAuth\n")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "baget.key", "-out", "baget.csr", "-subj", "/CN=baget",
            cwd=directory)
    openssl("x509", "-req", "-in", "baget.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "baget.crt", "-days", "2", "-extfile", "baget.ext", cwd=directory)
    openssl("pkcs12", "-export", "-out", "baget.pfx", "-inkey", "baget.key", "-in", "baget.crt", "-passout", "pass:baget",
            cwd=directory)
    (directory / "baget.pfx").chmod(0o644)


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
        cls.names = {role: f"{PREFIX}-{role}-{cls.run_id}"
                     for role in ("forgejo", "jenkins", "ci", "release", "vps", "baget")}
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

        # Boot 2, on the same JENKINS_HOME: the mirror set with the NuGet key, and the right ci work directory.
        cls.start_controller(casc=["casc.yaml", "casc.forgejo.yaml", "casc.github-app.yaml", "casc.nuget.yaml",
                                   "casc.test.yaml"],
                             ci_workdir=cls.ci_workdir)
        cls.second_boot = cls.inspect_controller()
        cls.stand_in_for_the_github_app()
        cls.write_github_double()
        cls.start_feed()
        cls.start_agents()

        cls.create_repositories()
        cls.apply_rules()
        cls.first_builds = {}
        cls.release_runs = {}
        cls.tag_runs = {}
        jobs = (APP, WEB, PLAIN, LIB, WORKER, *RELEASE_APPLICATIONS, *OWNER_TAGGED)
        for job in jobs:
            cls.jenkins.post(job_path(job) + "/build", {"delay": "0"})
        for job in jobs:
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
                          ("tag", LIBRARY_VERSION)):
            done = cls.git(OWNER, *arguments, cwd=tree)
            if done.returncode:
                raise AssertionError(f"git {arguments[0]}: {done.stderr}")
        # Then main moves on past the tag: a deploy.sh the pipeline must not run (the scripts are the
        # library's version) and owner data it must use (stacks/ comes from main).
        (tree / "scripts" / "deploy.sh").write_text('#!/usr/bin/env bash\necho "DEPLOY-FROM-MAIN"\n')
        (tree / "stacks").mkdir()
        (tree / "stacks" / "live-marker.yml").write_text("from-main\n")
        for arguments in (("add", "--all"), ("commit", "--quiet", "--message", "main moves on"),
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
        (files / "nuget-api-key").write_text(NUGET_KEY)
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
                               "casc.github-read.yaml", "casc.nuget.yaml")]
        mounts += [
            (cls.controller_files / "casc.test.yaml", "/etc/jenkins/casc.test.yaml"),
            (ROOT / "jenkins" / "Jenkinsfile.default", "/etc/jenkins/Jenkinsfile.default"),
            (ROOT / "jenkins" / "actions-release.groovy", "/etc/jenkins/actions-release.groovy"),
            (ROOT / "jenkins" / "release-resume.groovy", "/etc/jenkins/release-resume.groovy"),
            (ROOT / "jenkins" / "tag-check.groovy", "/etc/jenkins/tag-check.groovy"),
            (cls.controller_files / "nuget-api-key", "/run/secrets/nuget-api-key"),
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
            "pipeline_jobs": api.script_json(
                "jenkins.model.Jenkins.get().getAllItems(org.jenkinsci.plugins.workflow.job.WorkflowJob)"
                ".findAll { it.parent == jenkins.model.Jenkins.get() && !it.name.startsWith('probe-') }.collectEntries { [(it.name): [sandbox: it.definition.sandbox, "
                "script: it.definition.script, parameters: it.getProperty(hudson.model.ParametersDefinitionProperty)"
                "?.parameterDefinitions?.collect { d -> d.name }]] }"),
            "triggers": api.script_json(
                "jenkins.model.Jenkins.get().getAllItems(org.jenkinsci.plugins.workflow.job.WorkflowJob)"
                ".findAll { it.parent == jenkins.model.Jenkins.get() && !it.name.startsWith('probe-') }.collectEntries { "
                "[(it.name): it.triggers.values().collect { t -> [type: t.getClass().simpleName, spec: t.spec] }] }"),
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
    def stand_in_for_the_github_app(cls):
        """github-app is a GitHubAppCredentials (casc.github-app.yaml, a dummy key): binding it asks GitHub
        for an installation token, which no test can get. After the boots were inspected, it is replaced,
        under its id, by a username/password credential whose password the GitHub double sees as GH_TOKEN."""
        answer = cls.jenkins.script(
            "import com.cloudbees.plugins.credentials.CredentialsScope\n"
            "import com.cloudbees.plugins.credentials.SystemCredentialsProvider\n"
            "import com.cloudbees.plugins.credentials.domains.Domain\n"
            "import com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl\n"
            "def store = SystemCredentialsProvider.getInstance().getStore()\n"
            "def old = store.getCredentials(Domain.global()).find { it.id == 'github-app' }\n"
            "def stand_in = new UsernamePasswordCredentialsImpl(CredentialsScope.GLOBAL, 'github-app', "
            f"'GitHub test double', 'x-access-token', '{GITHUB_TOKEN}')\n"
            "println(store.updateCredentials(Domain.global(), old, stand_in))\n")
        if answer.strip() != "true":
            raise AssertionError(f"github-app was not replaced: {answer}")

    @classmethod
    def write_github_double(cls):
        """GITHUB_DOUBLE and its state: shop-desktop's run succeeds with an installer and a file the
        release does not declare; shop-tools' GitHub Actions never answers, its run has an installer."""
        cls.github_double = cls.temp / "github-double"
        state = cls.github_double / "state"
        state.mkdir(parents=True)
        (cls.github_double / "github.sh").write_text(GITHUB_DOUBLE)
        (cls.github_double / "github.sh").chmod(0o755)
        (state / "calls.log").write_text("")
        (state / "calls.log").chmod(0o666)
        for repo, files in ((DESKTOP, {"shop-desktop-setup.msi": "desktop installer\n", "debug.pdb": "symbols\n"}),
                            (TOOLS, {"shop-tools-setup.msi": "tools installer\n"})):
            artifact = state / repo / "artifacts" / f"{repo}-windows"
            artifact.mkdir(parents=True)
            for name, text in files.items():
                (artifact / name).write_text(text)

    @classmethod
    def start_feed(cls):
        """The local NuGet feed (BaGet) the release agent pushes to instead of nuget.org: on the engine's
        default bridge, published on its gateway, where both nuget-push.sh's container (on that bridge) and
        the tests reach it; over HTTPS with a certificate of the run's CA, which the release agent hands to
        nuget-push.sh (YGG_NUGET_CA_FILE)."""
        cls.feed_host = docker("network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}").stdout.strip()
        make_feed_certificate(cls.certificates, cls.feed_host)
        port = free_port()
        cls.feed_url = f"https://{cls.feed_host}:{port}"
        docker("run", "--detach", f"--name={cls.names['baget']}", "--memory=512m", "--tmpfs=/var/baget",
               f"--publish={cls.feed_host}:{port}:443", f"--volume={cls.certificates}:/certs:ro",
               f"--env=ApiKey={NUGET_KEY}", "--env=Storage__Type=FileSystem", "--env=Storage__Path=/var/baget/packages",
               "--env=Database__Type=Sqlite", "--env=Database__ConnectionString=Data Source=/var/baget/baget.db",
               "--env=Search__Type=Database", "--env=ASPNETCORE_URLS=https://+:443",
               "--env=ASPNETCORE_Kestrel__Certificates__Default__Path=/certs/baget.pfx",
               "--env=ASPNETCORE_Kestrel__Certificates__Default__Password=baget", BAGET_IMAGE)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(cls.feed_url + "/v3/index.json", timeout=5) as answer:
                    if answer.status == 200:
                        return
            except OSError:
                pass
            time.sleep(1)
        logs = docker("logs", "--tail=40", cls.names["baget"], check=False)
        raise AssertionError(f"the NuGet feed did not start:\n{logs.stdout}{logs.stderr}")

    def feed_versions(self, package):
        """The versions of the package the feed has ([] when none)."""
        try:
            with urllib.request.urlopen(f"{self.feed_url}/v3/package/{package.lower()}/index.json", timeout=30) as answer:
                return json.loads(answer.read())["versions"]
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return []
            raise

    def github_calls(self, repo=None):
        """The GitHub double's log, one call per line (of `repo` only, when given)."""
        lines = (self.github_double / "state" / "calls.log").read_text().splitlines()
        return [line for line in lines if repo is None or f" args={repo} " in f"{line} "]

    @classmethod
    def start_agents(cls):
        """ci and release as platform/build-agent.yml runs them, vps as a deploy agent; waits until all
        three are online. vps and release get the GitHub double as YGG_GITHUB_SH."""
        socket_path = "/var/run/docker.sock"
        if os.environ.get("DOCKER_HOST", "").startswith("unix://"):
            socket_path = os.environ["DOCKER_HOST"][len("unix://"):]
        for name, workdir in (("ci", cls.ci_workdir), ("release", cls.release_workdir), ("vps", None)):
            secret = cls.jenkins.script(f"println(jenkins.model.Jenkins.get().getComputer('{name}').jnlpMac)").strip()
            arguments = ["run", "--detach", f"--name={cls.names[name]}", f"--network={cls.network}", "--init",
                         "--user=0:0", "--env=JENKINS_URL=http://jenkins:8080", "--env=JENKINS_WEB_SOCKET=true",
                         f"--env=JENKINS_AGENT_NAME={name}", f"--env=JENKINS_SECRET={secret}"]
            if name in ("vps", "release"):
                arguments += ["--env=YGG_GITHUB_SH=/opt/github-double/github.sh",
                              f"--volume={cls.github_double}:/opt/github-double"]
            if name == "release":
                # The local feed instead of nuget.org, and its CA: a host path, which nuget-push.sh's
                # `docker run` mounts (the certificates are at the same path in the agent).
                arguments += [f"--env=YGG_NUGET_SOURCE={cls.feed_url}/v3/index.json",
                              f"--env=YGG_NUGET_CA_FILE={cls.certificates}/ca.crt",
                              f"--volume={cls.certificates}:{cls.certificates}:ro"]
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
        # The owner-tagged repositories: their files on main (before the rules protect it), develop cut from it.
        for repo, files in OWNER_TAGGED.items():
            cls.new_repository(repo)
            for path, text in files.items():
                cls.change_file(OWNER, repo, path, text, "main")
            cls.api[OWNER].request("POST", f"/repos/{OWNER}/{repo}/branches",
                                   {"new_branch_name": "develop", "old_branch_name": "main"})
        # The release applications: yggdrasil.yml on develop, which a release branch is a snapshot of.
        for repo, pipeline in RELEASE_APPLICATIONS.items():
            cls.new_repository(repo)
            cls.api[OWNER].request("POST", f"/repos/{OWNER}/{repo}/branches",
                                   {"new_branch_name": "develop", "old_branch_name": "main"})
            cls.change_file(OWNER, repo, "yggdrasil.yml", yaml.safe_dump(pipeline, sort_keys=False), "develop")

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
        self.assertEqual(set(boot["jobs"]), {APP, WEB, PLAIN, LIB, WORKER, *RELEASE_APPLICATIONS, *OWNER_TAGGED})
        # The actions-release, release-resume and tag-check jobs: pipeline jobs running the scripts kept in
        # Jenkins, in the sandbox; tag-check takes no parameters and runs every day.
        parameters = {"actions-release": ["REPOSITORY", "TAG"], "release-resume": ["REPOSITORY", "TAG", "COMMIT"],
                      "tag-check": ["FORGET"]}
        self.assertEqual(boot["pipeline_jobs"], {name: {
            "sandbox": True, "parameters": wanted,
            "script": (ROOT / "jenkins" / f"{name}.groovy").read_text(encoding="utf-8")}
            for name, wanted in parameters.items()})
        self.assertEqual(boot["triggers"], {"actions-release": [], "release-resume": [],
                                            "tag-check": [{"type": "TimerTrigger", "spec": "H 4 * * *"}]})
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
        # Four domainCredentials lists: casc.forgejo.yaml, casc.github-app.yaml, casc.nuget.yaml (the optional
        # NuGet key, its file mounted) and casc.test.yaml.
        self.assertEqual(self.second_boot["credentials"], {**self.first_boot["credentials"],
                                                           "github-app": "GitHubAppCredentials",
                                                           "nuget-api-key": "StringCredentialsImpl"})
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

    def test_given_a_check_asking_for_a_release_credential_when_built_then_the_build_fails_before_it_runs(self):
        # shop-desktop's release build may bind shop-db-password (releaseCredentials); a check, which runs
        # any branch's code, may not, although Jenkins has the credential.
        check = {**UNIT, "credentials": {"SIGNING_KEY": "shop-db-password"}}
        branch = "feature/release-credential"
        sha = self.branch_with(CLAUDE, DESKTOP, branch, {"yggdrasil.yml": pipeline_file(unit=check)})
        number, build = self.pull_request_build(CLAUDE, DESKTOP, branch)
        self.assertEqual(build.result, "FAILURE")
        self.assertIn("yggdrasil.yml cannot be used", build.console)
        self.assertIn(f"credential 'shop-db-password' is not allowed for {DESKTOP} (its catalog credentials: [])",
                      build.console)
        self.assertNotIn(UNIT_RAN, build.console)
        self.assertNotIn("(Checks)", build.console)
        self.assertEqual(self.statuses(DESKTOP, sha), [])

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
        # yggdrasil's scripts at the library's tag (main's deploy.sh never ran), its stacks/ from main.
        self.assertIn("DEPLOY-OWNER-DATA from-main", build.console)
        self.assertNotIn("DEPLOY-FROM-MAIN", build.console)
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
            # Built -- and refused: these tags are on a feature branch, not on main.
            self.assertIn("Not released: the tag is not on main", built.console)
            self.assertEqual(built.result, "FAILURE")
        deadline = time.monotonic() + 120
        scan = ""
        while "Finished:" not in scan or "Checking tag v1.0.0" not in scan:
            self.assertLess(time.monotonic(), deadline, scan)
            time.sleep(2)
            scan = self.jenkins.request("GET", job_path(LIB) + "/indexing/consoleText")[1]
        self.assertIn("No automatic build triggered for v1.0.0", scan)
        self.assertEqual(self.builds(LIB, "v1.0.0"), [], scan)

    # --- Releases ---------------------------------------------------------------------------------

    def release_run(self, repo, version="1.0.0", skip=False):
        """The owner's release pull request release/<version> -> main (the branch cut from develop) and its
        build, once finished: (pull request number, Build, the GitHub double's log and the release of
        v<version> at that moment -- later tests may change both). With
        `skip`, the skip is answered "Deploy without GitHub Actions checks" once the build asks. Each
        release is built once for the whole class: several tests look at the same one."""
        key = (repo, version)
        if key not in self.release_runs:
            branch = f"release/{version}"
            self.api[OWNER].request("POST", f"/repos/{OWNER}/{repo}/branches",
                                    {"new_branch_name": branch, "old_branch_name": "develop"})
            number = self.open_pull_request(OWNER, repo, branch, "main")
            job_branch = f"PR-{number}"
            if skip:
                deadline = time.monotonic() + 900
                while SKIP_QUESTION not in self.console(repo, job_branch, 1):
                    finished = [b for b in self.builds(repo, job_branch) if b["number"] == 1 and not b["building"]]
                    self.assertFalse(finished, self.console(repo, job_branch, 1)[-4000:])
                    self.assertLess(time.monotonic(), deadline, "the build never asked to skip")
                    time.sleep(3)
                self.jenkins.post(f"{job_path(repo, job_branch)}/1/input/SkipActions/proceedEmpty")
            build = self.wait_for_build(repo, job_branch, after=0, timeout=1200)
            self.release_runs[key] = (number, build, self.github_calls(), self.release_of(repo, f"v{version}"))
        return self.release_runs[key]

    def release_of(self, repo, tag):
        """The release of the tag, a draft too (the owner sees drafts), or None."""
        found = [r for r in self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/releases?limit=50") or []
                 if r["tag_name"] == tag]
        return found[0] if found else None

    def tag_commit(self, repo, tag):
        try:
            return self.api[OWNER].request("GET", f"/repos/{OWNER}/{repo}/tags/{tag}")["commit"]["sha"]
        except rules.ApiError as error:
            if error.status == 404:
                return None
            raise

    def description(self, job, branch, number):
        return self.jenkins.json(f"{job_path(job, branch)}/{number}/api/json?tree=description")["description"]

    def test_given_a_release_pull_request_when_built_then_it_deploys_merges_tags_and_publishes(self):
        number, build, _, _ = self.release_run(RELEASE)
        self.assertEqual(build.result, "SUCCESS", build.console[-6000:])
        pull = self.pull_request(RELEASE, number)
        head, merge = pull["head"]["sha"], pull["merge_commit_sha"]
        self.assertTrue(pull["merged"])
        # Checked (unit is a deploy input: run even when develop's build checked the same commit), then
        # deployed (the head) with its archive, then merged and tagged on the publish agent, then published.
        self.assertIn("Check unit: run (run: a deploy input, its archive goes into the image)", build.console)
        self.assertLess(build.console.index("(Branch policy)"), build.console.index("DEPLOY-STUB"))
        deployed = build.console.index(f"DEPLOY-STUB environment=production stack={RELEASE} checkout=app version=1.0.0-{head[:7]}")
        self.assertRegex(build.console[deployed:].splitlines()[0], r" inputs=/\S+/deploy-inputs$")
        self.assertIn("DEPLOY-INPUT input", build.console[deployed:])
        self.assertLess(deployed, build.console.index(f"Merged as {merge[:7]}, tagged v1.0.0."))
        self.assertLess(build.console.index("Merged as"), build.console.index("Released v1.0.0:"))
        latest = self.latest_statuses(RELEASE, head)
        self.assertEqual(latest["deploy/production"][1:4], ("success", JENKINS, "Deployed to Production"))
        self.assertEqual([s[1] for s in self.statuses(RELEASE, head) if s[0] == "deploy/production"], ["pending", "success"])
        self.assertEqual(latest["branch-policy"][1], "success")
        # The merge's title is the one release_notes.py leaves out; the tag is on the merge commit.
        message = self.api[OWNER].request("GET", f"/repos/{OWNER}/{RELEASE}/git/commits/{merge}")["commit"]["message"]
        self.assertTrue(message.startswith(f"release: v1.0.0 (#{number})"), message)
        self.assertEqual(self.tag_commit(RELEASE, "v1.0.0"), merge)
        release = self.release_of(RELEASE, "v1.0.0")
        self.assertIsNotNone(release)
        self.assertEqual((release["name"], release["draft"], release["assets"]), ("v1.0.0", False, []))
        self.assertEqual(release["author"]["login"], JENKINS)
        self.assertEqual(self.description(RELEASE, f"PR-{number}", build.number), "v1.0.0")
        self.assert_no_secret(build.console, "the build log")

    def test_given_the_release_branch_when_released_then_it_is_kept_with_a_note(self):
        number, build, _, _ = self.release_run(RELEASE)
        self.assertEqual(build.result, "SUCCESS", build.console[-6000:])
        # Forgejo protects release/* (rules.py): delete-branch exits 3, which is a note, not a failure.
        self.assertIn(f"release/1.0.0 {KEPT}", build.console)
        self.assertTrue(self.branch_exists(RELEASE, "release/1.0.0"))

    def test_given_release_files_when_released_then_the_forgejo_release_has_them_and_is_published(self):
        number, build, calls, _ = self.release_run(DESKTOP)
        self.assertEqual(build.result, "SUCCESS", build.console[-8000:])
        release = self.release_of(DESKTOP, "v1.0.0")
        self.assertIsNotNone(release)
        self.assertFalse(release["draft"])
        assets = {asset["name"]: asset["size"] for asset in release["assets"]}
        # The declared globs' files only: not dist/notes.txt, not the artifact's debug.pdb.
        self.assertEqual(set(assets), {"shop-desktop-1.0.0.tar.gz", "shop-desktop.sha256", "shop-desktop-setup.msi"})
        self.assertEqual(assets["shop-desktop-1.0.0.tar.gz"], len("shop-desktop 1.0.0\n"))
        self.assertEqual(assets["shop-desktop-setup.msi"], len("desktop installer\n"))
        # The validation is pass or fail only: what it added or rewrote after the files were gathered
        # (TAMPERED-2: it did) is not in the release -- no extra file, the built files at their sizes.
        self.assertIn("TAMPERED-2", build.console)
        self.assertEqual(assets["shop-desktop.sha256"], len(f"{'0' * 64}  dist/shop-desktop-1.0.0.tar.gz\n"))
        # The validation ran on the release agent over the whole set (both files and the installer).
        self.assertIn("VALIDATED-3-FILES", build.console)
        # The allow-listed credential reached the release build (its digest; the log masks the value).
        self.assertIn(f"BUILD-KEY {hashlib.sha256(DB_PASSWORD.encode()).hexdigest()}", build.console)
        self.assertNotIn(DB_PASSWORD, build.console)
        # GitHub: the tag waited for, the workflow dispatched on it, its run watched and its files
        # downloaded -- every call on the publish agent (vps), with the App's token; none on release.
        mine = [line for line in calls if f" args={DESKTOP} " in f"{line} "]
        self.assertEqual([line.split()[0] for line in mine], ["wait-ref", "dispatch", "run-status", "download-artifacts"],
                         mine)
        self.assertIn(f"args={DESKTOP} release-windows.yml v1.0.0 tag=v1.0.0", mine[1])
        for line in mine:
            self.assertIn(f"agent=vps token=set owner={MIRROR_OWNER} ", line)
        self.assertEqual([line for line in calls if "agent=release" in line], [])
        self.assertEqual(self.description(DESKTOP, f"PR-{number}", build.number), "v1.0.0")

    def test_given_a_failing_validation_when_released_then_no_release_is_made_and_resume_is_named(self):
        number, build, _, _ = self.release_run(BROKEN)
        self.assertEqual(build.result, "FAILURE", build.console[-6000:])
        self.assertIn("VALIDATION-3-FAILED", build.console)
        self.assertIn("release.validate failed (exit 3): nothing is published", build.console)
        # After the merge a rerun cannot finish it: the message says how.
        self.assertIn(f"Not released: the pull request is merged and tagged v1.0.0; finish the release with the "
                      f"release-resume job (REPOSITORY={BROKEN}, TAG=v1.0.0).", build.console)
        self.assertNotIn("(Publish the release)", build.console)
        # publish-release.sh, which makes the drafts, never ran: no release at all, draft or not. The merge
        # and the tag came before the files (the release's commit is the merge).
        self.assertIsNone(self.release_of(BROKEN, "v1.0.0"))
        self.assertTrue(self.pull_request(BROKEN, number)["merged"])
        self.assertIsNotNone(self.tag_commit(BROKEN, "v1.0.0"))

    def test_given_github_not_answering_when_released_then_the_skip_is_offered_and_recorded(self):
        # The release as this build left it (the actions-release test may have attached files since).
        number, build, calls, release = self.release_run(TOOLS, skip=True)
        self.assertEqual(build.result, "UNSTABLE", build.console[-8000:])
        console = build.console
        # Asked after checksTimeout (20 s), outside any agent, answered by the owner; then recorded.
        self.assertIn("GitHub Actions checks didn't finish within 20 s (actions-status exit 2): Console (Windows).", console)
        self.assertIn(SKIP_QUESTION, console)
        self.assertIn("Approved by admin", console)
        self.assertIn("Deploying without GitHub Actions checks: Console (Windows) did not finish.", console)
        # mirror.releases is off here: no GitHub copy to leave pending (static tests pin that case).
        self.assertEqual(self.description(TOOLS, f"PR-{number}", build.number),
                         "v1.0.0 (deployed without GitHub Actions checks)")
        self.assertNotIn("GitHub copy pending", console)
        head = self.pull_request(TOOLS, number)["head"]["sha"]
        deploys = [s for s in self.statuses(TOOLS, head) if s[0] == "deploy/production"]
        self.assertEqual([(s[1], s[3]) for s in deploys],
                         [("pending", "Waiting to deploy to Production (without GitHub Actions checks)"),
                          ("success", "Deployed to Production (without GitHub Actions checks)")])
        # Published without the Windows files, which are listed as missing; never asked for.
        self.assertIsNotNone(release)
        self.assertFalse(release["draft"])
        self.assertEqual([asset["name"] for asset in release["assets"]], ["shop-tools.tar.gz"])
        self.assertIn("## Missing files", release["body"])
        self.assertIn("- */*.msi", release["body"])
        self.assertIn("v1.0.0 was published without its Windows files: */*.msi (GitHub Actions checks were skipped)", console)
        mine = [line for line in calls if f" args={TOOLS} " in f"{line} "]
        self.assertTrue(mine)
        self.assertEqual({line.split()[0] for line in mine}, {"actions-status"})
        for line in mine:
            self.assertIn(f"agent=vps token=set owner={MIRROR_OWNER} ", line)

    def test_given_a_skipped_release_when_actions_release_runs_then_the_files_are_attached(self):
        number, build, _, published = self.release_run(TOOLS, skip=True)
        self.assertEqual(build.result, "UNSTABLE", build.console[-6000:])
        self.assertEqual([a["name"] for a in published["assets"]], ["shop-tools.tar.gz"])

        def actions_release(repository, tag):
            before = self.last_number("actions-release", None)
            self.jenkins.post(job_path("actions-release") + "/buildWithParameters", {"REPOSITORY": repository, "TAG": tag})
            return self.wait_for_build("actions-release", None, after=before, timeout=900)

        # REPOSITORY must be a catalog entry, TAG one of its tags that exists, and the entry must declare
        # release.windows: each checked before anything is asked of GitHub.
        unknown = actions_release("not-in-catalog", "v1.0.0")
        self.assertEqual(unknown.result, "FAILURE")
        self.assertIn("'not-in-catalog' is not an application or a repositories entry", unknown.console)
        absent = actions_release(TOOLS, "v9.9.9")
        self.assertEqual(absent.result, "FAILURE")
        self.assertIn(f"{TOOLS} has no tag v9.9.9.", absent.console)
        self.release_run(RELEASE)  # its v1.0.0 exists; its yggdrasil.yml has no release at all
        no_windows = actions_release(RELEASE, "v1.0.0")
        self.assertEqual(no_windows.result, "FAILURE")
        self.assertIn("declares no release.windows", no_windows.console)

        before = len(self.github_calls(TOOLS))
        attached = actions_release(TOOLS, "v1.0.0")
        self.assertEqual(attached.result, "SUCCESS", attached.console[-8000:])
        self.assertIn(f"Loading library yggdrasil@{LIBRARY_VERSION}", attached.console)
        release = self.release_of(TOOLS, "v1.0.0")
        self.assertEqual(sorted(a["name"] for a in release["assets"]), ["shop-tools-setup.msi", "shop-tools.tar.gz"])
        self.assertFalse(release["draft"])
        calls = self.github_calls(TOOLS)[before:]
        self.assertEqual([line.split()[0] for line in calls], ["wait-ref", "dispatch", "run-status", "download-artifacts"],
                         calls)
        self.assertIn(f"args={TOOLS} release-windows.yml v1.0.0 tag=v1.0.0", calls[1])
        for line in calls:
            self.assertIn(f"agent=vps token=set owner={MIRROR_OWNER} ", line)
        self.assertEqual(self.description("actions-release", None, attached.number), f"{TOOLS} v1.0.0: Windows files attached")

    # --- Owner-tagged releases, NuGet packages and release-resume ------------------------------------

    def tag_run(self, repo, tag, ref="main", new_branch=None):
        """The owner's annotated tag (a lightweight one on an older commit is never built, see the tag-age
        test) on the head of `ref` -- or, with `new_branch`, on a new commit of that branch cut from `ref` --
        pushed, then its build once finished: (the tagged commit, Build). Each tag is pushed and built once
        for the whole class."""
        key = (repo, tag)
        if key not in self.tag_runs:
            clone = self.clone(OWNER, repo)
            steps = ([("checkout", "--quiet", "-b", new_branch, f"origin/{ref}"),
                      ("commit", "--quiet", "--allow-empty", "-m", f"a commit for {tag}")] if new_branch
                     else [("checkout", "--quiet", "--detach", f"origin/{ref}")])
            steps += [("tag", "--annotate", tag, "-m", f"version {tag}"),
                      ("push", "--quiet", "origin", *([new_branch] if new_branch else []), tag)]
            for arguments in steps:
                done = self.git(OWNER, *arguments, cwd=clone)
                self.assertEqual(done.returncode, 0, done.stderr)
            sha = self.git(OWNER, "rev-parse", "HEAD", cwd=clone).stdout.strip()
            # The webhook's event may have started a build already; a scan makes sure the tag is seen.
            self.jenkins.post(job_path(repo) + "/build", {"delay": "0"})
            self.tag_runs[key] = (sha, self.wait_for_build(repo, tag, after=0, timeout=1200))
        return self.tag_runs[key]

    def release_resume(self, repository, tag, commit=""):
        before = self.last_number("release-resume", None)
        self.jenkins.post(job_path("release-resume") + "/buildWithParameters",
                          {"REPOSITORY": repository, "TAG": tag, "COMMIT": commit})
        return self.wait_for_build("release-resume", None, after=before, timeout=1200)

    def add_secret_text(self, credential, secret):
        answer = self.jenkins.script(
            "import com.cloudbees.plugins.credentials.CredentialsScope\n"
            "import com.cloudbees.plugins.credentials.SystemCredentialsProvider\n"
            "import com.cloudbees.plugins.credentials.domains.Domain\n"
            "import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl\n"
            "def store = SystemCredentialsProvider.getInstance().getStore()\n"
            f"println(store.addCredentials(Domain.global(), new StringCredentialsImpl(CredentialsScope.GLOBAL, "
            f"'{credential}', 'added by a test', hudson.util.Secret.fromString('{secret}'))))\n")
        self.assertEqual(answer.strip(), "true", answer)

    def assets(self, release):
        return {asset["name"]: asset["size"] for asset in release["assets"]}

    def test_given_an_owner_tag_on_main_when_built_then_the_release_is_published(self):
        sha, build = self.tag_run(OWNED, "v1.0.0")
        self.assertEqual(build.result, "SUCCESS", build.console[-8000:])
        console = build.console
        self.assertEqual(sha, self.head(OWNED, "main"))
        # On main, checked before anything of the repository ran; then built on the release agent and
        # published by jenkins, as a release pull request's files are after its merge.
        self.assertIn(f"v1.0.0 is on {sha[:7]}, on main.", console)
        self.assertLess(console.index("on main."), console.index("TOOL-BUILT-2"))
        release = self.release_of(OWNED, "v1.0.0")
        self.assertIsNotNone(release)
        self.assertFalse(release["draft"])
        self.assertEqual(release["author"]["login"], JENKINS)
        self.assertEqual(self.assets(release), {"ygg-tool-1.0.0.tar.gz": len("ygg-tool 1.0.0\n")})
        self.assertEqual(self.tag_commit(OWNED, "v1.0.0"), sha)
        self.assertEqual(self.description(OWNED, "v1.0.0", build.number), "v1.0.0: released")
        self.assert_no_secret(console, "the build log")
        # No Windows files and no GitHub copy (mirror.releases is off): GitHub was never asked.
        self.assertEqual(self.github_calls(OWNED), [])

    def test_given_an_owner_tag_off_main_when_built_then_it_fails_before_releasing(self):
        sha, build = self.tag_run(OWNED, "v0.9.0", ref="develop", new_branch="feature/off-main")
        self.assertEqual(build.result, "FAILURE", build.console[-6000:])
        self.assertIn(f"Not released: the tag is not on main (v0.9.0 is on {sha[:7]}, which main does not contain).",
                      build.console)
        # Nothing of the repository ran (the build command's text is not even in the trace), nothing published.
        for later in ("TOOL-BUILT", "(Release files)", "(Publish the release)"):
            self.assertNotIn(later, build.console)
        self.assertIsNone(self.release_of(OWNED, "v0.9.0"))

    def test_given_a_library_tag_when_built_then_the_packages_are_pushed_to_the_source(self):
        sha, build = self.tag_run(LIBRARY, "1.2.3")
        self.assertEqual(build.result, "SUCCESS", build.console[-8000:])
        console = build.console
        self.assertIn(f"1.2.3 is on {sha[:7]}, on main.", console)
        # Packed in the library's own command without the key in its environment, then pushed with it by
        # nuget-push.sh, to the feed the release agent names (YGG_NUGET_SOURCE).
        self.assertIn("PACK-SAW-KEY-0", console)
        pushed = console.index(f"nuget-push: pushing {PACKAGE_ID}.1.2.3.nupkg to {self.feed_url}/v3/index.json")
        self.assertLess(console.index("PACK-SAW-KEY-0"), pushed)
        self.assertIn("Your package was pushed.", console)
        self.assertEqual(self.feed_versions(PACKAGE_ID), ["1.2.3"])
        self.assertNotIn(NUGET_KEY, console)
        # A library's packages are its release: nothing on the git server.
        self.assertIsNone(self.release_of(LIBRARY, "1.2.3"))
        self.assertEqual(self.description(LIBRARY, "1.2.3", build.number), "1.2.3: packages pushed")

    def test_given_release_files_and_packages_when_the_validation_fails_then_no_package_is_pushed(self):
        # The release files are built and validated before the packages: a push cannot be taken back.
        _, build = self.tag_run(BOTH, "v1.0.0")
        self.assertEqual(build.result, "FAILURE", build.console[-6000:])
        self.assertIn("BOTH-VALIDATION-3-FAILED", build.console)
        self.assertIn("release.validate failed (exit 3)", build.console)
        # The pack command never ran (not even its text in the trace), nothing was pushed or published.
        for later in ("BOTH-PACKED", "(Packages)", "nuget-push", "(Publish the release)"):
            self.assertNotIn(later, build.console)
        self.assertIsNone(self.release_of(BOTH, "v1.0.0"))

    def test_given_a_library_tag_pushed_already_when_release_resume_runs_then_the_push_skips_the_duplicate(self):
        _, build = self.tag_run(LIBRARY, "1.2.3")
        self.assertEqual(build.result, "SUCCESS", build.console[-6000:])
        resumed = self.release_resume(LIBRARY, "1.2.3")
        self.assertEqual(resumed.result, "SUCCESS", resumed.console[-6000:])
        self.assertIn("already exists at feed", resumed.console)
        self.assertEqual(self.feed_versions(PACKAGE_ID), ["1.2.3"])
        self.assertEqual(self.description("release-resume", None, resumed.number), f"{LIBRARY} 1.2.3: packages pushed")

    def test_given_a_release_that_failed_after_its_merge_when_release_resume_runs_then_it_is_finished(self):
        # The release build binds shop-resume-key, which the catalog allows and Jenkins does not have yet:
        # the release fails after its merge and its tag.
        number, build, _, _ = self.release_run(RESUME)
        self.assertEqual(build.result, "FAILURE", build.console[-6000:])
        self.assertIn("shop-resume-key", build.console)
        self.assertIn(f"Not released: the pull request is merged and tagged v1.0.0; finish the release with the "
                      f"release-resume job (REPOSITORY={RESUME}, TAG=v1.0.0).", build.console)
        self.assertIsNone(self.release_of(RESUME, "v1.0.0"))
        merge = self.pull_request(RESUME, number)["merge_commit_sha"]
        self.assertEqual(self.tag_commit(RESUME, "v1.0.0"), merge)

        # The parameters are checked first: a catalog entry that releases, one of its version tags, that
        # exists and is on main.
        for repository, tag, message in (
                ("not-in-catalog", "v1.0.0", "'not-in-catalog' is not an application or a repositories entry"),
                (PLAIN, "v1.0.0", f"{PLAIN} does not release"),
                (RESUME, "1.0.0", f"'1.0.0' is not a version tag of {RESUME} (v*)"),
                (RESUME, "v9.9.9", f"{RESUME} has no tag v9.9.9.")):
            refused = self.release_resume(repository, tag)
            self.assertEqual(refused.result, "FAILURE", (repository, tag, refused.console[-3000:]))
            self.assertIn(message, refused.console)
        self.tag_run(OWNED, "v0.9.0", ref="develop", new_branch="feature/off-main")
        refused = self.release_resume(OWNED, "v0.9.0")
        self.assertEqual(refused.result, "FAILURE")
        self.assertIn("Not released: the tag is not on main", refused.console)
        self.assertNotIn("TOOL-BUILT", refused.console)

        # With the credential there, the release is finished from its tag: built, published.
        self.add_secret_text("shop-resume-key", RESUME_KEY)
        resumed = self.release_resume(RESUME, "v1.0.0")
        self.assertEqual(resumed.result, "SUCCESS", resumed.console[-8000:])
        self.assertIn(f"Loading library yggdrasil@{LIBRARY_VERSION}", resumed.console)
        self.assertIn(f"v1.0.0 is on {merge[:7]}, on main.", resumed.console)
        self.assertIn(f"RESUME-KEY {hashlib.sha256(RESUME_KEY.encode()).hexdigest()}", resumed.console)
        self.assertNotIn(RESUME_KEY, resumed.console)
        release = self.release_of(RESUME, "v1.0.0")
        self.assertFalse(release["draft"])
        self.assertEqual(release["author"]["login"], JENKINS)
        self.assertEqual(self.assets(release), {"shop-resume.tar.gz": len("resumed 1.0.0\n")})
        self.assertEqual(self.description("release-resume", None, resumed.number), f"{RESUME} v1.0.0: released")
        # Run again, it changes nothing: the tag, the release and its file are reused.
        again = self.release_resume(RESUME, "v1.0.0")
        self.assertEqual(again.result, "SUCCESS", again.console[-6000:])
        self.assertEqual(self.release_of(RESUME, "v1.0.0")["assets"], release["assets"])

    def test_given_a_release_merged_but_not_tagged_when_release_resume_gets_its_commit_then_it_is_tagged_and_released(self):
        # The release pull request waits for the owner's approval before production. Meanwhile jenkins (the
        # only user who may create v* tags) puts v1.0.0 on develop's head; approved, the release deploys and
        # merges, then cannot tag its merge commit: merged, not tagged.
        branch = "release/1.0.0"
        develop = self.head(UNTAGGED, "develop")
        self.api[OWNER].request("POST", f"/repos/{OWNER}/{UNTAGGED}/branches",
                                {"new_branch_name": branch, "old_branch_name": "develop"})
        number = self.open_pull_request(OWNER, UNTAGGED, branch, "main")
        job = f"{UNTAGGED}/PR-{number}"
        deadline = time.monotonic() + 900
        inputs = []
        while not inputs:
            self.assertLess(time.monotonic(), deadline, self.console(UNTAGGED, f"PR-{number}", 1)[-4000:])
            time.sleep(3)
            inputs = self.jenkins.script_json(
                f"(jenkins.model.Jenkins.get().getItemByFullName('{job}')?.getBuildByNumber(1)"
                "?.getAction(org.jenkinsci.plugins.workflow.support.steps.input.InputAction)?.executions ?: [])"
                ".collect { it.id }")
        self.api[JENKINS].request("POST", f"/repos/{OWNER}/{UNTAGGED}/tags", {"tag_name": "v1.0.0", "target": develop})
        self.jenkins.post(f"{job_path(UNTAGGED, f'PR-{number}')}/1/input/{inputs[0]}/proceedEmpty")
        build = self.wait_for_build(UNTAGGED, f"PR-{number}", after=0, timeout=1200)
        self.assertEqual(build.result, "FAILURE", build.console[-6000:])
        merge = self.pull_request(UNTAGGED, number)["merge_commit_sha"]
        self.assertTrue(self.pull_request(UNTAGGED, number)["merged"])
        self.assertIn(f"The tag v1.0.0 is on {develop[:7]}, not on the release's {merge[:7]}.", build.console)
        self.assertIn(f"Not released: the pull request is merged as {merge}, but not tagged v1.0.0; finish the release "
                      f"with the release-resume job (REPOSITORY={UNTAGGED}, TAG=v1.0.0, COMMIT={merge}), which tags "
                      "the merge commit first", build.console)
        # The tag in the way is removed (by jenkins: nobody else may touch a v* tag).
        self.api[JENKINS].request("DELETE", f"/repos/{OWNER}/{UNTAGGED}/tags/v1.0.0")
        self.assertIsNone(self.tag_commit(UNTAGGED, "v1.0.0"))

        # Refused: a COMMIT that is not hex; an owner-tagged repository (its tags are the owner's to make);
        # develop's head, an ancestor of main but merged into it from the release branch, so off main's
        # first-parent line; and no COMMIT at all, since there is no tag.
        for repository, commit, message in (
                (UNTAGGED, "main", "'main' is not a commit"),
                (OWNED, merge, f"COMMIT is only for an application's release"),
                (UNTAGGED, develop, f"{develop[:7]} is not one of main's own merges (on main's first-parent line)."),
                (UNTAGGED, "", f"{UNTAGGED} has no tag v1.0.0.")):
            refused = self.release_resume(repository, "v1.0.0", commit)
            self.assertEqual(refused.result, "FAILURE", (repository, commit, refused.console[-3000:]))
            self.assertIn(message, refused.console)
        self.assertIsNone(self.tag_commit(UNTAGGED, "v1.0.0"))

        # Given the merge commit: tagged by jenkins on it, then released.
        resumed = self.release_resume(UNTAGGED, "v1.0.0", merge)
        self.assertEqual(resumed.result, "SUCCESS", resumed.console[-8000:])
        self.assertIn(f"{merge[:7]} is the merge of the release v1.0.0, on main.", resumed.console)
        self.assertEqual(self.tag_commit(UNTAGGED, "v1.0.0"), merge)
        self.assertIn(f"v1.0.0 is on {merge[:7]}, on main.", resumed.console)
        release = self.release_of(UNTAGGED, "v1.0.0")
        self.assertFalse(release["draft"])
        self.assertEqual(release["author"]["login"], JENKINS)
        self.assertEqual(self.description("release-resume", None, resumed.number), f"{UNTAGGED} v1.0.0: released")
        self.assert_no_secret(resumed.console, "the release-resume log")

    # --- tag-check ---------------------------------------------------------------------------------

    def tag_check(self, forget=""):
        before = self.last_number("tag-check", None)
        self.jenkins.post(job_path("tag-check") + "/buildWithParameters", {"FORGET": forget})
        return self.wait_for_build("tag-check", None, after=before, timeout=900)

    def wait_for_release(self, repo, tag, draft, seconds=60):
        """Whether the tag's release is a draft (draft=True) or published (False) within `seconds`: Forgejo
        changes it after a push that deletes or restores its tag, in the background."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            release = self.release_of(repo, tag)
            if release is not None and release["draft"] is draft:
                return True
            time.sleep(1)
        return False

    def jenkins_moves_tags(self, repo, moves):
        """jenkins (the only user Forgejo lets change an application's v* tags) pushes each tag of `moves`
        ({tag: commit, or None to delete it}) with git, as anyone on the tag protection's list could. Each
        commit must be on a branch of the repository (the clone has every branch)."""
        clone = self.clone(JENKINS, repo)
        for tag, commit in moves.items():
            if commit is None:
                done = self.git(JENKINS, "push", "origin", f":refs/tags/{tag}", cwd=clone)
            else:
                tagged = self.git(JENKINS, "tag", "--force", tag, commit, cwd=clone)
                self.assertEqual(tagged.returncode, 0, tagged.stderr)
                done = self.git(JENKINS, "push", "--force", "origin", f"refs/tags/{tag}", cwd=clone)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_given_released_tags_when_jenkins_moves_one_and_deletes_another_then_tag_check_names_both(self):
        # Releases of shop-api made the way publish-release.sh makes them: jenkins tags main's head and
        # publishes a release whose target is that commit. v0.3.0's release names a branch instead (one
        # made by hand), so whether its tag moved is not known. The other tests' releases are checked too.
        main = self.head(APP, "main")
        for tag, target in (("v0.1.0", main), ("v0.2.0", main), ("v0.3.0", "main"), ("v0.4.0", main)):
            self.api[JENKINS].request("POST", f"/repos/{OWNER}/{APP}/tags", {"tag_name": tag, "target": main})
            self.api[JENKINS].request("POST", f"/repos/{OWNER}/{APP}/releases", {
                "tag_name": tag, "target_commitish": target, "name": tag, "body": "", "draft": False,
                "prerelease": False})

        untouched = self.tag_check()
        self.assertEqual(untouched.result, "SUCCESS", untouched.console[-6000:])
        self.assertIn(f"Loading library yggdrasil@{LIBRARY_VERSION}", untouched.console)
        self.assertIn("Running on ci", untouched.console)
        for tag in ("v0.1.0", "v0.2.0"):
            self.assertIn(f"{APP} {tag}: ok ({main[:7]})", untouched.console)
        self.assertIn(f"{APP} v0.3.0: unknown target: released from 'main', not a commit", untouched.console)
        self.assertRegex(self.description("tag-check", None, untouched.number), r"^[0-9]+ release tags checked$")

        # jenkins moves v0.1.0 onto another commit and deletes v0.2.0 (Forgejo allows it: the accepted gap).
        other = self.change_file(OWNER, APP, "tag-check.txt", "another commit\n", "develop", "test/tag-check")
        self.assertNotEqual(other, main)
        self.jenkins_moves_tags(APP, {"v0.1.0": other, "v0.2.0": None})
        self.assertEqual((self.tag_commit(APP, "v0.1.0"), self.tag_commit(APP, "v0.2.0")), (other, None))
        # Forgejo turns the deleted tag's release into a draft, which the read credential does not see: the
        # job finds it in the record its previous run archived.
        self.assertTrue(self.wait_for_release(APP, "v0.2.0", draft=True), "v0.2.0's release is still published")
        broken = self.tag_check()
        self.assertEqual(broken.result, "FAILURE", broken.console[-6000:])
        self.assertIn(f"MOVED: {APP} v0.1.0 (tag on {other}, released from {main})", broken.console)
        self.assertIn(f"DELETED: {APP} v0.2.0 (released from {main}): the tag is gone, and its release is no "
                      "longer published", broken.console)
        self.assertEqual(self.description("tag-check", None, broken.number), "2 release tags moved or deleted")
        # The problems are the last lines before the error, one each (in the order the releases were listed).
        first = min(broken.console.index(f"MOVED: {APP} v0.1.0"), broken.console.index(f"DELETED: {APP} v0.2.0"))
        tail = broken.console[first:]
        for line in (f"MOVED: {APP} v0.1.0", f"DELETED: {APP} v0.2.0"):
            self.assertEqual(tail.count(line), 1, tail)
        self.assertIn("ERROR: 2 release tags moved or deleted. Repair a moved or deleted tag", tail)
        self.assertNotIn(": ok (", tail)
        self.assert_no_secret(broken.console, "the tag-check log")

        # Put back where they were released from, the check passes again (and the later tests see no problem).
        # Pushing the deleted tag back publishes its release again (Forgejo does it, test_forgejo_live.py).
        self.jenkins_moves_tags(APP, {"v0.1.0": main, "v0.2.0": main})
        self.assertTrue(self.wait_for_release(APP, "v0.2.0", draft=False), "v0.2.0's release is still a draft")
        repaired = self.tag_check()
        self.assertEqual(repaired.result, "SUCCESS", repaired.console[-6000:])
        for tag in ("v0.1.0", "v0.2.0"):
            self.assertIn(f"{APP} {tag}: ok ({main[:7]})", repaired.console)
        self.assertEqual(self.description("tag-check", None, repaired.number),
                         self.description("tag-check", None, untouched.number))

        # A release deleted on purpose (its tag deleted; its release, a draft now, kept) stays red until FORGET
        # names it. v0.2.0, deleted again, is not forgotten: it is still reported, from the record.
        self.jenkins_moves_tags(APP, {"v0.4.0": None, "v0.2.0": None})
        for tag in ("v0.4.0", "v0.2.0"):
            self.assertTrue(self.wait_for_release(APP, tag, draft=True), f"{tag}'s release is still published")
        # An invalid line fails the build before anything is read.
        refused = self.tag_check(forget=f"{APP} v0.4.0
not-a-repository v1.0.0")
        self.assertEqual(refused.result, "FAILURE", refused.console[-4000:])
        self.assertIn("FORGET line 2 ('not-a-repository v1.0.0')", refused.console)
        self.assertNotIn("(Check the release tags)", refused.console)
        forgot = self.tag_check(forget=f"{APP} v0.4.0

{APP} v9.9.9
")
        self.assertEqual(forgot.result, "FAILURE", forgot.console[-6000:])
        self.assertIn(f"Forgotten: {APP} v0.4.0 (released from {main})", forgot.console)
        self.assertIn(f"FORGET: {APP} v9.9.9 is not in the record: nothing to forget", forgot.console)
        self.assertIn(f"DELETED: {APP} v0.2.0 (released from {main})", forgot.console)
        self.assertNotIn(f"DELETED: {APP} v0.4.0", forgot.console)
        self.assertEqual(self.description("tag-check", None, forgot.number), "1 release tag moved or deleted")
        # The next run starts from that build's record: v0.4.0 is gone from it, v0.2.0 is not.
        again = self.tag_check()
        self.assertEqual(again.result, "FAILURE", again.console[-6000:])
        self.assertIn(f"DELETED: {APP} v0.2.0", again.console)
        self.assertNotIn("v0.4.0", again.console)
        # v0.2.0 repaired: green, without v0.4.0.
        self.jenkins_moves_tags(APP, {"v0.2.0": main})
        self.assertTrue(self.wait_for_release(APP, "v0.2.0", draft=False), "v0.2.0's release is still a draft")
        clean = self.tag_check()
        self.assertEqual(clean.result, "SUCCESS", clean.console[-6000:])
        self.assertNotIn("v0.4.0", clean.console)

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
