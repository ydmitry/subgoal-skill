"""The loop: call the agent in fresh sessions until the finish line says met or a budget ends the run.

The finish line is a check command, or, with no command, a judge (judge.py).
Read this file first. Nothing in it knows which agent sits behind `provider` or `judge`.
"""
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .judge import Verdict, judge_prompt, parse_verdict
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
{finish}

RULES
- Work on the most important remaining part. One thing done fully beats many half-done.
- Do not edit these paths (the run stops if they change): {protected}
- Before you stop, rewrite {notes_path} in full: what you did, what you learned,
  what should happen next. Keep it under 40 lines.

NOTES FROM EARLIER STEPS
{notes}

{last}
"""

FINISH_CHECK = """\
After you stop, the loop runs this command in this folder: {check}
The goal counts as met only when that command exits 0. You cannot declare it met.
You may run the command yourself to see where things stand."""

FINISH_JUDGE = """\
After you stop, a separate reviewer reads your result against the goal and decides.
You cannot declare it met. The reviewer goes by the real files and the commands it runs, such
as the tests, not by what you say, and assumes you may have overclaimed: make every part of the
goal true and visible there. You may run commands in this folder yourself, such as the tests."""


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
    check: str | None        # None: no command, so a judge decides
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
    judge: str | None = None         # judge.describe(); None when a check command is the finish line
    judge_calls: int = 0
    last_verdict: Verdict | None = None


def run_check(check, workdir, timeout_s, step=0):
    """Run the user's check command in workdir. Exit code 0 means the goal holds."""
    start = time.monotonic()
    code, out, err, timed_out = run_process(check, workdir, timeout_s, shell=True)
    seconds = round(time.monotonic() - start, 2)
    return CheckResult(None if timed_out else code, (out + err)[-TAIL_CAP:], seconds, step)


def build_prompt(goal, check, protect, notes_path, notes, last):
    """The same template, in the same order, every step. Built from files, never from chat.

    `last` is the last CheckResult (the pre-check, before step 1), or, with no check
    command, the last Verdict (None before step 1)."""
    if len(notes) > NOTES_CAP:
        notes = notes[:NOTES_CAP] + f"\n[... notes cut at {NOTES_CAP:,} characters]"
    new = last is None or last.step == 0
    if check is None:
        finish = FINISH_JUDGE
        seen = "LAST REVIEW (none yet: this is the first step)" if new else (
            f"LAST REVIEW (after step {last.step}: a reviewer's opinion, not a command result)\n"
            f"{last.reasons}")
    else:
        finish = FINISH_CHECK.format(check=check)
        seen = f"LAST CHECK (after step {last.step}, exit code {last}, last 4,000 characters of output)\n" \
               f"{last.output.strip() or '(no output)'}"
    return PROMPT.format(goal=goal.strip(), finish=finish, protected=", ".join(protect) or "(none)",
                         notes_path=notes_path, last=seen,
                         notes=notes.strip() or ("(none yet: this is the first step)" if new
                                                 else "(none: no notes were left)"))


def safe_workdir(path):
    """Resolve DIR, and refuse $HOME and / so the loop never runs on whatever folder you are in."""
    workdir = Path(path).expanduser().resolve()
    if workdir in (Path.home().resolve(), Path("/")) or not workdir.is_dir():
        raise ValueError(f"--dir must be a project folder, not $HOME or / : {workdir}")
    return workdir


def run_loop(goal, check, workdir, provider: Provider, limits=Limits(), protect=(),
             clock=time.monotonic, judge: Provider | None = None):
    """Run the loop to its end and return the Run. Progress goes to stderr, the report to stdout.

    The finish line is `check`, a command, or else `judge`, a provider built with edit=False.
    Exactly one."""
    if (check is None) == (judge is None):
        raise ValueError("a run needs a check command or a judge: exactly one of them")
    workdir = safe_workdir(workdir)
    folder = workdir / ".subgoal" / time.strftime("%Y%m%d-%H%M%S", time.gmtime())   # run id: UTC start
    while folder.exists():                                          # a second run in the same second
        folder = folder.with_name(folder.name + "b")
    folder.mkdir(parents=True)
    run, start = Run(goal, check, workdir, list(protect), provider.describe(), limits, folder,
                     judge=judge.describe() if judge else None), clock()

    def time_left():
        run.seconds = round(clock() - start, 1)
        return limits.max_seconds - run.seconds

    guarded = fingerprint(workdir, run.protect)                     # setup: hash every --protect path
    save(run)
    try:
        run.stop_reason = _steps(run, provider, judge, guarded, time_left)
    except KeyboardInterrupt:
        run.stop_reason = StopReason.INTERRUPTED
    time_left()
    run.status = "ended"
    save(run)
    text = report(run)
    (folder / "report.md").write_text(text + "\n")
    print(text)
    return run


def _steps(run, provider, judge, guarded, time_left):
    if judge is None:                                               # step 0: the pre-check
        run.last_check = _check(run, 0)
        if run.last_check.exit_code == 0:
            return StopReason.GOAL_MET
    for i in range(1, run.limits.max_iterations + 1):
        if time_left() <= 0:                                        # a
            return StopReason.OUT_OF_TIME
        step, notes = run.folder / f"{i:02d}", run.folder / "notes.md"
        step.mkdir()
        prompt = build_prompt(run.goal, run.check, run.protect, notes.relative_to(run.dir),
                              notes.read_text() if notes.exists() else "",
                              run.last_check if judge is None else run.last_verdict)
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
        if judge is None:
            run.last_check = _check(run, i)                         # f: the loop checks, not the agent
            met = run.last_check.exit_code == 0
            said = f"check exit {run.last_check} ({'met' if met else 'not met'})"
        else:
            if stop := _budget(run, time_left):                     # f: no judge call past a limit
                return stop
            try:
                run.last_verdict = _judge(run, judge, step, time_left)
            except ProviderError as error:
                run.error = f"the judge failed: {error}"
                return StopReason.OUT_OF_TIME if time_left() <= 0 else StopReason.PROVIDER_FAILED
            met, said = run.last_verdict.met, f"judge: {run.last_verdict}"
        print(f"step {i}/{run.limits.max_iterations}  worker {mmss(worker_s)}  "
              f"{count(reply.tokens)} tokens  {said}", file=sys.stderr)
        save(run)
        if met:                                                     # g
            return StopReason.GOAL_MET
        if stop := _budget(run, time_left):                         # h
            return stop
    return StopReason.OUT_OF_ITERATIONS


def _budget(run, time_left):
    """The stop reason if a limit is reached, else None."""
    if run.limits.max_tokens and (run.tokens or 0) >= run.limits.max_tokens:
        return StopReason.OUT_OF_TOKENS
    if time_left() <= 0:
        return StopReason.OUT_OF_TIME


def _judge(run, judge, step, time_left):
    """Ask a fresh session with no edit tools if the goal is met. The worker's last words go in as
    a claim. Records NN/judge-prompt.md, judge.md and judge-call.json; raises ProviderError like any call."""
    prompt, began = judge_prompt(run.goal, run.dir, run.last_words), time.monotonic()
    (step / "judge-prompt.md").write_text(prompt)
    run.judge_calls += 1
    reply = judge.run(prompt, run.dir, time_left())
    (step / "judge.md").write_text(reply.text + "\n")
    write_json(step / "judge-call.json", dict(asdict(reply), text="(judge.md)",
                                              seconds=round(time.monotonic() - began)))
    run.tokens, run.cost_usd = add(run.tokens, reply.tokens), add(run.cost_usd, reply.cost_usd)
    return parse_verdict(reply.text, run.steps)


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
    """The final report, printed and saved as report.md. Only the finish line can make it say GOAL MET."""
    last, limits = run.last_check or run.last_verdict, run.limits
    judged = run.judge is not None
    if run.stop_reason is StopReason.GOAL_MET:
        when = f"after step {last.step}" if last.step else "at the pre-check, before any step"
        said = "a judge said met" if judged else "the check exited 0"
        caveat = ("That is a model's opinion, not a check command's exit code." if judged else
                  "This proves what the check tests and nothing more.")
        lines = [f"subgoal: GOAL MET {when}: {said}", f"  {caveat} Read the diff."]
    else:
        lines = [f"subgoal: GOAL NOT MET ({run.stop_reason.value})"]
        if judged and last and last.unseen:
            lines += [f"  the judge's last review says the files hold and only parts no file or command can "
                      f"show are left (a model's opinion): {last.unseen}",
                      "  Give the goal a --check that shows them, or take them out of it."]
    limit = f"limit {limits.max_tokens:,}" if limits.max_tokens else "no limit set"
    tokens = f"{count(run.tokens)} ({limit}; as reported by the provider)"
    if run.tokens is None and limits.max_tokens:
        tokens = "unknown: token limit NOT enforced (provider reported no tokens)"
    cost = "unknown" if run.cost_usd is None else \
        f"${run.cost_usd:.2f} (the provider's own estimate; a subscription is not billed per call)"
    if run.steps == 0:
        tokens = cost = "none (no worker calls)"
    if judged:
        ran = f"last verdict after step {last.step}: {last}" if last else "no verdict yet"
        finish = f"judge     no check command: an agent with no edit tools decides      {ran}"
    else:
        ran = f"last run after step {last.step}: exit {last}" if last else "did not finish"
        finish = f"check     {run.check}      {ran}"
    calls = f" (judge calls: {run.judge_calls})" if judged else ""
    lines += [f"  goal      {' '.join(run.goal.split())}",
              f"  {finish}",
              f"  steps     {run.steps} of {limits.max_iterations} worker calls{calls}",
              f"  time      {mmss(run.seconds)} of {mmss(limits.max_seconds)} "
              f"({'judge calls' if judged else 'checks'} included)",
              f"  tokens    {tokens}",
              f"  cost      {cost}",
              f"  provider  {run.provider}"]
    if judged:
        lines.append(f"  judge via {run.judge}")
    if run.error:
        lines.append(f"  error     {run.error}")
    if run.last_words is not None:
        lines.append(f"  worker's last words (unverified): \"{' '.join(run.last_words.split())[:300]}\"")
    detail = last and (last.reasons if judged else last.output)
    if detail and detail.strip():
        lines.append("  judge's reasons (a model's opinion; last lines):" if judged else
                     "  check output (tail):")
        shown = detail.strip().splitlines()
        lines += [f"    {line}".rstrip() for line in shown[-6:]]
    lines.append(f"  records   {run.folder}/")
    return "\n".join(lines)
