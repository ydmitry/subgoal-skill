"""The loop, driven by FakeProvider. No model calls; checks are tiny real commands.

Every stop reason has at least one test:

    stop reason              exit  tests
    goal_met                 0     L1 (at the pre-check), L2, L13
    out_of_iterations        1     L3, L4, L8, L12
    out_of_time              1     L6, L6b (the provider ran out of time)
    out_of_tokens            1     L7
    provider_failed          3     L9
    protected_file_changed   3     L11
    interrupted              130   L10
"""
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fake import FakeProvider, edit, fail, notes, say
from subgoal import loop
from subgoal.loop import Limits, StopReason as R, run_loop
from subgoal.provider import ProviderError, Reply

PY = shlex.quote(sys.executable)
CHECK = f"{PY} -c \"s = open('app.py').read(); print('app.py says', s); exit('done' not in s)\""
HANG = f"{PY} -c \"import os, time; open('pid', 'w').write(str(os.getpid())); time.sleep(30)\""


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

    def test_L1_goal_already_holds(self):
        (self.dir / "app.py").write_text("done\n")
        run = self.go([])
        self.ended(run, R.GOAL_MET, 0, calls=0)
        self.assertTrue((run.folder / "00" / "check.txt").exists())

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
            run = self.go([say()], check=HANG, limits=Limits(max_iterations=1))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=1)
        self.assertIn("timed out", (run.folder / "01" / "check.txt").read_text())
        self.assertTrue(gone(int((self.dir / "pid").read_text())))

    def test_L13_records(self):
        run = self.go([say(), edit("app.py", "done\n")])
        step = ["call.json", "check.txt", "prompt.md", "reply.md"]
        for name, files in [("00", ["check.txt"]), ("01", step), ("02", step)]:
            self.assertEqual(sorted(p.name for p in (run.folder / name).iterdir()), files)
        saved = json.loads((run.folder / "run.json").read_text())
        self.assertEqual((saved["stop_reason"], saved["steps"]), ("goal_met", 2))
        self.assertEqual((run.folder / "report.md").read_text(), self.printed)
        self.assertEqual((run.folder / "01" / "prompt.md").read_text(), self.fake.prompts[0])


def gone(pid):
    """True once the process has exited (allowing a moment for it to be reaped)."""
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


if __name__ == "__main__":
    unittest.main()
