#!/usr/bin/env python3
"""Reads and validates a repository's yggdrasil.yml, the data that describes its pipeline.

    python3 scripts/pipeline_config.py describe <job-id> <repo-dir>
    python3 scripts/pipeline_config.py plan <job-id> <repo-dir> --statuses <file> --changed <file|-> --branch <name>

describe prints one JSON document on stdout, {"entry", "config", "errors"}, and exits 1 when there
are errors (or the job is unknown):

    entry    the catalog's view of the job (an application's id or a repositories entry's id): kind,
             id, repository, credentials, checks, actionsChecks, release (deploy, owner-tag or none)
             and tags -- see entry_for()
    config   the validated yggdrasil.yml, or null when the repository has none (or it is invalid)
    errors   every problem found: a field of the file outside the schema, or something the catalog
             does not allow this repository (a credential, a missing check, a release...)

plan prints a JSON list, one entry per check of the file: name, action (run, reuse or skip), reason, image,
run, docker, credentials, junit and archive. --statuses is `forge.sh get-statuses` output, --changed lists the
changed files one per line ("-": unknown, every check runs). See plan_checks(); exit 1 when the file is invalid.

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
# The patterns below are used with fullmatch(): `$` would also accept a trailing newline, which would
# reach a shell command line or a docker argument.
# An environment variable name, the left side of a `credentials` binding.
ENV_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A check's name: the status context, a file name for its results. Lowercase, so "Unit" and "unit"
# cannot be two checks the server treats as one.
CHECK_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
# A Docker image reference: [registry[:port]/]path[:tag][@digest]. No whitespace, and it cannot start
# with '-' (it would be read as an option of `docker run`). The registry decides whether it exists.
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")


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


def _image(where, value, errors):
    if not isinstance(value, str) or not IMAGE.fullmatch(value):
        errors.append(f"{where}: a Docker image (registry/name:tag; no spaces, not starting with '-')")


def _flag(where, value, errors):
    # A YAML `yes` is already a bool; anything else (a string "true") is a mistake worth naming.
    if not isinstance(value, bool):
        errors.append(f"{where}: true or false")


def _texts(where, value, errors):
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        errors.append(f"{where}: a list of non-empty strings")


def _globs(where, value, errors):
    # [] would read as "never run" to one reader and "always run" to another: say which in the file.
    if isinstance(value, list) and not value:
        errors.append(f"{where}: must list at least one glob (leave paths out to always run)")
    else:
        _texts(where, value, errors)


def _credentials(where, value, errors):
    """ENV_VAR -> Jenkins credential id: what a withCredentials binding needs, nothing more."""
    if not isinstance(value, dict):
        errors.append(f"{where}: a mapping of environment variable name to Jenkins credential id")
        return
    for name, credential in value.items():
        if not isinstance(name, str) or not ENV_VAR.fullmatch(name):
            errors.append(f"{where}: '{name}' is not an environment variable name (letters, digits and '_')")
        elif not isinstance(credential, str) or not cat.CREDENTIAL_ID.fullmatch(credential):
            errors.append(f"{where}.{name}: a Jenkins credential id (letters, digits, '.', '_' and '-')")


# Each section: field -> (validator, required). A section is a mapping of exactly these fields.
CHECK = {"image": (_image, True), "run": (_text, True), "docker": (_flag, False),
         "credentials": (_credentials, False), "paths": (_globs, False), "junit": (_text, False),
         "archive": (_text, False)}
IMAGE_RUN = {"image": (_image, True), "run": (_text, True)}
SECTIONS = {
    "branchPolicy": IMAGE_RUN,
    "deploy": {"inputs": (_texts, False)},
    "release": {
        "build": {"image": (_image, True), "run": (_text, True), "files": (_texts, False),
                  "credentials": (_credentials, False)},
        "windows": {"workflow": (_text, True), "files": (_texts, False)},
        "validate": IMAGE_RUN,
    },
    "publish": {"nuget": {"image": (_image, True), "run": (_text, True), "packages": (_text, False)}},
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
                if not isinstance(name, str) or not CHECK_NAME.fullmatch(name):
                    errors.append(f"checks.{name}: a check's name is lowercase letters, digits, '.', '_' and '-', "
                                  "starting with a letter or digit")
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
        releaseCredentials  the ones its release build (release.build) may bind, and nothing else
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
                    "releaseCredentials": list(app.get("releaseCredentials") or []),
                    "checks": list(app.get("checks") or []),
                    "actionsChecks": list(app.get("actionsChecks") or []),
                    "release": "deploy" if releases else "none", "tags": "v*"}
    for entry in cat.repositories(catalog):
        if entry["id"] == job_id:
            return {"kind": "repository", "id": job_id, "repository": job_id,
                    "credentials": entry["credentials"], "releaseCredentials": entry["releaseCredentials"],
                    "checks": entry["checks"],
                    "actionsChecks": entry["actionsChecks"], "release": entry["release"], "tags": entry["tags"]}
    raise ConfigError(f"'{job_id}' is not an application or a repository in catalog.yaml")


def validate_against(config, entry):
    """What the catalog does not allow this repository to do, as sentences (empty when fine): a
    check's credential outside the entry's `credentials`, a release build's outside its
    `releaseCredentials` (each list only for its own context), a catalog check the file does not define, deploy
    inputs naming a check that is not defined, `publish` on anything but an owner-tagged
    repository, `release` on something that does not release. `config` is load_config()'s result."""
    if config is None:
        return []
    errors = []
    checks = config.get("checks") or {}

    # A check runs any branch's code: it may bind only `credentials`. The release build runs the
    # merged release's code on the release agent: only `releaseCredentials`. Neither list covers both.
    bindings = [(f"checks.{name}.credentials", check.get("credentials"), "credentials")
                for name, check in checks.items()]
    bindings.append(("release.build.credentials",
                     ((config.get("release") or {}).get("build") or {}).get("credentials"), "releaseCredentials"))
    for where, credentials, allowed in bindings:
        for variable, credential in (credentials or {}).items():
            if credential not in entry[allowed]:
                errors.append(f"{where}.{variable}: credential '{credential}' is not allowed for {entry['id']} "
                              f"(its catalog {allowed}: {entry[allowed]})")

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


# ---- Planning the checks ----


def _glob_regex(glob):
    """A path glob as a regular expression: `*` and `?` stay within one directory, `**` crosses them
    (and `**/` also matches no directory at all, so `**/*.md` matches README.md), `[abc]` and `[!abc]`
    are character classes. fnmatch cannot do this: its `*` matches `/`, so `docs/*.md` would take
    docs/a/b.md and `**` would mean nothing."""
    out, i = [], 0
    while i < len(glob):
        char = glob[i]
        if char == "*":
            j = i
            while j < len(glob) and glob[j] == "*":
                j += 1
            if j - i == 1:
                out.append("[^/]*")
            elif glob.startswith("/", j):
                out.append("(?:.*/)?")
                j += 1
            else:
                out.append(".*")
            i = j
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            j = i + 1
            if j < len(glob) and glob[j] in "!^":
                j += 1
            if j < len(glob) and glob[j] == "]":  # a leading ] is part of the class
                j += 1
            while j < len(glob) and glob[j] != "]":
                j += 1
            if j >= len(glob):  # never closed: a literal [
                out.append(re.escape(char))
                i += 1
                continue
            body = glob[i + 1:j]
            negated = body[:1] in ("!", "^")
            body = body[1:] if negated else body
            body = body.replace("\\", "\\\\").replace("[", "\\[")
            out.append("[^/" + body + "]" if negated else "[" + body + "]")
            i = j + 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out), re.DOTALL)


def glob_match(glob, path):
    """Whether the repository-relative `path` matches `glob` as a whole (see _glob_regex)."""
    if path.startswith("./"):
        path = path[2:]
    return _glob_regex(glob).fullmatch(path) is not None


def plan_checks(config, statuses, changed, branch, jenkins_user):
    """What to do with each check of the file, in its order: a list of

        name, action, reason   action is run, reuse or skip; reason says why, for the build log
        image, run, docker, credentials, junit, archive   what to run it with (docker false, credentials {},
                                                         junit and archive None when the file has none)

    reuse   the commit already has a `success` for the check created by `jenkins_user` (the Jenkins
            user on the git server; on GitHub the App's bot login). Anyone with write access can post
            a status, so a success by somebody else proves nothing and the check runs; a failure, an
            error or a pending of Jenkins' own runs again (that is what a rebuild is for).
    skip    the check has `paths`, the changed files are known and none matches one of them. An empty
            `paths` is no restriction, and changed None (unknown: no base to compare with) runs it.
    run     the rest.

    `statuses` are (context, state, creator) as `forge.sh get-statuses` prints them (the last of a
    context counts); `changed` the repository-relative paths changed by the build, or None when unknown.
    A renovate/* branch gets no credentials at all: dependency updates come from outside and their
    checks must never see a secret. `config` is not changed; None or without checks gives []."""
    latest = {context: (state, creator) for context, state, creator in statuses}
    no_secrets = branch.startswith("renovate/")
    planned = []
    for name, check in ((config or {}).get("checks") or {}).items():
        state, creator = latest.get(name, (None, None))
        paths = check.get("paths") or []
        if state == "success" and creator == jenkins_user:
            action, reason = "reuse", f"reused: success by {jenkins_user} on this commit"
        elif paths and changed is not None and not any(glob_match(glob, path) for glob in paths for path in changed):
            action, reason = "skip", f"skipped: no changes under {', '.join(paths)}"
        else:
            action = "run"
            if state is None:
                reason = "no earlier result"
            elif state == "success":
                reason = f"success by {creator or 'an unknown user'}, not {jenkins_user}"
            else:
                reason = f"last result: {state}"
            if paths:
                reason += "; changed files unknown" if changed is None else "; changed files match"
        planned.append({"name": name, "action": action, "reason": reason, "image": check["image"],
                        "run": check["run"], "docker": check.get("docker", False),
                        "credentials": {} if no_secrets else dict(check.get("credentials") or {}),
                        "junit": check.get("junit"), "archive": check.get("archive")})
    return planned


def read_statuses(text):
    """`forge.sh get-statuses` output -> [(context, state, creator)]. A line with two columns (an older
    forge.sh) has no creator, so nothing it says is ever reused; blank lines are ignored."""
    statuses = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        columns = line.split("\t")
        if len(columns) < 2 or not columns[0] or not columns[1]:
            raise ConfigError(f"statuses: not '<context><tab><state><tab><creator>': {line!r}")
        statuses.append((columns[0], columns[1], columns[2] if len(columns) > 2 else ""))
    return statuses


def _read(path, what):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: cannot read the {what} ({getattr(error, 'strerror', None) or error})") from None


# ---- Command line ----


def describe(catalog, job_id, repo_dir):
    """The document `describe` prints, and whether the job exists is for entry_for() to raise."""
    entry = entry_for(catalog, job_id)
    try:
        config = load_config(repo_dir)
    except ConfigError as error:
        return {"entry": entry, "config": None, "errors": error.errors}
    return {"entry": entry, "config": config, "errors": validate_against(config, entry)}


PLAN_OPTIONS = ("--statuses", "--changed", "--branch")


def plan(catalog, job_id, repo_dir, statuses_file, changed_file, branch):
    """The list `plan` prints: plan_checks() for the job's file, with the catalog's Jenkins user. A file
    that is invalid, or breaks the catalog's rules, is a ConfigError naming every problem. `changed_file`
    lists one path per line, or is "-" when the changes are unknown."""
    document = describe(catalog, job_id, repo_dir)
    if document["errors"]:
        raise ConfigError(document["errors"])
    statuses = read_statuses(_read(statuses_file, "statuses"))
    changed = None
    if changed_file != "-":
        lines = (line.rstrip("\r") for line in _read(changed_file, "changed files").split("\n"))
        changed = [line for line in lines if line]
    return plan_checks(document["config"], statuses, changed, branch, cat.forge(catalog)["jenkinsUser"])


def _command(argv):
    """The function (catalog -> JSON-able) the arguments ask for, or None when they are not a command."""
    if len(argv) == 3 and argv[0] == "describe":
        return lambda catalog: describe(catalog, argv[1], argv[2])
    if len(argv) == 3 + 2 * len(PLAN_OPTIONS) and argv[0] == "plan" and sorted(argv[3::2]) == sorted(PLAN_OPTIONS):
        options = dict(zip(argv[3::2], argv[4::2]))
        return lambda catalog: plan(catalog, argv[1], argv[2], options["--statuses"], options["--changed"],
                                    options["--branch"])
    return None


def main(argv):
    # Plain LF even on Windows, as catalog.py: shell callers would otherwise read a trailing CR.
    sys.stdout.reconfigure(newline=chr(10))
    command = _command(argv)
    if command is None:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if not pathlib.Path(argv[2]).is_dir():
            raise ConfigError(f"{argv[2]}: not a directory")
        result = command(cat.load())
    except (ConfigError, cat.CatalogError) as error:
        print(f"pipeline_config: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    # describe reports the file's problems in its document and by its status; plan raised them above.
    return 1 if argv[0] == "describe" and result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
