"""Tests of templates/ (the site and library templates, the stack files) and renovate/default.json.

    python3 -m unittest discover -s scripts -p test_templates.py
    YGG_TEMPLATE_TESTS=1 python3 -m unittest discover -s scripts -p test_templates.py -v     # + the live ones

What always runs: every template's yggdrasil.yml validates with pipeline_config.load_config, every image any of
them names is pinned by version and digest, the Dockerfiles take the build arguments and run unprivileged, the
stack templates render under `docker compose config` with sample values (skipped without docker), the ci/*.sh
scripts of the library template behave against a fake `dotnet`, and the Renovate preset is valid JSON whose
regex finds a pinned image in a yggdrasil.yml.

What YGG_TEMPLATE_TESTS=1 adds (and Docker): each site template is built, in a temporary directory, with a tiny
sample site from scripts/fixtures/template-sites, and the container answers /healthz and a deep link under its
path with 200; the Renovate preset goes through Renovate's own validator. The containers are named
ygg-template-test-* and always removed, with their images. The sample sites that have dependencies (Vite, Next.js)
install them from npm, so those tests need the network.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
import uuid

import pipeline_config as pc

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
FIXTURES = ROOT / "scripts" / "fixtures" / "template-sites"
PRESET = ROOT / "renovate" / "default.json"

SITES = ["site-static", "site-hugo", "site-vite", "site-next-static", "site-next-server", "site-node-api"]
# The sites a static-web-server image serves: they take the public path and the host as build arguments.
STATIC_SITES = ["site-static", "site-hugo", "site-vite", "site-next-static"]
# Everything with a yggdrasil.yml that a repository starts from.
CONFIGURED = [*SITES, "library", "application"]

DIGEST = r"@sha256:[0-9a-f]{64}"
# name:version@sha256:<64 hex>, the name possibly with a registry and its port (host:5000/path/name).
PINNED = re.compile(r"^[^\s@]+:[A-Za-z0-9._-]+" + DIGEST + "$")

# The values the stacks read, as scripts/deploy.sh exports them.
SAMPLE_ENV = {
    "YGG_ENVIRONMENT": "production",
    "YGG_PUBLIC_HOST": "docs.example.com",
    "YGG_PUBLIC_PATH": "/shop-docs",
    "GIT_COMMIT": "0123456789abcdef0123456789abcdef01234567",
    "IMAGE_TAG": "production-1.2.3-0123456",
    "YGG_APEX_HOST": "example.com",
}

docker_available = shutil.which("docker") is not None
live = os.environ.get("YGG_TEMPLATE_TESTS") == "1"


def image_lines(path):
    """The images a file names, as (kind, reference): FROM lines of a Dockerfile, `image:` lines of a YAML file.
    `FROM x AS y` and `FROM --platform=... x` are understood; `FROM stage` (a stage of the same file) is not an
    image and is left out."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    found = []
    stages = set()
    for line in text.splitlines():
        stripped = line.strip()
        if pathlib.Path(path).name == "Dockerfile" or pathlib.Path(path).name.startswith("Dockerfile."):
            match = re.match(r"FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", stripped, re.IGNORECASE)
            if match:
                if match.group(1) not in stages:
                    found.append(("FROM", match.group(1)))
                if match.group(2):
                    stages.add(match.group(2))
        else:
            match = re.match(r"(?:-\s+)?image:\s*[\"']?([^\s\"'#]+)", stripped)
            if match:
                found.append(("image", match.group(1)))
    return found


class TemplateFilesTests(unittest.TestCase):
    def test_given_each_site_template_when_listed_then_it_has_a_dockerfile_a_config_and_a_readme(self):
        for name in SITES:
            for file in ("Dockerfile", "yggdrasil.yml", "README.md"):
                self.assertTrue((TEMPLATES / name / file).is_file(), f"templates/{name}/{file}")

    def test_given_each_template_when_its_yggdrasil_yml_is_loaded_then_it_validates(self):
        for name in CONFIGURED:
            with self.subTest(template=name):
                config = pc.load_config(TEMPLATES / name)
                self.assertIsNotNone(config)

    def test_given_the_library_template_when_loaded_then_it_validates(self):
        config = pc.load_config(TEMPLATES / "library")
        self.assertEqual(list(config["checks"]), ["test", "coverage"])
        self.assertEqual(config["checks"]["test"]["run"], "ci/test.sh")
        coverage = config["checks"]["coverage"]
        self.assertEqual(coverage["run"], "ci/coverage.sh")
        self.assertEqual(coverage["credentials"], {"REPORTGENERATOR_LICENSE": "reportgenerator-license"})
        self.assertEqual(coverage["archive"], "coverage-report/**")
        self.assertEqual(config["publish"]["nuget"]["run"], "ci/pack.sh")
        self.assertEqual(config["publish"]["nuget"]["packages"], "artifacts/*.nupkg")
        self.assertEqual(config["deploy"]["inputs"], ["coverage"])
        # One image for the checks and the packing: the SDK the libraries build with.
        images = {coverage["image"], config["checks"]["test"]["image"], config["publish"]["nuget"]["image"]}
        self.assertEqual(len(images), 1)
        self.assertTrue(images.pop().startswith("mcr.microsoft.com/dotnet/sdk:10.0."))

    def test_given_the_library_template_when_checked_against_a_library_and_its_docs_then_the_catalog_accepts_it(self):
        # The file serves two catalog entries: the library (test, coverage, NuGet) and its docs application
        # (no checks of its own, coverage as a deploy input).
        config = pc.load_config(TEMPLATES / "library")
        library = {"id": "dotnet-sample", "kind": "repository", "repository": "dotnet-sample",
                   "release": "owner-tag", "tags": "[0-9]*", "checks": ["test", "coverage"],
                   "credentials": ["reportgenerator-license"], "releaseCredentials": [], "actionsChecks": []}
        docs = {"id": "dotnet-sample-docs", "kind": "application", "repository": "dotnet-sample",
                "release": "none", "tags": "v*", "checks": [], "credentials": ["reportgenerator-license"],
                "releaseCredentials": [], "actionsChecks": []}
        self.assertEqual(pc.validate_against(config, library), [])
        # The docs job is judged on what it runs (coverage, its deploy input, whose licence its entry allows),
        # and not on publish, which is the releasing library's.
        self.assertEqual(pc.job_checks(config, docs, []), {"coverage"})
        self.assertEqual(pc.validate_against(config, docs, pc.job_checks(config, docs, []), "dotnet-sample"), [])
        self.assertEqual(pc.unclaimed_checks(config, [library, docs]), [])
        # The docs job runs coverage only because it deploys, and nothing else of the file.
        self.assertEqual(pc.needed_checks(config, docs, deploying=True), ["coverage"])
        self.assertEqual(pc.needed_checks(config, docs, deploying=False), [])
        self.assertEqual(pc.needed_checks(config, library), ["test", "coverage"])

    def test_given_each_template_when_its_images_are_read_then_every_one_is_pinned_by_version_and_digest(self):
        files = [TEMPLATES / name / file for name in CONFIGURED for file in ("Dockerfile", "yggdrasil.yml")
                 if (TEMPLATES / name / file).is_file()]
        self.assertGreaterEqual(len(files), 12)
        for path in files:
            for kind, image in image_lines(path):
                with self.subTest(file=str(path.relative_to(ROOT)), image=image):
                    self.assertRegex(image, PINNED, f"{kind} {image}: name:version@sha256:<digest>")
                    # A moving tag next to the digest would only hide what the digest says.
                    self.assertNotRegex(image, r":(latest|lts|\d+(\.\d+)?(-[a-z]+[0-9.]*)?)@",
                                        f"{image}: a full version, not a floating tag")

    def test_given_the_same_image_in_several_templates_when_compared_then_it_is_the_same_pin(self):
        pins = {}
        for name in CONFIGURED:
            for file in ("Dockerfile", "yggdrasil.yml"):
                path = TEMPLATES / name / file
                if path.is_file():
                    for _, image in image_lines(path):
                        pins.setdefault(image.split(":")[0], set()).add(image)
        for repository, versions in pins.items():
            with self.subTest(image=repository):
                self.assertEqual(len(versions), 1, f"{repository} is pinned more than one way: {sorted(versions)}")

    def test_given_each_site_dockerfile_when_read_then_it_takes_the_build_arguments_and_serves_unprivileged_on_8080(self):
        for name in SITES:
            text = (TEMPLATES / name / "Dockerfile").read_text()
            with self.subTest(template=name):
                self.assertRegex(text, r"(?m)^ARG GIT_COMMIT\b")
                self.assertRegex(text, r"(?m)^ARG YGG_ENVIRONMENT\b")
                self.assertRegex(text, r"(?m)^EXPOSE 8080$")
                user = re.findall(r"(?m)^USER (\S+)$", text)
                self.assertTrue(user, "no USER")
                self.assertNotIn(user[-1], ("root", "0", "0:0"), "the last USER must not be root")
                self.assertRegex(text, r"\bPORT=8080\b|SERVER_PORT=8080\b")
                # What Vercel gave a site is a build argument now (the comments say what replaced what).
                code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                self.assertNotIn("VERCEL", code)

    def test_given_each_static_site_dockerfile_when_read_then_it_takes_the_public_path_and_serves_healthz(self):
        for name in STATIC_SITES:
            text = (TEMPLATES / name / "Dockerfile").read_text()
            with self.subTest(template=name):
                self.assertRegex(text, r"(?m)^ARG PUBLIC_PATH\b")
                self.assertIn("/public/healthz", text)
                self.assertIn("SERVER_ROOT=/public", text)

    def test_given_the_hugo_dockerfiles_when_read_then_the_base_url_is_the_host_and_path(self):
        for name in ("site-hugo", "library"):
            text = (TEMPLATES / name / "Dockerfile").read_text()
            with self.subTest(template=name):
                self.assertRegex(text, r"(?m)^ARG PUBLIC_HOST\b")
                self.assertRegex(text, r"--baseURL")
                self.assertIn("https://$PUBLIC_HOST$PUBLIC_PATH/", text)

    def test_given_the_library_dockerfile_when_read_then_it_builds_the_docs_with_the_coverage_report_from_the_deploy_input(self):
        text = (TEMPLATES / "library" / "Dockerfile").read_text()
        # deploy.sh copies the coverage check's archive into the build context's root: coverage-report/.
        self.assertRegex(text, r"(?m)^COPY coverage-report/ ")
        self.assertIn("docs", text)

    def test_given_the_next_server_template_when_listed_then_it_ships_the_healthz_route(self):
        route = TEMPLATES / "site-next-server" / "app" / "healthz" / "route.js"
        self.assertIn("200", route.read_text())

    def test_given_the_application_template_when_read_then_it_points_at_the_new_templates(self):
        text = (TEMPLATES / "application" / "yggdrasil.yml").read_text()
        for name in (*SITES, "library"):
            self.assertIn(f"templates/{name}/", text)
        self.assertIn("templates/stacks/", text)


def scripts_of_library():
    return sorted((TEMPLATES / "library" / "ci").glob("*.sh"))


class LibraryScriptsTests(unittest.TestCase):
    def test_given_the_library_scripts_when_listed_then_the_three_exist_executable_with_a_solution_placeholder(self):
        names = [path.name for path in scripts_of_library()]
        self.assertEqual(names, ["coverage.sh", "pack.sh", "test.sh"])
        for path in scripts_of_library():
            with self.subTest(script=path.name):
                lines = path.read_text().splitlines()
                self.assertEqual(lines[0], "#!/usr/bin/env bash")
                # The solution is a variable at the top, with a placeholder to replace.
                # Quoted: unquoted, the shell would read <Name> as a redirection.
                self.assertIn("SOLUTION='src/ArturRios.<Name>.sln'", lines)
                self.assertLess(lines.index("SOLUTION='src/ArturRios.<Name>.sln'"), 40)

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout")
    def test_given_the_library_scripts_when_committed_then_git_records_them_as_executable(self):
        # `git update-index --chmod=+x`: a Windows checkout has no mode bits to read, git's index does.
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--stage", "templates/library/ci"],
                                 capture_output=True, text=True, check=True).stdout.splitlines()
        if not listing:
            self.skipTest("the scripts are not tracked yet")
        for line in listing:
            self.assertTrue(line.startswith("100755 "), line)

    @unittest.skipUnless(shutil.which("shellcheck") or pathlib.Path.home().joinpath(".local/bin/shellcheck").exists(),
                         "needs shellcheck")
    def test_given_the_library_scripts_when_checked_then_shellcheck_is_silent(self):
        shellcheck = shutil.which("shellcheck") or str(pathlib.Path.home() / ".local/bin/shellcheck")
        result = subprocess.run([shellcheck, *map(str, scripts_of_library())], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# A stand-in for `dotnet`: logs every call and answers what the scripts read. $FAKE_DIR is its state.
FAKE_DOTNET = textwrap.dedent(r"""
    #!/usr/bin/env bash
    set -euo pipefail
    printf '%s\n' "$*" >>"$FAKE_DIR/calls"
    case "$1" in
      restore | build | format) ;;
      test)
        if [[ " $* " == *" --list-tests "* ]]; then
          # The tests outside every category: dotnet prints them indented by four spaces.
          if [[ -n "${FAKE_UNCATEGORIZED:-}" ]]; then
            printf 'The following Tests are available:\n'
            printf '    %s\n' "$FAKE_UNCATEGORIZED"
          fi
        fi
        ;;
      tool)
        # dotnet tool install <id> --version <v> --tool-path <dir>: leave a reportgenerator there.
        while (($#)); do
          [[ "$1" == --tool-path ]] && dir=$2
          shift
        done
        printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >"$FAKE_DIR/reportgenerator.args"\n' >"$dir/reportgenerator"
        chmod +x "$dir/reportgenerator"
        ;;
      pack)
        output=artifacts
        while (($#)); do
          [[ "$1" == --output || "$1" == -o ]] && output=$2
          shift
        done
        mkdir -p "$output"
        : >"$output/ArturRios.Sample.${FAKE_PACK_VERSION:-1.2.3}.nupkg"
        ;;
    esac
""").lstrip()


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on a POSIX system")
class LibraryScriptBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="template-library-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.state = self.temp / "state"
        self.state.mkdir()
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        (bin_dir / "dotnet").write_text(FAKE_DOTNET)
        (bin_dir / "dotnet").chmod(0o755)
        # A library checkout with the scripts as a repository would have them: placeholder replaced.
        self.repo = self.temp / "repo"
        (self.repo / "ci").mkdir(parents=True)
        for path in scripts_of_library():
            text = path.read_text().replace("<Name>", "Sample")
            (self.repo / "ci" / path.name).write_text(text)
            (self.repo / "ci" / path.name).chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_DIR=str(self.state),
                        HOME=str(self.temp / "home"), TMPDIR=str(self.temp))
        self.env.pop("REPORTGENERATOR_LICENSE", None)
        self.env.pop("RELEASE_VERSION", None)

    def run_script(self, name, **env):
        # From another directory than the root: the scripts find the repository's root themselves.
        return subprocess.run(["bash", str(self.repo / "ci" / name)], cwd=self.temp, env=dict(self.env, **env),
                              capture_output=True, text=True)

    def calls(self):
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_given_an_unreplaced_placeholder_when_a_script_runs_then_it_stops_naming_the_variable(self):
        for name in ("test.sh", "coverage.sh", "pack.sh"):
            with self.subTest(script=name):
                original = scripts_of_library()[0].parent / name
                broken = self.repo / "ci" / name
                broken.write_text(original.read_text())
                result = self.run_script(name)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SOLUTION", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_given_a_library_when_tested_then_it_checks_format_builds_checks_categories_and_runs_both_categories(self):
        result = self.run_script("test.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        sln = "src/ArturRios.Sample.sln"
        self.assertEqual(calls[0], f"restore {sln}")
        self.assertEqual(calls[1], f"format {sln} --verify-no-changes --no-restore")
        self.assertEqual(calls[2], f"build {sln} --configuration Release --no-restore")
        self.assertIn("--list-tests", calls[3])
        self.assertIn('Category!=Unit&Category!=Functional', calls[3])
        unit = [call for call in calls if 'Category=Unit' in call and "--list-tests" not in call]
        functional = [call for call in calls if 'Category=Functional' in call]
        self.assertEqual(len(unit), 1)
        self.assertEqual(len(functional), 1)
        self.assertIn("--no-build", unit[0])
        self.assertIn("artifacts/test-results/unit", unit[0])
        self.assertIn("artifacts/test-results/functional", functional[0])
        # The scripts run from the root: results are written there, whatever the caller's directory.
        self.assertFalse((self.temp / "artifacts").exists())

    def test_given_a_test_without_a_category_when_tested_then_it_fails_naming_it_and_runs_no_tests(self):
        result = self.run_script("test.sh", FAKE_UNCATEGORIZED="Sample.Tests.Orphan")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Sample.Tests.Orphan", result.stdout + result.stderr)
        self.assertFalse([call for call in self.calls() if "Category=" in call and "--list-tests" not in call])

    def test_given_no_license_when_coverage_runs_then_it_makes_the_free_report_and_says_so(self):
        result = self.run_script("coverage.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("free report", result.stdout + result.stderr)
        args = (self.state / "reportgenerator.args").read_text()
        self.assertNotIn("-license", args)
        self.assertIn("-reports:coverage/**/coverage.cobertura.xml", args)
        self.assertIn("-targetdir:coverage-report", args)
        self.assertIn("-reporttypes:Html", args)
        collect = [call for call in self.calls() if call.startswith("test ")]
        self.assertEqual(len(collect), 1)
        self.assertIn("--collect:XPlat Code Coverage", collect[0])
        install = [call for call in self.calls() if call.startswith("tool install")]
        self.assertIn("dotnet-reportgenerator-globaltool --version 5.5.11", install[0])

    def test_given_an_empty_license_when_coverage_runs_then_it_is_the_same_as_none(self):
        # An unset credential can arrive as an empty variable.
        result = self.run_script("coverage.sh", REPORTGENERATOR_LICENSE="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("free report", result.stdout + result.stderr)
        self.assertNotIn("-license", (self.state / "reportgenerator.args").read_text())

    def test_given_a_license_when_coverage_runs_then_it_is_passed_to_reportgenerator_and_never_printed(self):
        result = self.run_script("coverage.sh", REPORTGENERATOR_LICENSE="secret-license-value")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-license:secret-license-value", (self.state / "reportgenerator.args").read_text())
        self.assertNotIn("secret-license-value", result.stdout + result.stderr)
        self.assertNotIn("free report", result.stdout + result.stderr)

    def test_given_a_library_when_packed_then_the_packages_are_in_artifacts_at_the_root(self):
        result = self.run_script("pack.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in (self.repo / "artifacts").glob("*.nupkg")], ["ArturRios.Sample.1.2.3.nupkg"])
        pack = [call for call in self.calls() if call.startswith("pack ")][0]
        self.assertIn("src/ArturRios.Sample.sln", pack)
        self.assertIn("--configuration Release", pack)

    def test_given_a_release_version_when_packed_then_every_package_must_carry_it(self):
        result = self.run_script("pack.sh", RELEASE_VERSION="1.2.3", FAKE_PACK_VERSION="1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_script("pack.sh", RELEASE_VERSION="1.2.4", FAKE_PACK_VERSION="1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1.2.4", result.stderr)
        self.assertIn("ArturRios.Sample.1.2.3.nupkg", result.stderr)


def compose_config(files, env=None, expect_failure=False):
    """`docker compose config` over template files with the sample values: the rendered model as a dict."""
    with tempfile.TemporaryDirectory(prefix="template-stack-") as app_dir:
        command = ["docker", "compose", "--project-name", "sample-docs-production", "--env-file", os.devnull]
        for name in files:
            command += ["-f", str(TEMPLATES / "stacks" / name)]
        command += ["config", "--format", "json"]
        values = {name: value for name, value in os.environ.items() if name not in SAMPLE_ENV}
        values.update(APP_DIR=app_dir, **(SAMPLE_ENV if env is None else env))
        result = subprocess.run(command, env=values, capture_output=True, text=True)
    if expect_failure:
        return result
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def labels_of(model, service="web"):
    labels = model["services"][service]["labels"]
    return labels if isinstance(labels, dict) else dict(item.split("=", 1) for item in labels)


@unittest.skipUnless(docker_available, "needs docker")
class StackTemplateTests(unittest.TestCase):
    def test_given_the_site_stack_when_rendered_then_the_build_gets_the_commit_environment_host_and_path(self):
        service = compose_config(["site.yml"])["services"]["web"]
        args = service["build"]["args"]
        self.assertEqual(args["GIT_COMMIT"], SAMPLE_ENV["GIT_COMMIT"])
        self.assertEqual(args["YGG_ENVIRONMENT"], "production")
        self.assertEqual(args["PUBLIC_HOST"], "docs.example.com")
        self.assertEqual(args["PUBLIC_PATH"], "/shop-docs")
        # deploy.sh tags what it builds <stack>:<environment>-<version> and rolls those images back.
        self.assertEqual(service["image"], "APPLICATION_ID:production-1.2.3-0123456")
        # Build arguments are public: the file names no secret.
        self.assertTrue(all("SECRET" not in key.upper() and "TOKEN" not in key.upper() for key in args))

    def test_given_the_site_stack_without_a_public_host_or_path_when_rendered_then_those_build_args_are_empty(self):
        env = {key: value for key, value in SAMPLE_ENV.items() if key not in ("YGG_PUBLIC_HOST", "YGG_PUBLIC_PATH")}
        args = compose_config(["site.yml"], env)["services"]["web"]["build"]["args"]
        self.assertEqual(args["PUBLIC_HOST"], "")
        self.assertEqual(args["PUBLIC_PATH"], "")

    def test_given_the_site_stack_when_rendered_then_the_container_is_locked_down(self):
        service = compose_config(["site.yml"])["services"]["web"]
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["restart"], "unless-stopped")

    def test_given_the_site_proxy_when_rendered_then_the_router_matches_the_host(self):
        model = compose_config(["site.yml", "site.proxy.yml"])
        labels = labels_of(model)
        self.assertEqual(labels["traefik.http.routers.APPLICATION_ID-production.rule"], "Host(`docs.example.com`)")
        self.assertEqual(labels["traefik.http.routers.APPLICATION_ID-production.entrypoints"], "websecure")
        self.assertEqual(labels["traefik.http.routers.APPLICATION_ID-production.middlewares"], "secure-headers@file")
        self.assertEqual(labels["traefik.http.services.APPLICATION_ID-production.loadbalancer.server.port"], "8080")
        self.assertEqual(labels["traefik.docker.network"], "edge")
        # The status API probes http://APPLICATION_ID.production:8080/healthz over the edge network.
        self.assertEqual(model["services"]["web"]["networks"]["edge"]["aliases"], ["APPLICATION_ID.production"])
        self.assertTrue(model["networks"]["edge"]["external"])
        self.assertNotIn("ports", model["services"]["web"])

    def test_given_the_path_proxy_when_rendered_then_the_router_matches_host_and_path_without_stripping_the_prefix(self):
        model = compose_config(["site.yml", "site-path.proxy.yml"])
        labels = labels_of(model)
        rule = labels["traefik.http.routers.APPLICATION_ID-production.rule"]
        # The prefix, and the bare path (the server redirects it to the slash), but not a longer name that
        # merely begins with it: /shop-docs-old is another site.
        self.assertEqual(rule, "Host(`docs.example.com`) && (Path(`/shop-docs`) || PathPrefix(`/shop-docs/`))")
        # The site is built for its path (baseURL, base, basePath), so the prefix must reach it.
        self.assertFalse([key for key in labels if "stripprefix" in key.lower()])
        self.assertEqual(labels["traefik.http.routers.APPLICATION_ID-production.middlewares"], "secure-headers@file")
        self.assertEqual(model["services"]["web"]["networks"]["edge"]["aliases"], ["APPLICATION_ID.production"])

    def test_given_the_path_proxy_without_a_public_path_when_rendered_then_it_stops(self):
        env = {key: value for key, value in SAMPLE_ENV.items() if key != "YGG_PUBLIC_PATH"}
        result = compose_config(["site.yml", "site-path.proxy.yml"], env, expect_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("YGG_PUBLIC_PATH", result.stderr)

    def test_given_the_apex_redirect_when_rendered_then_the_bare_domain_redirects_to_www_and_www_serves_the_site(self):
        env = dict(SAMPLE_ENV, YGG_PUBLIC_HOST="www.example.com", YGG_APEX_HOST="example.com")
        model = compose_config(["site.yml", "site-apex-redirect.proxy.yml"], env)
        labels = labels_of(model)
        site = "traefik.http.routers.APPLICATION_ID-production"
        apex = "traefik.http.routers.APPLICATION_ID-production-apex"
        self.assertEqual(labels[f"{site}.rule"], "Host(`www.example.com`)")
        self.assertEqual(labels[f"{apex}.rule"], "Host(`example.com`)")
        self.assertEqual(labels[f"{apex}.entrypoints"], "websecure")
        middleware = "traefik.http.middlewares.APPLICATION_ID-production-apex"
        # The shared headers first, so the 301 carries HSTS like every other answer (scripts/test_edge.py).
        self.assertEqual(labels[f"{apex}.middlewares"], "secure-headers@file,APPLICATION_ID-production-apex")
        # Any path and query on the bare domain moves to the same one on www, permanently, over https. The
        # replacement keeps Traefik's ${1}: Compose must not have read it as one of its own variables.
        regex = labels[f"{middleware}.redirectregex.regex"]
        # `config` prints a literal $ as $$, so the file's $${1} shows as $${1}: Compose turns it into ${1}.
        self.assertEqual(labels[f"{middleware}.redirectregex.replacement"], "https://www.example.com$${1}")
        self.assertEqual(labels[f"{middleware}.redirectregex.permanent"], "true")
        for url in ("https://example.com/some/page?x=1", "http://example.com/some/page?x=1"):
            match = re.fullmatch(regex, url)
            self.assertIsNotNone(match, f"{regex} should match {url}")
            self.assertEqual(match.group(1), "/some/page?x=1")
        self.assertEqual(re.sub(regex, "https://www.example.com\\1", "https://example.com/"), "https://www.example.com/")
        # The redirect has no service of its own to reach: it answers before one is chosen, but a router needs
        # one to be valid, and it is the site's.
        self.assertEqual(labels[f"{apex}.service"], "APPLICATION_ID-production")
        self.assertEqual(labels[f"{site}.service"], "APPLICATION_ID-production")

    def test_given_the_apex_redirect_without_an_apex_host_when_rendered_then_it_stops(self):
        env = {key: value for key, value in SAMPLE_ENV.items() if key != "YGG_APEX_HOST"}
        result = compose_config(["site.yml", "site-apex-redirect.proxy.yml"], env, expect_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("YGG_APEX_HOST", result.stderr)

    def test_given_each_stack_template_when_rendered_without_the_deploy_values_then_it_stops_naming_them(self):
        for files in (["site.yml"], ["site.yml", "site.proxy.yml"]):
            with self.subTest(files=files):
                result = compose_config(files, {key: value for key, value in SAMPLE_ENV.items()
                                                if key != "YGG_ENVIRONMENT"}, expect_failure=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("YGG_ENVIRONMENT", result.stderr)


# The regex Renovate runs is JavaScript's/RE2's; Python spells a named group (?P<name>...).
def python_regex(source):
    return re.compile(source.replace("(?<", "(?P<"))


class RenovatePresetTests(unittest.TestCase):
    def setUp(self):
        self.preset = json.loads(PRESET.read_text(encoding="utf-8"))

    def manager(self):
        managers = self.preset["customManagers"]
        self.assertEqual(len(managers), 1)
        return managers[0]

    def test_given_the_preset_when_read_then_it_is_json_that_extends_the_recommended_config_on_develop(self):
        self.assertEqual(self.preset["extends"], ["config:recommended"])
        # Renovate 44 renamed baseBranches to baseBranchPatterns (it still reads the old name, but its validator, in
        # --strict mode, calls the file in need of migration).
        self.assertEqual(self.preset["baseBranchPatterns"], ["develop"])
        self.assertNotIn("baseBranches", self.preset)

    def test_given_the_preset_when_read_then_it_covers_the_four_ecosystems_dependabot_did(self):
        managers = set(self.preset["enabledManagers"])
        self.assertTrue({"nuget", "npm", "github-actions", "dockerfile", "custom.regex"} <= managers)

    def test_given_the_preset_when_read_then_pull_request_titles_are_lowercase_conventional_commits(self):
        self.assertEqual(self.preset["semanticCommits"], "enabled")
        self.assertEqual(self.preset["semanticCommitType"], "build")

    def test_given_the_nuget_groups_dependabot_had_when_read_then_the_preset_groups_them(self):
        groups = {rule["groupName"]: rule["matchPackageNames"] for rule in self.preset["packageRules"]
                  if "groupName" in rule}
        self.assertEqual(groups["microsoft"], ["Microsoft.*", "System.*"])
        self.assertEqual(groups["arturrios"], ["ArturRios.*"])
        self.assertEqual(groups["test-tooling"], ["xunit*", "coverlet.*"])

    def test_given_the_renovate_preset_when_its_image_regex_runs_then_a_pinned_image_line_matches(self):
        manager = self.manager()
        self.assertEqual(manager["customType"], "regex")
        self.assertEqual(manager["datasourceTemplate"], "docker")
        # Renovate writes a regex pattern between slashes. The file is yggdrasil.yml at any depth, no other.
        patterns = [re.compile(pattern.strip("/")) for pattern in manager["managerFilePatterns"]]
        for path, expected in (("yggdrasil.yml", True), ("sub/yggdrasil.yml", True), ("myyggdrasil.yml", False),
                               ("yggdrasil.yml.bak", False), ("docs/yggdrasil.yaml", False)):
            self.assertEqual(any(pattern.search(path) for pattern in patterns), expected, path)
        digest = "sha256:" + "ab12" * 16
        regex = python_regex(manager["matchStrings"][0])
        samples = {
            f"    image: mcr.microsoft.com/dotnet/sdk:10.0.402@{digest}":
                ("mcr.microsoft.com/dotnet/sdk", "10.0.402"),
            f"    image: node:24.22.1-alpine3.24@{digest}": ("node", "24.22.1-alpine3.24"),
            f"image: \"node:24.22.1-alpine3.24@{digest}\"": ("node", "24.22.1-alpine3.24"),
            f"image: 'busybox:1.37.0@{digest}'  # the archiver": ("busybox", "1.37.0"),
            f"  image: registry.example.com:5000/team/tool:2.1@{digest}": ("registry.example.com:5000/team/tool", "2.1"),
            f"  build:\n    image: debian:12@{digest}": ("debian", "12"),
        }
        for line, (name, version) in samples.items():
            with self.subTest(line=line):
                match = regex.search(line)
                self.assertIsNotNone(match)
                self.assertEqual((match["depName"], match["currentValue"], match["currentDigest"]),
                                 (name, version, digest))

    def test_given_the_renovate_preset_when_its_image_regex_runs_then_other_lines_do_not_match(self):
        regex = python_regex(self.manager()["matchStrings"][0])
        digest = "sha256:" + "ab" * 32
        for line in ("    image: node:24.22.1-alpine3.24",                  # not pinned by digest: nothing to bump
                     "    image: node@" + digest,                          # no version to follow
                     "    run: docker run node:24@" + digest,              # not an image: line
                     "    image: node:24@sha256:abc",                      # a digest that is not one
                     "# image: nothing here"):
            with self.subTest(line=line):
                self.assertIsNone(regex.search(line))

    def test_given_the_real_templates_when_the_image_regex_runs_then_every_image_line_is_found(self):
        regex = python_regex(self.manager()["matchStrings"][0])
        total = 0
        for name in CONFIGURED:
            text = (TEMPLATES / name / "yggdrasil.yml").read_text()
            lines = [line for line in text.splitlines() if re.match(r"\s*image:", line)]
            found = [match["depName"] for match in regex.finditer(text)]
            with self.subTest(template=name):
                self.assertEqual(len(found), len(lines), lines)
            total += len(lines)
        self.assertGreaterEqual(total, 8)

    @unittest.skipUnless(live and docker_available, "YGG_TEMPLATE_TESTS=1 and docker: pulls Renovate's image")
    def test_given_the_renovate_preset_when_validated_by_renovate_then_it_is_valid(self):
        # --strict fails on a warning or a config that needs migrating (a renamed option), --no-global validates
        # the file as a repository's config or preset, not as the self-hosted one. The image is read-only
        # mounted from the repository itself, which the container's non-root user can read.
        result = validate_with_renovate(PRESET.parent, PRESET.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("validated successfully", result.stdout + result.stderr)

    @unittest.skipUnless(live and docker_available, "YGG_TEMPLATE_TESTS=1 and docker: pulls Renovate's image")
    def test_given_a_broken_preset_when_validated_by_renovate_then_it_is_refused(self):
        # The validator must be able to say no, or the test above proves nothing.
        directory = pathlib.Path(tempfile.mkdtemp(prefix="template-renovate-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        directory.chmod(0o755)
        (directory / "broken.json").write_text(json.dumps({"extends": ["config:recommended"], "bogusOption": 1,
                                                           "customManagers": [{"customType": "regex"}]}))
        (directory / "broken.json").chmod(0o644)
        result = validate_with_renovate(directory, "broken.json")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


def validate_with_renovate(directory, name):
    return subprocess.run(
        ["docker", "run", "--rm", "-v", f"{directory}:/w:ro", "--entrypoint", "renovate-config-validator",
         renovate_image(), "--strict", "--no-global", f"/w/{name}"], capture_output=True, text=True)


def renovate_image():
    """The Renovate image scripts/renovate.sh runs: the preset is validated by the version it will meet."""
    text = (ROOT / "scripts" / "renovate.sh").read_text()
    return re.search(r"^RENOVATE_IMAGE=(\S+)$", text, re.MULTILINE).group(1)


# ---- Live: the site images ----

# Per template: the sample under fixtures, the build arguments, the path the site lives under, a deep link below
# it, and a marker each page carries so the test sees the build arguments arrive.
COMMIT = "0123456789abcdef0123456789abcdef01234567"
BUILD = {"GIT_COMMIT": COMMIT, "YGG_ENVIRONMENT": "homologation"}
PATH_BUILD = {**BUILD, "PUBLIC_HOST": "docs.example.com", "PUBLIC_PATH": "/sample"}
SITE_CASES = {
    "site-static": dict(sample="static", args=PATH_BUILD, home="/sample/", deep="/sample/docs/page/", marker="static sample"),
    "site-hugo": dict(sample="hugo", args=PATH_BUILD, home="/sample/", deep="/sample/docs/guide/page/",
                      marker=COMMIT, base="https://docs.example.com/sample/"),
    "site-vite": dict(sample="vite", args=PATH_BUILD, home="/sample/", deep="/sample/some/client/route",
                      marker=COMMIT),
    "site-next-static": dict(sample="next-static", args=PATH_BUILD, home="/sample/", deep="/sample/about/",
                             marker=COMMIT),
    "site-next-server": dict(sample="next-server", args=BUILD, home="/", deep="/blog/post", marker=COMMIT),
    "site-node-api": dict(sample="node-api", args=BUILD, home="/", deep="/v1/items/42", marker=COMMIT),
    # The library's docs: Hugo in docs/, with the coverage report the deploy copies into the context's root.
    "library": dict(sample="library", args=PATH_BUILD, home="/sample/", deep="/sample/guide/page/", marker=COMMIT,
                    base="https://docs.example.com/sample/", also=["/sample/coverage-report/"]),
}


def docker(*args, check=True, **kwargs):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kwargs)


def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


@unittest.skipUnless(live and docker_available, "YGG_TEMPLATE_TESTS=1 and docker")
class SiteTemplateLiveTests(unittest.TestCase):
    """Each template's Dockerfile, built as the stack would build it, around a tiny sample site."""

    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="template-site-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)

    def build_context(self, template, sample):
        """A directory like a repository checkout: the sample site plus the template's own files."""
        context = self.temp / template
        shutil.copytree(FIXTURES / sample, context)
        for path in (TEMPLATES / template).rglob("*"):
            if path.is_file() and path.name not in ("README.md", "yggdrasil.yml"):
                target = context / path.relative_to(TEMPLATES / template)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, target)
        if (context / "package.json").exists() and not (context / "package-lock.json").exists():
            # A real repository commits its lockfile; the sample has none to keep the fixtures small, so make
            # it as the repository's author would, with the Node image the Dockerfile builds in.
            node = next(image for kind, image in image_lines(context / "Dockerfile") if image.startswith("node:"))
            docker("run", "--rm", "-v", f"{context}:/app", "-w", "/app", node, "npm", "install",
                   "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund")
        return context

    def test_given_each_site_template_when_built_then_it_answers_healthz_and_a_deep_link_under_its_path(self):
        for template, case in SITE_CASES.items():
            with self.subTest(template=template):
                self.probe(template, case)

    def probe(self, template, case):
        context = self.build_context(template, case["sample"])
        suffix = uuid.uuid4().hex[:8]
        image = f"ygg-template-test-{template}:{suffix}"
        container = f"ygg-template-test-{template}-{suffix}"
        # Removed whatever happens next: the container first, then its image.
        self.addCleanup(lambda: docker("rmi", "--force", image, check=False))
        self.addCleanup(lambda: docker("rm", "--force", container, check=False))

        build = ["build", "--tag", image]
        for name, value in case["args"].items():
            build += ["--build-arg", f"{name}={value}"]
        built = docker(*build, str(context), check=False)
        self.assertEqual(built.returncode, 0, built.stdout[-3000:] + built.stderr[-3000:])

        # A random free port on the loopback: nothing else of the host is touched.
        docker("run", "--detach", "--name", container, "--publish", "127.0.0.1::8080", image)
        mapping = docker("port", container, "8080/tcp").stdout.split()[0]
        base = f"http://127.0.0.1:{mapping.rsplit(':', 1)[1]}"

        deadline = time.time() + 60
        status = 0
        while time.time() < deadline:
            try:
                status, _ = get(base + "/healthz")
            except OSError:
                status = 0
            if status == 200:
                break
            time.sleep(0.5)
        logs = docker("logs", container, check=False)
        self.assertEqual(status, 200, f"/healthz: {logs.stdout[-1500:]}{logs.stderr[-1500:]}")

        status, body = get(base + case["deep"])
        self.assertEqual(status, 200, f"{case['deep']}: {body[:300]}")
        for extra in case.get("also", []):
            self.assertEqual(get(base + extra)[0], 200, extra)
        status, body = get(base + case["home"])
        self.assertEqual(status, 200, case["home"])
        self.assertIn(case["marker"], body, f"the build arguments should show in {case['home']}")
        if "base" in case:
            self.assertIn(case["base"], body, "Hugo's baseURL is the host and the public path")
        # Something that is not there is a 404, not the app's front page (a server that answers 200 for
        # everything would pass the checks above for the wrong reason) -- except for the single page apps,
        # which answer their page for any path by design.
        if template not in ("site-vite", "site-next-server", "site-node-api"):
            self.assertEqual(get(base + "/sample/definitely/not/here")[0], 404)

        config = json.loads(docker("inspect", container, "--format", "{{json .Config}}").stdout)
        self.assertNotIn(config["User"], ("", "0", "root", "0:0"), "the image runs as a non-root user")


if __name__ == "__main__":
    unittest.main()
