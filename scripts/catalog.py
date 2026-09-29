#!/usr/bin/env python3
"""Reads catalog.yaml, validates it, and answers questions about it.

    python3 scripts/catalog.py validate
    python3 scripts/catalog.py environments [<app>]        ids, in promotion order
    python3 scripts/catalog.py get <app> <environment> <option>
    python3 scripts/catalog.py plan <app>                  JSON: the app's environments, options resolved
    python3 scripts/catalog.py applications [--deployable] ids
    python3 scripts/catalog.py systems                    id<TAB>name, one per line
    python3 scripts/catalog.py show <app>                 JSON: the application's fields and its system
    python3 scripts/catalog.py owner | repository         the GitHub owner, this repository's name
    python3 scripts/catalog.py add-application < new.json adds an application, keeping the comments

add-application reads {"system": {"id", "name", "description"}, "application": {...}} on stdin. The
application goes at the end of that system, or of a new system (name and description only count
then), inserted before the yggdrasil system if it is the last one.

Used by scripts/deploy.sh, scripts/ygg.sh and github/rulesets.py, and by CI to reject a broken catalog. The Jenkins
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


def system_of(catalog, app_id):
    for system, app in applications(catalog):
        if app["id"] == app_id:
            return system
    raise CatalogError(f"'{app_id}' is not an application in catalog.yaml")


SYSTEM_LINE = re.compile(r"^  - id:\s*(\S+)\s*$")


def _flow(value):
    """A YAML value on one line: scalars as a block would write them (plain when they can be, so
    URLs stay unquoted), lists and mappings in flow style."""
    if isinstance(value, (list, dict)):
        return yaml.safe_dump(value, default_flow_style=True, width=1 << 30, sort_keys=False).strip()
    text = yaml.safe_dump(value, width=1 << 30, allow_unicode=True).strip()
    return text[:-4].rstrip() if text.endswith("\n...") else text


def _entry(fields, indent):
    lines = []
    for i, (key, value) in enumerate(fields.items()):
        lead = "- " if i == 0 else "  "
        lines.append(f"{' ' * indent}{lead}{key}: {_flow(value)}\n")
    return lines


def add_application(text, system, app):
    """Returns the catalog text with `app` added to `system` (a mapping with at least an id). Works
    on the text rather than a dump of the parsed YAML, so every comment stays where it was. Expects
    the layout of the shipped catalog: `systems:` at column 0, each system as `  - id: <id>`."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "systems:")
    except StopIteration:
        raise CatalogError("catalog.yaml has no top-level 'systems:' line") from None
    heads = [i for i in range(start + 1, len(lines)) if SYSTEM_LINE.match(lines[i])]
    ids = [SYSTEM_LINE.match(lines[i]).group(1) for i in heads]

    def block_end(index):
        # The block runs to the next system (or the end), less the blank lines and comments that
        # introduce that next system.
        end = heads[index + 1] if index + 1 < len(heads) else len(lines)
        while end > heads[index] and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        return end

    if system["id"] in ids:
        index = ids.index(system["id"])
        end = block_end(index)
        block = lines[heads[index]:end]
        if not any(line.rstrip() == "    applications:" for line in block):
            new = ["    applications:\n"] + _entry(app, 6)
        else:
            new = _entry(app, 6)
        lines[end:end] = new
    else:
        fields = {"id": system["id"], "name": system.get("name") or system["id"]}
        if system.get("description"):
            fields["description"] = system["description"]
        new = _entry(fields, 2) + ["    applications:\n"] + _entry(app, 6) + ["\n"]
        if ids and ids[-1] == "yggdrasil":
            # Before the platform's own system and the comments that introduce it.
            at = heads[-1]
            while at > start + 1 and (lines[at - 1].lstrip().startswith("#")):
                at -= 1
        else:
            at = len(lines)
            new = ["\n"] + new[:-1]
        lines[at:at] = new
    result = "".join(lines)

    # The text edit must mean exactly "this application, in this system": check it on the parse.
    parsed = yaml.safe_load(result) or {}
    errors = validate(parsed)
    if errors:
        raise CatalogError("the application would make catalog.yaml invalid:\n  " + "\n  ".join(errors))
    if application(parsed, app["id"]) != app or system_of(parsed, app["id"])["id"] != system["id"]:
        raise CatalogError("catalog.yaml's layout is not the expected one: add the application by hand")
    return result


def main(argv):
    # Plain LF even on Windows: shell callers ($(catalog.py get ...) in Git Bash) would otherwise
    # get a trailing carriage return on every value.
    sys.stdout.reconfigure(newline=chr(10))
    try:
        catalog = load()
        command, args = (argv[0], argv[1:]) if argv else ("", [])
        if command == "add-application" and not args:
            request = json.load(sys.stdin)
            if not isinstance(request, dict) or not isinstance(request.get("system"), dict) \
                    or not isinstance(request.get("application"), dict):
                raise CatalogError('add-application reads {"system": {...}, "application": {...}}')
            system, app = request["system"], request["application"]
            if not isinstance(system.get("id"), str) or not ID.match(system["id"]):
                raise CatalogError(f"system id '{system.get('id')}' must be lowercase letters, digits and dashes")
            if any(a["id"] == app.get("id") for _, a in applications(catalog)):
                raise CatalogError(f"'{app.get('id')}' is already an application in catalog.yaml")
            text = add_application(CATALOG.read_text(encoding="utf-8"), system, app)
            with open(CATALOG, "w", encoding="utf-8", newline="\n") as file:
                file.write(text)
            print(f"catalog.yaml: added {app['id']} to {system['id']}")
            return 0
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
        elif command == "systems":
            print("\n".join(f"{s['id']}\t{s.get('name', s['id'])}" for s in catalog.get("systems") or []))
        elif command == "show" and len(args) == 1:
            app = dict(application(catalog, args[0]))
            app["system"] = system_of(catalog, args[0])["id"]
            print(json.dumps(app, indent=2))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except CatalogError as error:
        print(f"catalog: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
