"""The loop, driven by FakeProvider. No model calls; checks are tiny real commands."""
import io
import shlex
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fake import FakeProvider, edit, say
from subgoal.loop import Limits, StopReason, run_loop

PY = shlex.quote(sys.executable)
CHECK = f"{PY} -c \"import sys; sys.exit('done' not in open('app.py').read())\""


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

    def test_L2_fixed_on_step_2(self):
        run = self.go([say(), edit("app.py", "done\n")])
        self.assertEqual(run.reason, StopReason.GOAL_MET)
        self.assertEqual(len(self.fake.prompts), 2)

    def test_L3_never_fixed(self):
        run = self.go([say()] * 3, limits=Limits(max_iterations=3))
        self.assertEqual((run.reason, run.reason.exit_code), (StopReason.OUT_OF_ITERATIONS, 1))
        self.assertEqual(len(self.fake.prompts), 3)

    def test_L4_claim_is_not_trusted(self):
        run = self.go([say("DONE, all tests pass")] * 2, limits=Limits(max_iterations=2))
        self.assertEqual(run.reason, StopReason.OUT_OF_ITERATIONS)
        self.assertEqual(len(self.fake.prompts), 2)
        self.assertIn('last words (unverified): "DONE, all tests pass"', run.report)


if __name__ == "__main__":
    unittest.main()
