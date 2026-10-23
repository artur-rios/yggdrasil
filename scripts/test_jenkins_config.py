"""Static tests of the Jenkins controller's configuration (platform/jenkins/controller, jenkins/).

    python3 -m unittest discover -s scripts -p test_jenkins_config.py

They read the files and never start Jenkins: that is the live suite's job (Task 10 of plan 3, gated by
YGG_JENKINS_TESTS=1). What matters here is the one security decision of the pipeline: Jenkins never
reads a repository's own Jenkinsfile (a branch pushed by someone who is not the owner would run
arbitrary pipeline code with every credential in reach), so every job runs the one script kept in
Jenkins, which calls the pinned library; and that both git servers get the credentials, library
retriever and job sources they need.
"""

import pathlib
import re
import unittest

try:
    import yaml
except ImportError:  # CI installs it; these tests need it.
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = ROOT / "platform" / "jenkins" / "controller"


def read(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


def casc(name="casc.yaml"):
    return yaml.safe_load(read(CONTROLLER / name))


def job_dsl():
    return casc()["jobs"][0]["script"]


def credentials(name):
    """The credentials a casc file defines, as {id: (type, fields)}."""
    found = {}
    for domain in casc(name).get("credentials", {}).get("system", {}).get("domainCredentials", []):
        for entry in domain["credentials"]:
            (kind, fields), = entry.items()
            found[fields["id"]] = (kind, fields)
    return found


def libraries(name):
    return casc(name).get("unclassified", {}).get("globalLibraries", {}).get("libraries", [])


def jenkins_service():
    return yaml.safe_load(read(ROOT / "platform" / "compose.yml"))["services"]["jenkins"]


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class JenkinsPluginsTests(unittest.TestCase):
    def test_given_the_plugins_when_read_then_gitea_and_the_defaults_factory_are_there(self):
        plugins = read(CONTROLLER / "plugins.txt").split()
        for plugin in ("gitea", "pipeline-multibranch-defaults", "config-file-provider", "http_request", "junit",
                       "github-branch-source", "basic-branch-build-strategies"):
            self.assertIn(plugin, plugins)


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class JenkinsJobTests(unittest.TestCase):
    def test_given_the_job_dsl_when_read_then_every_job_uses_the_jenkinsfile_kept_in_jenkins(self):
        script = job_dsl()
        self.assertIn("pipelineBranchDefaultsProjectFactory", script)
        self.assertIn("scriptId('yggdrasil-default')", script)
        # The script runs unsandboxed: it loads a library, and the controller's own job config is the
        # only thing that can change it.
        self.assertIn("useSandbox(false)", script)
        # The factory that reads the repository's Jenkinsfile must be nowhere, not even in a branch.
        self.assertNotIn("workflowBranchProjectFactory", script)
        self.assertNotIn("scriptPath", script)

    def test_given_the_job_dsl_when_read_then_forks_are_not_discovered(self):
        script = job_dsl()
        self.assertNotRegex(script, r"(?i)fork\w*(discovery|trust)")
        # Pull requests are the origin ones only (a branch of the repository itself), on both servers.
        self.assertIn("giteaPullRequestDiscovery", script)
        self.assertIn("gitHubPullRequestDiscovery", script)

    def test_given_the_job_dsl_when_read_then_there_is_a_job_for_applications_and_for_repositories(self):
        script = job_dsl()
        self.assertIn("catalog.repositories", script)
        self.assertIn("catalog.systems", script)
        self.assertIn("multibranchPipelineJob", script)
        # A name two entries share would make the second silently replace the first.
        self.assertIn("more than one job named", script)

    def test_given_the_job_dsl_when_read_then_the_source_follows_the_forge_type_with_github_as_the_default(self):
        script = job_dsl()
        self.assertIn("catalog.forge", script)
        self.assertRegex(script, r"forgeType\s*=\s*.*\?:\s*'github'")
        self.assertIn("giteaSCMSource", script)
        self.assertIn("giteaBranchDiscovery", script)
        self.assertIn("gitHubBranchDiscovery", script)
        self.assertIn("credentialsId('forge-read')", script)
        self.assertIn("credentialsId('github-app')", script)

    def test_given_the_job_dsl_when_read_then_tags_are_discovered_and_built_for_owner_tag_entries_only(self):
        script = job_dsl()
        self.assertIn("giteaTagDiscovery", script)
        self.assertIn("gitHubTagDiscovery", script)
        self.assertIn("buildTags", script)
        self.assertIn("owner-tag", script)
        # Both the discovery and the build strategy are inside a condition on the entry's release.
        self.assertGreater(script.count("ownerTag"), 3)

    def test_given_the_job_dsl_when_read_then_the_heads_include_the_branching_model(self):
        script = job_dsl()
        self.assertRegex(script, r"\['develop', 'main', 'PR-\*', 'release/\*'\]")
        self.assertIn("headRegexFilter", script)

    def test_given_the_job_dsl_when_read_then_it_has_no_interpolation_casc_would_resolve(self):
        # Configuration as Code replaces a dollar-brace in any string of the file, scripts included.
        self.assertNotIn("${", job_dsl())

    def test_given_the_job_dsl_when_read_then_the_default_forge_equals_the_catalogs(self):
        # catalog.py's forge(): no block means GitHub on https://github.com, jenkinsUser unused there.
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import catalog as c
        self.assertEqual(c.forge({"owner": "x"})["type"], "github")
        self.assertIn("'https://github.com'", job_dsl())


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class JenkinsCredentialsTests(unittest.TestCase):
    def test_given_the_forgejo_configuration_when_read_then_it_has_the_forge_credentials_and_no_github_app(self):
        found = credentials("casc.forgejo.yaml")
        self.assertEqual(set(found), {"forge-write", "forge-read", "forge-webhook"})
        kind, write = found["forge-write"]
        self.assertEqual(kind, "usernamePassword")
        self.assertEqual(write["username"], "${FORGE_JENKINS_USER}")
        self.assertEqual(write["password"], "${readFile:/run/secrets/forge-write-token}")
        kind, read_only = found["forge-read"]
        self.assertEqual(kind, "usernamePassword")
        self.assertEqual(read_only["username"], "jenkins-ci")
        self.assertEqual(read_only["password"], "${readFile:/run/secrets/forge-read-token}")
        kind, webhook = found["forge-webhook"]
        self.assertEqual(kind, "string")
        self.assertEqual(webhook["secret"], "${readFile:/run/secrets/forge-webhook-secret}")

    def test_given_the_github_app_configuration_when_read_then_it_has_the_app_credential_only(self):
        found = credentials("casc.github-app.yaml")
        self.assertEqual(set(found), {"github-app"})
        self.assertEqual(found["github-app"][0], "gitHubApp")
        self.assertEqual(found["github-app"][1]["privateKey"], "${readFile:/run/secrets/github-app.pem}")

    def test_given_the_github_configuration_when_read_then_it_has_no_forge_credentials(self):
        # On GitHub the library's forgeCredential() answers github-app for both 'read' and 'write'.
        self.assertEqual(credentials("casc.github.yaml"), {})
        for name in ("casc.github.yaml", "casc.github-app.yaml"):
            self.assertNotIn("forge-", yaml.dump(casc(name)), name)
        # The common file defines none either: credentials belong to the forge's own file.
        self.assertNotIn("credentials", casc("casc.yaml"))

    def test_given_the_forgejo_configuration_when_read_then_the_gitea_server_is_configured_without_hooks(self):
        server, = casc("casc.forgejo.yaml")["unclassified"]["giteaServers"]["servers"]
        self.assertEqual(server["serverUrl"], "${FORGE_URL}")
        # rules.py creates the webhooks; Jenkins must not manage (and so must not need to write) any.
        self.assertIs(server["manageHooks"], False)


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class JenkinsLibraryTests(unittest.TestCase):
    def test_given_each_forge_when_read_then_the_library_is_pinned_to_the_configured_version(self):
        for name in ("casc.forgejo.yaml", "casc.github.yaml"):
            library, = libraries(name)
            self.assertEqual(library["name"], "yggdrasil", name)
            self.assertEqual(library["defaultVersion"], "${YGGDRASIL_LIBRARY_VERSION}", name)
            self.assertIs(library["allowVersionOverride"], False, name)
            self.assertEqual(library["retriever"]["modernSCM"]["libraryPath"], "jenkins/library/", name)

    def test_given_the_forgejo_configuration_when_read_then_the_library_comes_from_the_forge_with_the_read_credential(self):
        source, = libraries("casc.forgejo.yaml")[0]["retriever"]["modernSCM"]["scm"].values()
        self.assertEqual(source["serverUrl"], "${FORGE_URL}")
        self.assertEqual(source["repoOwner"], "${GITHUB_OWNER}")
        self.assertEqual(source["repository"], "${YGGDRASIL_REPOSITORY}")
        self.assertEqual(source["credentialsId"], "forge-read")
        # A version is a tag: without tag discovery the library could not be found at v1.0.0.
        self.assertIn("giteaTagDiscovery", [next(iter(t)) for t in source["traits"]])

    def test_given_the_github_configuration_when_read_then_the_library_comes_from_github_with_the_app(self):
        source = libraries("casc.github.yaml")[0]["retriever"]["modernSCM"]["scm"]["github"]
        self.assertEqual(source["credentialsId"], "github-app")
        self.assertIn("gitHubTagDiscovery", [next(iter(t)) for t in source["traits"]])

    def test_given_the_common_configuration_when_read_then_it_defines_no_library(self):
        # The libraries are per forge; two definitions of one name would collide.
        self.assertEqual(libraries("casc.yaml"), [])


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class JenkinsfileTests(unittest.TestCase):
    def test_given_the_jenkinsfile_when_read_then_it_only_calls_the_pinned_library(self):
        lines = [line for line in read(ROOT / "jenkins" / "Jenkinsfile.default").splitlines()
                 if line.strip() and not line.lstrip().startswith("//")]
        self.assertEqual(lines, ["@Library('yggdrasil') _", "yggdrasilPipeline()"])

    def test_given_casc_when_read_then_the_jenkinsfile_is_kept_in_config_file_provider(self):
        configs = casc()["unclassified"]["globalConfigFiles"]["configs"]
        config, = [next(iter(entry.values())) for entry in configs]
        self.assertEqual(config["id"], "yggdrasil-default")
        self.assertEqual(config["content"], "${readFile:/etc/jenkins/Jenkinsfile.default}")

    def test_given_the_compose_service_when_read_then_the_jenkinsfile_and_every_casc_file_are_mounted(self):
        volumes = " ".join(jenkins_service()["volumes"])
        self.assertIn("../jenkins/Jenkinsfile.default:/etc/jenkins/Jenkinsfile.default:ro", volumes)
        for name in ("casc.yaml", "casc.forgejo.yaml", "casc.github.yaml", "casc.github-app.yaml"):
            self.assertIn(f"./jenkins/controller/{name}:/etc/jenkins/{name}:ro", volumes)


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class JenkinsServiceTests(unittest.TestCase):
    def test_given_the_compose_service_when_read_then_casc_files_come_from_platform_sh(self):
        environment = jenkins_service()["environment"]
        self.assertTrue(environment["CASC_JENKINS_CONFIG"].startswith("${YGG_CASC_CONFIG:-"))
        self.assertEqual(environment["YGGDRASIL_LIBRARY_VERSION"], "${YGGDRASIL_LIBRARY_VERSION:-}")
        self.assertIn("FORGE_URL", environment)
        self.assertIn("FORGE_JENKINS_USER", environment)

    def test_given_the_compose_service_when_read_then_the_forge_secrets_are_files_mounted_read_only(self):
        volumes = jenkins_service()["volumes"]
        for name, variable in (("forge-write-token", "FORGE_WRITE_TOKEN_FILE"),
                               ("forge-read-token", "FORGE_READ_TOKEN_FILE"),
                               ("forge-webhook-secret", "FORGE_WEBHOOK_SECRET_FILE")):
            # /dev/null by default: a GitHub installation has no such file, and Docker would create a
            # directory in its place on the host.
            self.assertIn("${%s:-/dev/null}:/run/secrets/%s:ro" % (variable, name), volumes)

    def test_given_the_environment_example_when_read_then_it_documents_the_new_settings(self):
        text = read(ROOT / "env" / "platform.env.example")
        for name in ("YGGDRASIL_LIBRARY_VERSION", "FORGE_WRITE_TOKEN_FILE", "FORGE_READ_TOKEN_FILE",
                     "FORGE_WEBHOOK_SECRET_FILE"):
            self.assertRegex(text, rf"(?m)^{name}=")


class AgentsScriptTests(unittest.TestCase):
    def test_given_the_agents_script_when_read_then_ci_and_release_agents_are_created(self):
        script = read(CONTROLLER / "init.groovy.d" / "agents.groovy")
        self.assertRegex(script, r"ci:\s*\[\s*executors:\s*4,\s*label:\s*'ci'")
        self.assertRegex(script, r"release:\s*\[\s*executors:\s*1,\s*label:\s*'release'")

    def test_given_the_agents_script_when_read_then_an_environment_agent_named_ci_or_release_is_an_error(self):
        script = read(CONTROLLER / "init.groovy.d" / "agents.groovy")
        self.assertRegex(script, r"throw new IllegalStateException\(")
        self.assertIn("reserved", script)

    def test_given_the_agents_script_when_read_then_the_environment_agents_keep_two_executors(self):
        self.assertIn("create(name, 2, name)", read(CONTROLLER / "init.groovy.d" / "agents.groovy"))


if __name__ == "__main__":
    unittest.main()
