"""The loop, driven by FakeProvider. No model calls; checks are tiny real commands.

Every stop reason has at least one test:

    stop reason              exit  tests
    goal_met                 0     L2, L13
    check_already_passes     2     L1 (at the pre-check: no step runs)
    out_of_iterations        1     L3, L4, L8, L12, L18, L19 (a failed re-plan), L24 (no planner), L25 (no re-plan left)
    stuck                    1     L16, L17b, L19, L24 (the step after a re-plan failed the same way)
    out_of_time              1     L6, L6b (the provider ran out of time)
    out_of_tokens            1     L7, L20 (no re-plan past a limit), L20b (no step after one that crossed it)
    provider_failed          3     L9
    protected_file_changed   3     L11, L21 (a planner's command moved it)
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

    def replan(self, steps, plans, **kwargs):
        self.planner = FakeProvider(plans)
        return self.go(steps, plan=self.planner, **kwargs)

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

    def test_L16_the_same_failure_twice_brings_a_re_plan(self):
        run = self.replan([say()] * 3, [say("TRY app.py line 1", tokens=50)], limits=Limits(max_iterations=3))
        self.ended(run, R.STUCK, 1, calls=3)                            # step 3, with the plan, failed the same way
        self.assertEqual(len(self.planner.prompts), 1)    # after step 2: not after step 1 (vs the pre-check), nor after the last
        asked = self.planner.prompts[0]
        for said in ("The workers are stuck", "change no file", "GOAL\nmake app.py say done", CHECK, "app.py says todo",
                     "or a protected path (none)."):
            self.assertIn(said, asked)
        self.assertEqual((run.folder / "02" / "plan.md").read_text(), "TRY app.py line 1\n")
        self.assertEqual((run.folder / "02" / "plan-prompt.md").read_text(), asked)
        first, second, third = self.fake.prompts
        self.assertNotIn("PLAN (made", first + second)                 # no plan before step 1 with a check
        self.assertIn("PLAN (made by a read-only planner after step 2, when the check failed the same way twice", third)
        self.assertIn("TRY app.py line 1", third)
        self.assertLess(third.index("PLAN (made"), third.index("FINISH LINE"))
        self.assertEqual((run.replans, run.tokens), (1, 50))            # its tokens count against the limit
        self.assertIn("3 of 3 worker calls (re-plans: 1)", self.printed)

    def test_L23_the_report_says_since_when_the_check_has_failed_the_same_way(self):
        run = self.replan([say("x" * 400)] * 4, [say("P1"), say("P2")], limits=Limits(max_iterations=4))
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

    def test_L17_timings_clocks_dates_and_addresses_do_not_make_a_failure_new(self):
        timed = self.script("timed.py", "import time\nn = time.time_ns()\n"
                            "print(f'Start at {n % 24:02d}:{n % 60:02d}:{n % 59:02d}.{n % 997}')\n"
                            "print(f'stamp {2000 + n % 30}-0{1 + n % 9}-1{n % 10}T00:00:00Z')\n"
                            "print(f'{n % 24:02d}:{n % 60:02d}:{n % 59:02d},{n % 997} ERROR test_app failed')\n"
                            "print(f'  duration_ms: {n % 1000 / 7:.6f}')\nprint(f'Total time: {n % 97 / 7:.4f} Seconds')\n"
                            "print(f'FAILED in {n % 1000 / 1000:.6f}s at {hex(n)}')\nexit(1)\n")
        self.replan([say()] * 3, [say("P")], check=timed, limits=Limits(max_iterations=3))
        self.assertEqual(len(self.planner.prompts), 1)
        self.assertIn("The workers are stuck", self.planner.prompts[0])

    def test_L17b_a_long_output_repeats_even_when_a_timing_changes_width(self):
        long = self.script("long.py", "from pathlib import Path\np = Path('runs.txt')\n"
                           "n = int(p.read_text()) + 1 if p.exists() else 0\np.write_text(str(n))\n"
                           "print('E   assert 1 == 2\\n' * 300, end='')\nprint(f'FAIL test_app ({10 ** n} ms)')\nexit(1)\n")
        run = self.replan([say()] * 4, [say("P1"), say("P2")], check=long, limits=Limits(max_iterations=4))
        self.ended(run, R.STUCK, 1, calls=3)                            # the 4,000-character tail starts 1 later each run:
        self.assertEqual(run.replans, 1)                                # the same after step 2 (re-plan) and 3 (stuck)

    def test_L18_a_new_failure_each_step_brings_no_re_plan(self):
        run = self.replan([edit("app.py", f"try {n}\n") for n in (1, 2, 3)], [], limits=Limits(max_iterations=3))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=3)
        self.assertEqual(self.planner.prompts, [])
        self.assertNotIn("re-plans", self.printed)

    def test_L19_a_second_repeat_re_plans_from_the_last_plan_and_a_failed_one_keeps_it(self):
        moves = [say(), say(), edit("app.py", "try 1\n"), say(), say()]  # the plan moved it, then it stuck again
        run = self.replan(moves, [say("PLAN ONE"), say("PLAN TWO")], limits=Limits(max_iterations=5))
        self.ended(run, R.STUCK, 1, calls=5)
        self.assertEqual(len(self.planner.prompts), 2)                  # after steps 2 and 4
        self.assertIn("PLAN ONE", self.planner.prompts[1])               # it is told what did not get them past it
        self.assertIn("PLAN TWO", self.fake.prompts[4])
        self.assertNotIn("PLAN ONE", self.fake.prompts[4])
        self.assertEqual(run.replans, 2)
        (self.dir / "app.py").write_text("todo\n")
        run = self.replan(moves, [say("PLAN ONE"), fail(ProviderError("no answer"))], limits=Limits(max_iterations=5))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=5)                # no new plan reached step 5, so no stuck stop
        self.assertIn("PLAN ONE", self.fake.prompts[4])                  # a failed re-plan keeps the last plan
        self.assertFalse((run.folder / "04" / "plan.md").exists())
        self.assertEqual(run.replans, 1)

    def test_L24_the_step_after_a_re_plan_failing_the_same_way_ends_the_run_as_stuck(self):
        run = self.replan([say()] * 4, [say("P1"), say("P2")], limits=Limits(max_iterations=4))
        self.ended(run, R.STUCK, 1, calls=3)                            # not step 4 and a second plan
        self.assertEqual((len(self.planner.prompts), run.replans), (1, 1))
        self.assertIn("subgoal: GOAL NOT MET (stuck)", self.printed)
        self.assertIn("exit 1, the same failure since the pre-check", self.printed)
        run = self.replan([say(), say(), edit("app.py", "try 1\n"), edit("app.py", "done\n")], [say("P")],
                          limits=Limits(max_iterations=4))
        self.ended(run, R.GOAL_MET, 0, calls=4)                         # the plan moved the failure: no stop
        (self.dir / "app.py").write_text("todo\n")
        run = self.go([say()] * 4, limits=Limits(max_iterations=4))       # no planner, no re-plan, no stuck stop
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=4)

    def test_L25_no_re_plan_after_the_last_step(self):
        run = self.replan([say(), say(), edit("app.py", "try 1\n"), say()], [say("P1"), say("P2")],
                          limits=Limits(max_iterations=4))
        self.ended(run, R.OUT_OF_ITERATIONS, 1, calls=4)                # steps 3 and 4 failed alike, but no step is left
        self.assertEqual((len(self.planner.prompts), run.replans), (1, 1))

    def test_L26_a_different_exit_code_is_a_different_failure(self):
        a, b = loop.CheckResult(1, "FAILED x\n", 0.1, 1), loop.CheckResult(2, "FAILED x\n", 0.1, 2)
        self.assertFalse(loop.same_failure(a, b))
        self.assertTrue(loop.same_failure(a, loop.CheckResult(1, "FAILED x\n", 0.2, 2)))
        passed = loop.CheckResult(0, "ok\n", 0.1, 1)
        self.assertFalse(loop.same_failure(passed, loop.CheckResult(0, "ok\n", 0.1, 2)))    # two passes: no failure at all
        timed_out = loop.CheckResult(None, "", 600, 1)
        self.assertTrue(loop.same_failure(timed_out, loop.CheckResult(None, "", 600, 2)))  # a timeout is a failure

    def test_L27_the_re_plan_reads_the_workers_notes(self):
        self.replan([notes("tried app.py; the check wants the word done"), say(), say()], [say("P")],
                    limits=Limits(max_iterations=3))
        self.assertIn("tried app.py; the check wants the word done", self.planner.prompts[0])

    def test_L20_no_re_plan_past_a_limit(self):
        run = self.replan([say(tokens=600)] * 3, [say("P")], limits=Limits(max_iterations=3, max_tokens=1000))
        self.ended(run, R.OUT_OF_TOKENS, 1, calls=2)
        self.assertEqual(self.planner.prompts, [])

    def test_L20b_no_step_after_a_re_plan_that_crossed_the_token_limit(self):
        run = self.replan([say(tokens=100)] * 4, [say("P", tokens=10_000)], limits=Limits(max_iterations=4, max_tokens=5000))
        self.ended(run, R.OUT_OF_TOKENS, 1, calls=2)                    # not a third worker call at 10,200 tokens
        self.assertEqual((len(self.planner.prompts), run.tokens), (1, 10_200))

    def test_L21_a_protected_path_the_planner_moved_stops_the_run_before_the_next_step(self):
        (self.dir / "test_app.py").write_text("the finish line\n")
        run = self.replan([say()] * 4, [edit("test_app.py", "moved\n", text="P")], protect=["test_app.py"],
                          limits=Limits(max_iterations=4))
        self.ended(run, R.PROTECTED_FILE_CHANGED, 3, calls=2)            # caught before step 3, not blamed on its worker
        self.assertIn("error     the planner changed protected paths: test_app.py", self.printed)

    def test_L22_a_file_a_failing_check_leaves_in_a_protected_folder_is_not_the_planners(self):
        (self.dir / "tests").mkdir()
        (self.dir / "tests" / "test_app.txt").write_text("the finish line\n")
        leaves = self.script("leaves.py", "from pathlib import Path\nout = Path('tests/app.received.txt')\n"
                             "ok = 'done' in Path('app.py').read_text()\nout.unlink(missing_ok=True) if ok else out.write_text('todo')\n"
                             "exit(0 if ok else 1)\n")                    # like ApprovalTests: a file only while it fails

        def fixes_and_runs_the_check(workdir):                          # as FINISH_CHECK invites it to
            (workdir / "app.py").write_text("done\n")
            subprocess.run([sys.executable, "leaves.py"], cwd=workdir, check=False)
            return Reply("fixed")
        run = self.replan([fixes_and_runs_the_check], [], check=leaves, protect=["tests"])
        self.ended(run, R.GOAL_MET, 0, calls=1)                         # the pre-check's file stops nothing


if __name__ == "__main__":
    unittest.main()
