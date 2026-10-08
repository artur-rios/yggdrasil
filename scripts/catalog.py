#!/usr/bin/env python3
"""Reads catalog.yaml, validates it, and answers questions about it.

    python3 scripts/catalog.py validate
    python3 scripts/catalog.py environments [<app>]        ids, in promotion order
    python3 scripts/catalog.py get <app> <environment> <option>
    python3 scripts/catalog.py environment <environment> [<option>]  JSON: its options, defaults applied
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
# The status API's own rules for these fields (status/src/Yggdrasil.Status/Catalog/CatalogLoader.cs):
# it refuses the whole catalog at start-up when one is broken, so validate refuses it first.
HOST_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
# One DNS label: what <host><hostSuffix> must stay, so the wildcard certificate *.DOMAIN covers it.
HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
HOST_SUFFIX = re.compile(r"^([a-z0-9-]*[a-z0-9])?$")
HOST_PORT = re.compile(r"^[A-Za-z0-9.-]+:[0-9]{1,5}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+$")
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
    "hostSuffix": "",
    "onDemand": False,
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

    systems = catalog.get("systems")
    if not isinstance(systems, list) or not systems:
        errors.append("systems: required, a non-empty list")
        systems = systems if isinstance(systems, list) else []
    environments_by_id = {env["id"]: env for env in environments if isinstance(env, dict) and env.get("id") in env_ids}
    system_ids = []
    app_ids = []
    for i, system in enumerate(systems):
        if not isinstance(system, dict):
            errors.append(f"systems[{i}]: must be a mapping")
            continue
        sid = system.get("id", "?")
        if not isinstance(system.get("id"), str) or not ID.match(system["id"]):
            errors.append(f"system '{sid}': id must be lowercase letters, digits and dashes")
        elif sid in system_ids:
            errors.append(f"system '{sid}': duplicate id")
        else:
            system_ids.append(sid)
        if not _text(system.get("name")):
            errors.append(f"system '{sid}'.name: required")
        applications = system.get("applications")
        if not isinstance(applications, list) or not applications:
            errors.append(f"system '{sid}'.applications: required, a non-empty list")
            applications = applications if isinstance(applications, list) else []
        for app in applications:
            if not isinstance(app, dict):
                errors.append(f"system '{sid}': application {app!r} must be a mapping")
                continue
            aid = app.get("id")
            where = f"application '{aid}'"
            if not isinstance(aid, str) or not ID.match(aid):
                errors.append(f"system '{sid}': application id '{aid}' must be lowercase letters, digits and dashes")
                continue
            if aid in app_ids:
                errors.append(f"{where}: duplicate id")
            app_ids.append(aid)
            if not _text(app.get("name")):
                errors.append(f"{where}.name: required")
            if app.get("kind") not in KINDS:
                errors.append(f"{where}.kind: one of {sorted(KINDS)}")
            if not str(app.get("health", "")).startswith(("http://", "https://")):
                errors.append(f"{where}.health: an absolute http(s) URL")
            errors += _application_field_errors(where, app)
            errors += _host_suffix_errors(where, app, environments_by_id)
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
                    # An override may switch an environment to trigger branch and rely on the
                    # environment's branches -- but something has to name them.
                    resolved = {**environments_by_id[env_id], **(options or {})}
                    if resolved.get("trigger") == "branch" and not resolved.get("branches"):
                        errors.append(f"{where}.environments.{env_id}.branches: required when trigger is branch "
                                      "(neither the application nor the environment sets them)")
    errors += _project_errors(systems, env_ids)
    return errors


def _project_errors(systems, env_ids):
    """deploy.sh names each Compose project <application>-<environment>: two pairs must not give the
    same name (application a-b in environment c, application a in environment b-c), or one deploy
    would replace the other's containers."""
    errors = []
    projects = {}
    for system in systems:
        for app in (system.get("applications") or []) if isinstance(system, dict) else []:
            if not isinstance(app, dict) or app.get("kind") == "platform" or not isinstance(app.get("id"), str):
                continue
            overrides = app.get("environments")
            for env_id in (overrides if isinstance(overrides, dict) else env_ids):
                project = f"{app['id']}-{env_id}"
                other = projects.setdefault(project, (app["id"], env_id))
                if other != (app["id"], env_id):
                    errors.append(f"application '{app['id']}' in '{env_id}': Compose project '{project}' is also "
                                  f"'{other[0]}' in '{other[1]}'; rename one of them")
    return errors


def _text(value):
    """The value stripped when it is a non-blank string, else None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _application_field_errors(where, app):
    errors = []
    for key, pattern, what in (
        ("metrics", HOST_PORT, "host:port, e.g. shop-api:9464"),
        ("host", HOST_NAME, "a host name label, e.g. shop-api"),
        ("repository", REPOSITORY, "a GitHub repository name"),
    ):
        if app.get(key) is not None and not (_text(app[key]) and pattern.match(_text(app[key]))):
            errors.append(f"{where}.{key}: {what}")
    if app.get("metricsPath") is not None:
        if not (_text(app["metricsPath"]) or "").startswith("/"):
            errors.append(f"{where}.metricsPath: a path starting with /")
        if app.get("metrics") is None:
            errors.append(f"{where}.metricsPath: set without metrics")
    checks = app.get("checks")
    if checks is not None and (not isinstance(checks, list) or not all(_text(check) for check in checks)):
        errors.append(f"{where}.checks: a list of GitHub check names, none empty")
    container = app.get("container")
    if container is not None and (not isinstance(container, dict)
                                  or any(container.get(k) is not None and not _text(container[k]) for k in ("project", "service"))):
        errors.append(f"{where}.container: a mapping of project and/or service names")
    return errors


def _host_suffix_errors(where, app, environments_by_id):
    """<host><hostSuffix> must stay one DNS label of at most 63 characters in every environment the
    application deploys to, so that the wildcard certificate and DNS record *.DOMAIN cover it."""
    host = _text(app.get("host"))
    overrides = app.get("environments")
    if not host or (overrides is not None and not isinstance(overrides, dict)):
        return []
    errors = []
    for env_id, env in environments_by_id.items():
        if overrides is not None and env_id not in overrides:
            continue
        override = overrides.get(env_id) if overrides is not None else None
        suffix = (override if isinstance(override, dict) and "hostSuffix" in override else env).get("hostSuffix", "")
        if not suffix or not isinstance(suffix, str) or not HOST_SUFFIX.match(suffix):
            continue  # nothing appended, or reported as an invalid hostSuffix already
        name = host + suffix
        if len(name) > 63 or not HOST_LABEL.match(name):
            errors.append(f"{where}.host: '{host}' with the hostSuffix '{suffix}' of '{env_id}' must be one DNS label "
                          "of at most 63 characters (lowercase letters, digits and dashes, no dots)")
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
        # bool is an int in Python: `true` would otherwise pass as 1.
        value = options.get(key)
        if key in options and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            errors.append(f"{where}.{key}: a positive whole number")
    for key in ("approval", "onDemand"):
        if key in options and not isinstance(options[key], bool):
            errors.append(f"{where}.{key}: true or false")
    if "hostSuffix" in options and not (isinstance(options["hostSuffix"], str) and HOST_SUFFIX.match(options["hostSuffix"])):
        errors.append(f"{where}.hostSuffix: lowercase letters, digits and dashes, not ending with a dash "
                      "(e.g. -dev), or empty")
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


def environment_options(catalog, env_id):
    """An environment's own options, defaults applied: what it is before any application overrides it."""
    for env in catalog["environments"]:
        if env["id"] == env_id:
            resolved = {"id": env["id"], "name": env["name"]}
            for key, default in DEFAULTS.items():
                resolved[key] = env.get(key, default)
            resolved["branches"] = _branch_list(resolved["branches"]) or []
            resolved["agent"] = resolved["agent"] or env["id"]
            return resolved
    raise CatalogError(f"'{env_id}' is not an environment in catalog.yaml")


def _print_value(value):
    print(" ".join(value) if isinstance(value, list) else str(value).lower() if isinstance(value, bool) else value)


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
                    _print_value(env[option])
                    return 0
            raise CatalogError(f"'{app_id}' does not deploy to '{env_id}' (catalog.yaml)")
        elif command == "environment" and len(args) in (1, 2):
            env = environment_options(catalog, args[0])
            if len(args) == 1:
                print(json.dumps(env, indent=2))
            elif args[1] not in env:
                raise CatalogError(f"unknown option '{args[1]}'")
            else:
                _print_value(env[args[1]])
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
