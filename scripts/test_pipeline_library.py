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

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "jenkins" / "library" / "vars" / "yggdrasilPipeline.groovy"

# The Jenkins credential ids of the forge itself (platform/jenkins/controller/casc.*.yaml).
FORGE_CREDENTIAL_IDS = ("forge-read", "forge-write", "forge-webhook", "github-app", "github-read")
# Constants whose value is a shell script, accepted as `sh(script: <NAME>, ...)` when the constant itself
# is a single-quoted literal (checked below).
SCRIPT_CONSTANTS = ("TOOL", "LINK_GUARD")
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

    def call_blocks(self, name):
        """[(block start, block end)] for every call `name(...) { ... }` (the closure given as its last
        argument), the function's own definition left out."""
        blocks = []
        for match in re.finditer(rf"\b{name}\s*\(", self.masked):
            if re.search(r"(?:def|void|String|Map|List|boolean|int)\s+$", self.masked[max(0, match.start() - 20):match.start()]):
                continue
            paren = match.end() - 1
            close = matching(self.masked, paren, "(", ")")
            brace = close + 1
            while self.masked[brace] in " \t":
                brace += 1
            if self.masked[brace] == "{":
                blocks.append((brace, matching(self.masked, brace, "{", "}")))
        return blocks

    def agent_blocks(self):
        """[(label, start, end)]: every node(...) block, and every closure given to onPublishAgent(), which
        runs it inside that function's node(ctx.forge.publishAgent) -- lexically outside any node block."""
        return self.node_blocks() + [("publish agent closure", start, end) for start, end in self.call_blocks("onPublishAgent")]

    def inside(self, offset, blocks):
        return any(start <= offset <= end for start, end in blocks)

    def string_at(self, offset):
        for start, end, quote, body in self.strings:
            if start == offset:
                return quote, body
        return None


def library():
    return Library(LIBRARY.read_text(encoding="utf-8"))


def field_literal(lib, name):
    """(quote, body) of a @Field static final String constant's literal."""
    match = re.search(rf"@Field static final String {name}\s*=\s*", lib.masked)
    assert match, name
    return lib.string_at(match.end())


def link_guard(lib):
    """LINK_GUARD's script."""
    quote, body = field_literal(lib, "LINK_GUARD")
    assert quote == "'''"
    return body


def field_pattern(lib, name):
    """A single-quoted @Field regex constant as Python's re reads it (Groovy's escaped backslashes undone)."""
    quote, body = field_literal(lib, name)
    assert quote == "'", name
    return body.replace("\\\\", "\\")


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
        # withCredentials only in the three binding helpers: the forge's token, a check's allow-listed ids,
        # and the NuGet key.
        for match in re.finditer(r"\bwithCredentials\s*\(", lib.masked):
            self.assertIn(lib.function_at(match.start()), ("withForgeToken", "withCheckCredentials", "withNugetKey"),
                          f"line {lib.line(match.start())}: withCredentials outside the binding helpers")
        # Every credentialsId names forgeCredential(...), except the check bindings, whose ids the plan gives
        # and withCheckCredentials checks against the catalog entry first, and the NuGet key's fixed id.
        for match in re.finditer(r"\bcredentialsId\s*:\s*", lib.code):
            function = lib.function_at(match.start())
            if function == "withCheckCredentials":
                continue
            if function == "withNugetKey":
                self.assertTrue(lib.code.startswith("NUGET_CREDENTIAL,", match.end()), f"line {lib.line(match.start())}")
                continue
            self.assertTrue(lib.code.startswith("forgeCredential(", match.end()),
                            f"line {lib.line(match.start())}: credentialsId not from forgeCredential()")
        # withCheckCredentials compares each id with the catalog entry's list for its context before
        # binding it: a check with `credentials`, the release build with `releaseCredentials` (C1).
        start, end = lib.functions["withCheckCredentials"]
        body = lib.code[start:end]
        self.assertRegex(lib.code, r"void withCheckCredentials\(Map check, Map entry, String allowList, Closure body\)")
        self.assertIn("allowList in ['credentials', 'releaseCredentials']", body)
        self.assertIn("entry[allowList]", body)
        self.assertNotIn("entry.credentials", body)

    def test_given_check_and_release_bindings_when_read_then_each_uses_its_own_catalog_list(self):
        # A check runs any branch's code: a signing key the owner allows for the release build must never
        # be bindable there. Each caller names its list; nothing else calls the helper.
        lib = self.lib
        calls = [(lib.function_at(m.start()), lib.code[m.start():m.start() + 200])
                 for m in re.finditer(r"(?<!void )\bwithCheckCredentials\(", lib.masked)]
        self.assertEqual(sorted(f for f, _ in calls), ["buildReleaseFiles", "runCheck"])
        for function, call in calls:
            wanted = "'releaseCredentials'" if function == "buildReleaseFiles" else "'credentials'"
            self.assertRegex(call, r"^withCheckCredentials\([^{]*, " + re.escape(wanted) + r"\) \{", function)
        # The plan's second look (planProblem) is about checks: `credentials` only.
        body = lib.code[slice(*lib.functions["planProblem"])]
        self.assertIn("entry.credentials", body)
        self.assertNotIn("releaseCredentials", body)

    def test_given_the_library_when_read_then_only_report_status_and_the_publish_agent_ask_for_write(self):
        lib = self.lib
        # forge-write only in forgeCredential (above); the 'write' access only in reportStatus (the
        # controller) and onPublishAgent (inside its node on the publish agent: tested below).
        for match in re.finditer(r"\b(?:forgeCredential|withForgeToken)\s*\(\s*'write'", lib.code):
            self.assertIn(lib.function_at(match.start()), ("reportStatus", "onPublishAgent"),
                          f"line {lib.line(match.start())}: the write credential outside reportStatus()/onPublishAgent()")
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
        start, end = lib.functions["githubLook"]
        self.assertRegex(lib.code[start:end], r"onPublishAgent\(ctx, false, true\)")
        (label, start, end), = [block for block in lib.node_blocks() if lib.function_at(block[1]) == "onPublishAgent"]
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
        # Every node block, and every closure run on the publish agent (onPublishAgent), never waits.
        for label, start, end in lib.agent_blocks():
            reached = lib.words_reached(start, end)
            for waiting in ("sleep", "input", "pollGithub", "askToSkip"):
                self.assertNotIn(waiting, reached, f"line {lib.line(start)}: {label} reaches {waiting}")
        # The loop itself: a sleep of the poll interval between looks, each look on the publish agent.
        start, end = lib.functions["pollGithub"]
        self.assertRegex(lib.masked[start:end], r"\bwhile\s*\(")
        self.assertRegex(lib.masked[start:end], r"\bsleep\b")
        self.assertIn("githubLook", lib.calls["pollGithub"])
        self.assertIn("onPublishAgent", lib.calls["githubLook"])
        self.assertIn("pollGithub", lib.calls["copyActionsChecks"])

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
        start, end = self.lib.functions["checkoutCommit"]
        body = self.lib.code[start:end]
        self.assertIn("withForgeToken('read')", body)
        # Every branch (origin/develop and origin/main with them), the pull request's head, the tags, and
        # no depth: a shallow clone or missing tags would make branch-policy.sh pass what it should fail.
        for wanted in ("+refs/heads/*:refs/remotes/origin/*", "+refs/pull/$YGG_CHANGE_ID/head:", "--tags",
                       'checkout --quiet --detach "$YGG_SHA"'):
            self.assertIn(wanted, body)
        self.assertNotIn("--depth", body)
        self.assertIn("sameCommit(head, sha)", body)

    def test_given_ci_node_blocks_when_read_then_none_checks_out_with_the_jobs_credential(self):
        # checkout scm uses the job's branch-source credential (the main GitHub App on GitHub): on the ci
        # agent the build's commit is fetched by checkoutCommit() with the read credential instead. The
        # word scm must not occur at all in what a ci block reaches (no checkout scm, no checkout(scm),
        # no GitSCM built from scm's remotes).
        lib = self.lib
        ci_blocks = [(label, start, end) for label, start, end in lib.node_blocks() if re.search(r"\bci\b", label)]
        self.assertTrue(ci_blocks)
        for label, start, end in ci_blocks:
            where = f"line {lib.line(start)}: node({label})"
            self.assertNotIn("scm", lib.words_reached(start, end), f"{where} reaches the job's scm")
            self.assertNotIn("checkoutBuild", lib.reachable(start, end), where)
            self.assertIn("checkoutCommit", lib.reachable(start, end), f"{where} checks out nothing")
        # The commit is known on the controller, from the build's revision, before any ci checkout.
        start, end = lib.functions["prepare"]
        self.assertRegex(lib.masked[start:end], r"sha:\s*buildCommit\(\)")
        # checkout scm is left only where the deploy agents check out.
        for match in re.finditer(r"\bcheckout\s+scm\b|\bcheckout\s*\(\s*scm\b", lib.masked):
            self.assertEqual(lib.function_at(match.start()), "checkoutBuild", f"line {lib.line(match.start())}")

    def test_given_a_build_without_a_revision_action_when_its_commit_is_asked_then_the_source_is_asked_once(self):
        # pipelineBranchDefaultsProjectFactory records no SCMRevisionAction (test_jenkins_live.py): the
        # branch source is asked for the head's revision on the controller, and the answer is recorded
        # on the build so every later step sees the same commit.
        start, end = self.lib.functions["revisionHash"]
        body = self.lib.code[start:end]
        self.assertIn("run.getAction(jenkins.scm.api.SCMRevisionAction)", body)
        for wanted in ("SCMHead.HeadByItem.findHead(run.parent)", "SCMSource.SourceByItem.findSource(run.parent)",
                       "source.fetch(head, hudson.model.TaskListener.NULL)",
                       "run.addAction(new jenkins.scm.api.SCMRevisionAction(source, revision))"):
            self.assertIn(wanted, body)
        self.assertLess(body.index("source.fetch("), body.index("run.addAction("))
        self.assertRegex(self.lib.code[self.lib.functions["buildCommit"][0]:], r"^\{\s*String sha = revisionHash\(currentBuild\.rawBuild\)")

    def test_given_a_checked_push_when_deployed_then_the_deployed_commit_must_be_the_checked_one(self):
        # A push that lands while the checks run must not be deployed by `checkout scm` on the deploy agent:
        # branchBuild hands the checked commit to deployTo, which refuses any other checkout before deploy.sh.
        lib = self.lib
        start, end = lib.functions["branchBuild"]
        body = lib.code[start:end]
        self.assertRegex(body, r"checkedSha = state\.sha")
        self.assertRegex(body, r"deployTo\([^\n]*, inputs, checkedSha\)")
        self.assertLess(body.index("checkedSha = state.sha"), body.index("deployTo("))
        self.assertIsNotNone(re.search(r"void deployTo\([^)]*String expectedSha = null[,)]", lib.code))
        start, end = lib.functions["deployTo"]
        body = lib.code[start:end]
        guard = body.index("sameCommit(sha, expectedSha)")
        self.assertLess(body.index("sha = checkoutBuild()"), guard)
        self.assertLess(guard, body.index("deploy.sh"))
        self.assertLess(guard, body.index("unstash"))
        # No status for it: branch builds post none.
        self.assertNotRegex(body[guard - 200:guard + 50], r"reportStatus")

    def test_given_describe_when_read_then_a_failed_run_without_errors_fails_the_build(self):
        # describe exits 0 with a document, or 1 with the document's errors. A run that failed some other
        # way must not be read as "no yggdrasil.yml" (which turns every check off).
        start, end = self.lib.functions["describeCheckout"]
        body = self.lib.code[start:end]
        self.assertIn("pipeline_config.py describe", body)
        self.assertIn("returnStatus: true", body)
        self.assertRegex(body, r"!\(document instanceof Map\) \|\| \(code != 0 && !document\.errors\)")
        failed = body.index("code != 0 && !document.errors")
        self.assertLess(failed, body.index("described = [entry: document.entry, config: document.config]"))
        start, end = self.lib.functions["prepare"]
        body = self.lib.code[start:end]
        self.assertLess(body.index("describeCheckout(ctx)"), body.index("state.config = document.config"))

    def test_given_a_plan_when_read_then_it_must_cover_every_check_of_the_file_once(self):
        start, end = self.lib.functions["planChecks"]
        self.assertRegex(self.lib.code[start:end], r"planProblem\(planned, state\.entry\) \?: planCoverage\(planned, state\.config\)")
        start, end = self.lib.functions["planCoverage"]
        body = self.lib.code[start:end]
        self.assertIn("config.checks", body)
        for wanted in ("leaves out", "does not", "more than once"):
            self.assertIn(wanted, body)

    def test_given_collected_results_when_read_then_no_link_out_of_the_checkout_is_followed(self):
        lib = self.lib
        start, end = lib.functions["collectResults"]
        body = lib.code[start:end]
        self.assertRegex(body, r"archiveArtifacts [^\n]*followSymlinks: false")
        # junit and stash cannot be told not to follow links: the refusal comes before all three.
        refusal = body.index("sh(script: LINK_GUARD")
        for step in ("junit testResults", "archiveArtifacts", "stash name"):
            self.assertLess(refusal, body.index(step), step)
        self.assertRegex(body, r"if \(links != 0\) \{\s*error ")

    @unittest.skipUnless(os.name == "posix" and shutil.which("realpath"), "runs the embedded script with sh")
    def test_given_links_in_a_checkout_when_the_collect_guard_runs_then_only_links_leading_out_refuse(self):
        lib = self.lib
        script = link_guard(lib)
        self.assertNotIn("\\", script, "the guard has Groovy escapes this test does not undo")
        with tempfile.TemporaryDirectory() as temp:
            outside = pathlib.Path(temp) / "secret"
            outside.write_text("host file\n")
            app = pathlib.Path(temp) / "app"
            (app / "results").mkdir(parents=True)
            (app / "results" / "a.xml").write_text("<testsuite/>")
            (app / "inside.xml").symlink_to("results/a.xml")
            (app / "here").symlink_to(".")

            def guard(**env):
                return subprocess.run(["sh", "-c", script], cwd=app, capture_output=True, text=True, timeout=30,
                                      env={**os.environ, "YGG_ANY_LINK": "", **env})

            done = guard()
            self.assertEqual(done.returncode, 0, done.stderr)
            for name, target in (("results/b.xml", outside), ("up.xml", "../secret"), ("etc", "/etc")):
                link = app / name
                link.symlink_to(target)
                done = guard()
                self.assertEqual(done.returncode, 3, name)
                self.assertIn(name, done.stderr)
                link.unlink()
            # A release's files (YGG_ANY_LINK): no link at all, even one that stays inside.
            done = guard(YGG_ANY_LINK="1")
            self.assertEqual(done.returncode, 3, done.stderr)
            self.assertIn("inside.xml", done.stderr)
            (app / "inside.xml").unlink()
            (app / "here").unlink()
            self.assertEqual(guard(YGG_ANY_LINK="1").returncode, 0)

    def test_given_the_release_path_when_read_then_it_still_runs_under_the_application_lock(self):
        # test_catalog pins the same; the release pull request's checks run inside it too.
        code = self.lib.code
        lock = code.index('lock(resource: "release-${app}")')
        self.assertLess(lock, code.index("verify(ctx, state)", lock))
        self.assertLess(lock, code.index("releasePullRequest(ctx, state, environments)", lock))
        # The path it always had, kept for GitHub repositories without yggdrasil.yml, under the same lock.
        self.assertLess(lock, code.index("release(app, environments, releaseArtifacts, ctx.repo)", lock))
        self.assertRegex(code, r"if \(ctx\.forge\.type == 'github' && state\.config == null\) \{\s*(//[^\n]*\s*)*"
                               r"release\(app, environments")


class ReleaseLibraryTests(unittest.TestCase):
    """The release pull request, its files and the actions-release job: who holds which token, where
    the waits are, and what goes into a release."""

    def setUp(self):
        self.lib = library()

    def release_blocks(self):
        blocks = [(label, start, end) for label, start, end in self.lib.node_blocks() if label == "'release'"]
        self.assertGreaterEqual(len(blocks), 3, "the release build, the gathering and actions-release's reading")
        return blocks

    def only_on_publish_agent(self, function, publish, depth=0):
        """Whether every call of the library function is inside a closure given to onPublishAgent, or in a
        function that is itself only called there."""
        lib = self.lib
        own = lib.functions[function]
        # Its calls: not inside itself, and not its definition (a return type before the name).
        calls = [m.start() for m in re.finditer(rf"\b{function}\s*\(", lib.masked)
                 if not own[0] <= m.start() <= own[1]
                 and not re.search(r"(?:def|void|String|Map|List|boolean|int)\s+$", lib.masked[max(0, m.start() - 20):m.start()])]
        if not calls or depth > 5:
            return False
        return all(lib.inside(offset, publish)
                   or (lib.function_at(offset) and self.only_on_publish_agent(lib.function_at(offset), publish, depth + 1))
                   for offset in calls)

    def test_given_release_agent_blocks_when_read_then_they_bind_only_the_read_token_and_allow_listed_ids(self):
        # The release agent runs a repository's build and validation: no write token, no GitHub App, nothing
        # posted, nothing published from there. It reads with the read credential and binds the ids of
        # release.build.credentials through withCheckCredentials (checked against the catalog entry).
        lib = self.lib
        for label, start, end in self.release_blocks():
            where = f"line {lib.line(start)}: node({label})"
            reached = lib.reachable(start, end)
            for forbidden in ("onPublishAgent", "github", "githubLook", "pollGithub", "reportStatus", "httpRequest",
                              "mergeAndTag", "publishRelease", "windowsFiles"):
                self.assertNotIn(forbidden, reached, f"{where} reaches {forbidden}")
            code = lib.code_reached(start, end, leaving_out=("forgeCredential", "withForgeToken", "github"))
            self.assertLessEqual(set(re.findall(r"withForgeToken\(\s*'(\w+)'", code)), {"read"}, where)
            for literal in ("'write'", "'github'", "'github.sh'", "'publish-release.sh'", "'github-app'"):
                self.assertNotIn(literal, code, f"{where} has {literal}")
        start, end = lib.functions["buildReleaseFiles"]
        self.assertRegex(lib.code[start:end], r"withCheckCredentials\(\[name: 'release-build', credentials: build\.credentials\], entry, 'releaseCredentials'\)")

    def test_given_the_publish_agent_when_read_then_it_alone_gets_the_write_token_and_the_github_app(self):
        lib = self.lib
        start, end = lib.functions["onPublishAgent"]
        (label, node_start, node_end), = [b for b in lib.node_blocks() if start < b[1] < end]
        self.assertEqual(label, "ctx.forge.publishAgent")
        for access in ("write", "github"):
            for match in re.finditer(rf"withForgeToken\(\s*'{access}'", lib.code):
                function = lib.function_at(match.start())
                if function == "onPublishAgent":
                    self.assertTrue(node_start < match.start() < node_end, f"line {lib.line(match.start())}: outside its node")
                else:
                    # reportStatus posts from the controller; github() is the GitHub path that has always
                    # run on the last release environment's agent (the default publish agent).
                    self.assertIn((access, function), (("write", "reportStatus"), ("github", "github")),
                                  f"line {lib.line(match.start())}: {access} bound in {function}")
        self.assertEqual({name for name, calls in lib.calls.items() if "github" in calls and name in lib.functions},
                         {"release"}, "github() is only the old GitHub path's")
        # Every merge, tag, upload, release, branch deletion and GitHub Actions call runs in a closure given to
        # onPublishAgent (or is the old GitHub path's github() call).
        publish = lib.call_blocks("onPublishAgent")
        for start, _, quote, body in lib.strings:
            if body in ("merge-pr", "create-tag", "delete-branch", "publish-release.sh", "download-artifacts", "github.sh"):
                function = lib.function_at(start)
                if function in ("release", "githubLook") or (body == "github.sh" and function == "github"):
                    continue
                self.assertTrue(lib.inside(start, publish) or self.only_on_publish_agent(function, publish),
                                f"line {lib.line(start)}: '{body}' outside onPublishAgent")
        # The Windows files are downloaded and stashed there, and only unstashed on the release agent.
        start, end = lib.functions["windowsFiles"]
        body = lib.code[start:end]
        self.assertRegex(body, r"onPublishAgent\(ctx, false, true\)")
        self.assertLess(body.index("'download-artifacts'"), body.index("stashGlob("))

    def test_given_the_release_waits_when_read_then_each_is_outside_node_with_short_looks(self):
        lib = self.lib
        # The skip: exactly the design's words, an input outside any node, Abort failing the release.
        self.assertEqual(field_literal(lib, "SKIP_QUESTION"), ('"', "GitHub Actions checks didn't finish. Deploy without them?"))
        self.assertEqual(field_literal(lib, "SKIP_BUTTON"), ("'", "Deploy without GitHub Actions checks"))
        start, end = lib.functions["askToSkip"]
        self.assertRegex(lib.code[start:end], r"input id: 'SkipActions', message: SKIP_QUESTION, ok: SKIP_BUTTON")
        self.assertIn("rejected(interruption)", lib.code[start:end])
        for label, start, end in lib.agent_blocks():
            self.assertNotIn("askToSkip", lib.reachable(start, end), f"line {lib.line(start)}: {label}")
        # The Actions checks, the tag reaching GitHub and the Windows run: each a poll, never a blocking call
        # (wait-ref is asked with a timeout of 0: one look).
        start, end = lib.functions["waitForReleaseActions"]
        self.assertRegex(lib.code[start:end], r"pollGithub\(ctx, seconds, 'GitHub Actions', \['actions-status'")
        start, end = lib.functions["windowsFiles"]
        body = lib.code[start:end]
        self.assertRegex(body, r"pollGithub\([^\n]*\['wait-ref', ctx\.repo, \"refs/tags/\$\{tag\}\", '0'\]")
        self.assertRegex(body, r"pollGithub\([^\n]*\['run-status', ctx\.repo, run\]")
        self.assertNotIn("'wait-checks'", lib.code[slice(*lib.functions["releasePullRequest"])])
        start, end = lib.functions["publishRelease"]
        self.assertRegex(lib.code[start:end], r"pollGithub\([^\n]*\['wait-ref', ctx\.repo, \"refs/tags/\$\{tag\}\", '0'\]")

    def test_given_release_files_when_read_then_only_the_declared_globs_get_in_and_links_are_refused(self):
        lib = self.lib
        start, end = lib.functions["buildReleaseFiles"]
        body = lib.code[start:end]
        self.assertIn("List globs = (build.files ?: []) as List", body)
        self.assertLess(body.index("refuseLinks(false"), body.index("stashGlob("))
        self.assertRegex(lib.code[slice(*lib.functions["stashGlob"])], r"stash name: name, includes: glob, allowEmpty: false")
        start, end = lib.functions["windowsFiles"]
        body = lib.code[start:end]
        self.assertIn("List globs = windows.files ? (windows.files as List) : ['**']", body)
        self.assertLess(body.index("refuseLinks(true"), body.index("stashGlob("))
        # Gathered on the release agent from those stashes only, no link among them, and stashed as the set
        # publish-release.sh gets BEFORE the validation runs: the validation (repository code, root, the
        # whole workspace mounted) is pass or fail only and reads a copy, so nothing it adds or rewrites is
        # uploaded.
        start, end = lib.functions["gatherReleaseFiles"]
        body = lib.code[start:end]
        first = body.index("refuseLinks(true, 'the release files')")
        self.assertLess(body.index("unstash name"), first)
        stashed = body.index("stash name: 'release-files'")
        self.assertEqual(body.count("stash name:"), 1)
        self.assertLess(first, stashed)
        validated = body.index("run-check.sh release-validate")
        self.assertLess(stashed, validated)
        self.assertLess(body.index("cp -R release-files release-check"), validated)
        self.assertIn("printf 'RELEASE_FILES=%s\\\\n' \"$WORKSPACE/release-check\"", body)
        self.assertNotIn("$WORKSPACE/release-files", body)
        self.assertNotRegex(body[validated:], r"\bstash\b")
        # publish-release.sh gets a directory emptied first, holding only that stash, checked again.
        start, end = lib.functions["publishRelease"]
        body = lib.code[start:end]
        cleared = body.index("deleteDir()")
        self.assertLess(cleared, body.index("unstash files.stash"))
        self.assertLess(body.index("unstash files.stash"), body.index("refuseLinks(true, 'the release files')"))
        self.assertLess(body.index("refuseLinks(true, 'the release files')"), body.index("'publish-release.sh'"))
        self.assertIn('"${env.WORKSPACE}/release-files"]', body)
        # A glob is relative, with nothing the stash step would split on.
        glob = re.compile(field_pattern(lib, "RELEASE_GLOB"))
        for good in ("dist/*.tar.gz", "*/*.msi", "**", "out/app-1.0.0+linux.zip"):
            self.assertTrue(glob.fullmatch(good), good)
        for bad in ("a,b", "a b", "/etc/passwd", "-x", "dist/*.zip\n", ""):
            self.assertFalse(glob.fullmatch(bad), repr(bad))
        start, end = lib.functions["usableGlob"]
        self.assertIn("!glob.split('/').any { it in ['.', '..', ''] }", lib.code[start:end])
        self.assertIn("usableGlob", lib.calls["releaseProblem"])
        self.assertIn("usableGlob", lib.calls["nugetProblem"])

    def test_given_a_failing_validation_when_read_then_publish_release_is_never_reached(self):
        # The drafts are made by publish-release.sh: a validation that fails before it is called leaves
        # nothing published (and no draft at all).
        lib = self.lib
        start, end = lib.functions["gatherReleaseFiles"]
        body = lib.code[start:end]
        self.assertRegex(body, r"if \(code != 0\) \{\s*error \"release\.validate failed")
        self.assertNotIn("publish-release", body)
        start, end = lib.functions["releasePullRequest"]
        body = lib.code[start:end]
        self.assertLess(body.index("deployTo("), body.index("mergeAndTag("))
        self.assertLess(body.index("mergeAndTag("), body.index("releaseFromTag("))
        self.assertLess(body.index("releaseFromTag("), body.index("deleteReleaseBranch("))
        start, end = lib.functions["releaseFromTag"]
        body = lib.code[start:end]
        self.assertLess(body.index("releaseFiles("), body.index("publishRelease("))
        # The merge's title is the one release_notes.py leaves out of the notes.
        self.assertIn('"release: v${version} (#${env.CHANGE_ID})"', lib.code[slice(*lib.functions["mergeAndTag"])])
        notes = (ROOT / "scripts" / "release_notes.py").read_text(encoding="utf-8")
        self.assertIn('JENKINS_RELEASE_MERGE = re.compile(r"^release: v?\\d")', notes)

    def test_given_a_failure_after_the_merge_when_read_then_it_says_to_finish_with_release_resume(self):
        # Steps 4-5 are one function (releaseFromTag) the tag builds and the release-resume job (Task 12)
        # call too; a failure there, after the merge, says the release is merged and tagged and how to
        # finish it, whatever failed (an error, or an interruption, which stays one).
        lib = self.lib
        self.assertIsNotNone(re.search(r"Map releaseFromTag\(Map ctx, Map entry, Map releaseConfig, String tag, String sha, "
                                       r"Map opts\)", lib.code))
        start, end = lib.functions["releasePullRequest"]
        body = lib.code[start:end]
        merged = body.index("mergeAndTag(")
        attempt = body.index("try {", merged)
        self.assertLess(attempt, body.index("releaseFromTag(", merged))
        handled = body[body.index("releaseFromTag(", merged):body.index("deleteReleaseBranch(")]
        self.assertRegex(handled, r"catch \(FlowInterruptedException interruption\) \{\s*echo resumeNote\(ctx, tag\)\s*"
                                  r"throw interruption")
        self.assertRegex(handled, r"catch \(Exception problem\) \{\s*error ")
        self.assertIn('error "${failureText(problem)}\\n${resumeNote(ctx, tag)}"', handled)
        start, end = lib.functions["resumeNote"]
        self.assertIn("the pull request is merged and tagged ${tag}; finish the release with the release-resume job "
                      "(REPOSITORY=${ctx.app}, TAG=${tag})", lib.code[start:end])

    def test_given_a_failure_between_the_merge_and_the_tag_when_read_then_it_says_how_to_finish(self):
        lib = self.lib
        start, end = lib.functions["mergeAndTag"]
        body = lib.code[start:end]
        merged = body.index("'merge-pr'")
        guarded = body.index("try {", merged)
        self.assertLess(guarded, body.index("ensureTag(ctx, tag, commit)"))
        self.assertRegex(body, r"catch \(FlowInterruptedException interruption\) \{\s*echo untaggedNote\(ctx, tag, commit\)\s*"
                               r"throw interruption")
        self.assertIn('error "${failureText(problem)}\\n${untaggedNote(ctx, tag, commit)}"', body)
        start, end = lib.functions["untaggedNote"]
        self.assertIn("the pull request is merged as ${merge}, but not tagged; tag ${tag} on the merge commit ${merge}, then "
                      "finish the release with the release-resume job (REPOSITORY=${ctx.app}, TAG=${tag})", lib.code[start:end])
        # A failure without a message (a NullPointerException) still says what it was.
        start, end = lib.functions["failureText"]
        self.assertIn("problem.message ?: problem.toString()", lib.code[start:end])
        start, end = lib.functions["releasePullRequest"]
        self.assertNotIn("problem.message", lib.code[start:end])

    def test_given_a_skip_with_release_copies_when_read_then_github_gets_none_and_it_is_pending(self):
        # After a skip GitHub may be down: the release is published on the git server only (no
        # --mirror-releases), and the description and the log say the GitHub copy is pending.
        lib = self.lib
        start, end = lib.functions["releaseFromTag"]
        body = lib.code[start:end]
        self.assertIn("boolean githubPending = mirrorsReleases(ctx) && skipped", body)
        self.assertIn("publishRelease(ctx, entry, tag.replaceFirst('^v', ''), tag, sha, files, mirrorsReleases(ctx) && !skipped)", body)
        self.assertRegex(body, r"if \(githubPending\) \{\s*echo \"GitHub copy pending: ")
        start, end = lib.functions["publishRelease"]
        body = lib.code[start:end]
        self.assertNotIn("mirrorsReleases(", body)
        self.assertRegex(body, r"if \(mirrorReleases\) \{\s*arguments << '--mirror-releases'")
        start, end = lib.functions["releasePullRequest"]
        body = lib.code[start:end]
        self.assertIn("' (deployed without GitHub Actions checks; GitHub copy pending)'", body)
        # The Windows files missing with the GitHub copy pending: release-resume, not actions-release.
        self.assertRegex(body, r"String finish = published\.githubPending\s*\? \"The release-resume job "
                               r"\(REPOSITORY=\$\{ctx\.app\}, TAG=\$\{tag\}\) makes the GitHub copy and adds them\.\"")

    def test_given_a_skip_when_read_then_it_is_recorded_and_missing_files_make_the_build_unstable(self):
        lib = self.lib
        start, end = lib.functions["releasePullRequest"]
        body = lib.code[start:end]
        self.assertIn("String note = skipped ? \" (${SKIPPED})\" : ''", body)
        self.assertIn("deployTo(app, environment, version, state.sha, false, ctx.repo, [], null, note)", body)
        self.assertIn(": ' (deployed without GitHub Actions checks)'", body)
        self.assertIn("currentBuild.description = description", body)
        self.assertRegex(body, r"if \(published\.missing\) \{(\s*//[^\n]*)*\s*String finish = [^\n]*\n[^\n]*\n[^\n]*\n\s*unstable ")
        start, end = lib.functions["releaseFiles"]
        self.assertIn("skipped ? [stashes: null", lib.code[start:end])
        start, end = lib.functions["publishRelease"]
        self.assertIn("arguments += ['--missing', files.missing.join(',')]", lib.code[start:end])
        start, end = lib.functions["deleteReleaseBranch"]
        body = lib.code[start:end]
        self.assertRegex(body, r"if \(code == 3\) \{\s*echo ")
        self.assertEqual(field_literal(lib, "KEPT_BRANCH"),
                         ("'", "kept: release branches are protected on Forgejo; the owner deletes them"))

    def test_given_the_actions_release_job_when_read_then_it_checks_its_parameters_before_anything(self):
        lib = self.lib
        start, end = lib.functions["actionsRelease"]
        body = lib.code[start:end]
        context = body.index("catalogContext(app)")
        self.assertLess(context, body.index("globMatches(ctx.tags, tag)"))
        self.assertLess(body.index("globMatches(ctx.tags, tag)"), body.index("readReleaseTag(ctx, tag)"))
        # The tag read (it exists, it is on main) like every other release from a tag.
        self.assertLess(body.index("readReleaseTag(ctx, tag)"), body.index("windowsFiles("))
        self.assertIn("declares no release.windows", body)
        self.assertLess(body.index("windowsFiles("), body.index("['attach', ctx.repo, tag"))
        self.assertIn('lock(resource: "release-${app}")', body)
        entry = (ROOT / "jenkins" / "library" / "vars" / "yggdrasilActionsRelease.groovy").read_text(encoding="utf-8")
        self.assertIn("yggdrasilPipeline.actionsRelease()", entry)

    def test_given_actions_release_while_the_github_copy_is_pending_when_read_then_it_points_at_release_resume(self):
        # publish-release.sh attach exits 5 when GitHub has no release of the tag (after a skip): the files
        # are on the git server's release only, and the owner is told how the GitHub copy is made.
        start, end = self.lib.functions["actionsRelease"]
        body = self.lib.code[start:end]
        self.assertRegex(body, r"toolStatus\('publish-release\.sh', arguments, ")
        self.assertIn("code != 0 && code != 5", body)
        self.assertIn("pending: code == 5", body)
        self.assertRegex(body, r"if \(attached\.pending\) \{\s*currentBuild\.description = [^\n]*\n\s*"
                               r"unstable \"GitHub has no release of \$\{tag\} yet")
        self.assertIn("The release-resume job (REPOSITORY=${app}, TAG=${tag}) makes the GitHub copy, with them.", body)


class TagReleaseLibraryTests(unittest.TestCase):
    """Owner-tagged releases (a tag build), NuGet packages and the release-resume job: the tag checked to be
    on main before anything, one copy of the release logic, and the NuGet key bound only around the push."""

    def setUp(self):
        self.lib = library()

    def body(self, name):
        start, end = self.lib.functions[name]
        return self.lib.code[start:end]

    def test_given_a_tag_build_when_read_then_only_an_owner_tag_matching_its_pattern_releases(self):
        call = self.body("call")
        self.assertRegex(call, r"if \(env\.TAG_NAME\) \{\s*tagBuild\(ctx\)\s*return")
        body = self.body("tagBuild")
        owner = body.index("ctx.release != 'owner-tag'")
        pattern = body.index("globMatches(ctx.tags, tag)")
        lock = body.index('lock(resource: "release-${ctx.app}")')
        self.assertLess(owner, lock)
        self.assertLess(pattern, lock)
        self.assertLess(lock, body.index("readReleaseTag(ctx, tag)"))
        self.assertLess(body.index("readReleaseTag(ctx, tag)"), body.index("releaseTag(ctx, tag, described)"))
        self.assertIn("Nothing to do for the tag", body)
        # catalogContext says which path an entry is on.
        self.assertIn("release: application != null", self.body("catalogContext"))

    def test_given_a_tag_when_read_then_its_commit_must_be_on_main_before_anything_else(self):
        lib = self.lib
        body = self.body("readReleaseTag")
        # On the release agent, with the read token only.
        (label, start, end), = [b for b in lib.node_blocks() if lib.function_at(b[1]) == "readReleaseTag"]
        self.assertEqual(label, "'release'")
        self.assertIn("withForgeToken('read')", body)
        self.assertLess(body.index("'tag-sha'"), body.index("checkoutTag(ctx, tag, sha)"))
        main = body.index('merge-base --is-ancestor "$YGG_SHA" refs/remotes/origin/main')
        self.assertLess(body.index("checkoutTag(ctx, tag, sha)"), main)
        self.assertIn("the tag is not on main", body)
        self.assertLess(main, body.index("the tag is not on main"))
        self.assertLess(body.index("the tag is not on main"), body.index("describeCheckout(ctx)"))
        # Nothing of the repository runs while it is read: no run-check.sh, no release step, no key.
        for forbidden in ("packPackages", "pushPackages", "buildReleaseFiles", "withNugetKey", "withCheckCredentials"):
            self.assertNotIn(forbidden, lib.reachable(start, end))
        # And every path that releases from a tag reads it first.
        for name in ("tagBuild", "releaseResume", "actionsRelease"):
            self.assertIn("readReleaseTag", lib.calls[name], name)

    def test_given_the_release_logic_when_read_then_there_is_one_copy_of_it(self):
        lib = self.lib
        # releaseFromTag is called by the release pull request and by releaseTag, which the tag build and
        # release-resume share; publishRelease and the files only from releaseFromTag.
        callers = {name for name, calls in lib.calls.items() if "releaseFromTag" in calls and name != "releaseFromTag"}
        self.assertEqual(callers, {"releasePullRequest", "releaseTag"})
        callers = {name for name, calls in lib.calls.items() if "releaseTag" in calls and name != "releaseTag"}
        self.assertEqual(callers, {"tagBuild", "releaseResume"})
        for step in ("publishRelease", "releaseFiles", "publishPackages"):
            callers = {name for name, calls in lib.calls.items() if step in calls and name != step}
            self.assertEqual(callers, {"releaseFromTag"}, step)
        body = self.body("releaseFromTag")
        self.assertLess(body.index("publishPackages("), body.index("publishRelease("))
        # The release pull request passes no packages: applications have none.
        self.assertNotIn("nuget", self.body("releasePullRequest"))
        # A library declaring only publish.nuget is published, not released; anything else releases.
        self.assertIn("boolean releasing = releaseConfig != null || nuget == null", self.body("releaseTag"))
        self.assertIn("ctx.release == 'owner-tag'", self.body("releaseTag"))

    def test_given_release_files_and_packages_when_read_then_the_files_are_validated_before_any_push(self):
        # A NuGet push cannot be taken back: an entry declaring both release and publish.nuget builds and
        # validates its release files first (a failed validation is an error in gatherReleaseFiles, so
        # nothing after it runs), then packs and pushes, then publishes the release.
        body = self.body("releaseFromTag")
        files = body.index("releaseFiles(")
        packages = body.index("publishPackages(")
        self.assertLess(files, packages)
        self.assertLess(packages, body.index("publishRelease("))
        # Nothing in between can swallow a failure of the files.
        self.assertNotRegex(body[:packages], r"\btry\b|\bcatch\b")
        self.assertRegex(self.body("gatherReleaseFiles"), r"if \(code != 0\) \{\s*error \"release\.validate failed")
        self.assertIn("releasing && releaseConfig != null ? releaseFiles(", body)

    def test_given_packages_when_read_then_they_are_packed_without_any_credential_and_stashed_past_the_link_guard(self):
        lib = self.lib
        (label, start, end), = [b for b in lib.node_blocks() if lib.function_at(b[1]) == "packPackages"]
        self.assertEqual(label, "'release'")
        reached = lib.reachable(start, end)
        # The checkout's read token (withForgeToken) is the only binding there.
        for forbidden in ("withNugetKey", "withCheckCredentials", "onPublishAgent", "github"):
            self.assertNotIn(forbidden, lib.words_reached(start, end), forbidden)
        self.assertEqual({lib.function_at(m.start()) for m in re.finditer(r"\bwithCredentials\b", lib.masked)}
                         & {name for name in reached if name in lib.functions}, {"withForgeToken"})
        self.assertLessEqual(set(re.findall(r"withForgeToken\(\s*'(\w+)'", lib.code_reached(start, end, ("withForgeToken",)))),
                             {"read"})
        body = self.body("packPackages")
        self.assertIn("run-check.sh nuget-pack", body)
        self.assertLess(body.index("run-check.sh nuget-pack"), body.index("refuseLinks(false, 'the packages')"))
        self.assertLess(body.index("refuseLinks(false, 'the packages')"), body.index("stashGlob('nuget-packages'"))
        self.assertIn("nugetProblem", lib.calls["releaseTag"])

    def test_given_the_nuget_key_when_read_then_it_is_bound_only_around_nuget_push_on_the_release_agent(self):
        lib = self.lib
        self.assertEqual(field_literal(lib, "NUGET_CREDENTIAL"), ("'", "nuget-api-key"))
        # Called without parentheses (a closure only): every use of the word, but its definition, is there.
        uses = {lib.function_at(m.start()) for m in re.finditer(r"\bwithNugetKey\b", lib.masked)
                if not re.search(r"def\s+$", lib.masked[max(0, m.start() - 10):m.start()])}
        self.assertEqual(uses, {"pushPackages"})
        (label, start, end), = [b for b in lib.node_blocks() if lib.function_at(b[1]) == "pushPackages"]
        self.assertEqual(label, "'release'")
        # No repository code where the key is: no checkout of the repository, no run-check.sh, no build.
        reached = lib.reachable(start, end)
        for forbidden in ("checkoutCommit", "checkoutTag", "describeCheckout", "withCheckCredentials", "onPublishAgent"):
            self.assertNotIn(forbidden, reached, forbidden)
        code = lib.code[start:end]
        self.assertNotIn("run-check.sh", code)
        self.assertEqual(code.count("withNugetKey"), 1)
        # The packages from the stash only, no link among them, then nuget-push.sh inside the binding.
        self.assertLess(code.index("deleteDir()"), code.index("unstash 'nuget-packages'"))
        self.assertLess(code.index("unstash 'nuget-packages'"), code.index("refuseLinks(true, 'the packages')"))
        key = code.index("withNugetKey")
        self.assertLess(code.index("refuseLinks(true, 'the packages')"), key)
        self.assertIn("bash yggdrasil/scripts/nuget-push.sh", code[key:])
        self.assertIn('"$WORKSPACE/packages/**/*.nupkg"', code[key:])
        self.assertRegex(self.body("withNugetKey"), r"string\(credentialsId: NUGET_CREDENTIAL, variable: 'NUGET_API_KEY'\)")
        # A check (or a release build) never gets it, whatever the catalog says.
        self.assertIn("id == NUGET_CREDENTIAL", self.body("withCheckCredentials"))

    def test_given_the_release_resume_job_when_read_then_it_checks_its_parameters_then_releases_the_tag(self):
        body = self.body("releaseResume")
        context = body.index("catalogContext(app)")
        self.assertLess(context, body.index("ctx.release in ['deploy', 'owner-tag']"))
        self.assertLess(body.index("ctx.release in ['deploy', 'owner-tag']"), body.index("globMatches(ctx.tags, tag)"))
        lock = body.index('lock(resource: "release-${app}")')
        self.assertLess(body.index("globMatches(ctx.tags, tag)"), lock)
        self.assertLess(lock, body.index("readReleaseTag(ctx, tag)"))
        self.assertLess(body.index("readReleaseTag(ctx, tag)"), body.index("releaseTag(ctx, tag, described)"))
        self.assertNotIn("node(", body)
        entry = (ROOT / "jenkins" / "library" / "vars" / "yggdrasilReleaseResume.groovy").read_text(encoding="utf-8")
        self.assertRegex(entry, r"def call\(\) \{\s*yggdrasilPipeline\.releaseResume\(\)\s*\}")


if __name__ == "__main__":
    unittest.main()
