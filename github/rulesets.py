#!/usr/bin/env python3
"""Moved: the branch rules are applied by scripts/rules.py, for GitHub and Forgejo.

    python3 github/rulesets.py [--dry-run] [<repo>...]

still works: it says so and runs scripts/rules.py with the same arguments. Use that script directly;
this file only keeps old commands (and the documentation that mentions them) from breaking.
"""

import pathlib
import subprocess
import sys

TARGET = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "rules.py"

print("github/rulesets.py moved to scripts/rules.py; running that.", file=sys.stderr, flush=True)
sys.exit(subprocess.run([sys.executable, str(TARGET), *sys.argv[1:]]).returncode)
