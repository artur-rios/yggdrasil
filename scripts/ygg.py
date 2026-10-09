#!/usr/bin/env python3
"""ygg: yggdrasil's command line app for a host. `ygg help` lists the commands; docs/cli.md is the
guide. The code is in scripts/yggcli/."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yggcli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
