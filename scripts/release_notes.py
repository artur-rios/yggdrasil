#!/usr/bin/env python3
"""The notes of a release: what the release's page says changed.

    python3 scripts/release_notes.py <repo-dir> <version> [<previous-tag>]

Prints, the first that exists:

  1. the `## [<version>]` section of the repository's CHANGELOG.md, without its heading line, up to the
     next `## [` (the version may be given with or without a leading v);
  2. `- <title>` for each pull request merged since <previous-tag> (everything reachable from HEAD without
     one), the newest first;
  3. "No changes recorded."

The merged pull requests are the merge commits of `git log --merges <previous-tag>..HEAD`, all parents:
main's first-parent history holds only the release merges (develop's pull requests came in through them),
so a first-parent log would list nothing but the releases. Merges of `release/*` branches are left out
(they are the release itself). The title of a merge is, by the server that made it:

    Forgejo   "Merge pull request '<title>' (#N) from <head> into <base>"   -> <title>
    GitHub    "Merge pull request #N from <owner>/<head>"                   -> the first line of the body
    other     the subject as it is ("Merge branch 'x'", a custom title)

The release merge Jenkins makes has the subject "release: v<version> (#N)" and is left out as well. Exit 0
with the notes on stdout; 1 with "release_notes: <what> failed" on stderr when git or the changelog cannot
be read (an unknown previous tag among them); 2 for a wrong command line.
"""

import pathlib
import re
import subprocess
import sys

NO_CHANGES = "No changes recorded."

FORGEJO_MERGE = re.compile(r"^Merge pull request '(?P<title>.*)' \(#\d+\) from (?P<head>\S+) into \S+$")
GITHUB_MERGE = re.compile(r"^Merge pull request #\d+ from (?P<head>\S+)")
# "Merge branch 'release/1.4.0'", "Merge remote-tracking branch 'origin/release/1.4.0'".
PLAIN_RELEASE_MERGE = re.compile(r"^Merge (?:remote-tracking )?branch '(?:[^'/]+/)?release/")
# What Jenkins titles the merge of a release branch into main (yggdrasilPipeline).
JENKINS_RELEASE_MERGE = re.compile(r"^release: v?\d")

# Separators git is asked to put between the fields and the commits: control characters no subject holds.
FIELD, RECORD = "\x1f", "\x1e"


class NotesError(Exception):
    pass


def changelog_section(repo, version):
    """The text under the `## [<version>]` heading, or None when the file or the section is not there."""
    path = pathlib.Path(repo) / "CHANGELOG.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise NotesError(f"reading {path} failed: {error}") from None
    bare = version[1:] if version.startswith("v") else version
    heading = re.compile(r"^##\s*\[v?" + re.escape(bare) + r"\]")
    section, inside = [], False
    for line in lines:
        if inside:
            if line.startswith("## ["):
                break
            section.append(line)
        elif heading.match(line):
            inside = True
    text = "\n".join(section).strip()
    return text or None


def is_release_merge(subject, head):
    """Whether a merge brought in a release branch: the release itself, not one of its changes."""
    if head is not None:
        # The branch is "release/x" or "<owner>/release/x" (GitHub), "<owner>:release/x" from a fork.
        return re.search(r"(?:^|[/:])release/", head) is not None
    return bool(PLAIN_RELEASE_MERGE.match(subject) or JENKINS_RELEASE_MERGE.match(subject))


def merge_title(subject, body):
    """(title, head branch or None) of a merge commit."""
    forgejo = FORGEJO_MERGE.match(subject)
    if forgejo:
        return forgejo["title"], forgejo["head"]
    github = GITHUB_MERGE.match(subject)
    if github:
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        return first or subject, github["head"]
    return subject, None


def merged_pull_requests(repo, previous_tag):
    # --merges, no --first-parent; the range is empty of nothing but what the tag does not have.
    command = ["git", "-C", str(repo), "log", "--merges", f"--format=%s{FIELD}%b{RECORD}"]
    command.append(f"{previous_tag}..HEAD" if previous_tag else "HEAD")
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise NotesError(f"git log failed: {done.stderr.strip() or 'exit ' + str(done.returncode)}")
    titles = []
    for record in done.stdout.split(RECORD):
        if not record.strip():
            continue
        subject, _, body = record.lstrip("\n").partition(FIELD)
        title, head = merge_title(subject.strip(), body)
        if not is_release_merge(subject.strip(), head):
            titles.append(title)
    return titles


def release_notes(repo, version, previous_tag=None):
    section = changelog_section(repo, version)
    if section:
        return section
    titles = merged_pull_requests(repo, previous_tag)
    if titles:
        return "\n".join(f"- {title}" for title in titles)
    return NO_CHANGES


def main(argv):
    if len(argv) not in (2, 3):
        print("usage: release_notes.py <repo-dir> <version> [<previous-tag>]", file=sys.stderr)
        return 2
    try:
        print(release_notes(argv[0], argv[1], argv[2] if len(argv) == 3 else None))
    except NotesError as error:
        print(f"release_notes: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
