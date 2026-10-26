"""The opencode provider: its command line, its config, its parsing, and the CLI. No model calls.

fixtures/opencode_*.jsonl are real outputs of `opencode run --format json` (opencode 1.19.3)
against a scripted local model (see test_opencode_real.py), with temp paths renamed:
ok         the worker: a write outside the folder (denied), a read, a write, then its text
judge      the judge: write, edit and apply_patch (not offered), then a command of its own, a read
           and `git status` (allowed), then its verdict
error      the model answered HTTP 400
The `opencode` these tests start is a fake: a Python script that records how it was called.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fake import fake_opencode, gone, opencode_match
from subgoal.cli import main
from subgoal.opencode import AGENT, JUDGE_RULES, WORKER_RULES, OpenCode, parse
from subgoal.provider import ProviderError

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
OK, JUDGE, ERROR = ((FIXTURES / f"opencode_{name}.jsonl").read_text() for name in ("ok", "judge", "error"))


def decide(permission, pattern, rules=JUDGE_RULES):
    """opencode's own rule (docs: Permissions): the last rule that matches both wins."""
    flat = [(key, pat, action) for key, value in rules.items()
            for pat, action in (value.items() if isinstance(value, dict) else [("*", value)])]
    return [action for key, pat, action in flat
            if opencode_match(permission, key) and opencode_match(pattern, pat)][-1]


def step(text, tokens=110, cost=0):
    """One step of `opencode run --format json` that says text."""
    events = [{"type": "step_start", "sessionID": "ses_x", "part": {"type": "step-start"}},
              {"type": "text", "sessionID": "ses_x", "part": {"type": "text", "text": text}},
              {"type": "step_finish", "sessionID": "ses_x", "part": {
                  "type": "step-finish", "cost": cost, "tokens": {
                      "total": tokens, "input": tokens - 10, "output": 10, "reasoning": 0,
                      "cache": {"read": 0, "write": 0}}}}]
    return "".join(json.dumps(event) + "\n" for event in events)


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        self.bin, self.dir = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "app.txt").write_text("todo\n")

    def fake(self, replies):
        return fake_opencode(self.bin, "opencode", replies)

    def calls(self):
        return json.loads((self.bin / "calls.json").read_text())

    def subgoal(self, *argv):
        """`python -m subgoal --provider opencode` with only the fake opencode on PATH."""
        env = dict(os.environ, PATH=os.pathsep.join([str(self.bin), "/usr/bin", "/bin"]))
        done = subprocess.run([sys.executable, "-m", "subgoal", "--provider", "opencode", "--dir", str(self.dir),
                               "--goal", "Make app.txt say done.", "--max-iterations", "1", *argv],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout + done.stderr

    def test_O1_worker_argv(self):
        provider = OpenCode(model="mock/scripted", allow=["Bash(make test)"], executable=self.fake([{"stdout": OK}]))
        provider.run("THE PROMPT", self.dir, 60)
        (call,) = self.calls()
        self.assertEqual(call["argv"], ["run", "--format", "json", "--model", "mock/scripted", "--dir", str(self.dir)])
        self.assertEqual((call["stdin"], Path(call["cwd"]).resolve()), ("THE PROMPT", self.dir.resolve()))
        self.assertEqual(json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"]), {"default_agent": AGENT, "agent": {AGENT: {
            "mode": "primary", "permission": WORKER_RULES}}})
        self.assertEqual(WORKER_RULES, {"external_directory": "deny", "doom_loop": "deny"})   # never "ask"
        self.assertEqual(provider.argv[:2], ["env", "OPENCODE_CONFIG_CONTENT=" + call["env"]["OPENCODE_CONFIG_CONTENT"]])
        self.assertNotIn("--model", OpenCode().argv)
        for unsafe in ("--continue", "--session", "--fork", "--auto", "--yolo", "dangerously", "Bash(make test)"):
            self.assertNotIn(unsafe, " ".join(provider.argv) + provider.describe())
        self.assertNotIn("no edit tools", provider.describe())
        self.assertIn("--dir DIR  (prompt on stdin)", provider.describe())

    def test_O1b_judge_argv_and_rules(self):
        provider = OpenCode(allow=["Edit", "Bash(rm -rf *)"], executable=self.fake([{"stdout": JUDGE}]),
                            edit=False)
        provider.run("THE PROMPT", self.dir, 60)
        (call,) = self.calls()
        self.assertEqual(call["argv"], ["run", "--format", "json", "--pure", "--dir", str(self.dir)])
        self.assertEqual(json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])["agent"][AGENT]["permission"], JUDGE_RULES)
        self.assertEqual(list(JUDGE_RULES)[0], "*")                         # deny all first, then allow
        for tool in ("edit", "write", "apply_patch", "patch", "multiedit", "task", "webfetch", "todowrite",
                     "skill", "external_directory", "a_mcp_tool"):
            self.assertEqual(decide(tool, "app.txt"), "deny", tool)         # no edit tool, nothing else
        for tool in ("read", "glob", "grep"):
            self.assertEqual(decide(tool, "app.txt"), "allow", tool)
        for command in ("git status", "git diff HEAD~1", "python3 -m unittest -q", "npm test", "./check.sh",
                        "node build.js", "grep -c done app.txt", "make test > build.log"):
            self.assertEqual(decide("bash", command), "allow", command)      # it may run commands
        self.assertIn("no edit tools: it may run commands", provider.describe())
        self.assertNotIn("Edit", " ".join(provider.argv))                   # the caller's rules never widen it

    def test_O2_parse_ok(self):
        reply = parse(0, OK, "")
        self.assertEqual(reply.text, "I made app.txt say done.")
        self.assertEqual(reply.tokens, 4 * 110)                 # four steps: input + output + reasoning + cache
        self.assertIsNone(reply.cost_usd)                       # the mock model has no price: unknown, not $0
        self.assertTrue(reply.session.startswith("ses_"))
        judged = parse(0, JUDGE, "")
        self.assertEqual((judged.text, judged.tokens), ("app.txt says done.\nVERDICT: met", 7 * 110))
        later = parse(0, step("Let me look.", cost=0.25) + step("Done.", cost=0.25), "")
        self.assertEqual((later.text, later.tokens, later.cost_usd), ("Done.", 220, 0.5))   # the last step's words

    def test_O3_error_event(self):
        with self.assertRaisesRegex(ProviderError, r"exit 1\): scripted failure$"):
            parse(1, ERROR, "")
        with self.assertRaisesRegex(ProviderError, "scripted failure"):     # an error event fails even with exit 0
            parse(0, step("partial") + ERROR, "")

    def test_O4_unusable_output(self):
        with self.assertRaisesRegex(ProviderError, "not JSON events.*boom"):
            parse(0, "this is not JSON", "boom")
        with self.assertRaisesRegex(ProviderError, "not JSON events"):
            parse(0, step("hi") + "[1, 2]\n", "")
        with self.assertRaisesRegex(ProviderError, r"exit 1\): please log in"):
            parse(1, "", "please log in")
        with self.assertRaisesRegex(ProviderError, r"exit 2\): .*bad flag"):
            parse(2, step("hi"), "bad flag")
        with self.assertRaisesRegex(ProviderError, r"exit 0\): no final text"):
            parse(0, OK + step("   "), "a warning")                    # the last step had no words
        with self.assertRaisesRegex(ProviderError, "no final text"):
            parse(0, "", "")

    def test_O5_executable_not_found(self):
        with self.assertRaisesRegex(ProviderError, "no-such-opencode not found"):
            OpenCode(executable="no-such-opencode").run("hi", self.dir, 5)

    def test_O5b_opencode_2_is_named(self):
        said = "\nERROR\n  Unrecognized flag: --dir in command opencode run\n"     # opencode 2.1.4's words
        provider = OpenCode(executable=self.fake([{"stdout": "DESCRIPTION\n  Run OpenCode with a message\n",
                                                   "stderr": said, "exit": 1}]))
        with self.assertRaisesRegex(ProviderError, "looks like opencode 2.*--provider opencode2"):
            provider.run("hi", self.dir, 30)

    def test_O6_timeout_stops_the_child(self):
        provider = OpenCode(executable=self.fake([{"sleep": 30}]))
        start = time.monotonic()
        with self.assertRaisesRegex(ProviderError, "no reply within"):
            provider.run("hi", self.dir, 0.5)
        self.assertLess(time.monotonic() - start, 10)    # the child sleeps 30 s: stopped, with room for a busy machine
        self.assertTrue(gone(str(self.bin)))

    def test_O7_cli_with_a_check(self):
        self.fake([{"write": {"app.txt": "done\n"}, "stdout": OK}])
        code, out = self.subgoal("--check", "grep -q done app.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: the check exited 0", out)
        self.assertIn("tokens    440 (", out)
        self.assertIn("worker's last words (unverified): \"I made app.txt say done.\"", out)
        (call,) = self.calls()
        self.assertIn("grep -q done app.txt", call["stdin"])
        self.assertNotIn("--pure", call["argv"])

    def test_O8_cli_with_a_judge(self):
        self.fake([{"write": {"app.txt": "done\n"}, "stdout": OK}, {"stdout": JUDGE}])
        code, out = self.subgoal("--no-plan")
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: a judge said met", out)
        self.assertIn("tokens    1,210 (", out)                         # the worker's 440 and the judge's 770
        worker, judge = self.calls()
        self.assertTrue(judge["stdin"].startswith("You are a reviewer"))
        self.assertIn("--pure", judge["argv"])
        self.assertEqual(json.loads(judge["env"]["OPENCODE_CONFIG_CONTENT"])["agent"][AGENT]["permission"], JUDGE_RULES)
        self.assertEqual(json.loads(worker["env"]["OPENCODE_CONFIG_CONTENT"])["agent"][AGENT]["permission"], WORKER_RULES)
        self.assertEqual((self.dir / "app.txt").read_text(), "done\n")

    def test_O9_cli_surface(self):
        def cli(*argv):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                try:
                    return main(list(argv)), out.getvalue()
                except SystemExit as stop:
                    return stop.code, out.getvalue()
        here = ["--dir", str(self.dir), "--goal", "g", "--provider", "opencode"]
        code, out = cli(*here, "--dry-run", "--model", "mock/scripted")
        self.assertEqual(code, 0)
        self.assertIn(f"provider: {OpenCode(model='mock/scripted').describe()}\n", out)
        self.assertIn(f"judge: {OpenCode(model='mock/scripted', edit=False).describe()}\n", out)
        self.assertEqual(cli(*here, "--check", "true", "--allow", "Edit")[0], 2)    # Claude Code rules
        self.assertEqual(cli(*here, "--check", "true", "--allow", "Edit", "--provider", "claude-code",
                             "--dry-run")[0], 0)
        self.assertEqual(list(self.dir.iterdir()), [self.dir / "app.txt"])
        shown = subprocess.run([sys.executable, "-m", "subgoal", "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertRegex(shown.stdout, r"--provider \{claude-code,opencode[,}]")


if __name__ == "__main__":
    unittest.main()
