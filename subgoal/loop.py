"""The loop: call the agent in fresh sessions until the check passes or a budget ends the run.

Read this file first. Nothing in it knows which agent sits behind `provider`.
"""
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .provider import Provider

PROMPT = """\
GOAL
{goal}

FINISH LINE
After you stop, the loop runs this command in this folder: {check}
The goal counts as met only when that command exits 0. You cannot declare it met.
"""


class StopReason(Enum):
    GOAL_MET = "goal_met"
    OUT_OF_ITERATIONS = "out_of_iterations"

    @property
    def exit_code(self):
        return 0 if self is StopReason.GOAL_MET else 1


@dataclass(frozen=True)
class Limits:
    max_iterations: int = 5


@dataclass(frozen=True)
class CheckResult:
    exit_code: int
    output: str
    step: int          # the check ran after this worker step; 0 is the pre-check

    @property
    def met(self):
        return self.exit_code == 0


@dataclass
class Run:
    """What happened so far. The report is built from this and nothing else."""
    goal: str
    check: str
    workdir: Path
    limits: Limits
    reason: StopReason | None = None
    steps: int = 0
    last_check: CheckResult | None = None
    last_words: str | None = None
    report: str = ""


def run_check(check, workdir, step=0):
    """Run the user's check command in workdir. Exit code 0 means the goal holds."""
    done = subprocess.run(check, shell=True, cwd=workdir, capture_output=True, text=True)
    return CheckResult(done.returncode, done.stdout + done.stderr, step)


def build_prompt(goal, check):
    return PROMPT.format(goal=goal, check=check)


def run_loop(goal, check, workdir, provider: Provider, limits=Limits()):
    """Run the whole loop and return the finished Run. The report goes to stdout."""
    run = Run(goal, check, Path(workdir), limits)
    run.reason = _steps(run, provider)
    run.report = report(run)
    print(run.report)
    return run


def _steps(run, provider):
    run.last_check = run_check(run.check, run.workdir)             # step 0: the pre-check
    if run.last_check.met:
        return StopReason.GOAL_MET
    for i in range(1, run.limits.max_iterations + 1):
        prompt = build_prompt(run.goal, run.check)
        run.steps = i
        reply = provider.run(prompt, run.workdir, 0)
        run.last_words = reply.text                                # recorded, never trusted
        run.last_check = run_check(run.check, run.workdir, i)     # the loop checks, not the agent
        if run.last_check.met:
            return StopReason.GOAL_MET
    return StopReason.OUT_OF_ITERATIONS


def report(run):
    """The final report. Only the check can make it say GOAL MET."""
    last = run.last_check
    if run.reason is StopReason.GOAL_MET:
        head = [f"subgoal: GOAL MET after step {last.step}: the check exited 0",
                "  This proves what the check tests and nothing more. Read the diff."]
    else:
        head = [f"subgoal: GOAL NOT MET ({run.reason.value})"]
    lines = head + [
        f"  goal      {' '.join(run.goal.split())}",
        f"  check     {run.check}      last run after step {last.step}: exit {last.exit_code}",
        f"  steps     {run.steps} of {run.limits.max_iterations} worker calls",
    ]
    if run.last_words is not None:
        lines.append(f"  worker's last words (unverified): \"{' '.join(run.last_words.split())}\"")
    return "\n".join(lines)
