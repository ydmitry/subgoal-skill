"""The opencode2 provider: its command line, environment, config, parsing, and the CLI. No model calls.

fixtures/opencode2_*.jsonl are real outputs of `opencode run --standalone --format json`
(opencode 2.1.4) against a scripted local model (see test_opencode2_real.py), with temp paths
renamed. opencode 2.1.4 stops reading events once the session is idle, so the last step's
step_finish (its tokens and cost) is usually missing:
ok         the worker: a write outside the folder (denied), a read, a write, then its text
read_only  the judge: write, edit and patch (not offered), a redirect, `git diff --output`
           and `git status && touch` (denied), a read and `git status` (allowed), then "not met"
error      the model answered HTTP 400
The `opencode2` these tests start is a fake: a Python script that records how it was called.
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
from unittest import mock

from fake import fake_opencode, gone, opencode_match
from subgoal.cli import main
from subgoal.opencode2 import AGENT, READ_ONLY_RULES, WORKER_RULES, OpenCode2
from subgoal.opencode import parse
from subgoal.provider import ProviderError

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
OK, READ_ONLY, ERROR = ((FIXTURES / f"opencode2_{name}.jsonl").read_text() for name in ("ok", "read_only", "error"))
FLAGS = ["run", "--standalone", "--format", "json", "--agent", AGENT]
BASE = {"action": "*", "resource": "*", "effect": "allow"}    # opencode 2's base policy (docs: Permissions)


def decide(action, resource, rules=READ_ONLY_RULES):
    """opencode 2's own rule (docs: Permissions): the last rule that matches both wins."""
    return [r["effect"] for r in rules
            if opencode_match(action, r["action"]) and opencode_match(resource, r["resource"])][-1]


class OpenCode2Test(unittest.TestCase):
    def setUp(self):
        self.bin, self.dir = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "app.txt").write_text("todo\n")

    def fake(self, replies):
        return fake_opencode(self.bin, "opencode2", replies)

    def calls(self):
        return json.loads((self.bin / "calls.json").read_text())

    def subgoal(self, *argv):
        """`python -m subgoal --provider opencode2` with only the fake on PATH, and a $PWD that is not DIR."""
        env = dict(os.environ, PATH=os.pathsep.join([str(self.bin), "/usr/bin", "/bin"]), PWD=str(ROOT))
        done = subprocess.run([sys.executable, "-m", "subgoal", "--provider", "opencode2", "--dir", str(self.dir),
                               "--goal", "Make app.txt say done.", "--max-iterations", "1", *argv],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout + done.stderr

    def test_P1_worker_argv_and_environment(self):
        provider = OpenCode2(model="mock/scripted", allow=["Bash(make test)"], executable=self.fake([{"stdout": OK}]))
        with mock.patch.dict(os.environ, PWD=str(ROOT)):        # the caller's folder, not DIR
            provider.run("THE PROMPT", self.dir, 60)
        (call,) = self.calls()
        self.assertEqual(call["argv"], [*FLAGS, "--model", "mock/scripted"])
        self.assertEqual((call["stdin"], Path(call["cwd"]).resolve()), ("THE PROMPT", self.dir.resolve()))
        self.assertNotIn("PWD", call["env"])                 # else opencode would work in the caller's folder
        self.assertNotIn("OPENCODE_DISABLE_PROJECT_CONFIG", call["env"])
        self.assertEqual(json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"]),
                         {"agents": {AGENT: {"mode": "primary", "permissions": WORKER_RULES}}})
        self.assertEqual(decide("external_directory", "/tmp/elsewhere/*", WORKER_RULES), "deny")
        for command in ("ls", "grep -q done app.txt", "python3 -m unittest -q tests/test_app.py", "./check.sh",
                        "git diff HEAD~1..HEAD", "make test > build.log"):
            self.assertEqual(decide("shell", command, [BASE, *WORKER_RULES]), "allow", command)
        for command in ("cat /etc/hosts", "touch /tmp/x-outside", "echo x > /tmp/x", "echo x >/tmp/x",
                        "cp app.txt ../x", "cat ~/.ssh/config", "git diff --output=/tmp/x", 'cat "/etc/hosts"',
                        "cat '/etc/hosts'"):
            self.assertEqual(decide("shell", command, WORKER_RULES), "deny", command)
        self.assertNotIn("--model", OpenCode2().argv)
        for unsafe in ("--continue", "--session", "--fork", "--auto", "--server", "--yolo", "dangerously",
                       "Bash(make test)"):
            self.assertNotIn(unsafe, " ".join(provider.argv) + provider.describe())
        self.assertNotIn("read-only", provider.describe())
        self.assertIn("(run in DIR, prompt on stdin)", provider.describe())

    def test_P1b_read_only_argv_and_rules(self):
        provider = OpenCode2(allow=["Edit"], executable=self.fake([{"stdout": READ_ONLY}]), read_only=True)
        provider.run("THE PROMPT", self.dir, 60)
        (call,) = self.calls()
        self.assertEqual(call["argv"], FLAGS)
        self.assertEqual(call["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")    # no plugins from DIR
        self.assertEqual(json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])["agents"][AGENT]["permissions"],
                         READ_ONLY_RULES)
        self.assertEqual(READ_ONLY_RULES[0], {"action": "*", "resource": "*", "effect": "deny"})
        for action in ("edit", "shell", "execute", "subagent", "skill", "webfetch", "websearch", "question",
                       "external_directory", "a_mcp_tool"):
            self.assertEqual(decide(action, "app.txt"), "deny", action)
        for action in ("read", "glob", "grep"):
            self.assertEqual(decide(action, "app.txt"), "allow", action)
        for command in ("git status", "git status --short", "git diff HEAD~1", "git log -p -3", "git show HEAD"):
            self.assertEqual(decide("shell", command), "allow", command)
        for command in ("git status > app.txt", "git log >> notes.txt", "git diff --output=app.txt",
                        "git diff --output app.txt", "touch judge.txt", "rm app.txt", "git commit -m x",
                        "git -C /tmp diff", "cat app.txt", "git checkout ."):
            self.assertEqual(decide("shell", command), "deny", command)
        self.assertIn("read-only", provider.describe())
        self.assertNotIn("Edit", " ".join(provider.argv))                   # the caller's rules never widen it

    def test_P2_parse_real_output(self):
        reply = parse(0, OK, "")
        self.assertEqual(reply.text, "I made app.txt say done.")
        self.assertEqual(reply.tokens, 3 * 110)              # four steps, the last one's tokens not reported
        self.assertIsNone(reply.cost_usd)                    # the mock model has no price: unknown, not $0
        self.assertTrue(reply.session.startswith("ses_"))
        judged = parse(0, READ_ONLY, "")
        self.assertEqual((judged.text, judged.tokens), ("app.txt says todo.\nVERDICT: not met", 8 * 110))

    def test_P3_error_event(self):
        with self.assertRaisesRegex(ProviderError, r"exit 1\): scripted failure$"):
            parse(1, ERROR, "")

    def test_P4_failures(self):
        with self.assertRaisesRegex(ProviderError, "no-such-opencode not found"):
            OpenCode2(executable="no-such-opencode").run("hi", self.dir, 5)
        provider = OpenCode2(executable=self.fake([{"stderr": "Error: Agent not found", "exit": 1}]))
        with self.assertRaisesRegex(ProviderError, r"exit 1\): Error: Agent not found"):
            provider.run("hi", self.dir, 30)

    def test_P5_timeout_stops_the_child(self):
        provider = OpenCode2(executable=self.fake([{"sleep": 30}]))
        start = time.monotonic()
        with self.assertRaisesRegex(ProviderError, "no reply within"):
            provider.run("hi", self.dir, 0.5)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(gone(str(self.bin)))

    def test_P6_cli_with_a_check(self):
        self.fake([{"write": {"app.txt": "done\n"}, "stdout": OK}])
        code, out = self.subgoal("--check", "grep -q done app.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: the check exited 0", out)
        self.assertIn("tokens    330 (", out)
        (call,) = self.calls()
        self.assertIn("grep -q done app.txt", call["stdin"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.dir.resolve())
        self.assertNotIn("PWD", call["env"])

    def test_P7_cli_with_a_judge(self):
        self.fake([{"write": {"app.txt": "done\n"}, "stdout": OK}, {"stdout": READ_ONLY}])
        code, out = self.subgoal()
        self.assertEqual(code, 1, out)                       # the recorded judge said not met
        self.assertIn("GOAL NOT MET (out_of_iterations)", out)
        self.assertIn("last verdict after step 1: not met", out)
        self.assertIn("app.txt says todo.", out)            # the judge's reasons, from its real reply
        worker, judge = self.calls()
        self.assertTrue(judge["stdin"].startswith("You are a reviewer"))
        self.assertEqual(judge["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertNotIn("OPENCODE_DISABLE_PROJECT_CONFIG", worker["env"])
        for call, rules in ((worker, WORKER_RULES), (judge, READ_ONLY_RULES)):
            self.assertEqual(json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])["agents"][AGENT]["permissions"], rules)

    def test_P8_cli_surface(self):
        def cli(*argv):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                try:
                    return main(list(argv)), out.getvalue()
                except SystemExit as stop:
                    return stop.code, out.getvalue()
        here = ["--dir", str(self.dir), "--goal", "g", "--provider", "opencode2"]
        code, out = cli(*here, "--dry-run", "--model", "mock/scripted")
        self.assertEqual(code, 0)
        self.assertIn(f"provider: {OpenCode2(model='mock/scripted').describe()}\n", out)
        self.assertIn(f"judge: {OpenCode2(model='mock/scripted', read_only=True).describe()}\n", out)
        self.assertEqual(cli(*here, "--check", "true", "--allow", "Edit")[0], 2)    # Claude Code rules
        self.assertEqual(list(self.dir.iterdir()), [self.dir / "app.txt"])
        shown = subprocess.run([sys.executable, "-m", "subgoal", "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertIn("{claude-code,opencode,opencode2}", shown.stdout)


if __name__ == "__main__":
    unittest.main()
