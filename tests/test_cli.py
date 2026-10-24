"""The command line: dry run, argument guards, and the size budget of the core."""
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from subgoal.claude_code import ClaudeCode
from subgoal.cli import main
from subgoal.loop import StopReason

CORE = Path(__file__).resolve().parent.parent / "subgoal"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        nobody = mock.patch.object(ClaudeCode, "run", side_effect=AssertionError("tests call no agent"))
        nobody.start()
        self.addCleanup(nobody.stop)

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                return main(list(argv)), out.getvalue()
            except SystemExit as stop:
                return stop.code, out.getvalue()

    def test_X1_dry_run(self):
        with mock.patch.object(ClaudeCode, "run", side_effect=AssertionError("a dry run calls nobody")):
            code, out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--check", "exit 1",
                                 "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(ClaudeCode(allow=["Bash(exit 1)"]).describe(), out)
        self.assertIn("GOAL\nSay hi.", out)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_X2_argument_guards(self):
        ok, here = ["--goal", "g", "--check", "true"], ["--dir", str(self.dir)]
        for argv in (["--dir", "~", *ok], ["--dir", "/", *ok], [*here, "--check", "true"],
                     [*here, *ok, "--goal-file", "f"], [*here, *ok, "--max-iterations", "0"],
                     [*here, *ok, "--judge-model", "m"]):
            self.assertEqual(self.cli(*argv)[0], 2, argv)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_X3_no_check_means_a_judge(self):
        code, out = self.cli("--dir", str(self.dir), "--goal", "Review the docs.", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"provider: {ClaudeCode(allow=['Bash']).describe()}\n", out)    # any command, no check
        self.assertIn(f"judge: {ClaudeCode(edit=False).describe()}\n", out)
        self.assertIn("a separate reviewer reads your result against the goal and decides", out)
        self.assertIn("LAST REVIEW (none yet: this is the first step)", out)
        self.assertIn("<the worker's last words go here>", out)             # the judge prompt template
        self.assertIn(f"in the run folder {self.dir.resolve()}/.subgoal/<run-id>/ is the loop's own record", out)
        self.assertNotIn("pre-check: exit", out)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_X4_the_judge_is_built_with_edit_off(self):
        for argv, check, worker_model, judge_model in [
                (["--model", "w", "--judge-model", "j"], None, "w", "j"),
                (["--model", "w"], None, "w", "w"),
                (["--check", "true", "--model", "w"], "true", "w", None)]:
            with mock.patch("subgoal.cli.run_loop") as run_loop:
                run_loop.return_value.stop_reason = StopReason.GOAL_MET
                self.assertEqual(self.cli("--dir", str(self.dir), "--goal", "g", *argv)[0], 0)
            (goal, passed_check, _dir, worker), judge = run_loop.call_args.args[:4], run_loop.call_args.kwargs["judge"]
            self.assertEqual((passed_check, worker.edit), (check, True))
            self.assertEqual("--model" in worker.argv and worker.argv[worker.argv.index("--model") + 1], worker_model)
            self.assertEqual("Bash(true)" in worker.argv, check is not None)    # the allow rule needs --check
            if check is None:
                self.assertFalse(judge.edit)                                    # no edit tools, may run commands
                self.assertEqual(judge.argv[judge.argv.index("--model") + 1], judge_model)
            else:
                self.assertIsNone(judge)

    def test_X5_what_the_worker_may_run(self):
        def worker_allows(*argv):
            with mock.patch("subgoal.cli.run_loop") as run_loop:
                run_loop.return_value.stop_reason = StopReason.GOAL_MET
                self.assertEqual(self.cli("--dir", str(self.dir), "--goal", "g", *argv)[0], 0)
            worker = run_loop.call_args.args[3]
            return worker.argv[worker.argv.index("--allowedTools") + 1:], worker.argv
        self.assertEqual(worker_allows()[0], ["Bash"])                          # a judge decides: any command
        self.assertEqual(worker_allows("--allow", "WebFetch")[0], ["Bash", "WebFetch"])
        allowed, argv = worker_allows("--check", "make test", "--allow", "Bash(npm run lint)")
        self.assertEqual(allowed, ["Bash(make test)", "Bash(npm run lint)"])    # the check and --allow, no more
        self.assertNotIn("Bash", allowed)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_S1_size_budget(self):
        def count(f):
            return sum(1 for line in f.read_text().splitlines() if line.strip())
        providers = ("claude_code.py", "opencode.py", "opencode2.py")   # a provider is not part of the loop core
        core = sum(count(f) for f in CORE.glob("*.py") if f.name not in providers)
        self.assertLessEqual(core, 550, "the core must stay readable in one sitting")
        for name in providers:
            self.assertLessEqual(count(CORE / name), 100, f"{name}: a provider must stay small")


if __name__ == "__main__":
    unittest.main()
