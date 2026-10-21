"""The loop: call the agent in fresh sessions until the check passes or a budget ends the run.

Read this file first. Nothing in it knows which agent sits behind `provider`.
"""
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from .provider import Provider

NOTES_CAP = 8_000   # characters of notes.md that go into the prompt
TAIL_CAP = 4_000    # characters of check output that go into the prompt

PROMPT = """\
You are one step in a loop working toward a goal in this folder.
You start with no memory of earlier steps. What they learned is in the notes below.

GOAL
{goal}

FINISH LINE
After you stop, the loop runs this command in this folder: {check}
The goal counts as met only when that command exits 0. You cannot declare it met.
You may run the command yourself to see where things stand.

RULES
- Work on the most important remaining part. One thing done fully beats many half-done.
- Do not edit these paths (the run stops if they change): {protected}
- Before you stop, rewrite {notes_path} in full: what you did, what you learned,
  what should happen next. Keep it under 40 lines.

NOTES FROM EARLIER STEPS
{notes}

LAST CHECK (after step {step}, exit code {code}, last 4,000 characters of output)
{tail}
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
    seconds: float
    step: int          # the check ran after this worker step; 0 is the pre-check

    @property
    def met(self):
        return self.exit_code == 0


@dataclass
class Run:
    """What happened so far. run.json and the report are built from this alone."""
    goal: str
    check: str
    workdir: Path
    protect: list
    provider: str
    limits: Limits
    folder: Path
    status: str = "running"
    reason: StopReason | None = None
    steps: int = 0
    tokens: int | None = None
    cost_usd: float | None = None
    last_check: CheckResult | None = field(default=None, repr=False)
    last_words: str | None = None
    report: str = ""


def run_check(check, workdir, step=0):
    """Run the user's check command in workdir. Exit code 0 means the goal holds."""
    start = time.monotonic()
    done = subprocess.run(check, shell=True, cwd=workdir, capture_output=True, text=True)
    return CheckResult(done.returncode, done.stdout + done.stderr, time.monotonic() - start, step)


def build_prompt(goal, check, protect, notes_path, notes, last):
    """The same template, in the same order, every step. Built from files, never from chat."""
    if len(notes) > NOTES_CAP:
        notes = notes[:NOTES_CAP] + f"\n[... notes cut at {NOTES_CAP:,} characters]"
    first = "(none yet: this is the first step)" if last.step == 0 else "(none: no notes were left)"
    return PROMPT.format(goal=goal.strip(), check=check, protected=", ".join(protect) or "(none)",
                         notes_path=notes_path, notes=notes.strip() or first, step=last.step,
                         code=last.exit_code, tail=last.output[-TAIL_CAP:].strip() or "(no output)")


def run_loop(goal, check, workdir, provider: Provider, limits=Limits(), protect=()):
    """Run the whole loop and return the finished Run. Progress goes to stderr, the report to stdout."""
    workdir = Path(workdir).resolve()
    run = Run(goal, check, workdir, list(protect), provider.describe(), limits, new_run_folder(workdir))
    save(run)
    run.reason = _steps(run, provider)
    run.status, run.report = "ended", report(run)
    save(run)
    (run.folder / "report.md").write_text(run.report + "\n")
    print(run.report)
    return run


def _steps(run, provider):
    run.last_check = _check(run, 0)                                 # step 0: the pre-check
    if run.last_check.met:
        return StopReason.GOAL_MET
    for i in range(1, run.limits.max_iterations + 1):
        step = run.folder / f"{i:02d}"
        step.mkdir()
        notes_file = run.folder / "notes.md"                        # b: the prompt, from files only
        notes = notes_file.read_text() if notes_file.exists() else ""
        prompt = build_prompt(run.goal, run.check, run.protect,
                              notes_file.relative_to(run.workdir), notes, run.last_check)
        (step / "prompt.md").write_text(prompt)
        run.steps, began = i, time.monotonic()
        reply = provider.run(prompt, run.workdir, 0)                # c: a fresh session
        (step / "reply.md").write_text(reply.text + "\n")           # d: kept for you, not the model
        write_json(step / "call.json", {"tokens": reply.tokens, "cost_usd": reply.cost_usd,
                                        "session": reply.session, "seconds": round(time.monotonic() - began, 1)})
        run.last_words = reply.text                                 # recorded, never trusted
        run.tokens = add(run.tokens, reply.tokens)
        run.cost_usd = add(run.cost_usd, reply.cost_usd)
        run.last_check = _check(run, i)                             # f: the loop checks, not the agent
        progress(run, reply)
        save(run)
        if run.last_check.met:                                      # g
            return StopReason.GOAL_MET
    return StopReason.OUT_OF_ITERATIONS


def _check(run, step):
    result = run_check(run.check, run.workdir, step)
    folder = run.folder / f"{step:02d}"
    folder.mkdir(exist_ok=True)
    (folder / "check.txt").write_text(
        f"command:   {run.check}\nexit code: {result.exit_code}\nseconds:   {result.seconds:.1f}\n\n"
        f"output (last {TAIL_CAP:,} characters):\n{result.output[-TAIL_CAP:]}")
    return result


def add(total, value):
    """Add a reported number to a running total. None means unknown, never 0."""
    return total if value is None else (total or 0) + value


def new_run_folder(workdir):
    """DIR/.subgoal/<UTC start time>/: one folder per run, holding every record."""
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    folder, n = workdir / ".subgoal" / run_id, 1
    while folder.exists():
        n += 1
        folder = workdir / ".subgoal" / f"{run_id}-{n}"
    folder.mkdir(parents=True)
    return folder


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def save(run):
    """Rewrite run.json, so a crash leaves the last known state on disk."""
    write_json(run.folder / "run.json", {
        "goal": run.goal, "check": run.check, "dir": str(run.workdir), "protect": run.protect,
        "provider": run.provider, "limits": asdict(run.limits), "status": run.status,
        "stop_reason": run.reason and run.reason.value, "steps": run.steps, "tokens": run.tokens,
        "cost_usd": run.cost_usd, "last_check_exit_code": run.last_check and run.last_check.exit_code})


def progress(run, reply):
    tokens = "unknown" if reply.tokens is None else f"{reply.tokens:,}"
    verdict = "met" if run.last_check.met else "not met"
    print(f"step {run.steps}/{run.limits.max_iterations}  {tokens} tokens  "
          f"check exit {run.last_check.exit_code} ({verdict})", file=sys.stderr)


def report(run):
    """The final report, printed and saved as report.md. Only the check can make it say GOAL MET."""
    last, nothing = run.last_check, "0 (no worker calls)" if run.steps == 0 else "unknown"
    if run.reason is StopReason.GOAL_MET:
        when = f"after step {last.step}" if last.step else "before any step"
        head = [f"subgoal: GOAL MET {when}: the check exited 0",
                "  This proves what the check tests and nothing more. Read the diff."]
    else:
        head = [f"subgoal: GOAL NOT MET ({run.reason.value})"]
    lines = head + [
        f"  goal      {' '.join(run.goal.split())}",
        f"  check     {run.check}      last run after step {last.step}: exit {last.exit_code}",
        f"  steps     {run.steps} of {run.limits.max_iterations} worker calls",
        f"  tokens    {nothing if run.tokens is None else f'{run.tokens:,} (as reported by the provider)'}",
        f"  cost      {nothing if run.cost_usd is None else f'${run.cost_usd:.2f} (the provider estimate; a subscription is not billed per call)'}",
        f"  provider  {run.provider}"]
    if run.last_words is not None:
        lines.append(f"  worker's last words (unverified): \"{' '.join(run.last_words.split())[:300]}\"")
    lines += ["  check output (tail):"] + [f"    {line}" for line in last.output.strip().splitlines()[-6:]]
    lines.append(f"  records   {run.folder}/")
    return "\n".join(lines)
