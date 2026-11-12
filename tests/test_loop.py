"""The loop, driven by FakeProvider. No model calls; checks are tiny real commands.

Every stop reason has at least one test:

    stop reason              exit  tests
    goal_met                 0     L2, L13
    check_already_passes     2     L1 (at the pre-check: no step runs)
    out_of_iterations        1     L3, L4, L8, L12
    out_of_time              1     L6, L6b (the provider ran out of time)
    out_of_tokens            1     L7
    provider_failed          3     L9
    protected_file_changed   3     L11
    interrupted              130   L10
"""
import io
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fake import FakeProvider, edit, fail, gone, notes, say
from subgoal import loop
from subgoal.loop import Limits, StopReason as R, run_loop
from subgoal.provider import ProviderError, Reply

PY = shlex.quote(sys.executable)
CHECK = f"{PY} -c \"s = open('app.py').read(); print('app.py says', s); exit('done' not in s)\""


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "app.py").write_text("todo\n")

    def go(self, steps, check=CHECK, **kwargs):
        self.fake = FakeProvider(steps)
        printed = io.StringIO()
        with redirect_stdout(printed), redirect_stderr(io.StringIO()):
            run = run_loop("make app.py say done", check, self.dir, self.fake, **kwargs)
        self.printed = printed.getvalue()
        return run

    def ended(self, run, reason, exit_code, calls):
        self.assertEqual((run.stop_reason, run.stop_reason.exit_code), (reason, exit_code))
        self.assertEqual(len(self.fake.prompts), calls)
        self.assertEqual(json.loads((run.folder / "run.json").read_text())["status"], "ended")

    def test_L1_a_check_that_already_passes_runs_no_step(self):
        (self.dir / "app.py").write_text("done\n")
        run = self.go([])
        self.ended(run, R.CHECK_ALREADY_PASSES, 2, calls=0)
        self.assertTrue((run.folder / "00" / "check.txt").exists())
        self.assertIn("subgoal: CHECK ALREADY PASSES before any step", self.printed)
        self.assertIn("make it fail first", self.printed)
        self.assertNotIn("GOAL MET", self.printed)

    def test_L2_fixed_on_step_2(self):
        run = self.go([edit("app.py", "halfway\n"), edit("app.py", "done\n")])
        self.ended(run, R.GOAL_MET, 0, calls=2)
        self.assertIn("LAST CHECK (after step 1, exit code 1", self.fake.prompts[1])
        self.assertIn("app.py says halfway", self.fake.prompts[1])

    def test_L3_never_fixed(self):
        run = self.go([say()] * 3, limits=Limits(max_iterations=3))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=3)

    def test_L4_claim_is_not_trusted(self):
        run = self.go([say("DONE, all tests pass")] * 2, limits=Limits(max_iterations=2))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=2)
        self.assertIn('last words (unverified): "DONE, all tests pass"', self.printed)

    def test_L5_fresh_context(self):
        self.go([say("REPLY-ONE"), notes("NOTES-TWO", text="REPLY-TWO"), say()],
                limits=Limits(max_iterations=3))
        third = self.fake.prompts[2]
        self.assertIn("NOTES-TWO", third)
        self.assertNotIn("REPLY-ONE", third)
        self.assertNotIn("REPLY-TWO", third)

    def test_L6_time_limit(self):
        now = [0.0]

        def slow(workdir):
            now[0] += 400
            return Reply("still working")
        run = self.go([slow] * 5, limits=Limits(max_seconds=1000), clock=lambda: now[0])
        self.ended(run, R.OUT_OF_TIME, 1, calls=3)
        self.assertEqual(self.fake.timeouts, [1000, 600, 200])

    def test_L6b_provider_runs_out_of_time(self):
        now = [0.0]

        def hang(workdir):
            now[0] += 1000
            raise ProviderError("timed out")
        run = self.go([hang], limits=Limits(max_seconds=1000), clock=lambda: now[0])
        self.ended(run, R.OUT_OF_TIME, 1, calls=1)

    def test_L7_token_limit(self):
        run = self.go([say(tokens=600)] * 5, limits=Limits(max_tokens=1000))
        self.ended(run, R.OUT_OF_TOKENS, 1, calls=2)
        self.assertEqual(run.tokens, 1200)
        self.assertTrue((run.folder / "02" / "check.txt").exists())

    def test_L8_token_limit_without_tokens(self):
        run = self.go([say()] * 2, limits=Limits(max_iterations=2, max_tokens=1000))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=2)
        self.assertIn("token limit NOT enforced (provider reported no tokens)", self.printed)

    def test_L9_provider_failed(self):
        run = self.go([fail(ProviderError("login expired"))])
        self.ended(run, R.PROVIDER_FAILED, 3, calls=1)
        self.assertIn("login expired", self.printed)

    def test_L10_interrupted(self):
        run = self.go([fail(KeyboardInterrupt())])
        self.ended(run, R.INTERRUPTED, 130, calls=1)
        self.assertTrue((run.folder / "report.md").exists())

    def test_L11_protected_file_changed(self):
        (self.dir / "test_app.py").write_text("the finish line\n")
        run = self.go([edit("test_app.py", "moved\n")], protect=["test_app.py"])
        self.ended(run, R.PROTECTED_FILE_CHANGED, 3, calls=1)
        self.assertFalse((run.folder / "01" / "check.txt").exists())
        self.assertIn("test_app.py", self.printed)

    def test_L12_check_timeout(self):
        with mock.patch.object(loop, "CHECK_TIMEOUT_S", 0.3):
            hang = f"{PY} -c 'import time; time.sleep(30)' {shlex.quote(str(self.dir))}"
            run = self.go([say()], check=hang, limits=Limits(max_iterations=1))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=1)
        self.assertIn("timed out", (run.folder / "01" / "check.txt").read_text())
        self.assertTrue(gone(str(self.dir)))

    def test_L13_records(self):
        run = self.go([say(), edit("app.py", "done\n")])
        step = ["call.json", "check.txt", "prompt.md", "reply.md"]
        for name, files in [("00", ["check.txt"]), ("01", step), ("02", step)]:
            self.assertEqual(sorted(p.name for p in (run.folder / name).iterdir()), files)
        saved = json.loads((run.folder / "run.json").read_text())
        self.assertEqual((saved["stop_reason"], saved["steps"]), ("goal_met", 2))
        self.assertEqual((run.folder / "report.md").read_text(), self.printed)
        self.assertEqual((run.folder / "01" / "prompt.md").read_text(), self.fake.prompts[0])

    def test_L14_trace_only_when_reported(self):
        traced = lambda trace: lambda workdir: Reply("I wrote it.", trace=trace)
        run = self.go([say(), traced("Skill tiny\nWrite /w/app.py"), traced(""), edit("app.py", "done\n")],
                      limits=Limits(max_iterations=4))
        self.ended(run, R.GOAL_MET, 0, calls=4)
        has = {name: (run.folder / name / "trace.md").exists() for name in ("01", "02", "03", "04")}
        self.assertEqual(has, {"01": False, "02": True, "03": True, "04": False})    # None: no file
        self.assertEqual((run.folder / "02" / "trace.md").read_text(), "Skill tiny\nWrite /w/app.py\n")
        self.assertEqual((run.folder / "03" / "trace.md").read_text(), "(no tool calls)\n")
        calls = [json.loads((run.folder / n / "call.json").read_text())["trace"] for n in ("01", "02")]
        self.assertEqual(calls, [None, "(trace.md)"])                     # not copied into call.json
        self.assertFalse(list(self.dir.glob("trace.md")))                 # never in the work folder
        for prompt in self.fake.prompts:
            self.assertNotIn("trace", prompt)                             # the worker is not told

    def test_L15_later_steps_are_told_the_skills_used(self):
        traced = lambda trace: lambda workdir: Reply("did some.", trace=trace)
        run = self.go([traced("Skill tiny\nRead: a.md\nSkill other"), traced("Bash ls"), edit("app.py", "done\n")],
                      limits=Limits(max_iterations=3))
        self.ended(run, R.GOAL_MET, 0, calls=3)
        first, second, third = self.fake.prompts
        self.assertNotIn("SKILLS EARLIER STEPS USED", first)              # nothing used yet
        for later in (second, third):                                     # kept from step 1 even when step 2 used none
            self.assertIn("SKILLS EARLIER STEPS USED: other, tiny. Invoke them again", later)
            self.assertIn("do not search the disk", later)

    def test_L23_the_report_says_since_when_the_check_has_failed_the_same_way(self):
        run = self.go([say("x" * 400)] * 4, limits=Limits(max_iterations=4))
        self.assertEqual(run.same_since, 0)                                # no step changed what the check printed
        self.assertIn("exit 1, the same failure since the pre-check", self.printed)
        self.assertIn('"' + "x" * 150 + " [...] " + "x" * 150 + '"', self.printed)   # last words: the start and the end
        run = self.go([edit("app.py", "try 1\n")] + [say("short")] * 2, limits=Limits(max_iterations=3))
        self.assertEqual(run.same_since, 1)
        self.assertIn("exit 1, the same failure since step 1", self.printed)
        self.assertIn('(unverified): "short"', self.printed)                # a short reply is shown whole
        run = self.go([edit("app.py", "a\n"), edit("app.py", "b\n")], limits=Limits(max_iterations=2))
        self.assertEqual(run.same_since, 2)                                # the last step changed the failure
        self.assertNotIn("the same failure", self.printed)
        self.go([edit("app.py", "done\n")])
        self.assertNotIn("the same failure", self.printed)

    def script(self, name, body):
        (self.dir / name).write_text(body)
        return f"{PY} {name}"

    def test_L26_a_different_exit_code_is_a_different_failure(self):
        a, b = loop.CheckResult(1, "FAILED x\n", 0.1, 1), loop.CheckResult(2, "FAILED x\n", 0.1, 2)
        self.assertFalse(loop.same_failure(a, b))
        self.assertTrue(loop.same_failure(a, loop.CheckResult(1, "FAILED x\n", 0.2, 2)))
        passed = loop.CheckResult(0, "ok\n", 0.1, 1)
        self.assertFalse(loop.same_failure(passed, loop.CheckResult(0, "ok\n", 0.1, 2)))    # two passes: no failure at all
        timed_out = loop.CheckResult(None, "", 600, 1)
        self.assertTrue(loop.same_failure(timed_out, loop.CheckResult(None, "", 600, 2)))  # a timeout is a failure

    def test_L22_a_file_a_failing_check_leaves_in_a_protected_folder_stops_nothing(self):
        (self.dir / "tests").mkdir()
        (self.dir / "tests" / "test_app.txt").write_text("the finish line\n")
        leaves = self.script("leaves.py", "from pathlib import Path\nout = Path('tests/app.received.txt')\n"
                             "ok = 'done' in Path('app.py').read_text()\nout.unlink(missing_ok=True) if ok else out.write_text('todo')\n"
                             "exit(0 if ok else 1)\n")                    # like ApprovalTests: a file only while it fails

        def fixes_and_runs_the_check(workdir):                          # as FINISH_CHECK invites it to
            (workdir / "app.py").write_text("done\n")
            subprocess.run([sys.executable, "leaves.py"], cwd=workdir, check=False)
            return Reply("fixed")
        run = self.go([fixes_and_runs_the_check], check=leaves, protect=["tests"])
        self.ended(run, R.GOAL_MET, 0, calls=1)                         # the pre-check's file stops nothing

    def test_L30_same_session_sends_the_findings_back_to_the_workers_own_chat(self):
        steps = [edit("app.py", "halfway\n", session="chat-1"), edit("app.py", "done\n", session="chat-1")]
        run = self.go(steps, same_session=True)
        self.ended(run, R.GOAL_MET, 0, calls=2)
        self.assertEqual(self.fake.resumes, [None, "chat-1"])               # step 1 is a new chat, step 2 continues it
        first, second = self.fake.prompts
        self.assertIn("You are one step in a loop", first)
        self.assertNotIn("You start with no memory", second)                # the chat remembers: only what the finish line said
        self.assertNotIn("GOAL\n", second)
        self.assertIn("LAST CHECK (after step 1, exit code 1", second)
        self.assertIn("app.py says halfway", second)
        self.assertIn("notes.md", second)                                   # the notes are still rewritten
        self.assertEqual((run.folder / "02" / "prompt.md").read_text(), second)

    def test_L31_without_the_flag_every_step_is_a_new_chat(self):
        self.go([edit("app.py", "halfway\n", session="chat-1"), edit("app.py", "done\n", session="chat-2")])
        self.assertEqual(self.fake.resumes, [None, None])
        self.assertIn("You start with no memory", self.fake.prompts[1])

    def test_L32_a_chat_that_cannot_be_continued_falls_back_to_a_new_one(self):
        steps = [say(session="chat-1"), fail(ProviderError("no such session")), edit("app.py", "done\n", session="chat-2")]
        run = self.go(steps, same_session=True)
        self.ended(run, R.GOAL_MET, 0, calls=3)                             # the failed try, then the fresh step 2
        self.assertEqual(self.fake.resumes, [None, "chat-1", None])
        self.assertIn("You start with no memory", self.fake.prompts[2])
        self.assertEqual((run.folder / "02" / "prompt.md").read_text(), self.fake.prompts[2])
        self.assertEqual(run.steps, 2)
        (self.dir / "app.py").write_text("todo\n")
        self.go([say(), edit("app.py", "done\n")], same_session=True)       # a provider that gave no chat id: a new chat
        self.assertEqual(self.fake.resumes, [None, None])

    def test_L33_the_report_and_run_json_say_how_to_continue_each_chat(self):
        run = self.go([edit("app.py", "halfway\n", session="chat-1"), edit("app.py", "done\n", session="chat-2")])
        chats = json.loads((run.folder / "run.json").read_text())["chats"]
        self.assertEqual(chats, [{"who": "worker", "step": 1, "session": "chat-1", "continue": "fake --continue chat-1"},
                                 {"who": "worker", "step": 2, "session": "chat-2", "continue": "fake --continue chat-2"}])
        self.assertIn("continue  worker, step 1: fake --continue chat-1", self.printed)
        self.assertIn("worker, step 2: fake --continue chat-2", self.printed)
        self.assertIn(f"(run in {self.dir.resolve()})", self.printed)
        self.go([edit("app.py", "done\n")])                                 # no chat id: nothing to continue
        self.assertNotIn("continue  ", self.printed)


if __name__ == "__main__":
    unittest.main()
