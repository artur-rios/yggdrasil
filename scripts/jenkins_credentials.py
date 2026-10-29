"""The repositories' own Jenkins credentials, as Configuration as Code, from one file per credential.

    python3 scripts/jenkins_credentials.py <directory>

Prints a casc.credentials.yaml with a secret text credential for each file of <directory> (JENKINS_CREDENTIALS_DIR in
platform.env), named by the file and read from it inside the controller, where scripts/platform.sh mounts the
directory at /run/secrets/jenkins-credentials. Prints nothing when the directory holds no credential (dot files are
left out). The secrets stay in their files: the output names only the ids.

Why files and not the UI: the controller replaces its whole list of system credentials with the configured ones
(casc.forgejo.yaml, casc.nuget.yaml, ...) at every start, so a credential made in the UI was gone after the next
`platform.sh up` that recreated it. Every repository credential (reportgenerator-license, the signing keys) lives
here instead, and survives.

Each file is checked before Jenkins would read it, since a credential it cannot read stops the controller's start:
- its name is a `credentials` or `releaseCredentials` id of some catalog entry (a typo is refused, naming the ids
  that are allowed);
- it is not empty, and does not end with a newline, which would become part of the secret (write it with
  printf '%s' SECRET > FILE);
- the controller can read it: it runs as uid 1000 in its container, which on a rootless engine is a subordinate id of
  the platform's user and reads the file as "other", so the file is mode 644 and the directory 755, inside
  /etc/yggdrasil (750), which only that user can enter -- like the other secret files (docs/setup.md, 9.1).
Exit status 1 names every problem; nothing is printed then.
"""
import pathlib
import stat
import sys

import catalog

MOUNT = "/run/secrets/jenkins-credentials"


def allowed_ids(cat):
    """Every credential id a catalog entry may bind: its checks' (`credentials`) and its release build's."""
    entries = [app for _, app in catalog.applications(cat)] + catalog.repositories(cat)
    return sorted({cid for e in entries for field in ("credentials", "releaseCredentials") for cid in e.get(field) or []})


def problems_of(directory, allowed):
    """The files of `directory` that are credentials, and every reason one of them cannot be used."""
    if not directory.is_dir():
        return [], [f"{directory} is not a directory (JENKINS_CREDENTIALS_DIR)"]
    errors = []
    if directory.stat().st_mode & stat.S_IXOTH == 0:
        errors.append(f"{directory}: the controller cannot enter it (it reads as \"other\"): chmod 755 {directory}")
    names = sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))
    for name in names:
        path = directory / name
        if name not in allowed:
            errors.append(f"{path}: '{name}' is no credential of any catalog entry; the ids are: {', '.join(allowed)}")
            continue
        if not path.is_file():
            errors.append(f"{path}: not a file")
            continue
        data = path.read_bytes()
        if not data:
            errors.append(f"{path}: empty")
        elif data.endswith(b"\n"):
            errors.append(f"{path}: has a trailing newline, which would be part of the secret; write it with "
                          f"printf '%s' SECRET > {path}")
        if path.stat().st_mode & stat.S_IROTH == 0:
            errors.append(f"{path}: the controller cannot read it (it reads as \"other\"): chmod 644 {path}")
    return names, errors


def render(names):
    if not names:
        return ""
    lines = ["# Rendered by scripts/platform.sh from JENKINS_CREDENTIALS_DIR (scripts/jenkins_credentials.py): the",
             "# repositories' own credentials, one secret text each, read from its file. Do not edit: it is rendered",
             "# again at every `platform.sh up`.",
             "credentials:", "  system:", "    domainCredentials:", "      - credentials:"]
    for name in names:
        lines += ["          - string:",
                  "              scope: GLOBAL",
                  f'              id: "{name}"',
                  f'              description: "{name} (JENKINS_CREDENTIALS_DIR)"',
                  f'              secret: "${{readFile:{MOUNT}/{name}}}"']
    return "\n".join(lines) + "\n"


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        allowed = allowed_ids(catalog.load())
    except catalog.CatalogError as error:
        print(f"jenkins_credentials: {error}", file=sys.stderr)
        return 1
    names, errors = problems_of(pathlib.Path(argv[0]), allowed)
    if errors:
        for error in errors:
            print(f"jenkins_credentials: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(render(names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
