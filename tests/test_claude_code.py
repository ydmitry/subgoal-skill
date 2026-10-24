"""The Claude Code provider: its command line and its parsing. No model calls.

fixtures/claude_ok.jsonl and fixtures/claude_error.jsonl are real outputs of one tiny
`claude -p --output-format stream-json --verbose` call each (claude-haiku-5-5, Claude Code 2.1.310).
The ok one was asked to read two files, then use a tiny project skill to write hello.txt.
The error one is real too: a call made with an impossibly small --max-budget-usd.
Cleaned for the repo: the session ids removed, the uuids replaced by zeros, the thinking
signatures emptied, the temp folder replaced by /tmp, the init event cut to a few fields, and
the hook, rate-limit and thinking-token events left out.
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
from subgoal.claude_code import JUDGE_TOOLS, TRACE_CAP, ClaudeCode, parse, tool_trace
from subgoal.provider import ProviderError

FIXTURES = Path(__file__).parent / "fixtures"
OK = (FIXTURES / "claude_ok.jsonl").read_text()
RESULT = [json.loads(line) for line in OK.splitlines() if '"type": "result"' in line][-1]


def tool_calls(*calls):
    """A stream of assistant events, one per (name, input) tool call, each followed by its result."""
    events = []
    for i, (name, args) in enumerate(calls):
        events += [{"type": "assistant", "message": {"content": [
                       {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}]}},
                   {"type": "user", "message": {"content": [
                       {"type": "tool_result", "tool_use_id": f"t{i}", "content": "SECRET RESULT"}]}}]
    return events


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
        self.assertEqual(argv, ["claude", "-p", "--output-format", "stream-json", "--verbose",
                                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                                "--model", "claude-haiku-5-5",
                                "--allowedTools", "Bash(make test)", "Edit"])
        self.assertEqual(stdin, "THE PROMPT")
        self.assertNotIn("--model", ClaudeCode().argv)
        for unsafe in ("dangerously-skip-permissions", "bypassPermissions"):     # safety regression
            self.assertNotIn(unsafe, " ".join(argv) + provider.describe())

    def test_C1b_judge_argv(self):
        seen = []

        def fake_run_process(cmd, cwd, timeout_s, stdin=None):
            seen.append(cmd)
            return 0, OK, "", False
        provider = ClaudeCode(model="claude-haiku-5-5", allow=["Edit", "Bash(rm -rf *)"], edit=False)
        with mock.patch.object(claude_code, "run_process", fake_run_process):
            provider.run("THE PROMPT", Path("."), 60)
        argv = seen[0]
        self.assertEqual(argv, ["claude", "-p", "--output-format", "stream-json", "--verbose",
                                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                                "--tools", "Read,Grep,Glob,Bash", "--strict-mcp-config",
                                "--model", "claude-haiku-5-5", "--allowedTools", *JUDGE_TOOLS])
        tools = set(argv[argv.index("--tools") + 1].split(","))
        self.assertEqual(tools, {"Read", "Grep", "Glob", "Bash"})                # no edit tool exists
        for edit_ish in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            self.assertNotIn(edit_ish, tools)
        allowed = argv[argv.index("--allowedTools") + 1:]
        self.assertIn("Bash", allowed)                                         # any command in the folder
        for edit_ish in ("acceptEdits", "Edit", "Write", "NotebookEdit", "Bash(rm -rf *)"):
            self.assertNotIn(edit_ish, argv)                                   # the caller's rules never widen it
        self.assertIn("--strict-mcp-config", argv)                             # no MCP tools
        for unsafe in ("dangerously-skip-permissions", "bypassPermissions"):
            self.assertNotIn(unsafe, " ".join(argv) + provider.describe())
        self.assertIn("no edit tools: it may run commands", provider.describe())
        self.assertNotIn("no edit tools", ClaudeCode().describe())

    def test_C2_parse_ok(self):
        reply = parse(0, OK, "")
        self.assertEqual((reply.text, reply.session), ("done", None))  # the fixture has no session id: unknown
        self.assertEqual(reply.tokens, 10 + 819 + 21757 + 142464)    # input, output, cache writes, reads
        self.assertAlmostEqual(reply.cost_usd, 0.00618654)
        self.assertEqual(reply.trace, "Read x2\nSkill tiny\nBash ls\nWrite /tmp/hello.txt\nBash cat hello.txt")
        for leaked in ("alpha", "beta", "Before writing any file", "File created", "hi\n"):
            self.assertNotIn(leaked, reply.trace)                      # results and contents stay out
        with_id = OK.replace('"type": "result"', f'"{claude_code.RESUME_ID_KEY}": "abc-123", "type": "result"')
        self.assertEqual(parse(0, with_id, "").session, "abc-123")

    def test_C2b_the_old_single_json_object_still_parses(self):
        for old in (json.dumps(RESULT), json.dumps(RESULT, indent=2)):   # --output-format json, as before
            reply = parse(0, old, "")
            self.assertEqual((reply.text, reply.tokens, reply.trace), ("done", 10 + 819 + 21757 + 142464, None))

    def test_C2c_trace_lines(self):
        events = tool_calls(("Skill", {"skill": "tiny", "args": "x"}), ("Glob", {"pattern": "*"}),
                            ("Grep", {"pattern": "p"}), ("Read", {"file_path": "/a"}),
                            ("Task", {"description": "look around", "prompt": "SECRET PROMPT"}),
                            ("Agent", {"description": "review it"}), ("Read", {"file_path": "/b"}),
                            ("Edit", {"file_path": "/a", "old_string": "SECRET", "new_string": "SECRET"}),
                            ("Write", {"file_path": "/b", "content": "SECRET"}),
                            ("Bash", {"command": "echo   one\n" + "x" * 200, "description": "SECRET"}),
                            ("WebFetch", {"url": "https://example.com"}), ("Read", {}), ("Grep", {}))
        events.insert(1, {"type": "system", "subtype": "init"})          # other events count for nothing
        self.assertEqual(tool_trace(events).splitlines(), [
            "Skill tiny", "Read x3", "Task look around", "Agent review it", "Read x1", "Edit /a", "Write /b",
            "Bash echo one " + "x" * 91, "WebFetch", "Read x2"])
        deep = "/" + "d/" * 80 + "PLAN.md"                              # a path cut from the front would lose its file name
        trace = tool_trace(tool_calls(("Write", {"file_path": deep}), ("Bash", {"command": "ls " + "z" * 300}))).splitlines()
        self.assertEqual((trace[0], len(trace[1])), ("Write " + deep[-100:], 105))
        self.assertTrue(trace[0].endswith("/PLAN.md"))
        self.assertEqual(tool_trace([]), "")
        self.assertEqual(parse(0, OK.replace('"tool_use"', '"no_tool"'), "").trace, "")   # no calls: empty, not None

    def test_C2d_trace_cap(self):
        lines = [f"Bash echo {i:04d} " + "x" * 30 for i in range(300)]
        trace = tool_trace(tool_calls(*[("Bash", {"command": line[5:]}) for line in lines]))
        self.assertLess(len(trace), TRACE_CAP + 100)
        head, gap, tail = trace.partition("\n... ")
        left_out = int(gap and tail.split(" ", 1)[0])
        tail = tail.split(" lines left out ...\n", 1)[1]
        self.assertEqual(head.splitlines(), lines[:len(head.splitlines())])          # the first half, whole lines
        self.assertEqual(tail.splitlines(), lines[-len(tail.splitlines()):])          # the last half
        self.assertEqual(len(head.splitlines()) + left_out + len(tail.splitlines()), 300)
        self.assertGreater(len(head), TRACE_CAP // 2 - 50)
        self.assertGreater(len(tail), TRACE_CAP // 2 - 50)
        for long in ([("Write", {"file_path": "/" + "p" * 9000})], [("Task", {"description": "d" * 3000})] * 2,
                     [("T" * 9000, {})]):                         # one long argument or name cannot fill it
            trace = tool_trace(tool_calls(*long)).splitlines()
            self.assertEqual(len(trace), len(long))                 # whole, nothing left out
            self.assertTrue(all(len(line) <= 201 for line in trace))
        many = tool_trace(tool_calls(*[("Write", {"file_path": f"/{i:04d}" + "p" * 9000}) for i in range(100)]))
        self.assertRegex(many, r"\n\.\.\. [1-9]\d* lines left out \.\.\.\n")

    def test_C3_parse_error_result(self):
        with self.assertRaisesRegex(ProviderError, r"exit 1\): Reached maximum budget \(\$0.0001\)"):
            parse(1, (FIXTURES / "claude_error.jsonl").read_text(), "")
        is_error = OK.replace('"is_error": false', '"is_error": true')
        with self.assertRaisesRegex(ProviderError, r"exit 0\): done"):         # is_error, even with exit 0
            parse(0, is_error, "")
        with self.assertRaisesRegex(ProviderError, r"exit 2\): done"):         # a non-zero exit, even with a result
            parse(2, OK, "")

    def test_C4_not_json_or_non_zero_exit(self):
        with self.assertRaisesRegex(ProviderError, "boom"):
            parse(0, "this is not JSON", "boom")
        with self.assertRaisesRegex(ProviderError, "please log in"):
            parse(1, "", "please log in")
        with self.assertRaisesRegex(ProviderError, r"exit 0\): no output"):
            parse(0, "", "")
        no_result = "\n".join(line for line in OK.splitlines() if '"type": "result"' not in line)
        for code, stream, said in ((0, no_result, r"no result event in \d+ stream events"),
                                   (1, OK.replace('"result": "done"', '"result": ""'), "its result event gave no detail")):
            with self.assertRaisesRegex(ProviderError, rf"exit {code}\): {said}$") as raised:
                parse(code, stream, "")                                    # a stream cut before its result
            for leaked in ("alpha", "beta", "tool_result"):
                self.assertNotIn(leaked, str(raised.exception))            # never what the tools returned
        with self.assertRaisesRegex(ProviderError, "exit 1\): cut off"):  # stderr still comes first
            parse(1, no_result, "cut off")
        reply = parse(0, "not JSON\n[1, 2]\n" + OK + "garbage {\n", "")          # stray lines are skipped
        self.assertEqual((reply.text, reply.trace.splitlines()[0]), ("done", "Read x2"))

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
        self.assertLess(time.monotonic() - start, 10)    # the child sleeps 30 s: stopped, with room for a busy machine
        self.assertTrue(gone(str(sleeper)))

if __name__ == "__main__":
    unittest.main()
