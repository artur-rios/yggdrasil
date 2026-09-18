#!/usr/bin/env python3
"""Reads catalog.yaml, validates it, and answers questions about it.

    python3 scripts/catalog.py validate
    python3 scripts/catalog.py environments [<app>]        ids, in promotion order
    python3 scripts/catalog.py get <app> <environment> <option>
    python3 scripts/catalog.py plan <app>                  JSON: the app's environments, options resolved
    python3 scripts/catalog.py applications [--deployable] ids
    python3 scripts/catalog.py owner | repository         the GitHub owner, this repository's name

Used by scripts/deploy.sh and github/rulesets.py, and by CI to reject a broken catalog. The Jenkins
shared library resolves the same options itself (it cannot run Python on the controller); keep
resolve() and jenkins/library/vars/yggdrasilPipeline.groovy in step. Reference: docs/catalog.md.

Needs PyYAML (pip install pyyaml; Ubuntu: apt install python3-yaml).
"""

import fnmatch
import json
import pathlib
import re
import sys

import yaml

CATALOG = pathlib.Path(__file__).resolve().parent.parent / "catalog.yaml"

ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KINDS = {"api", "web", "worker", "platform"}
MODES = {"proxy", "ports"}
TRIGGERS = {"manual", "branch", "release"}

# Every environment option, with its default. An application's override may set any of them except
# the identity fields (id, name).
DEFAULTS = {
    "mode": "proxy",
    "trigger": "manual",
    "branches": [],
    "agent": None,  # defaults to the environment id
    "approval": False,
    "waitTimeout": 300,
    "keepImages": 3,
    "checksTimeout": 3600,
}


class CatalogError(Exception):
    pass


def load(path=CATALOG):
    catalog = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    errors = validate(catalog)
    if errors:
        raise CatalogError("catalog.yaml is invalid:\n  " + "\n  ".join(errors))
    return catalog


def validate(catalog):
    errors = []
    if not isinstance(catalog.get("owner"), str) or not catalog["owner"]:
        errors.append("owner: required (the GitHub user or organisation of the repositories)")
    if "repository" in catalog and (not isinstance(catalog["repository"], str) or not catalog["repository"]):
        errors.append("repository: the name of this repository under the owner (default yggdrasil)")

    environments = catalog.get("environments")
    if not isinstance(environments, list) or not environments:
        errors.append("environments: required, a non-empty list")
        environments = []
    env_ids = []
    for i, env in enumerate(environments):
        where = f"environments[{i}]"
        if not isinstance(env, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        env_id = env.get("id")
        if not isinstance(env_id, str) or not ID.match(env_id):
            errors.append(f"{where}.id: required, lowercase letters, digits and dashes")
        elif env_id in env_ids:
            errors.append(f"{where}.id: duplicate '{env_id}'")
        else:
            env_ids.append(env_id)
            where = f"environment '{env_id}'"
        if not isinstance(env.get("name"), str) or not env["name"]:
            errors.append(f"{where}.name: required")
        errors += _options_errors(where, env)

    app_ids = []
    for system in catalog.get("systems") or []:
        sid = system.get("id", "?")
        if not isinstance(system.get("id"), str) or not ID.match(system["id"]):
            errors.append(f"system '{sid}': id must be lowercase letters, digits and dashes")
        for app in system.get("applications") or []:
            aid = app.get("id")
            where = f"application '{aid}'"
            if not isinstance(aid, str) or not ID.match(aid):
                errors.append(f"system '{sid}': application id '{aid}' must be lowercase letters, digits and dashes")
                continue
            if aid in app_ids:
                errors.append(f"{where}: duplicate id")
            app_ids.append(aid)
            if app.get("kind") not in KINDS:
                errors.append(f"{where}.kind: one of {sorted(KINDS)}")
            if not str(app.get("health", "")).startswith(("http://", "https://")):
                errors.append(f"{where}.health: an absolute http(s) URL")
            overrides = app.get("environments")
            if overrides is None:
                continue
            if not isinstance(overrides, dict) or not overrides:
                errors.append(f"{where}.environments: a non-empty mapping of environment id to options")
                continue
            for env_id, options in overrides.items():
                if env_id not in env_ids:
                    errors.append(f"{where}.environments.{env_id}: not an environment in the catalog")
                elif options is not None and not isinstance(options, dict):
                    errors.append(f"{where}.environments.{env_id}: options must be a mapping (or {{}})")
                else:
                    errors += _options_errors(f"{where}.environments.{env_id}", options or {}, override=True)
    return errors


def _options_errors(where, options, override=False):
    errors = []
    if override:
        for key in ("id", "name"):
            if key in options:
                errors.append(f"{where}.{key}: an application cannot rename an environment")
    unknown = set(options) - set(DEFAULTS) - {"id", "name"}
    if unknown:
        errors.append(f"{where}: unknown option(s) {sorted(unknown)}")
    if "mode" in options and options["mode"] not in MODES:
        errors.append(f"{where}.mode: one of {sorted(MODES)}")
    if "trigger" in options and options["trigger"] not in TRIGGERS:
        errors.append(f"{where}.trigger: one of {sorted(TRIGGERS)}")
    if "branches" in options and not _branch_list(options["branches"]):
        errors.append(f"{where}.branches: a glob or a list of globs")
    if not override and options.get("trigger") == "branch" and not options.get("branches"):
        errors.append(f"{where}.branches: required when trigger is branch")
    for key in ("waitTimeout", "keepImages", "checksTimeout"):
        if key in options and (not isinstance(options[key], int) or options[key] < 1):
            errors.append(f"{where}.{key}: a positive whole number")
    if "approval" in options and not isinstance(options["approval"], bool):
        errors.append(f"{where}.approval: true or false")
    return errors


def _branch_list(value):
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return value
    return None


def applications(catalog, deployable=False):
    for system in catalog.get("systems") or []:
        for app in system.get("applications") or []:
            if deployable and app.get("kind") == "platform":
                continue
            yield system, app


def application(catalog, app_id):
    for _, app in applications(catalog):
        if app["id"] == app_id:
            return app
    raise CatalogError(f"'{app_id}' is not an application in catalog.yaml")


def resolve(catalog, app_id):
    """The application's environments, in promotion order, each with every option resolved:
    the default, then the environment's value, then the application's override."""
    app = application(catalog, app_id)
    overrides = app.get("environments")
    plan = []
    for env in catalog["environments"]:
        if overrides is not None and env["id"] not in overrides:
            continue
        resolved = {"id": env["id"], "name": env["name"]}
        for key, default in DEFAULTS.items():
            resolved[key] = default
            if key in env:
                resolved[key] = env[key]
            if overrides is not None and key in (overrides[env["id"]] or {}):
                resolved[key] = overrides[env["id"]][key]
        resolved["branches"] = _branch_list(resolved["branches"]) or []
        resolved["agent"] = resolved["agent"] or env["id"]
        plan.append(resolved)
    return plan


def matches_branch(environment, branch):
    return any(fnmatch.fnmatchcase(branch, pattern) for pattern in environment["branches"])


def main(argv):
    # Plain LF even on Windows: shell callers ($(catalog.py get ...) in Git Bash) would otherwise
    # get a trailing carriage return on every value.
    sys.stdout.reconfigure(newline=chr(10))
    try:
        catalog = load()
        command, args = (argv[0], argv[1:]) if argv else ("", [])
        if command == "validate":
            apps = sum(1 for _ in applications(catalog))
            print(f"catalog.yaml: {len(catalog['environments'])} environments, "
                  f"{len(catalog.get('systems') or [])} systems, {apps} applications")
        elif command == "environments":
            ids = [e["id"] for e in resolve(catalog, args[0])] if args else [e["id"] for e in catalog["environments"]]
            print("\n".join(ids))
        elif command == "get" and len(args) == 3:
            app_id, env_id, option = args
            for env in resolve(catalog, app_id):
                if env["id"] == env_id:
                    if option not in env:
                        raise CatalogError(f"unknown option '{option}'")
                    value = env[option]
                    print(" ".join(value) if isinstance(value, list) else str(value).lower() if isinstance(value, bool) else value)
                    return 0
            raise CatalogError(f"'{app_id}' does not deploy to '{env_id}' (catalog.yaml)")
        elif command == "plan" and len(args) == 1:
            print(json.dumps(resolve(catalog, args[0]), indent=2))
        elif command == "owner":
            print(catalog["owner"])
        elif command == "repository":
            print(catalog.get("repository") or "yggdrasil")
        elif command == "applications":
            print("\n".join(a["id"] for _, a in applications(catalog, deployable="--deployable" in args)))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except CatalogError as error:
        print(f"catalog: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
