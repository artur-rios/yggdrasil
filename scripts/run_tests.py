#!/usr/bin/env python3
"""The unit suite of scripts/, run in parallel: what `python3 -m unittest discover -s scripts` runs, the same
tests with the same results, in a fraction of the time.

    python3 scripts/run_tests.py [-s <dir>] [-p <pattern>] [-j <workers>]

The suite is single-threaded, and most of its time is a few classes whose tests each start thousands of short
processes (test_machine's MachineRoleTests: about 85% of it). The tests already keep to their own temporary
directories, so they can run side by side. This lists every test the way discover does, cuts the list into
chunks (a class; a class of more than CHUNK tests into pieces of CHUNK, so the slow ones spread over every
worker), and runs the chunks as separate `python3 -m unittest <test ids>` processes, <workers> at a time, the
largest first. A module that cannot be imported is reported as discover reports it.

Workers: -j, else YGG_TEST_WORKERS, else the machine's processors up to MAX_WORKERS (a check shares its
machine with the others). -j 1 runs the chunks one after another.

Prints a line per chunk as it ends, then the output of each chunk that failed, then the totals in unittest's
words (`Ran N tests in Xs`, then `OK` or `FAILED (...)`, with the skipped). Exit 0 when every test passed (or
was skipped), 1 when one failed or a chunk could not run, 2 for a wrong command line.
"""

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest

CHUNK = 10
MAX_WORKERS = 8

# unittest's own last lines: "Ran 12 tests in 1.234s", then "OK", "OK (skipped=2)" or
# "FAILED (failures=1, errors=2, skipped=3)".
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
OUTCOME = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
# The order unittest prints them in.
ORDER = ("failures", "errors", "skipped", "expected failures", "unexpected successes")


def tests(suite):
    """Every test of a suite, in its order (discover's: modules by name, classes and methods sorted)."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests(item)
        else:
            yield item


def chunks(ids, size=CHUNK):
    """The ids cut into chunks: one per class, a class of more than `size` tests in pieces of `size`; the
    largest first, so the long ones start at once and the short ones fill the gaps."""
    classes = {}
    for test_id in ids:
        classes.setdefault(test_id.rsplit(".", 1)[0], []).append(test_id)
    pieces = [tests[start:start + size] for tests in classes.values() for start in range(0, len(tests), size)]
    return sorted(pieces, key=len, reverse=True)


def counts(output):
    """{"run", "failures", "errors", "skipped", "expected failures", "unexpected successes"} from a unittest
    run's output; None when it has no "Ran" line (the run itself broke)."""
    ran = RAN.search(output)
    outcome = OUTCOME.search(output)
    if not ran or not outcome:
        return None
    found = {"run": int(ran.group(1))}
    for part in (outcome.group(2) or "").split(","):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            found[name.replace("_", " ")] = int(value)
    return found


def run_chunk(start_dir, ids):
    """Runs one chunk in its own `python3 -m unittest`; returns (exit code, combined output)."""
    done = subprocess.run([sys.executable, "-m", "unittest", *ids], cwd=start_dir, capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def workers_wanted(asked):
    if asked:
        return asked
    if os.environ.get("YGG_TEST_WORKERS"):
        return int(os.environ["YGG_TEST_WORKERS"])
    return max(1, min(os.cpu_count() or 1, MAX_WORKERS))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run_tests.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("-s", "--start-directory", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("-p", "--pattern", default="test_*.py")
    parser.add_argument("-j", "--workers", type=int)
    args = parser.parse_args(argv)
    if args.workers is not None and args.workers < 1:
        parser.error("-j must be 1 or more")
    start_dir = os.path.abspath(args.start_directory)

    began = time.monotonic()
    # discover imports the test modules with start_dir on sys.path, as `-m unittest discover -s` does.
    sys.path.insert(0, start_dir)
    found_tests = list(tests(unittest.defaultTestLoader.discover(start_dir, pattern=args.pattern)))
    # A module that fails to import becomes a test of unittest.loader that raises its error: run those here,
    # where the loader made them; a child could not find them by id.
    broken = [test for test in found_tests if test.id().startswith("unittest.loader.")]
    ids = [test.id() for test in found_tests if test not in broken]
    totals = {"run": 0}
    failed_outputs = []
    if broken:
        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(unittest.TestSuite(broken))
        totals["run"] += result.testsRun
        totals["errors"] = len(result.errors) + len(result.failures)

    pieces = chunks(ids)
    workers = workers_wanted(args.workers)
    print(f"run_tests: {len(ids)} tests in {len(pieces)} chunks, {workers} at a time", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_chunk, start_dir, piece): piece for piece in pieces}
        for future in concurrent.futures.as_completed(futures):
            piece = futures[future]
            code, output = future.result()
            found = counts(output)
            name = piece[0].rsplit(".", 1)[0] + (f" ({len(piece)})" if len(piece) > 1 else "")
            if found is None or code != 0:
                failed_outputs.append(output)
                print(f"FAIL  {name}", flush=True)
            else:
                print(f"ok    {name}", flush=True)
            if found is None:
                totals["errors"] = totals.get("errors", 0) + 1
                continue
            for key, value in found.items():
                totals[key] = totals.get(key, 0) + value

    for output in failed_outputs:
        print("\n" + "=" * 70 + "\n" + output.rstrip(), flush=True)
    elapsed = time.monotonic() - began
    print("-" * 70)
    print(f"Ran {totals['run']} tests in {elapsed:.3f}s\n")
    extra = ", ".join(f"{key.replace(' ', '_')}={totals[key]}" for key in ORDER if totals.get(key))
    failed = bool(failed_outputs or totals.get("failures") or totals.get("errors")
                  or totals.get("unexpected successes"))
    print(("FAILED" if failed else "OK") + (f" ({extra})" if extra else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
