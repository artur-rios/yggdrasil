#!/usr/bin/env python3
"""Reads and validates a repository's yggdrasil.yml, the data that describes its pipeline.

    python3 scripts/pipeline_config.py describe <job-id> <repo-dir>

describe prints one JSON document on stdout, {"entry", "config", "errors"}, and exits 1 when there
are errors (or the job is unknown):

    entry    the catalog's view of the job (an application's id or a repositories entry's id): kind,
             id, repository, credentials, checks, actionsChecks, release (deploy, owner-tag or none)
             and tags -- see entry_for()
    config   the validated yggdrasil.yml, or null when the repository has none (or it is invalid)
    errors   every problem found: a field of the file outside the schema, or something the catalog
             does not allow this repository (a credential, a missing check, a release...)

A repository without a yggdrasil.yml is not an error: it behaves as it did before the file existed
(deploys only; the pipeline asks nothing else of it).

The file is data, never code run by the controller: images, commands (run on the isolated CI or
release agent), globs and Jenkins credential *names*. It is read from the repository's checkout
by the Jenkins library (yggdrasilPipeline), which calls this script and acts on its answer. The
fields, all optional, and unknown keys are errors at every level:

    checks:            name -> {image, run, docker, credentials, paths, junit, archive}
    branchPolicy:      {image, run}
    deploy:            {inputs}       check names whose archives go into the image build
    release:           {build: {image, run, files, credentials},
                        windows: {workflow, files},
                        validate: {image, run}}
    publish:           {nuget: {image, run, packages}}

Reference: docs/catalog.md ("Repository pipeline file"). Needs PyYAML.
"""

import json
import pathlib
import re
import sys

import yaml

import catalog as cat

CONFIG_FILE = "yggdrasil.yml"

# The name of the check that the branch policy reports as: a check of that name would be reused
# like any other, which the branch policy never is.
RESERVED_CHECKS = ("branch-policy",)
# An environment variable name, the left side of a `credentials` binding.
ENV_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(Exception):
    """yggdrasil.yml (or the job asked about) is not valid. `errors` has one sentence per problem,
    each starting with the field's path (checks.unit.dockr); str() joins them."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("\n".join(self.errors))


# ---- The schema ----


def _text(where, value, errors):
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: a non-empty string")


def _flag(where, value, errors):
    # A YAML `yes` is already a bool; anything else (a string "true") is a mistake worth naming.
    if not isinstance(value, bool):
        errors.append(f"{where}: true or false")


def _texts(where, value, errors):
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        errors.append(f"{where}: a list of non-empty strings")


def _credentials(where, value, errors):
    """ENV_VAR -> Jenkins credential id: what a withCredentials binding needs, nothing more."""
    if not isinstance(value, dict):
        errors.append(f"{where}: a mapping of environment variable name to Jenkins credential id")
        return
    for name, credential in value.items():
        if not isinstance(name, str) or not ENV_VAR.match(name):
            errors.append(f"{where}: '{name}' is not an environment variable name (letters, digits and '_')")
        elif not isinstance(credential, str) or not cat.CREDENTIAL_ID.match(credential):
            errors.append(f"{where}.{name}: a Jenkins credential id (letters, digits, '.', '_' and '-')")


# Each section: field -> (validator, required). A section is a mapping of exactly these fields.
CHECK = {"image": (_text, True), "run": (_text, True), "docker": (_flag, False),
         "credentials": (_credentials, False), "paths": (_texts, False), "junit": (_text, False),
         "archive": (_text, False)}
IMAGE_RUN = {"image": (_text, True), "run": (_text, True)}
SECTIONS = {
    "branchPolicy": IMAGE_RUN,
    "deploy": {"inputs": (_texts, False)},
    "release": {
        "build": {"image": (_text, True), "run": (_text, True), "files": (_texts, False),
                  "credentials": (_credentials, False)},
        "windows": {"workflow": (_text, True), "files": (_texts, False)},
        "validate": IMAGE_RUN,
    },
    "publish": {"nuget": {"image": (_text, True), "run": (_text, True), "packages": (_text, False)}},
}


def _section(where, value, fields, errors):
    """`value` must be a mapping of the fields in `fields` (a validator and whether it is required,
    or, for a nested section, another such mapping)."""
    if not isinstance(value, dict):
        errors.append(f"{where}: a mapping with {', '.join(fields)}")
        return
    for key in cat._keys(set(value) - set(fields)):
        errors.append(f"{where}.{key}: unknown field (one of {', '.join(fields)})")
    for key, spec in fields.items():
        if key not in value:
            if isinstance(spec, tuple) and spec[1]:
                errors.append(f"{where}.{key}: required")
        elif isinstance(spec, dict):
            _section(f"{where}.{key}", value[key], spec, errors)
        else:
            spec[0](f"{where}.{key}", value[key], errors)


def _errors_in(config):
    errors = []
    if not isinstance(config, dict):
        return [f"{CONFIG_FILE}: a mapping with {', '.join(['checks', *SECTIONS])}"]
    for key in cat._keys(set(config) - {"checks", *SECTIONS}):
        errors.append(f"{key}: unknown field (one of {', '.join(['checks', *SECTIONS])})")
    if "checks" in config:
        checks = config["checks"]
        if not isinstance(checks, dict):
            errors.append("checks: a mapping of check name to {image, run, ...}")
        else:
            for name, check in checks.items():
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"checks.{name}: a check's name is a non-empty string")
                elif name in RESERVED_CHECKS:
                    errors.append(f"checks.{name}: reserved (the branch policy reports it; set branchPolicy)")
                else:
                    _section(f"checks.{name}", check, CHECK, errors)
    for name, fields in SECTIONS.items():
        if name in config:
            _section(name, config[name], fields, errors)
    return errors


def load_config(repo_dir):
    """The repository's yggdrasil.yml as validated data, or None when it has none. Raises ConfigError
    naming every field outside the schema (this checks the file alone; validate_against() checks it
    against the catalog). An empty file is {}."""
    path = pathlib.Path(repo_dir) / CONFIG_FILE
    if not path.is_file():
        return None
    try:
        # safe_load: the file is untrusted (a branch of the repository), it must stay plain data.
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as error:
        raise ConfigError(f"{CONFIG_FILE}: not valid YAML ({' '.join(str(error).split())})") from None
    if config is None:
        return {}
    errors = _errors_in(config)
    if errors:
        raise ConfigError(errors)
    return config


# ---- The catalog's side ----


def entry_for(catalog, job_id):
    """What the catalog says about a job: an application's id, else a `repositories` entry's id.

        kind            "application" | "repository"
        id, repository  the job id, and the git repository it builds (an application's `repository`, else its id)
        credentials     the Jenkins credential ids its checks may bind
        checks          the checks Jenkins must report on pull requests
        actionsChecks   the GitHub Actions jobs required besides them
        release         "deploy" (an application with a release environment: Jenkins tags it),
                        "owner-tag" (the owner tags it), "none"
        tags            the version tag pattern (v* for applications)
    """
    for _, app in cat.applications(catalog):
        if app["id"] == job_id:
            releases = any(env["trigger"] == "release" for env in cat.resolve(catalog, job_id))
            return {"kind": "application", "id": job_id, "repository": app.get("repository") or job_id,
                    "credentials": list(app.get("credentials") or []),
                    "checks": list(app.get("checks") or []),
                    "actionsChecks": list(app.get("actionsChecks") or []),
                    "release": "deploy" if releases else "none", "tags": "v*"}
    for entry in cat.repositories(catalog):
        if entry["id"] == job_id:
            return {"kind": "repository", "id": job_id, "repository": job_id,
                    "credentials": entry["credentials"], "checks": entry["checks"],
                    "actionsChecks": entry["actionsChecks"], "release": entry["release"], "tags": entry["tags"]}
    raise ConfigError(f"'{job_id}' is not an application or a repository in catalog.yaml")


def validate_against(config, entry):
    """What the catalog does not allow this repository to do, as sentences (empty when fine): a
    credential outside the entry's `credentials`, a catalog check the file does not define, deploy
    inputs naming a check that is not defined, `publish` on anything but an owner-tagged
    repository, `release` on something that does not release. `config` is load_config()'s result."""
    if config is None:
        return []
    errors = []
    checks = config.get("checks") or {}

    bindings = [(f"checks.{name}.credentials", check.get("credentials")) for name, check in checks.items()]
    bindings.append(("release.build.credentials", ((config.get("release") or {}).get("build") or {}).get("credentials")))
    for where, credentials in bindings:
        for variable, credential in (credentials or {}).items():
            if credential not in entry["credentials"]:
                errors.append(f"{where}.{variable}: credential '{credential}' is not allowed for {entry['id']} "
                              f"(its catalog credentials: {entry['credentials']})")

    for name in entry["checks"]:
        if name not in checks:
            errors.append(f"checks: '{name}' is a check {entry['id']} requires in catalog.yaml but "
                          f"{CONFIG_FILE} does not define it")
    for name in (config.get("deploy") or {}).get("inputs") or []:
        if name not in checks:
            errors.append(f"deploy.inputs: '{name}' is not a check in {CONFIG_FILE}")

    if "publish" in config and not (entry["kind"] == "repository" and entry["release"] == "owner-tag"):
        errors.append(f"publish: only for a library (a catalog repositories entry with release: owner-tag); "
                      f"{entry['id']} is {_described(entry)}")
    if "release" in config and entry["release"] not in ("deploy", "owner-tag"):
        errors.append(f"release: {entry['id']} does not release (catalog.yaml: release {entry['release']}); "
                      "it needs a release environment, or release: owner-tag")
    return errors


def _described(entry):
    return f"an {entry['kind']} with release: {entry['release']}"


# ---- Command line ----


def describe(catalog, job_id, repo_dir):
    """The document `describe` prints, and whether the job exists is for entry_for() to raise."""
    entry = entry_for(catalog, job_id)
    try:
        config = load_config(repo_dir)
    except ConfigError as error:
        return {"entry": entry, "config": None, "errors": error.errors}
    return {"entry": entry, "config": config, "errors": validate_against(config, entry)}


def main(argv):
    # Plain LF even on Windows, as catalog.py: shell callers would otherwise read a trailing CR.
    sys.stdout.reconfigure(newline=chr(10))
    if len(argv) != 3 or argv[0] != "describe":
        print(__doc__, file=sys.stderr)
        return 2
    _, job_id, repo_dir = argv
    try:
        if not pathlib.Path(repo_dir).is_dir():
            raise ConfigError(f"{repo_dir}: not a directory")
        document = describe(cat.load(), job_id, repo_dir)
    except (ConfigError, cat.CatalogError) as error:
        print(f"pipeline_config: {error}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2))
    return 1 if document["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
