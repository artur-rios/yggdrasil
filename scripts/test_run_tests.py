"""Tests for scripts/run_tests.py, the parallel unit suite runner.

    python3 -m unittest discover -s scripts

Each test writes small test modules into a temporary directory and runs the runner on them, so the runner is
proved against suites whose outcome is known, never against this repository's own.
"""

import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import run_tests

RUNNER = pathlib.Path(__file__).resolve().parent / "run_tests.py"

# A test that ends its process before unittest prints its last lines: the chunk's output has no "Ran" line.
DYING = """
    import os
    import unittest

    class Dying(unittest.TestCase):
        def test_dies(self):
            os._exit(3)
"""

PASSING = """
    import unittest

    class Passing(unittest.TestCase):
        def test_one(self):
            pass

        def test_two(self):
            pass

        @unittest.skip("not here")
        def test_skipped(self):
            pass
"""

MANY = """
    import unittest

    class Many(unittest.TestCase):
        pass

    for number in range(25):
        setattr(Many, f"test_{number:02}", lambda self: None)
"""

FAILING = """
    import unittest

    class Failing(unittest.TestCase):
        def test_fails(self):
            self.assertEqual(1, 2, "the failure's own message")
"""


class RunTestsTests(unittest.TestCase):
    def setUp(self):
        self.suite = pathlib.Path(tempfile.mkdtemp(prefix="run-tests-"))
        self.addCleanup(shutil.rmtree, self.suite, ignore_errors=True)

    def module(self, name, body):
        (self.suite / f"{name}.py").write_text(textwrap.dedent(body))

    def run_runner(self, *argv):
        return subprocess.run([sys.executable, str(RUNNER), "-s", str(self.suite), *argv],
                              capture_output=True, text=True)

    def test_given_passing_and_skipped_tests_when_run_then_it_counts_them_as_unittest_does(self):
        self.module("test_passing", PASSING)
        self.module("test_many", MANY)
        run = self.run_runner("-j", "2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("Ran 28 tests in ", run.stdout)
        self.assertTrue(run.stdout.rstrip().endswith("OK (skipped=1)"), run.stdout)

    def test_given_a_failing_test_when_run_then_it_fails_and_shows_that_tests_output(self):
        self.module("test_passing", PASSING)
        self.module("test_failing", FAILING)
        run = self.run_runner("-j", "2")
        self.assertEqual(run.returncode, 1)
        self.assertIn("FAIL  test_failing.Failing", run.stdout)
        self.assertIn("the failure's own message", run.stdout)
        self.assertTrue(run.stdout.rstrip().endswith("FAILED (failures=1, skipped=1)"), run.stdout)

    def test_given_a_module_that_cannot_be_imported_when_run_then_it_fails_with_the_import_error(self):
        self.module("test_passing", PASSING)
        self.module("test_broken", "import no_such_module_here\n")
        run = self.run_runner("-j", "2")
        self.assertEqual(run.returncode, 1)
        self.assertIn("no_such_module_here", run.stdout + run.stderr)
        self.assertIn("FAILED (", run.stdout)

    def test_given_one_worker_when_run_then_it_gives_the_same_result(self):
        self.module("test_passing", PASSING)
        self.module("test_many", MANY)
        run = self.run_runner("-j", "1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("Ran 28 tests in ", run.stdout)

    def test_given_no_workers_when_asked_then_it_is_a_usage_error(self):
        self.assertEqual(self.run_runner("-j", "0").returncode, 2)

    def test_given_a_class_larger_than_a_chunk_when_cut_then_it_is_split_and_the_largest_go_first(self):
        ids = [f"test_a.Big.test_{n:02}" for n in range(25)] + ["test_b.Small.test_one", "test_b.Small.test_two"]
        pieces = run_tests.chunks(ids, size=10)
        self.assertEqual([len(piece) for piece in pieces], [10, 10, 5, 2])
        self.assertEqual(sorted(test for piece in pieces for test in piece), sorted(ids))

    def test_given_unittests_last_lines_when_read_then_the_counts_are_found(self):
        self.assertEqual(run_tests.counts("...\nRan 12 tests in 0.5s\n\nFAILED (failures=1, errors=2, skipped=3)\n"),
                         {"run": 12, "failures": 1, "errors": 2, "skipped": 3})
        self.assertEqual(run_tests.counts("Ran 1 test in 0.1s\n\nOK\n"), {"run": 1})
        self.assertIsNone(run_tests.counts("Traceback (most recent call last):\n"))

    def test_given_a_chunk_whose_process_dies_when_run_then_it_is_an_error_and_its_output_is_shown(self):
        self.module("test_passing", PASSING)
        self.module("test_dying", DYING)
        run = self.run_runner("-j", "2")
        self.assertEqual(run.returncode, 1)
        self.assertIn("FAIL  test_dying.Dying", run.stdout)
        self.assertTrue(run.stdout.rstrip().endswith("FAILED (errors=1, skipped=1)"), run.stdout)

    def test_given_workers_asked_on_the_command_line_when_counted_then_they_win(self):
        with mock.patch.dict(run_tests.os.environ, {"YGG_TEST_WORKERS": "5"}):
            self.assertEqual(run_tests.workers_wanted(3), 3)

    def test_given_ygg_test_workers_when_counted_then_it_is_the_number(self):
        with mock.patch.dict(run_tests.os.environ, {"YGG_TEST_WORKERS": "5"}):
            self.assertEqual(run_tests.workers_wanted(None), 5)

    def test_given_neither_when_counted_then_it_is_the_processors_up_to_the_maximum_and_at_least_one(self):
        environment = {k: v for k, v in run_tests.os.environ.items() if k != "YGG_TEST_WORKERS"}
        with mock.patch.dict(run_tests.os.environ, environment, clear=True):
            for processors, expected in ((2, 2), (64, run_tests.MAX_WORKERS), (None, 1)):
                cpu_count = mock.patch.object(run_tests.os, "cpu_count", return_value=processors)
                with self.subTest(processors=processors), cpu_count:
                    self.assertEqual(run_tests.workers_wanted(None), expected)


if __name__ == "__main__":
    unittest.main()
