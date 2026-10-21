"""The loop: call the agent in fresh sessions until the check passes or a budget ends the run.

Read this file first. Nothing in it knows which agent sits behind `provider`.
"""
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .process import run_process
from .provider import Provider, ProviderError

CHECK_TIMEOUT_S = 600   # a check may take this long, so a run's time can overshoot by one check
NOTES_CAP = 8_000       # characters of notes.md that go into the prompt
TAIL_CAP = 4_000        # characters of check output that are kept and go into the prompt

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


class StopReason(str, Enum):
    """Every run ends with exactly one of these."""
    GOAL_MET = "goal_met"
    OUT_OF_ITERATIONS = "out_of_iterations"
    OUT_OF_TIME = "out_of_time"
    OUT_OF_TOKENS = "out_of_tokens"
    PROVIDER_FAILED = "provider_failed"
    PROTECTED_FILE_CHANGED = "protected_file_changed"
    INTERRUPTED = "interrupted"

    @property
    def exit_code(self):
        return {"goal_met": 0, "provider_failed": 3, "protected_file_changed": 3,
                "interrupted": 130}.get(self.value, 1)


@dataclass(frozen=True)
class Limits:
    max_iterations: int = 5          # always bounded: there is no "unlimited"
    max_seconds: float = 30 * 60     # always bounded, checks included
    max_tokens: int | None = None    # enforced only when the provider reports tokens


@dataclass(frozen=True)
class CheckResult:
    exit_code: int | None    # None: the check timed out and was stopped
    output: str              # the last TAIL_CAP characters of stdout, then stderr
    seconds: float
    step: int                # the worker step it ran after; 0 is the pre-check

    def __str__(self):
        return "none (timed out)" if self.exit_code is None else str(self.exit_code)


@dataclass
class Run:
    """The run so far. Saved as run.json after every step; the report reads nothing else."""
    goal: str
    check: str
    dir: Path
    protect: list
    provider: str            # provider.describe(): the tool, model and permissions used
    limits: Limits
    folder: Path             # DIR/.subgoal/<run-id>/
    status: str = "running"
    stop_reason: StopReason | None = None
    error: str | None = None
    steps: int = 0
    seconds: float = 0.0
    tokens: int | None = None        # None means unknown, never 0
    cost_usd: float | None = None
    last_words: str | None = None    # the worker's last reply: recorded, never trusted
    last_check: CheckResult | None = None


def run_check(check, workdir, timeout_s, step=0):
    """Run the user's check command in workdir. Exit code 0 means the goal holds."""
    start = time.monotonic()
    code, out, err, timed_out = run_process(check, workdir, timeout_s, shell=True)
    seconds = round(time.monotonic() - start, 2)
    return CheckResult(None if timed_out else code, (out + err)[-TAIL_CAP:], seconds, step)


def build_prompt(goal, check, protect, notes_path, notes, last):
    """The same template, in the same order, every step. Built from files, never from chat."""
    if len(notes) > NOTES_CAP:
        notes = notes[:NOTES_CAP] + f"\n[... notes cut at {NOTES_CAP:,} characters]"
    first = "(none yet: this is the first step)" if last.step == 0 else "(none: no notes were left)"
    return PROMPT.format(goal=goal.strip(), check=check, protected=", ".join(protect) or "(none)",
                         notes_path=notes_path, notes=notes.strip() or first, step=last.step,
                         code=last, tail=last.output.strip() or "(no output)")


def safe_workdir(path):
    """Resolve DIR, and refuse $HOME and / so the loop never runs on whatever folder you are in."""
    workdir = Path(path).expanduser().resolve()
    if workdir in (Path.home().resolve(), Path("/")) or not workdir.is_dir():
        raise ValueError(f"--dir must be a project folder, not $HOME or / : {workdir}")
    return workdir


def run_loop(goal, check, workdir, provider: Provider, limits=Limits(), protect=(),
             clock=time.monotonic):
    """Run the loop to its end and return the Run. Progress goes to stderr, the report to stdout."""
    workdir = safe_workdir(workdir)
    folder = workdir / ".subgoal" / time.strftime("%Y%m%d-%H%M%S", time.gmtime())   # run id: UTC start
    while folder.exists():                                          # a second run in the same second
        folder = folder.with_name(folder.name + "b")
    folder.mkdir(parents=True)
    run, start = Run(goal, check, workdir, list(protect), provider.describe(), limits, folder), clock()

    def time_left():
        run.seconds = round(clock() - start, 1)
        return limits.max_seconds - run.seconds

    guarded = fingerprint(workdir, run.protect)                     # setup: hash every --protect path
    save(run)
    try:
        run.stop_reason = _steps(run, provider, guarded, time_left)
    except KeyboardInterrupt:
        run.stop_reason = StopReason.INTERRUPTED
    time_left()
    run.status = "ended"
    save(run)
    text = report(run)
    (folder / "report.md").write_text(text + "\n")
    print(text)
    return run


def _steps(run, provider, guarded, time_left):
    run.last_check = _check(run, 0)                                 # step 0: the pre-check
    if run.last_check.exit_code == 0:
        return StopReason.GOAL_MET
    for i in range(1, run.limits.max_iterations + 1):
        if time_left() <= 0:                                        # a
            return StopReason.OUT_OF_TIME
        step, notes = run.folder / f"{i:02d}", run.folder / "notes.md"
        step.mkdir()
        prompt = build_prompt(run.goal, run.check, run.protect, notes.relative_to(run.dir),
                              notes.read_text() if notes.exists() else "", run.last_check)
        (step / "prompt.md").write_text(prompt)                     # b: built from files only
        run.steps, began = i, time.monotonic()
        try:
            reply = provider.run(prompt, run.dir, time_left())      # c: a fresh session
        except ProviderError as error:
            run.error = str(error)
            write_json(step / "call.json", {"error": run.error, "seconds": round(time.monotonic() - began)})
            return StopReason.OUT_OF_TIME if time_left() <= 0 else StopReason.PROVIDER_FAILED
        worker_s = time.monotonic() - began
        (step / "reply.md").write_text(reply.text + "\n")           # d: kept for you, not the model
        write_json(step / "call.json", dict(asdict(reply), text="(reply.md)", seconds=round(worker_s)))
        run.last_words = reply.text
        run.tokens, run.cost_usd = add(run.tokens, reply.tokens), add(run.cost_usd, reply.cost_usd)
        changed = [p for p, digest in fingerprint(run.dir, run.protect).items() if digest != guarded[p]]
        if changed:                                                 # e: the finish line was moved
            run.error = "the worker changed protected paths: " + ", ".join(changed)
            return StopReason.PROTECTED_FILE_CHANGED
        run.last_check = _check(run, i)                             # f: the loop checks, not the agent
        verdict = "met" if run.last_check.exit_code == 0 else "not met"
        print(f"step {i}/{run.limits.max_iterations}  worker {mmss(worker_s)}  "
              f"{count(reply.tokens)} tokens  check exit {run.last_check} ({verdict})", file=sys.stderr)
        save(run)
        if run.last_check.exit_code == 0:                           # g
            return StopReason.GOAL_MET
        if run.limits.max_tokens and (run.tokens or 0) >= run.limits.max_tokens:
            return StopReason.OUT_OF_TOKENS                         # h
        if time_left() <= 0:
            return StopReason.OUT_OF_TIME
    return StopReason.OUT_OF_ITERATIONS


def _check(run, step):
    """Run the check and record it as NN/check.txt."""
    result = run_check(run.check, run.dir, CHECK_TIMEOUT_S, step)
    (run.folder / f"{step:02d}").mkdir(exist_ok=True)
    stopped = "" if result.exit_code is not None else f" (timed out after {CHECK_TIMEOUT_S}s, stopped)"
    (run.folder / f"{step:02d}" / "check.txt").write_text(
        f"command:   {run.check}\nexit code: {result}{stopped}\nseconds:   {result.seconds:.1f}\n\n"
        f"output (last {TAIL_CAP:,} characters):\n{result.output}")
    return result


def fingerprint(workdir, paths):
    """One hash per protected path: a file, or every file in a folder. A missing path hashes too."""
    digests = {}
    for rel in paths:
        root, digest = workdir / rel, hashlib.sha256()
        for f in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if not f.is_dir() and not {".subgoal", "__pycache__"} & set(f.relative_to(root).parts):
                digest.update(str(f).encode() + (f.read_bytes() if f.exists() else b"(missing)"))
        digests[rel] = digest.hexdigest()
    return digests


def add(total, value):
    """Add a reported number to a running total. None means unknown, never 0."""
    return total if value is None else (total or 0) + value


def save(run):
    """Rewrite run.json, so a crash leaves the last known state on disk."""
    write_json(run.folder / "run.json", asdict(run))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def count(n):
    return "unknown" if n is None else f"{n:,}"


def mmss(seconds):
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def report(run):
    """The final report, printed and saved as report.md. Only the check can make it say GOAL MET."""
    last, limits = run.last_check, run.limits
    if run.stop_reason is StopReason.GOAL_MET:
        when = f"after step {last.step}" if last.step else "at the pre-check, before any step"
        lines = [f"subgoal: GOAL MET {when}: the check exited 0",
                 "  This proves what the check tests and nothing more. Read the diff."]
    else:
        lines = [f"subgoal: GOAL NOT MET ({run.stop_reason.value})"]
    limit = f"limit {limits.max_tokens:,}" if limits.max_tokens else "no limit set"
    tokens = f"{count(run.tokens)} ({limit}; as reported by the provider)"
    if run.tokens is None and limits.max_tokens:
        tokens = "unknown: token limit NOT enforced (provider reported no tokens)"
    cost = "unknown" if run.cost_usd is None else \
        f"${run.cost_usd:.2f} (the provider's own estimate; a subscription is not billed per call)"
    if run.steps == 0:
        tokens = cost = "none (no worker calls)"
    ran = f"last run after step {last.step}: exit {last}" if last else "did not finish"
    lines += [f"  goal      {' '.join(run.goal.split())}",
              f"  check     {run.check}      {ran}",
              f"  steps     {run.steps} of {limits.max_iterations} worker calls",
              f"  time      {mmss(run.seconds)} of {mmss(limits.max_seconds)} (checks included)",
              f"  tokens    {tokens}",
              f"  cost      {cost}",
              f"  provider  {run.provider}"]
    if run.error:
        lines.append(f"  error     {run.error}")
    if run.last_words is not None:
        lines.append(f"  worker's last words (unverified): \"{' '.join(run.last_words.split())[:300]}\"")
    if last and last.output.strip():
        lines += ["  check output (tail):"]
        lines += [f"    {line}".rstrip() for line in last.output.strip().splitlines()[-6:]]
    lines.append(f"  records   {run.folder}/")
    return "\n".join(lines)
