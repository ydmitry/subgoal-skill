"""The command line: dry run, argument guards, and the size budget of the core."""
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import fake  # noqa: F401  (throwaway config and records homes)
from subgoal.claude_code import ClaudeCode
from subgoal.cli import main
from subgoal.loop import Limits, StopReason, records_home
from subgoal.opencode import OpenCode

CORE = Path(__file__).resolve().parent.parent / "subgoal"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.records = records_home(self.dir.resolve())
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
        self.assertIn(ClaudeCode(allow=["Bash(exit 1)"], records=self.records).describe(), out)
        self.assertIn("GOAL\nSay hi.", out)
        self.assertIn("pre-check: exit 1\n", out)
        self.assertIn("plan: none (a run with a check has no planner)", out)
        self.assertNotIn("the worker's own chat is continued", out)
        out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--same-session", "--dry-run")[1]
        self.assertIn(f"plan: a read-only session before step 1\nplanner: {ClaudeCode(edit=False, records=self.records).describe()}\n", out)   # shown before the yes
        self.assertIn("later steps: the worker's own chat is continued (--same-session)", out)
        self.assertEqual(list(self.dir.iterdir()), [])
        out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--check", "exit 1", "--no-plan", "--dry-run")[1]
        self.assertIn("plan: none (--no-plan)", out)
        self.assertNotIn("planner:", out)

    def test_X1b_a_dry_run_says_when_the_check_already_passes(self):
        code, out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--check", "true", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("pre-check: exit 0. It already passes, so a run would stop before any step "
                      "(check_already_passes): make it fail first.", out)

    def test_X2_argument_guards(self):
        ok, here = ["--goal", "g", "--check", "true"], ["--dir", str(self.dir)]
        for argv in (["--dir", "~", *ok], ["--dir", "/", *ok], [*here, "--check", "true"],
                     [*here, *ok, "--goal-file", "f"], [*here, *ok, "--max-iterations", "0"],
                     [*here, *ok, "--judge-provider", "nope"]):
            self.assertEqual(self.cli(*argv)[0], 2, argv)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_X8_four_steps_by_default_and_the_re_planner_takes_the_judge_model(self):
        self.assertEqual(Limits().max_iterations, 4)
        with mock.patch("subgoal.cli.run_loop") as run_loop:
            run_loop.return_value.stop_reason = StopReason.GOAL_MET
            self.cli("--dir", str(self.dir), "--goal", "g", "--check", "true", "--model", "w", "--judge-model", "j")
        self.assertEqual(run_loop.call_args.args[4].max_iterations, 4)
        plan = run_loop.call_args.kwargs["plan"]
        self.assertEqual((plan.edit, plan.argv[plan.argv.index("--model") + 1]), (False, "j"))
        self.assertIn("default 4", self.cli("--help")[1])

    def test_X3_no_check_and_a_judge_flag_means_a_judge(self):
        code, out = self.cli("--dir", str(self.dir), "--goal", "Review the docs.", "--judge", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"provider: {ClaudeCode(allow=['Bash', 'Read'], records=self.records).describe()}\n", out)    # any command, no check
        self.assertIn(f"judge: {ClaudeCode(edit=False, records=self.records).describe()}\n", out)
        self.assertIn("a separate reviewer reads your result against the goal and decides", out)
        self.assertIn("LAST REVIEW (none yet: this is the first step)", out)
        self.assertIn("<the worker's last words go here>", out)             # the judge prompt template
        self.assertIn(f"in the run folder {self.records}/<run-id>/ (outside the work) is the loop's own record", out)
        self.assertNotIn("pre-check: exit", out)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_X10_a_check_then_a_judge(self):
        code, out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--check", "true", "--judge", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("pre-check: exit 0\n", out)                     # with a judge, a passing check guards: no warning
        self.assertNotIn("check_already_passes", out)
        self.assertIn(f"provider: {ClaudeCode(allow=['Bash(true)'], records=self.records).describe()}\n", out)    # the worker may run only the check
        self.assertIn("Once it exits 0, a separate reviewer reads your result", out)
        self.assertIn(f"judge: {ClaudeCode(edit=False, records=self.records).describe()}\n", out)
        self.assertIn("judge prompt, after each step whose check exits 0:\n", out)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_X11_the_judge_and_the_planner_may_run_on_another_tool(self):
        def built(*argv):
            with mock.patch("subgoal.cli.run_loop") as run_loop:
                run_loop.return_value.stop_reason = StopReason.GOAL_MET
                self.assertEqual(self.cli("--dir", str(self.dir), "--goal", "g", "--judge", *argv)[0], 0)
            return run_loop.call_args.args[3], run_loop.call_args.kwargs["judge"], run_loop.call_args.kwargs["plan"]
        worker, judge, plan = built("--provider", "opencode", "--model", "a/b", "--judge-provider", "claude-code")
        self.assertEqual((type(worker), type(judge), plan), (OpenCode, ClaudeCode, judge))
        self.assertEqual(worker.argv[worker.argv.index("--model") + 1], "a/b")
        self.assertNotIn("--model", judge.argv)                         # one tool's model name means nothing to another
        judge = built("--provider", "opencode", "--model", "a/b", "--judge-provider", "claude-code", "--judge-model", "j")[1]
        self.assertEqual(judge.argv[judge.argv.index("--model") + 1], "j")
        worker, judge, _ = built("--provider", "opencode", "--model", "a/b")
        self.assertEqual((type(judge), judge.argv[judge.argv.index("--model") + 1]), (OpenCode, "a/b"))   # default: the worker's
        self.assertIn("--judge-provider", self.cli("--help")[1])

    def test_X6_the_planner_is_on_unless_no_plan(self):
        for argv, planner in (([], True), (["--judge"], True), (["--no-plan"], False), (["--check", "true"], True),
                              (["--check", "true", "--no-plan"], False)):
            with mock.patch("subgoal.cli.run_loop") as run_loop:
                run_loop.return_value.stop_reason = StopReason.GOAL_MET
                self.assertEqual(self.cli("--dir", str(self.dir), "--goal", "g", *argv)[0], 0)
            plan = run_loop.call_args.kwargs["plan"]
            self.assertEqual(plan is not None, planner, argv)
            self.assertTrue(plan is None or not plan.edit)                      # the planner has no edit tools
        self.assertIn("plan: a read-only session", self.cli("--dir", str(self.dir), "--goal", "g", "--dry-run")[1])
        self.assertIn("plan: none (--no-plan)", self.cli("--dir", str(self.dir), "--goal", "g", "--no-plan", "--dry-run")[1])

    def test_X7_no_check_and_no_judge_is_one_unchecked_step(self):
        code, out = self.cli("--dir", str(self.dir), "--goal", "Fix the bug.", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Nothing checks your work after you stop: this is the only step", out)
        self.assertNotIn("judge:", out)
        self.assertNotIn("LAST REVIEW", out)
        self.assertNotIn("pre-check: exit", out)
        with mock.patch("subgoal.cli.run_loop") as run_loop:
            run_loop.return_value.stop_reason = StopReason.UNCHECKED
            self.assertEqual(self.cli("--dir", str(self.dir), "--goal", "g", "--max-iterations", "7")[0], 0)
        self.assertIsNone(run_loop.call_args.kwargs["judge"])
        self.assertEqual(run_loop.call_args.args[4].max_iterations, 1)           # one step, whatever --max-iterations says

    def test_X4_the_judge_is_built_with_edit_off(self):
        for argv, check, worker_model, judge_model in [
                (["--judge", "--model", "w", "--judge-model", "j"], None, "w", "j"),
                (["--judge", "--model", "w"], None, "w", "w"),
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
        self.assertEqual(worker_allows()[0], ["Bash", "Read"])                  # a judge decides: any command, and it may look at its screenshots
        self.assertEqual(worker_allows("--allow", "WebFetch")[0], ["Bash", "Read", "WebFetch"])
        allowed, argv = worker_allows("--check", "make test", "--allow", "Bash(npm run lint)")
        self.assertEqual(allowed, ["Bash(make test)", "Bash(npm run lint)"])    # the check and --allow, no more
        self.assertNotIn("Bash", allowed)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_X9_a_stop_signal_also_stops_what_the_loop_started(self):
        """SIGTERM (kill, a supervisor's timeout) stops the running check or agent, as Ctrl+C does, and the run ends interrupted."""
        root = CORE.parent
        loop = subprocess.Popen([sys.executable, "-m", "subgoal", "--dir", str(self.dir), "--goal", "Wait.",
                                 "--check", "echo $$ > group.pid; sleep 60"], cwd=root, stdout=subprocess.PIPE, text=True)
        pid = self.dir / "group.pid"
        for _ in range(200):
            if pid.exists() and pid.read_text().strip():
                break
            time.sleep(0.05)
        loop.send_signal(signal.SIGTERM)
        out, _ = loop.communicate(timeout=30)
        group = int(pid.read_text())
        self.assertRaises(ProcessLookupError, os.killpg, group, 0)       # nothing the check started is left running
        self.assertEqual(loop.returncode, StopReason.INTERRUPTED.exit_code)
        self.assertIn("interrupted", out)

    def test_X20_defaults_come_from_the_flags_file(self):
        flags = Path(os.environ["XDG_CONFIG_HOME"], "subgoal", "flags")
        flags.parent.mkdir(exist_ok=True)
        self.addCleanup(flags.unlink)
        flags.write_text("# my defaults\n--max-minutes 7 --provider opencode\n--judge-model 'big one'   # for the judge\n")
        code, out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--judge", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(f"defaults from {flags}: --max-minutes 7 --provider opencode --judge-model 'big one'\nDRY RUN"))
        self.assertIn(f"provider: {OpenCode(records=self.records).describe()}\n", out)
        self.assertIn("--model 'big one'", out)
        out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--provider", "claude-code", "--dry-run")[1]
        self.assertIn(f"provider: {ClaudeCode(allow=['Bash', 'Read'], records=self.records).describe()}\n", out)   # what is typed wins
        for never in ("--dir /tmp", "--goal=x", "--goal-file g.md"):
            flags.write_text(never + "\n")
            self.assertEqual(self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--dry-run")[0], 2, never)
        flags.write_text("")
        self.assertTrue(self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--dry-run")[1].startswith("DRY RUN"))

    def test_X21_the_records_are_kept_outside_the_folder(self):
        with mock.patch.dict(os.environ, XDG_DATA_HOME="/data"):
            self.assertEqual(records_home(Path("/work/my app")), Path("/data/subgoal/-work-my app"))
        with mock.patch.dict(os.environ, XDG_DATA_HOME=""):
            self.assertEqual(records_home(Path("/w")), Path.home() / ".local/share/subgoal/-w")
        out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--judge", "--dry-run")[1]
        self.assertIn(f"rewrite {self.records}/<run-id>/notes.md in full", out)
        self.assertEqual(out.count(f"--add-dir {self.records} "), 3)                # the worker, the planner and the judge may reach them
        self.assertNotIn(".subgoal", out)

    def test_S1_size_budget(self):
        def count(f):
            return sum(1 for line in f.read_text().splitlines() if line.strip())
        providers = ("claude_code.py", "opencode.py", "opencode2.py")   # a provider is not part of the loop core
        core = sum(count(f) for f in CORE.glob("*.py") if f.name not in providers)
        self.assertLessEqual(core, 585, "the core must stay readable in one sitting")
        for name in providers:
            self.assertLessEqual(count(CORE / name), 100, f"{name}: a provider must stay small")


if __name__ == "__main__":
    unittest.main()
