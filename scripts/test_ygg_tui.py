"""Full-screen tests of `ygg` in a pseudo-terminal: the menu draws, filters, runs a command and
quits; `ygg.py prompt` answers on file descriptor 3. Linux only."""

import fcntl
import os
import pathlib
import pty
import re
import select
import struct
import sys
import termios
import time
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b(\[[0-9;?]*[A-Za-z]|[()][A-Z0-9]|[=>]|O[A-Z])")


def spawn(arguments, fd3=None, rows=30, cols=100):
    env = dict(os.environ, TERM="xterm-256color", ESCDELAY="25", LANG="C.UTF-8")
    env.pop("YGG_PLAIN", None)
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            if fd3 is not None:
                os.dup2(fd3, 3)
            os.execve(sys.executable, [sys.executable, str(SCRIPTS / "ygg.py"), *arguments], env)
        finally:
            os._exit(127)
    return pid, fd


def read_until(fd, needle, timeout=15):
    seen, deadline = "", time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            seen += chunk.decode("utf-8", "replace")
            if needle in ANSI.sub("", seen):
                return ANSI.sub("", seen)
    raise AssertionError(f"{needle!r} never appeared; saw: {ANSI.sub('', seen)[-500:]!r}")


def wait(pid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.05)
    os.kill(pid, 9)
    raise AssertionError("still running")


@unittest.skipUnless(sys.platform.startswith("linux"), "needs a Linux pty")
class FullScreenTests(unittest.TestCase):
    def test_the_menu_draws_and_q_quits(self):
        pid, fd = spawn([])
        read_until(fd, "Applications")
        os.write(fd, b"q")
        self.assertEqual(wait(pid), 0)

    def test_filter_enter_and_a_command_run_in_the_terminal(self):
        pid, fd = spawn([])
        read_until(fd, "Applications")
        os.write(fd, b"Help\r")
        read_until(fd, "completion")
        os.write(fd, b"version\r")
        read_until(fd, "Enter to return")
        os.write(fd, b"\r")
        read_until(fd, "Help and version")  # back in the group, still filtered to `version`
        os.write(fd, b"\x1b")  # clears the filter
        time.sleep(0.2)
        os.write(fd, b"q")
        self.assertEqual(wait(pid), 0)


@unittest.skipUnless(sys.platform.startswith("linux"), "needs a Linux pty")
class PromptTests(unittest.TestCase):
    def prompt(self, arguments, keys):
        read_end, write_end = os.pipe()
        pid, fd = spawn(["prompt", *arguments], fd3=write_end)
        os.close(write_end)
        read_until(fd, arguments[1])
        os.write(fd, keys)
        status = wait(pid)
        answer = os.read(read_end, 4096).decode()
        os.close(read_end)
        return status, answer

    def test_choose_answers_on_fd_3(self):
        self.assertEqual(self.prompt(["choose", "Which one?", "alpha", "beta"], b"beta\r"), (0, "beta"))

    def test_esc_cancels_with_status_1(self):
        self.assertEqual(self.prompt(["choose", "Which one?", "alpha", "beta"], b"\x1b")[0], 1)

    def test_ask_and_confirm(self):
        self.assertEqual(self.prompt(["ask", "Name?", "--default", "x"], b"\x15abc\r"), (0, "abc"))
        self.assertEqual(self.prompt(["confirm", "Sure?"], b"y"), (0, "y"))
        self.assertEqual(self.prompt(["confirm", "Sure?", "--yes"], b"\r"), (0, "y"))
        self.assertEqual(self.prompt(["confirm", "Sure?"], b"\x1b"), (0, "n"))


if __name__ == "__main__":
    unittest.main()
