"""Static tests of the pipeline library, jenkins/library/vars/yggdrasilPipeline.groovy.

    python3 -m unittest discover -s scripts -p test_pipeline_library.py

They read the Groovy and never run it: there is no Groovy or Jenkins here, and the live suite (Task 10 of
plan 3, gated by YGG_JENKINS_TESTS=1) is what proves the flow. What these pin are the library's security
rules, the ones a later edit could break without any test noticing:

  - a credential is bound only through the allow-list helpers: the forge's own ids live in
    forgeCredential() alone, and a check binds only the ids its plan names (withCheckCredentials);
  - the write credential is asked for by reportStatus() alone, which posts from the controller (outside
    node()), so neither the ci nor the release agent ever holds it;
  - nothing waits (sleep, input) while holding an agent;
  - no `sh` script is a Groovy string with interpolation: every value reaches the shell as an
    environment variable, so a branch name, a check's command or an image cannot rewrite the script.

How honest this is: Groovy is not parsed here. A small lexer blanks comments and string literals (so a
brace or a word inside them does not count), braces are matched to find each top-level function and each
node(...) { } block, and a function's calls are the names followed by "(" in its body. Calls are followed
transitively from a node block through the library's own functions, so moving a sleep into a helper that
a node block calls is still caught; but a call through a variable (a closure passed around, a method
called by name) or dynamic dispatch is invisible, and the lexer's guess between a slashy string and a
division is a heuristic. The rules below are written so that the code they accept is easy to read as
correct, not so that every possible evasion is caught.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "jenkins" / "library" / "vars" / "yggdrasilPipeline.groovy"

# The Jenkins credential ids of the forge itself (platform/jenkins/controller/casc.*.yaml).
FORGE_CREDENTIAL_IDS = ("forge-read", "forge-write", "forge-webhook", "github-app", "github-read")
# Constants whose value is a shell script, accepted as `sh(script: <NAME>, ...)` when the constant itself
# is a single-quoted literal (checked below).
SCRIPT_CONSTANTS = ("TOOL",)
# A top-level function definition: a return type (or def) at the start of a line, then name(.
FUNCTION = re.compile(r"^(?:def|void|String|Map|List|boolean|int|long|Object)\s+([A-Za-z_]\w*)\s*\(", re.M)
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
# Characters after which a `/` starts a slashy string (a regular expression) rather than a division.
SLASHY_AFTER = set("(=,~[:!&|?{;}+-*") | {""}


def lex(source):
    """-> (code, masked, strings).

    code     the source with comments blanked (strings kept), so a regex can look at literal arguments
    masked   comments blanked (spaces) and string literals blanked (NULs), for brace matching and
             finding calls
    strings  [(start, end, opening quote, body)] for every string literal, slashy strings included

    Blanking keeps every newline and the length, so offsets are the source's. A GString's ${...} is
    skipped as part of its string (strings nested in it included)."""
    n = len(source)
    code = list(source)
    masked = list(source)
    strings = []

    def blank(chars, start, end, filler=" "):
        for k in range(start, end):
            if chars[k] != "\n":
                chars[k] = filler

    def string_end(start, quote):
        """The offset just after the literal that opens with `quote` at `start`."""
        i = start + len(quote)
        triple = len(quote) == 3
        while i < n:
            c = source[i]
            if c == "\\":
                i += 2
                continue
            if quote.startswith('"') and source.startswith("${", i):
                i = gstring_expression_end(i + 2)
                continue
            if source.startswith(quote, i):
                return i + len(quote)
            if c == "\n" and not triple and quote != "/":
                raise ValueError(f"unterminated string at offset {start}")
            i += 1
        raise ValueError(f"unterminated string at offset {start}")

    def gstring_expression_end(i):
        depth = 1
        while i < n and depth:
            c = source[i]
            if c in "'\"":
                quote = source[i:i + 3] if source[i:i + 3] in ("'''", '"""') else c
                i = string_end(i, quote)
                continue
            depth += {"{": 1, "}": -1}.get(c, 0)
            i += 1
        return i

    i = 0
    previous = ""
    while i < n:
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            blank(code, i, end)
            blank(masked, i, end)
            i = end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(code, i, end)
            blank(masked, i, end)
            i = end
            continue
        c = source[i]
        quote = None
        if source.startswith("'''", i) or source.startswith('"""', i):
            quote = source[i:i + 3]
        elif c in "'\"":
            quote = c
        elif c == "/" and previous in SLASHY_AFTER:
            quote = "/"
        if quote:
            end = string_end(i, quote)
            strings.append((i, end, quote, source[i + len(quote):end - len(quote)]))
            # NUL, not a space: `sh '...'` must not read as `sh` followed by nothing, and a NUL is
            # neither a word character nor a bracket.
            blank(masked, i, end, "\0")
            previous = "a"  # a value: a `/` after it divides
            i = end
            continue
        if not c.isspace():
            # A keyword before a slashy string (return /x/) is rare enough here to leave out.
            previous = c if not (c.isalnum() or c == "_") else "a"
        i += 1
    return "".join(code), "".join(masked), strings


def matching(masked, start, opening, closing):
    """The offset of the bracket closing the one at `start` (in the masked text)."""
    depth = 0
    for k in range(start, len(masked)):
        if masked[k] == opening:
            depth += 1
        elif masked[k] == closing:
            depth -= 1
            if depth == 0:
                return k
    raise ValueError(f"unbalanced {opening}{closing} from offset {start}")


class Library:
    def __init__(self, source):
        self.source = source
        self.code, self.masked, self.strings = lex(source)
        # name -> (body start, body end): from the function's { to its }.
        self.functions = {}
        for match in FUNCTION.finditer(self.masked):
            paren = self.masked.index("(", match.start(1))
            close = matching(self.masked, paren, "(", ")")
            brace = self.masked.index("{", close)
            assert not self.masked[close + 1:brace].strip(), f"{match.group(1)}: no body after its parameters"
            self.functions[match.group(1)] = (brace, matching(self.masked, brace, "{", "}"))
        self.calls = {name: self.called(start, end) for name, (start, end) in self.functions.items()}

    def line(self, offset):
        return self.source.count("\n", 0, offset) + 1

    def function_at(self, offset):
        for name, (start, end) in self.functions.items():
            if start <= offset <= end:
                return name
        return None

    def called(self, start, end):
        return {name for name in CALL.findall(self.masked[start:end])}

    def reachable(self, start, end):
        """Every name called from the span, following the library's own functions transitively."""
        seen, todo = set(), list(self.called(start, end))
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            todo.extend(self.calls.get(name, ()))
        return seen

    def code_reached(self, start, end, leaving_out=()):
        """The code (comments blanked, strings kept) of the span and of the library functions it reaches,
        but those in leaving_out, joined."""
        spans = [(start, end)] + [self.functions[name] for name in self.reachable(start, end)
                                  if name in self.functions and name not in leaving_out]
        return "\n".join(self.code[a:b] for a, b in spans)

    def words_reached(self, start, end):
        """Every word in the span and in the library functions it reaches. A word, not only a call: a
        step called without parentheses (`sleep time: 60`, `input message: ...`) has no "(" after it."""
        spans = [(start, end)] + [self.functions[name] for name in self.reachable(start, end) if name in self.functions]
        return {word for a, b in spans for word in re.findall(r"[A-Za-z_]\w*", self.masked[a:b])}

    def node_blocks(self):
        """[(label source text, block start, block end)] for every node(<label>) { ... }."""
        blocks = []
        for match in re.finditer(r"\bnode\s*\(", self.masked):
            paren = match.end() - 1
            close = matching(self.masked, paren, "(", ")")
            brace = close + 1
            while self.masked[brace].isspace():
                brace += 1
            assert self.masked[brace] == "{", f"line {self.line(match.start())}: node(...) without a block"
            blocks.append((self.code[paren + 1:close].strip(), brace, matching(self.masked, brace, "{", "}")))
        return blocks

    def string_at(self, offset):
        for start, end, quote, body in self.strings:
            if start == offset:
                return quote, body
        return None


def library():
    return Library(LIBRARY.read_text(encoding="utf-8"))


class LexerTests(unittest.TestCase):
    """The lexer the other tests stand on: if it miscounted, they would check the wrong spans."""

    def test_given_braces_in_strings_and_comments_when_lexed_then_only_code_braces_remain(self):
        source = "def f() { // }\n  x = '}' + \"${a ? '{' : b}\" + /[0-9]{40}/\n}\n"
        _, masked, strings = lex(source)
        self.assertEqual(len(masked), len(source))
        self.assertEqual(masked.count("{"), 1)
        self.assertEqual(masked.count("}"), 1)
        self.assertEqual([quote for _, _, quote, _ in strings], ["'", '"', "/"])

    def test_given_a_division_when_lexed_then_it_is_not_a_slashy_string(self):
        _, masked, strings = lex("int x = (a + b) / 2 / c\n")
        self.assertEqual(strings, [])

    def test_given_functions_and_node_blocks_when_read_then_calls_are_followed_through_helpers(self):
        lib = Library("void a() {\n  node('ci') { b() }\n}\n\nvoid b() {\n  c('x')\n}\n\nvoid c(String s) {\n  sleep(1)\n}\n")
        self.assertEqual(set(lib.functions), {"a", "b", "c"})
        (label, start, end), = lib.node_blocks()
        self.assertEqual(label, "'ci'")
        self.assertIn("sleep", lib.reachable(start, end))
        self.assertIn("sleep", lib.words_reached(start, end))
        self.assertNotIn("a", lib.reachable(start, end))


class PipelineLibraryTests(unittest.TestCase):
    def setUp(self):
        self.lib = library()

    def test_given_the_library_when_read_then_it_takes_no_arguments_and_names_the_job_by_catalog_id(self):
        # The one Jenkinsfile kept in Jenkins calls yggdrasilPipeline() with nothing; stack: is what old
        # Jenkinsfiles passed.
        self.assertIsNotNone(re.search(r"def call\(Map config = \[:\]\)", self.lib.code), "call(Map config = [:])")
        self.assertIn("config.stack ?:", self.lib.code)
        self.assertIn("env.JOB_NAME", self.lib.code)

    def test_given_the_library_when_read_then_credentials_are_bound_only_through_the_allow_list_helper(self):
        lib = self.lib
        self.assertIn("forgeCredential", lib.functions)
        self.assertIn("withCheckCredentials", lib.functions)
        # The forge's credential ids, as literals, only in forgeCredential().
        for start, _, _, body in lib.strings:
            if body in FORGE_CREDENTIAL_IDS:
                self.assertEqual(lib.function_at(start), "forgeCredential",
                                 f"line {lib.line(start)}: credential id '{body}' outside forgeCredential()")
        # withCredentials only in the two binding helpers: the forge's token, and a check's allow-listed ids.
        for match in re.finditer(r"\bwithCredentials\s*\(", lib.masked):
            self.assertIn(lib.function_at(match.start()), ("withForgeToken", "withCheckCredentials"),
                          f"line {lib.line(match.start())}: withCredentials outside the binding helpers")
        # Every credentialsId names forgeCredential(...), except the check bindings, whose ids the plan gives
        # and withCheckCredentials checks against the catalog entry first.
        for match in re.finditer(r"\bcredentialsId\s*:\s*", lib.code):
            if lib.function_at(match.start()) == "withCheckCredentials":
                continue
            self.assertTrue(lib.code.startswith("forgeCredential(", match.end()),
                            f"line {lib.line(match.start())}: credentialsId not from forgeCredential()")
        # withCheckCredentials compares each id with the catalog entry's credentials before binding it.
        start, end = lib.functions["withCheckCredentials"]
        self.assertIn("entry.credentials", lib.code[start:end])

    def test_given_the_library_when_read_then_the_write_credential_is_asked_for_only_by_report_status(self):
        lib = self.lib
        # forge-write only in forgeCredential (above); the 'write' access only in reportStatus.
        for match in re.finditer(r"\b(?:forgeCredential|withForgeToken)\s*\(\s*'write'", lib.code):
            self.assertEqual(lib.function_at(match.start()), "reportStatus",
                             f"line {lib.line(match.start())}: the write credential outside reportStatus()")
        self.assertRegex(lib.code[slice(*lib.functions["reportStatus"])], r"withForgeToken\(\s*'write'")
        # reportStatus posts with httpRequest, and nothing else does.
        for match in re.finditer(r"\bhttpRequest\s*\(", lib.masked):
            self.assertEqual(lib.function_at(match.start()), "reportStatus",
                             f"line {lib.line(match.start())}: httpRequest outside reportStatus()")

    def test_given_ci_node_blocks_when_read_then_none_binds_the_github_app(self):
        # The ci agent runs the repositories' check code: the GitHub App's token (which can write to the
        # mirror, or to every repository on a GitHub installation) is never bound there. forgeCredential()
        # itself is left out of the scan: it is the table of ids, and its 'read' answer on GitHub without
        # github-read is the accepted, logged fallback; what matters is which access a ci block binds.
        lib = self.lib
        ci_blocks = [(label, start, end) for label, start, end in lib.node_blocks() if re.search(r"\bci\b", label)]
        self.assertTrue(ci_blocks)
        for label, start, end in ci_blocks:
            where = f"line {lib.line(start)}: node({label})"
            code = lib.code_reached(start, end, leaving_out=("forgeCredential",))
            self.assertNotRegex(code, r"(?:withForgeToken|forgeCredential)\(\s*'github'", f"{where} binds the GitHub App")
            self.assertNotIn("'github-app'", code, where)
            self.assertNotIn("github", lib.reachable(start, end), f"{where} runs github.sh with the App")
        # The Actions lookup is where the App is bound for a check build: on the publish agent.
        (label, start, end), = [block for block in lib.node_blocks()
                                if lib.function_at(block[1]) == "actionsLook"]
        self.assertEqual(label, "ctx.forge.publishAgent")
        self.assertIn("withForgeToken('github')", lib.code[start:end])

    def test_given_github_when_read_then_the_ci_agent_reads_with_github_read_when_it_is_configured(self):
        start, end = self.lib.functions["forgeCredential"]
        body = self.lib.code[start:end]
        self.assertIn("credentialExists('github-read')", body)
        self.assertIn("? 'github-read' : 'github-app'", body)
        self.assertIn("'github-read is not configured: the CI agent uses the GitHub App'", body)
        start, end = self.lib.functions["credentialExists"]
        self.assertIn("CredentialsProvider.lookupCredentials", self.lib.code[start:end])
        # A check never gets one of the pipeline's own credentials, the read-only App included.
        start, end = self.lib.functions["withCheckCredentials"]
        self.assertIn("id.startsWith('github-')", self.lib.code[start:end])

    def test_given_node_blocks_when_read_then_none_reaches_the_write_credential_or_a_status(self):
        # httpRequest inside node() runs on that agent; the write token must stay on the controller. And on
        # the ci and release agents, nothing may ask for the write credential at all.
        blocks = self.lib.node_blocks()
        self.assertTrue(any(label == "'ci'" for label, _, _ in blocks), "no node('ci') block found")
        for label, start, end in blocks:
            reached = self.lib.words_reached(start, end)
            for forbidden in ("reportStatus", "httpRequest"):
                self.assertNotIn(forbidden, reached,
                                 f"line {self.lib.line(start)}: node({label}) reaches {forbidden}")
            if label in ("'ci'", "'release'"):
                self.assertNotRegex(self.lib.code[start:end], r"'write'",
                                    f"line {self.lib.line(start)}: node({label}) asks for the write credential")

    def test_given_the_library_when_read_then_no_wait_loop_sleeps_inside_a_node(self):
        lib = self.lib
        self.assertIsNotNone(re.search(r"\bsleep\b", lib.masked), "no sleep at all: the GitHub Actions wait loop is missing")
        for label, start, end in lib.node_blocks():
            reached = lib.words_reached(start, end)
            for waiting in ("sleep", "input"):
                self.assertNotIn(waiting, reached, f"line {lib.line(start)}: node({label}) reaches {waiting}")
        # The loop itself: a sleep of the poll interval in the function that looks at GitHub Actions.
        start, end = lib.functions["copyActionsChecks"]
        self.assertRegex(lib.masked[start:end], r"\bwhile\s*\(")
        self.assertRegex(lib.masked[start:end], r"\bsleep\b")
        self.assertIn("actionsLook", lib.calls["copyActionsChecks"])

    def test_given_sh_steps_when_read_then_no_script_is_an_interpolated_groovy_string(self):
        # Every script is a single-quoted literal ('...' or '''...''', where Groovy interpolates nothing),
        # or one of the SCRIPT_CONSTANTS, itself a single-quoted literal. Allow-list of interpolated scripts:
        # none. Values reach the shell through withEnv, quoted "$VAR" there.
        lib = self.lib
        found = 0
        for match in re.finditer(r"\bsh\b\s*(\(\s*)?(script\s*:\s*)?", lib.masked):
            found += 1
            where = f"line {lib.line(match.start())}"
            literal = lib.string_at(match.end())
            if literal is not None:
                quote, _ = literal
                self.assertIn(quote, ("'", "'''"), f"{where}: sh script in {quote}...{quote}, which interpolates")
                continue
            name = re.match(r"[A-Za-z_]\w*", lib.masked[match.end():])
            self.assertIsNotNone(name, f"{where}: sh without a literal script")
            self.assertIn(name.group(0), SCRIPT_CONSTANTS, f"{where}: sh(script: {name.group(0)}) is not allow-listed")
        self.assertGreater(found, 5)
        for constant in SCRIPT_CONSTANTS:
            match = re.search(rf"@Field static final String {constant}\s*=\s*", lib.masked)
            self.assertIsNotNone(match, f"{constant} is not a static final String")
            literal = lib.string_at(match.end())
            self.assertIsNotNone(literal, f"{constant} is not a literal")
            self.assertIn(literal[0], ("'", "'''"), f"{constant} interpolates")

    def test_given_report_status_when_read_then_descriptions_are_sanitized_and_the_token_masked(self):
        start, end = self.lib.functions["reportStatus"]
        body = self.lib.code[start:end]
        self.assertRegex(body, r"description:\s*safeText\(")
        self.assertIn("maskValue: true", body)
        signature = (r"void reportStatus\(String repo, String sha, String state, String context, "
                     r"String description, String targetUrl = null\)")
        self.assertIsNotNone(re.search(signature, self.lib.code), "reportStatus' signature")

    def test_given_the_ci_checkouts_when_read_then_history_branches_and_tags_are_fetched_with_the_read_token(self):
        start, end = self.lib.functions["fetchHistory"]
        body = self.lib.code[start:end]
        self.assertIn("withForgeToken('read')", body)
        for wanted in ("--unshallow", "refs/heads/$branch:refs/remotes/origin/$branch", "--tags"):
            self.assertIn(wanted, body)

    def test_given_the_release_path_when_read_then_it_still_runs_under_the_application_lock(self):
        # test_catalog pins the same; the release pull request's checks run inside it too.
        code = self.lib.code
        lock = code.index('lock(resource: "release-${app}")')
        self.assertLess(lock, code.index("release(app, plan.findAll"))
        self.assertLess(lock, code.index("verify(ctx, state)", lock))


if __name__ == "__main__":
    unittest.main()
