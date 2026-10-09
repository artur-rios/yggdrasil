"""`ygg self-install`: the ygg command on the PATH, as a link to this checkout's scripts/ygg, and its
tab completion. $YGG_BIN_DIR and $YGG_COMPLETION_DIR (default /usr/local/bin and
/etc/bash_completion.d) are for tests and unusual layouts."""

import os
import pathlib
import subprocess
import sys

from . import complete


def _privileged(argv, directory):
    """argv as is when this user can write the directory, else through sudo."""
    if os.geteuid() == 0 or os.access(directory, os.W_OK):
        return argv
    return ["sudo", *argv]


def self_install(ctx, environ=None):
    environ = os.environ if environ is None else environ
    bin_dir = pathlib.Path(environ.get("YGG_BIN_DIR") or "/usr/local/bin")
    completion_dir = pathlib.Path(environ.get("YGG_COMPLETION_DIR") or "/etc/bash_completion.d")
    target, link = ctx.scripts / "ygg", bin_dir / "ygg"
    try:
        if link.is_symlink() and link.resolve() == target.resolve():
            print(f"{link} already runs {target}")
        elif link.exists() and not link.is_symlink():
            print(f"ygg: {link} is a file, not a link to a checkout: remove it first", file=sys.stderr)
            return 1
        else:
            subprocess.run(_privileged(["ln", "-sfn", str(target), str(link)], bin_dir), check=True)
            print(f"Linked {link} -> {target}")
        file = completion_dir / "ygg"
        try:
            current = file.read_text()
        except OSError:
            current = None
        if current == complete.script():
            print(f"{file} is up to date")
        else:
            if not completion_dir.is_dir():
                subprocess.run(_privileged(["mkdir", "-p", str(completion_dir)], completion_dir.parent), check=True)
            subprocess.run(_privileged(["tee", str(file)], completion_dir), input=complete.script(), text=True,
                           stdout=subprocess.DEVNULL, check=True)
            print(f"Wrote {file} (new shells complete ygg; in this one: source {file})")
    except subprocess.CalledProcessError as error:
        print(f"ygg: {' '.join(error.cmd)} failed (exit {error.returncode})", file=sys.stderr)
        return 1
    return 0
