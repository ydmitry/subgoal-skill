"""The loop, driven by FakeProvider. No model calls; checks are tiny real commands."""
import io
import json
import shlex
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fake import FakeProvider, edit, notes, say
from subgoal.loop import Limits, StopReason, run_loop

PY = shlex.quote(sys.executable)
CHECK = f"{PY} -c \"import sys; s = open('app.py').read(); print('app.py says', s); sys.exit('done' not in s)\""


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

    def test_L1_goal_already_holds(self):
        (self.dir / "app.py").write_text("done\n")
        run = self.go([])
        self.assertEqual((run.reason, run.reason.exit_code), (StopReason.GOAL_MET, 0))
        self.assertEqual(self.fake.prompts, [])
        self.assertTrue((run.folder / "00" / "check.txt").exists())

    def test_L2_fixed_on_step_2(self):
        run = self.go([edit("app.py", "halfway\n"), edit("app.py", "done\n")])
        self.assertEqual(run.reason, StopReason.GOAL_MET)
        self.assertEqual(len(self.fake.prompts), 2)
        self.assertIn("LAST CHECK (after step 1, exit code 1", self.fake.prompts[1])
        self.assertIn("app.py says halfway", self.fake.prompts[1])

    def test_L3_never_fixed(self):
        run = self.go([say()] * 3, limits=Limits(max_iterations=3))
        self.assertEqual((run.reason, run.reason.exit_code), (StopReason.OUT_OF_ITERATIONS, 1))
        self.assertEqual(len(self.fake.prompts), 3)

    def test_L4_claim_is_not_trusted(self):
        run = self.go([say("DONE, all tests pass")] * 2, limits=Limits(max_iterations=2))
        self.assertEqual(run.reason, StopReason.OUT_OF_ITERATIONS)
        self.assertEqual(len(self.fake.prompts), 2)
        self.assertIn('last words (unverified): "DONE, all tests pass"', run.report)

    def test_L5_fresh_context(self):
        self.go([say("REPLY-ONE"), notes("NOTES-TWO", text="REPLY-TWO"), say()],
                limits=Limits(max_iterations=3))
        third = self.fake.prompts[2]
        self.assertIn("NOTES-TWO", third)
        self.assertNotIn("REPLY-ONE", third)
        self.assertNotIn("REPLY-TWO", third)

    def test_L13_records(self):
        run = self.go([say(), edit("app.py", "done\n")])
        for step, names in [("00", ["check.txt"]), ("01", ["call.json", "check.txt", "prompt.md", "reply.md"]),
                            ("02", ["call.json", "check.txt", "prompt.md", "reply.md"])]:
            self.assertEqual(sorted(p.name for p in (run.folder / step).iterdir()), names)
        saved = json.loads((run.folder / "run.json").read_text())
        self.assertEqual((saved["status"], saved["stop_reason"], saved["steps"]), ("ended", "goal_met", 2))
        self.assertEqual((run.folder / "report.md").read_text(), self.printed)
        self.assertEqual((run.folder / "01" / "prompt.md").read_text(), self.fake.prompts[0])


if __name__ == "__main__":
    unittest.main()
