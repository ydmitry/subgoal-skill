"""The Claude Code provider: its command line and its parsing. No model calls.

fixtures/claude_ok.json and fixtures/claude_error.json are real outputs of one tiny
`claude -p --output-format json` call each, pretty-printed, with the session id removed
and the message uuid replaced by zeros.
The error one is real too: a call made with an impossibly small --max-budget-usd.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fake import gone
from subgoal import claude_code
from subgoal.claude_code import READ_ONLY_ALLOW, READ_ONLY_DENY, ClaudeCode, parse
from subgoal.provider import ProviderError

FIXTURES = Path(__file__).parent / "fixtures"
OK = (FIXTURES / "claude_ok.json").read_text()


class ClaudeCodeTest(unittest.TestCase):
    def test_C1_argv(self):
        seen = []

        def fake_run_process(cmd, cwd, timeout_s, stdin=None):
            seen.append((cmd, stdin))
            return 0, OK, "", False
        provider = ClaudeCode(model="claude-haiku-5-5", allow=["Bash(make test)", "Edit"])
        with mock.patch.object(claude_code, "run_process", fake_run_process):
            provider.run("THE PROMPT", Path("."), 60)
        argv, stdin = seen[0]
        self.assertEqual(argv, ["claude", "-p", "--output-format", "json",
                                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                                "--model", "claude-haiku-5-5",
                                "--allowedTools", "Bash(make test)", "Edit"])
        self.assertEqual(stdin, "THE PROMPT")
        self.assertNotIn("--model", ClaudeCode().argv)
        for unsafe in ("dangerously-skip-permissions", "bypassPermissions"):     # safety regression
            self.assertNotIn(unsafe, " ".join(argv) + provider.describe())

    def test_C1b_read_only_argv(self):
        seen = []

        def fake_run_process(cmd, cwd, timeout_s, stdin=None):
            seen.append(cmd)
            return 0, OK, "", False
        provider = ClaudeCode(model="claude-haiku-5-5", allow=["Edit", "Bash(rm -rf *)"], read_only=True)
        with mock.patch.object(claude_code, "run_process", fake_run_process):
            provider.run("THE PROMPT", Path("."), 60)
        argv = seen[0]
        self.assertEqual(argv, ["claude", "-p", "--output-format", "json",
                                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                                "--tools", "Read,Grep,Glob,Bash", "--strict-mcp-config",
                                "--disallowedTools", *READ_ONLY_DENY,
                                "--model", "claude-haiku-5-5", "--allowedTools", *READ_ONLY_ALLOW])
        self.assertEqual(READ_ONLY_DENY, ("Bash(*--output*)",))        # git's own way to write a file
        for edit_ish in ("acceptEdits", "Edit", "Write", "NotebookEdit", "Bash(rm -rf *)"):
            self.assertNotIn(edit_ish, argv)                           # the caller's rules never widen it
        self.assertEqual(set(argv[argv.index("--tools") + 1].split(",")), {"Read", "Grep", "Glob", "Bash"})
        for rule in READ_ONLY_ALLOW:
            self.assertRegex(rule, r"^(Read|Grep|Glob|Bash\(git (status|diff|log|show) \*\))$")
        for unsafe in ("dangerously-skip-permissions", "bypassPermissions"):
            self.assertNotIn(unsafe, " ".join(argv) + provider.describe())
        self.assertIn("read-only", provider.describe())
        self.assertNotIn("read-only", ClaudeCode().describe())

    def test_C2_parse_ok(self):
        self.assertIsNone(parse(0, OK, "").session)       # the fixture has no session id: unknown
        data = dict(json.loads(OK), **{claude_code.RESUME_ID_KEY: "abc-123"})
        reply = parse(0, json.dumps(data), "")
        self.assertEqual((reply.text, reply.session), ("ok", "abc-123"))
        self.assertEqual(reply.tokens, 2 + 93 + 20233 + 11453)   # input, output, cache writes, reads
        self.assertAlmostEqual(reply.cost_usd, 0.00420783)

    def test_C3_parse_error_result(self):
        with self.assertRaisesRegex(ProviderError, r"exit 1\): Reached maximum budget"):
            parse(1, (FIXTURES / "claude_error.json").read_text(), "")

    def test_C4_not_json_or_non_zero_exit(self):
        with self.assertRaisesRegex(ProviderError, "boom"):
            parse(0, "this is not JSON", "boom")
        with self.assertRaisesRegex(ProviderError, "please log in"):
            parse(1, "", "please log in")

    def test_C5_executable_not_found(self):
        with self.assertRaisesRegex(ProviderError, "not found"):
            ClaudeCode(executable="no-such-binary").run("hi", Path(tempfile.gettempdir()), 5)

    def test_C6_timeout_stops_the_child(self):
        folder = Path(tempfile.mkdtemp())
        sleeper = folder / "claude"
        sleeper.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        sleeper.chmod(0o755)
        start = time.monotonic()
        with self.assertRaisesRegex(ProviderError, "no reply within"):
            ClaudeCode(executable=str(sleeper)).run("hi", folder, 0.5)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(gone(str(sleeper)))

if __name__ == "__main__":
    unittest.main()
