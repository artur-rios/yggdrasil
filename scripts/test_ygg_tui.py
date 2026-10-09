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


def spawn(arguments, fd3=None, rows=30, cols=100, env=None, term="xterm-256color"):
    env = dict(os.environ, **(env or {}), TERM=term, ESCDELAY="25", LANG="C.UTF-8")
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


class Screen:
    """What a terminal shows, for the few controls an inline prompt may send: text, CR, LF, cursor
    up (ESC [ n A), clear to the end of the screen or line (ESC [ J, ESC [ K), colours and the
    cursor's visibility. Anything else (the alternate screen, clearing the whole screen, moving
    the cursor home) is recorded in `unknown`: an inline prompt must not need it. Lines don't
    scroll away: the model keeps them all."""

    CONTROL = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b(.)|(.)", re.S)

    def __init__(self, cols):
        self.cols, self.lines, self.row, self.col, self.unknown = cols, [""], 0, 0, []

    def feed(self, text):
        for match in self.CONTROL.finditer(text):
            params, final, other, char = match.groups()
            if char is not None:
                self.put(char)
            elif other is not None:
                self.unknown.append("ESC " + other)
            elif final == "A":
                self.row = max(0, self.row - int(params or 1))
            elif final == "J" and params in ("", "0"):
                self.lines[self.row] = self.lines[self.row][:self.col]
                del self.lines[self.row + 1:]
            elif final == "K" and params in ("", "0"):
                self.lines[self.row] = self.lines[self.row][:self.col]
            elif final == "m" or (final in "hl" and params == "?25"):
                pass
            else:
                self.unknown.append(f"ESC [{params}{final}")

    def put(self, char):
        if char == "\r":
            self.col = 0
        elif char == "\n":
            self.row += 1
            while len(self.lines) <= self.row:
                self.lines.append("")
        elif char.isprintable():
            if self.col >= self.cols:
                self.put("\r")
                self.put("\n")
            line = self.lines[self.row].ljust(self.col)
            self.lines[self.row] = line[:self.col] + char + line[self.col + 1:]
            self.col += 1

    def text(self):
        return "\n".join(line.rstrip() for line in self.lines).strip("\n")


def read_screen(fd, screen, needle, timeout=15):
    """Feeds what the program writes to `screen` until it shows `needle`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            screen.feed(chunk.decode("utf-8", "replace"))
            if needle in screen.text():
                return screen.text()
    raise AssertionError(f"{needle!r} never appeared; the screen: {screen.text()[-800:]!r}")


def stop(pid):
    """Kills what a failed test left running."""
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


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

    def test_ctrl_c_at_the_pause_goes_back_to_the_menu(self):
        pid, fd = spawn([])
        read_until(fd, "Applications")
        os.write(fd, b"Help\r")
        read_until(fd, "completion")
        os.write(fd, b"version\r")
        read_until(fd, "Enter to return")
        os.write(fd, b"\x03")
        read_until(fd, "Help and version")
        os.write(fd, b"\x1b")
        time.sleep(0.2)
        os.write(fd, b"q")
        self.assertEqual(wait(pid), 0)



    def test_a_terminal_without_terminfo_gets_the_numbered_menu(self):
        pid, fd = spawn([], term="xterm-no-such-terminal")
        self.addCleanup(stop, pid)
        seen = read_until(fd, "0) Back")
        self.assertIn("== yggdrasil ==", seen)
        os.write(fd, b"q\n")
        self.assertEqual(wait(pid), 0)

@unittest.skipUnless(sys.platform.startswith("linux"), "needs a Linux pty")
class ConfigTests(unittest.TestCase):
    """`ygg config` in a terminal with a store opens the menu, but only for what is on this host."""

    def setUp(self):
        import shutil
        import tempfile
        sys.path.insert(0, str(SCRIPTS))
        from yggcli import context
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ygg-config-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        v = context.vars_module()
        v.init(self.dir, confirm=lambda prompt: "saved")
        with v.Store.open(self.dir) as store:
            store.set("platform", "ENVIRONMENTS", "development,production", None, "set")
        self.env = {"YGG_SECRETS_DIR": str(self.dir)}

    def refused(self, arguments, message):
        pid, fd = spawn(["config", *arguments], env=self.env)
        self.addCleanup(stop, pid)
        seen = read_until(fd, message)
        self.assertEqual(wait(pid), 1)
        self.assertNotIn("Traceback", seen)

    def test_an_unknown_application_is_refused(self):
        self.refused(["nope"], "'nope' is not an application")

    def test_an_environment_not_on_this_host_is_refused(self):
        self.refused(["heimdall-api", "nowhere"], "'nowhere' is not an environment of this host")

    def test_a_long_confirmation_is_shown_in_full(self):
        pid, fd = spawn(["config", "heimdall-api", "development"], env=self.env, cols=60)
        self.addCleanup(stop, pid)
        read_until(fd, "from its stack files?")
        os.write(fd, b"\x1b")  # no: nothing is created
        read_until(fd, "+ Add a variable")
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

    def test_ctrl_c_exits_130(self):
        self.assertEqual(self.prompt(["choose", "Which one?", "alpha", "beta"], b"\x03")[0], 130)

    def test_ask_and_confirm(self):
        self.assertEqual(self.prompt(["ask", "Name?", "--default", "x"], b"\x15abc\r"), (0, "abc"))
        self.assertEqual(self.prompt(["confirm", "Sure?"], b"y"), (0, "y"))
        self.assertEqual(self.prompt(["confirm", "Sure?", "--yes"], b"\r"), (0, "y"))
        self.assertEqual(self.prompt(["confirm", "Sure?"], b"\x1b"), (0, "n"))


@unittest.skipUnless(sys.platform.startswith("linux"), "needs a Linux pty")
class HostShPromptTests(unittest.TestCase):
    """host.sh's choose, ask and confirm in a terminal, through a bash driver that has its prompts:
    the answers reach the caller's variables, and the prompts are drawn below the output."""

    def driver(self, body, term="xterm-256color", cols=100):
        import shutil
        import tempfile
        host = (SCRIPTS / "host.sh").read_text()
        start, end = host.index("say() {"), host.index("title() {")
        prompts = host[host.index("# ---- Prompts"):host.index("# ---- Host facts")]
        tmp = tempfile.mkdtemp(prefix="ygg-prompts-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.out = pathlib.Path(tmp, "out")
        driver = pathlib.Path(tmp, "driver.sh")
        driver.write_text(
            "set -euo pipefail\n"
            f"root={SCRIPTS.parent}\n"
            f"out={self.out}\n"
            "dim='' reset=''\n"
            + host[start:end]
            + host[host.index("warn() {"):host.index("\n", host.index("die() {"))]
            + "\n" + prompts + body)
        env = dict(os.environ, TERM=term, ESCDELAY="25", LANG="C.UTF-8")
        env.pop("YGG_PLAIN", None)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, cols, 0, 0))
                os.execve("/bin/bash", ["bash", str(driver)], env)
            finally:
                os._exit(127)
        self.addCleanup(stop, pid)
        return pid, fd

    def test_the_callers_receive_the_answers(self):
        pid, fd = self.driver(
            'choose c "Pick one?" alpha beta\n'
            'ask a "Name?" dflt\n'
            'ask d "Dash?" -x\n'
            'if confirm "Sure?"; then y=yes; else y=no; fi\n'
            'if confirm "Really?"; then e=yes; else e=no; fi\n'
            'choose b "Back?" alpha Back\n'
            'choose l "Lower?" start stop back\n'
            'printf "%s|%s|%s|%s|%s|%s|%s" "$c" "$a" "$d" "$y" "$e" "$b" "$l" > "$out"\n')
        for question, keys in (("Pick one?", b"beta\r"), ("Name?", b"\x15abc\r"), ("Dash?", b"\r"), ("Sure?", b"y"),
                               ("Really?", b"\x1b"), ("Back?", b"\x1b"), ("Lower?", b"\x1b")):
            read_until(fd, question)
            os.write(fd, keys)
        self.assertEqual(wait(pid), 0)
        self.assertEqual(self.out.read_text(), "beta|abc|-x|yes|no|Back|back")

    def test_the_prompt_is_drawn_below_the_output_and_leaves_its_answer(self):
        pid, fd = self.driver('say "Output before the prompt"\n'
                              'choose c "Pick one?" alpha beta\n'
                              'say "Output after it"\n')
        screen = Screen(100)
        shown = read_screen(fd, screen, "beta")
        self.assertIn("Output before the prompt\nPick one?", shown)
        os.write(fd, b"beta\r")
        shown = read_screen(fd, screen, "Output after it")
        self.assertEqual(wait(pid), 0)
        self.assertEqual(screen.unknown, [])
        self.assertEqual(shown, "Output before the prompt\nPick one? beta\nOutput after it")

    def test_a_long_question_is_shown_in_full(self):
        question = ("How can Docker check its health from inside the container? (deploy.sh rolls back a "
                    "release that never gets healthy, so pick what the image has)")
        pid, fd = self.driver(f'choose c "{question}" wget curl none\nsay "done: $c"\n', cols=80)
        screen = Screen(80)
        shown = read_screen(fd, screen, "none")
        self.assertIn(question, " ".join(shown.split()))
        self.assertTrue(all(len(line) < 80 for line in shown.splitlines()))
        os.write(fd, b"curl\r")
        shown = read_screen(fd, screen, "done: curl")
        self.assertEqual(wait(pid), 0)
        self.assertIn(question + ": curl", " ".join(shown.split()))

    def test_ctrl_c_in_a_prompt_stops_the_operation_with_130(self):
        pid, fd = self.driver('choose c "Pick one?" alpha beta\nsay "not reached" > "$out"\n')
        read_until(fd, "beta")
        os.write(fd, b"\x03")
        self.assertEqual(wait(pid), 130)
        self.assertFalse(self.out.exists())

    def test_a_terminal_without_terminfo_still_gets_its_answer(self):
        pid, fd = self.driver('choose c "Pick one?" alpha beta\nprintf %s "$c" > "$out"\n', term="xterm-no-such-terminal")
        read_until(fd, "beta")
        os.write(fd, b"beta\r")
        self.assertEqual(wait(pid), 0)
        self.assertEqual(self.out.read_text(), "beta")

    def test_a_dumb_terminal_gets_the_numbered_prompt(self):
        pid, fd = self.driver('choose c "Pick one?" alpha beta\nprintf %s "$c" > "$out"\n', term="dumb")
        read_until(fd, "2) beta")
        os.write(fd, b"2\n")
        self.assertEqual(wait(pid), 0)
        self.assertEqual(self.out.read_text(), "beta")


if __name__ == "__main__":
    unittest.main()
