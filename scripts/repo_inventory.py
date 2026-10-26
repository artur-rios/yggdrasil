#!/usr/bin/env python3
"""The per-repository table of the move to Forgejo and Jenkins: what each repository is, what its GitHub
workflows do today, and what happens to each job.

    python3 scripts/repo_inventory.py <repos-dir> [--github-owner artur-rios] [--output FILE] [--no-github]

It reads every git clone directly under <repos-dir> (its `.github/workflows`, `.github/dependabot.yml`,
`vercel.json` and `git status`) and, when a GitHub token is to be had (GH_TOKEN, else `gh auth token`, as
rules.py finds it), the owner's repository list through migrate.github_repositories: forks, archived
repositories and the repositories that have no clone here. Without a token it works from the clones alone
and marks the GitHub-only facts unknown. It only reads: nothing is written to GitHub, and nothing in the
clones (git is told not to take its optional locks, so even `git status` leaves them untouched).

It prints a Markdown table, one row per repository:

  Kind         from the spec's table (KINDS below), the owner's decision of what each repository is.
               Observation (workflows, vercel.json, a Pages deploy step, GitHub's fork and archived) is
               checked against it second, and a disagreement is written in the row's Notes.
  Jobs         every job of every workflow as `<workflow>/<job> on <runs-on> <verdict>`, the verdict being
               decision 3 of the plan (classify_job): `→ check <job>` for a job on ubuntu-* (`→ release
               (release.build)` in a release*.yml workflow, `→ publish (publish.nuget)` in a
               publish-package*.yml one: the release agent's steps), `stays
               (Actions)` for windows-* and macos-*, `goes (Pages|Vercel|branch-policy)`, or
               `unknown — check by hand` for a runner that cannot be resolved; a call to a reusable workflow of the
               same repository is `calls <file> (judged there)`. `dependabot.yml` goes too.
  Secrets      the `secrets.*` the workflows use.
  Pages / Vercel, Dependabot, Branch model (`release` where a branch-policy.yml workflow exists, else
  `basic`) and the clone's state (clean, dirty, missing).

--output FILE writes docs/migration-checklist.md: the table with a title and a legend, and two empty
columns, Prepared branch and Moved, that the move fills by hand. Run again over an existing file, it keeps
what those two columns hold, and the sections written by hand after the table (each starts with a `## `
heading: the check names and credentials each job and secret became), so regenerating the facts never
loses the progress or the decisions.

Run it where the clones were made (on this machine, with Windows Python and Windows git over D:/Repositories):
git in WSL reads the same files through a CRLF-converting checkout it does not know about and calls nearly
every clone dirty, and is far slower on /mnt/d.

Exit status: 0 done, 2 usage (<repos-dir> is not a directory).
"""

import argparse
import dataclasses
import fnmatch
import os
import pathlib
import re
import subprocess
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import migrate  # noqa: E402  (scripts/migrate.py: github_repositories)
import rules  # noqa: E402  (scripts/rules.py: the GitHub client and its token discovery)

DEFAULT_OWNER = "artur-rios"

# --- The spec's table ---------------------------------------------------------------------------------

# "Every repository" in docs/superpowers/specs/2026-10-22-self-hosted-git-and-ci-design.md: the one place the
# kinds are written. A repository can be of more than one kind; one in none of them is "code only" (the
# spec's "every other repository"). The order here is the order kinds are shown in.
#
# The 24 sites are the 19 GitHub Pages sites and the 5 Vercel projects of the spec's "Sites" section: the
# repositories that are a site (the three docs repositories, personal-blog, personal-page,
# tech-craftsmen-company-page, personal-lab, under-construction-template, github-readme-stats) and the ones
# that carry a docs site besides what they are (orpheus, heimdall-api, dotnet-libraries and the 12 libraries).
KINDS = {
    "application": ("heimdall-api", "heimdall-ui", "fortuna-api", "fortuna-ui", "chimera"),
    "library": ("dotnet-configuration", "dotnet-data", "dotnet-extensions", "dotnet-jwt", "dotnet-logging",
                "dotnet-mediator", "dotnet-messaging", "dotnet-output", "dotnet-test-util", "dotnet-util",
                "dotnet-validation", "dotnet-webapi-util"),
    "ci": ("alexandria-api", "alexandria-ui", "cerberus-api", "cerberus-ui", "maestro", "orpheus", "personal-lab",
           "ai-coding-skills", "coding-challenge-library", "dotnet-libraries", "reveal.js",
           "yggdrasil"),  # yggdrasil: the spec's Catalog section makes it a `repositories` entry (owner's ruling)
    "site": ("tech-craftsmen-company-page", "personal-page", "personal-blog", "personal-lab",
             "under-construction-template", "github-readme-stats",
             "heimdall-docs", "fortuna-docs", "alexandria-docs", "orpheus", "heimdall-api", "dotnet-libraries",
             "dotnet-configuration", "dotnet-data", "dotnet-extensions", "dotnet-jwt", "dotnet-logging",
             "dotnet-mediator", "dotnet-messaging", "dotnet-output", "dotnet-test-util", "dotnet-util",
             "dotnet-validation", "dotnet-webapi-util"),
    "archived": ("dotnet-common", "eocp-ecommerce-system-api", "eocp-ecommerce-system-email-templates",
                 "eocp-ecommerce-system-ui"),
    "fork": ("file-tree-generator", "github-readme-stats", "gui-challenges", "hugo-theme-nuno",
             "hugo-theme-re-terminal", "hugo-theme-rusty-typewriter", "landing-page", "reveal.js",
             "under-construction-template"),
}
CODE_ONLY = "code only"

# The 5 Vercel projects of the spec's Sites section. Vercel's git integration needs no vercel.json, so a site of
# these with none is still a Vercel site: the Kind column says `site (Vercel)`, and the note stays as information.
VERCEL_SITES = ("tech-craftsmen-company-page", "personal-page", "personal-blog", "under-construction-template",
                "github-readme-stats")

# Kinds whose repositories have GitHub workflows today (a site's evidence is a Pages step or a vercel.json).
KINDS_WITH_WORKFLOWS = ("application", "library", "ci")


def spec_kinds(name):
    return [kind for kind, names in KINDS.items() if name in names] or [CODE_ONLY]


def spec_names():
    return {name for names in KINDS.values() for name in names}


# --- Decision 3: which jobs move, stay or go ----------------------------------------------------------

CHECK = "check"
# An ubuntu job of a release*.yml or publish-package*.yml workflow is not a check: plan 3 runs those steps on
# the release agent (release.build, publish.nuget).
RELEASE = "release (release.build)"
PUBLISH = "publish (publish.nuget)"
STAYS = "stays (Actions)"
UNKNOWN = "unknown — check by hand"

# What marks a Pages deploy and a Vercel deploy. Matched on the action's name, without its version.
PAGES_ACTIONS = ("actions/deploy-pages", "actions/upload-pages-artifact", "peaceiris/actions-gh-pages")
VERCEL_ACTIONS = ("amondnet/vercel-action", "vercel/vercel-action")
BRANCH_POLICY_WORKFLOWS = ("branch-policy.yml", "branch-policy.yaml")


def _action(step):
    """The action a step uses, lowercase and without its @version ('' for a `run` step)."""
    uses = step.get("uses") if isinstance(step, dict) else None
    return str(uses).split("@")[0].strip().lower() if uses else ""


def uses_pages(steps):
    return any(_action(step) in PAGES_ACTIONS for step in steps)


def uses_vercel(steps):
    for step in steps:
        if not isinstance(step, dict):
            continue
        if _action(step) in VERCEL_ACTIONS or _action(step).rsplit("/", 1)[-1] == "vercel":
            return True
        # `npx vercel deploy`, `vercel pull`: the word on its own, not `vercel.app` in a URL or a path.
        if re.search(r"(?<![\w./-])vercel(?![\w.-])", str(step.get("run") or ""), re.IGNORECASE):
            return True
    return False


def _runner_verdict(labels):
    """The verdict of one runner: the operating system of its labels. Anything that is not plainly Linux,
    Windows or macOS (self-hosted, a group, an expression) is for a person to look at."""
    labels = [str(label).strip().lower() for label in (labels if isinstance(labels, (list, tuple)) else [labels])
              if label is not None]
    if not labels or any("${{" in label or not label for label in labels):
        return UNKNOWN
    if any(label.startswith(("windows", "macos")) for label in labels):
        return STAYS
    if all(label.startswith("ubuntu") for label in labels):
        return CHECK
    return UNKNOWN


def _release_step(workflow):
    """RELEASE or PUBLISH for a release*.yml or publish-package*.yml workflow, else CHECK."""
    name = str(workflow).lower()
    if fnmatch.fnmatch(name, "release*.y*ml"):
        return RELEASE
    if fnmatch.fnmatch(name, "publish-package*.y*ml"):
        return PUBLISH
    return CHECK


def classify_job(runs_on, workflow, steps):
    """What becomes of a job (decision 3 of the plan), as a pure function.

    `runs_on` is the job's runner label (a list of labels is read by its operating system), `workflow` the
    workflow's file name and `steps` its steps (for a Pages or Vercel workflow, the steps of all its jobs: the
    workflow goes whole, its build job with its deploy job). Returns one of CHECK, RELEASE, PUBLISH, STAYS,
    UNKNOWN or `goes (Pages)`, `goes (Vercel)`, `goes (branch-policy)`.

    What goes is decided before the runner is looked at: a Pages or Vercel workflow, whatever it runs on, and
    branch-policy.yml leave GitHub for good. Of the rest, ubuntu-* jobs become checks (RELEASE or PUBLISH in a
    release*.yml or publish-package*.yml workflow: the release agent's steps), windows-* and macos-* jobs stay
    on GitHub Actions. A runner this cannot resolve is never guessed."""
    if str(workflow).lower() in BRANCH_POLICY_WORKFLOWS:
        return "goes (branch-policy)"
    if uses_pages(steps):
        return "goes (Pages)"
    if uses_vercel(steps):
        return "goes (Vercel)"
    verdict = _runner_verdict(runs_on)
    return _release_step(workflow) if verdict == CHECK else verdict


# --- Reading a clone ----------------------------------------------------------------------------------

@dataclasses.dataclass
class Job:
    workflow: str
    job: str            # the job's id; "" for a workflow that could not be read
    runs_on: str
    verdict: str

    def text(self):
        if not self.job:
            return f"{self.workflow}: {self.verdict}"
        if self.verdict.startswith("calls "):
            return f"{self.workflow}/{self.job} {self.verdict}"
        if self.verdict == CHECK:
            verdict = f"→ check {self.job}"
        elif self.verdict in (RELEASE, PUBLISH):
            verdict = f"→ {self.verdict}"
        else:
            verdict = self.verdict
        return f"{self.workflow}/{self.job} on {self.runs_on} {verdict}"


@dataclasses.dataclass
class Row:
    name: str
    kinds: list = dataclasses.field(default_factory=list)
    github: str = "unknown"     # plain | fork | archived | fork, archived | not on GitHub | unknown
    state: str = "missing"      # clean | dirty | missing | unreadable
    branch: str = ""
    branch_model: str = "basic"
    jobs: list = dataclasses.field(default_factory=list)
    secrets: list = dataclasses.field(default_factory=list)
    pages: bool = False
    vercel: bool = False
    dependabot: list = dataclasses.field(default_factory=list)
    workflows: int = 0
    notes: list = dataclasses.field(default_factory=list)


_MATRIX_KEY = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z_][\w-]*)\s*\}\}$")
_SECRET_DOT = re.compile(r"\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)")
_SECRET_INDEX = re.compile(r"\bsecrets\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")
_COMMENT_LINE = re.compile(r"(?m)^[ \t]*#.*$")


def _display(runs_on):
    if isinstance(runs_on, list):
        return ", ".join(str(label) for label in runs_on)
    return str(runs_on)


def _expand_runs_on(job):
    """The runners a job runs on, one label (or one list of labels) each. `runs-on: ${{ matrix.os }}` is
    expanded from the job's `strategy.matrix`, both the `os: [..]` list and the `include:` entries; every
    other expression is kept as it is, for classify_job to call unknown."""
    runs_on = job.get("runs-on")
    if runs_on is None:
        return [f"reusable workflow {job['uses']}" if "uses" in job else "no runs-on"]
    match = _MATRIX_KEY.match(runs_on) if isinstance(runs_on, str) else None
    if not match:
        return [runs_on]
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return [runs_on]
    key, found = match.group(1), []
    listed = matrix.get(key)
    for value in (listed if isinstance(listed, list) else [listed]):
        if value is not None:
            found.append(value)
    for entry in matrix.get("include") or []:
        if isinstance(entry, dict) and key in entry:
            found.append(entry[key])
    unique = []
    for value in found:
        if value not in unique:
            unique.append(value)
    return unique or [runs_on]


def read_workflow(path):
    """(jobs, secrets, steps) of one workflow file: its Jobs, the secrets it names and all its steps.
    A file that cannot be read or parsed gives one Job saying so."""
    name = path.name
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return [Job(name, "", "", "unreadable — check by hand")], [], []
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        return [Job(name, "", "", "unreadable — check by hand")], [], []
    # Secrets are read from the text without its comment lines, which may name one that is long gone.
    code = _COMMENT_LINE.sub("", text)
    secrets = set(_SECRET_DOT.findall(code)) | set(_SECRET_INDEX.findall(code))
    definitions = [(job_id, job) for job_id, job in data["jobs"].items() if isinstance(job, dict)]
    steps = [step for _, job in definitions for step in (job.get("steps") or []) if isinstance(step, dict)]
    jobs = []
    for job_id, job in definitions:
        called = str(job.get("uses") or "")
        if "runs-on" not in job and called.startswith("./"):
            # A call to a reusable workflow of the same repository: its own jobs are listed under it, with
            # their own runners, so this one has no verdict of its own.
            jobs.append(Job(name, str(job_id), "", f"calls {called.rsplit('/', 1)[-1]} (judged there)"))
            continue
        for runs_on in _expand_runs_on(job):
            jobs.append(Job(name, str(job_id), _display(runs_on), classify_job(runs_on, name, steps)))
    return jobs, sorted(secrets), steps


def dependabot_ecosystems(path):
    """The package ecosystems a dependabot.yml updates, in file order (an unreadable file gives none)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    found = []
    for update in (data.get("updates") if isinstance(data, dict) else None) or []:
        ecosystem = update.get("package-ecosystem") if isinstance(update, dict) else None
        if ecosystem and ecosystem not in found:
            found.append(str(ecosystem))
    return found


def clone_state(path, ignore=None):
    """('clean' | 'dirty' | 'unreadable', current branch). Untracked files make a clone dirty. Git is told not to
    take its optional locks, so that reading the status does not even refresh the index. `ignore` is a file
    inside the clone that does not count: the checklist this run writes into yggdrasil's own docs/, which
    would otherwise make yggdrasil's row say dirty because of the very file being written."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    command = ["git", "-C", str(path), "status", "--porcelain", "--branch"]
    if ignore is not None:
        try:
            command += ["--", ".", ":(exclude)" + pathlib.Path(ignore).resolve().relative_to(
                pathlib.Path(path).resolve()).as_posix()]
        except ValueError:
            pass  # not inside this clone
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env)
    except OSError:
        return "unreadable", ""
    if done.returncode != 0:
        return "unreadable", ""
    lines = done.stdout.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    branch = head.split("...")[0]
    if branch.startswith("No commits yet on "):
        branch = branch[len("No commits yet on "):]
    return ("dirty" if len(lines) > 1 else "clean"), branch


def read_clone(path, name, ignore=None):
    """The Row of one clone, without GitHub's facts or the notes."""
    row = Row(name=name, kinds=spec_kinds(name))
    row.state, row.branch = clone_state(path, ignore)
    secrets, steps_seen = set(), []
    workflows = path / ".github" / "workflows"
    files = sorted(p for p in workflows.glob("*") if p.suffix in (".yml", ".yaml") and p.is_file()) \
        if workflows.is_dir() else []
    for file in files:
        jobs, found, steps = read_workflow(file)
        row.jobs += jobs
        secrets |= set(found)
        steps_seen += steps
    row.workflows = len(files)
    row.secrets = sorted(secrets)
    row.pages = uses_pages(steps_seen)
    row.vercel = (path / "vercel.json").is_file() or uses_vercel(steps_seen)
    row.branch_model = "release" if any(f.name in BRANCH_POLICY_WORKFLOWS for f in files) else "basic"
    for dependabot in (path / ".github" / "dependabot.yml", path / ".github" / "dependabot.yaml"):
        if dependabot.is_file():
            row.dependabot = dependabot_ecosystems(dependabot)
            break
    return row


# --- Putting the spec and the observation side by side ------------------------------------------------

def _github_label(entry):
    if entry is None:
        return "not on GitHub"
    flags = [flag for flag in ("fork", "archived") if entry.get(flag)]
    return ", ".join(flags) or "plain"


def disagreements(row, entry, github_known):
    """The notes of a row: where what the clone and GitHub show differs from the spec's table. A repository
    with no clone is not observed, and with no GitHub listing nothing is said about forks or archives."""
    notes = []
    kinds = set(row.kinds)
    if github_known:
        if entry is None:
            notes.append("not on GitHub" + ("" if row.state != "missing" else " (the spec's table names it)"))
        else:
            for flag in ("fork", "archived"):
                if bool(entry.get(flag)) and flag not in kinds:
                    notes.append(f"GitHub says {flag}; the spec's table does not")
                if not entry.get(flag) and flag in kinds:
                    notes.append(f"the spec's table says {flag}; GitHub says it is not")
    if row.state == "missing":
        return notes
    if "site" in kinds and not (row.pages or row.vercel):
        if row.name in VERCEL_SITES:
            notes.append("the spec's table says site (Vercel); no vercel.json found (information only: Vercel's "
                         "git integration needs none)")
        else:
            notes.append("the spec's table says site; no Pages deploy and no vercel.json found")
    if "site" not in kinds and (row.pages or row.vercel):
        found = " and ".join(name for name, on in (("Pages", row.pages), ("Vercel", row.vercel)) if on)
        notes.append(f"{found} found; the spec's table has no site here")
    for kind in KINDS_WITH_WORKFLOWS:
        if kind in kinds and not row.workflows:
            notes.append(f"the spec's table says {kind}; no workflows found")
            break
    if kinds == {CODE_ONLY} and row.workflows:
        notes.append(f"the spec's table says code only; {row.workflows} workflow"
                     f"{'' if row.workflows == 1 else 's'} found")
    return notes


def _linked_worktree(path):
    """True for a `git worktree add` checkout (its `.git` is a file that points into another clone's `.git/worktrees`):
    a second working copy of a repository that is already a row, such as yggdrasil's own move branch."""
    marker = pathlib.Path(path) / ".git"
    try:
        return marker.is_file() and "/worktrees/" in marker.read_text(encoding="utf-8").replace("\\", "/")
    except (OSError, UnicodeDecodeError):
        return False


def inventory(repos_dir, github=None, ignore=None):
    """The Rows, sorted by name: every git clone directly under repos_dir, and (with GitHub's listing, the
    list of dicts migrate.github_repositories returns) the repositories that have no clone, plus the ones the
    spec's table names that GitHub does not list. Without `github`, only clones are rows. `ignore`: see clone_state."""
    repos_dir = pathlib.Path(repos_dir)
    clones = {p.name: p for p in sorted(repos_dir.iterdir()) if p.is_dir() and (p / ".git").exists()
              and not _linked_worktree(p)}
    listed = {entry["name"]: entry for entry in github} if github is not None else None
    names = set(clones) | (set(listed) | spec_names() if listed is not None else set())
    rows = []
    for name in sorted(names):
        row = read_clone(clones[name], name, ignore) if name in clones else Row(name=name, kinds=spec_kinds(name))
        entry = listed.get(name) if listed is not None else None
        row.github = "unknown" if listed is None else _github_label(entry)
        row.notes = disagreements(row, entry, listed is not None)
        rows.append(row)
    return rows


# --- The table ----------------------------------------------------------------------------------------

COLUMNS = ("Repository", "Kind", "GitHub", "Jobs", "Secrets", "Pages / Vercel", "Dependabot", "Branch model",
           "Clone", "Notes", "Prepared branch", "Moved")


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def _pages_vercel(row):
    return ", ".join(name for name, on in (("Pages", row.pages), ("Vercel", row.vercel)) if on) or "none"


def _kinds_label(row):
    """The Kind column: the spec's kinds, with `site (Vercel)` for the spec's Vercel sites."""
    return ", ".join("site (Vercel)" if kind == "site" and row.name in VERCEL_SITES else kind for kind in row.kinds)


def _cells(row, kept):
    jobs = [job.text() for job in row.jobs]
    dependabot = f"{', '.join(row.dependabot)} → goes (dependabot)" if row.dependabot else ""
    clone = f"{row.state} ({row.branch})" if row.branch else row.state
    prepared, moved = kept.get(row.name, ("", ""))
    return [_cell(row.name), _cell(_kinds_label(row)), _cell(row.github), "<br>".join(_cell(j) for j in jobs),
            ", ".join(f"`{s}`" for s in row.secrets), _pages_vercel(row), _cell(dependabot), row.branch_model,
            _cell(clone), "<br>".join(_cell(n) for n in row.notes), prepared, moved]


def render_table(rows, carried=None):
    """The Markdown table. `carried` ({name: (prepared, moved)}, already escaped) fills the last two columns."""
    kept = carried or {}
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "|".join("---" for _ in COLUMNS) + "|"]
    lines += ["| " + " | ".join(_cells(row, kept)) + " |" for row in rows]
    return "\n".join(lines)


LEGEND = """\
Generated by `python3 scripts/repo_inventory.py <repos-dir> --output docs/migration-checklist.md` from the
GitHub workflows of the local clones and, with a GitHub token, the owner's repository list. Regenerating keeps
the *Prepared branch* and *Moved* columns, and the sections after the table. Nothing here is written by hand
except those.

**Run the tool with Windows Python and git** (`python scripts/repo_inventory.py D:/Repositories --output
docs/migration-checklist.md` from Windows). In WSL, git sees the clones' CRLF line endings as changes and shows
nearly every clone dirty; Windows git does not.

- **Kind** is the spec's table ("Every repository"; `site (Vercel)` marks the five Vercel projects). Where what the
  clone or GitHub shows differs, *Notes* says so; the Kind stays the spec's.
- **Jobs** are `<workflow>/<job> on <runs-on> <verdict>`: `→ check <job>` (a job on `ubuntu-*`: it leaves
  `.github/workflows` and becomes a check), `→ release (release.build)` (a job on `ubuntu-*` in a `release*.yml`
  workflow) and `→ publish (publish.nuget)` (in a `publish-package*.yml` workflow), which plan 3 runs on the
  release agent rather than as checks, `stays (Actions)` (`windows-*` and `macos-*`),
  `goes (Pages)`, `goes (Vercel)` or `goes (branch-policy)` (the workflow is deleted), or `unknown — check by
  hand` (a runner that is an expression this cannot resolve). A `${{ matrix.os }}` is expanded into one line per
  operating system. `dependabot.yml` goes too; Renovate replaces it.
- **Branch model** is `release` where a `branch-policy.yml` workflow exists today, else `basic`.
- **Clone** is the state of `D:\\Repositories\\<repo>` when this was generated: `clean`, `dirty` (the move skips
  it) or `missing` (cloned from GitHub first, read-only).
- **Prepared branch** is filled when a repository's `feature/move-to-forgejo` branch is ready (never pushed);
  **Moved** is filled by the owner after the cutover."""


def _summary(rows):
    kinds = {}
    for row in rows:
        for kind in row.kinds:
            kinds[kind] = kinds.get(kind, 0) + 1
    order = [*KINDS, CODE_ONLY]
    by_kind = ", ".join(f"{kinds[kind]} {kind}" for kind in order if kind in kinds)
    states = {}
    for row in rows:
        states[row.state] = states.get(row.state, 0) + 1
    by_state = ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
    return f"{len(rows)} repositories. By kind (a repository can be of several): {by_kind}. Clones: {by_state}."


def document(rows, carried=None, sections=""):
    """The whole checklist: title, legend, summary and table, then `sections` (carried_sections) as they were."""
    text = f"# Migration checklist\n\n{LEGEND}\n\n{_summary(rows)}\n\n{render_table(rows, carried)}\n"
    return f"{text}\n{sections}" if sections else text


_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def carried_columns(path):
    """{repository: (prepared branch, moved)} read from an earlier migration checklist, ({} when there is
    none)."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    carried = {}
    for line in text.splitlines():
        if not line.startswith("| ") or line.startswith("| Repository |") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in _CELL_SPLIT.split(line)[1:-1]]
        if len(cells) == len(COLUMNS):
            carried[cells[0]] = (cells[-2], cells[-1])
    return carried


def carried_sections(path):
    """The sections written by hand after the table of an earlier migration checklist: everything from its first
    `## ` heading to the end, as it is ("" when there is no file or no such heading). The generated part has no
    `## ` heading of its own, so nothing generated is carried twice."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^## ", text, re.MULTILINE)
    return text[match.start():] if match else ""


# --- The command --------------------------------------------------------------------------------------

def fetch_github(owner):
    """The owner's repositories as migrate.github_repositories lists them, or None (with the reason on stderr)
    when there is no token or GitHub does not answer: the inventory then works from the clones alone."""
    token = rules.find_github_token()
    if not token:
        print("no GitHub token (set GH_TOKEN, or log in with `gh auth login`): working from the clones alone; "
              "forks, archives and missing clones are unknown", file=sys.stderr)
        return None
    gh = rules.Api(rules.GITHUB_API, token, "Bearer", rules.GITHUB_HEADERS)
    try:
        return migrate.github_repositories(gh, owner)
    except (rules.ApiError, migrate.MigrateError, migrate.UsageError) as error:
        print(f"could not list {owner}'s repositories on GitHub ({error}): working from the clones alone",
              file=sys.stderr)
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Per-repository table of the move to Forgejo.")
    parser.add_argument("repos_dir", help="the directory that holds the clones")
    parser.add_argument("--github-owner", default=DEFAULT_OWNER)
    parser.add_argument("--output", help="write the checklist document here (docs/migration-checklist.md)")
    parser.add_argument("--no-github", action="store_true", help="do not ask GitHub: work from the clones alone")
    args = parser.parse_args(argv)
    repos_dir = pathlib.Path(args.repos_dir)
    if not repos_dir.is_dir():
        print(f"{repos_dir}: not a directory", file=sys.stderr)
        return 2
    github = None if args.no_github else fetch_github(args.github_owner)
    rows = inventory(repos_dir, github, ignore=args.output)
    carried = carried_columns(args.output) if args.output else {}
    sections = carried_sections(args.output) if args.output else ""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # not a real stdout (a test's StringIO)
    print(render_table(rows, carried))
    if args.output:
        pathlib.Path(args.output).write_text(document(rows, carried, sections), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
