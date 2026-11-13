"""The loop: call the agent, a fresh session per step, until the finish line (a check command, a judge, or the check and then
the judge) says met or a budget ends the run. Read this file first. Nothing in it knows which agent sits behind a provider."""
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from .judge import CAP, Verdict, judge_prompt, parse_verdict
from .process import run_process
from .provider import Provider, ProviderError

CHECK_TIMEOUT_S = 600   # a check may take this long, so a run's time can overshoot by one check
NOTES_CAP = 8_000       # characters of notes.md that go into the prompt
TAIL_CAP = 4_000        # characters of check output that are kept and go into the prompt
PLAN_CAP = 6_000        # characters of the plan that go into the prompts

PROMPT = """\
You are one step in a loop working toward a goal in this folder.
You start with no memory of earlier steps. What they learned is in the notes below.

GOAL
{goal}

{plan}FINISH LINE
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
{when}, a separate reviewer reads your result against the goal and decides.
You cannot declare it met. The reviewer goes by the real files and the commands it runs, such
as the tests, not by what you say, and assumes you may have overclaimed: make every part of the
goal true and visible there."""
FIX_CAUSE = "\nFix the cause of what the reviewer names: never reword a true sentence, cut evidence or change a true number to clear it."

FINISH_NONE = """\
Nothing checks your work after you stop: this is the only step. Do not stop after one pass. Keep testing
your change and the code around it, write your own checks, try awkward inputs and edge cases, and fix what
you find, until you cannot find another problem. Then say what you checked."""

PLAN_PROMPT = """\
You plan the work of a loop of workers on the folder {workdir}. You do not do the work, and you change no file.

GOAL
{goal}

Read the real files; you may run commands that only read or exercise the work.
Plan in under 80 lines, fewer for a small goal. Three parts:
PARTS: every program, page, feature, control, rule and flow the goal covers, as found in the files.
SUSPECTED DEFECTS: what looks wrong or untested in each part, with file and line, and the cases workers
skip (modifier keys, empty or repeated input, a reset from a state with progress, real random values).
CHECKS: for each part, one line on how to verify it by running it. Give no phases, steps or order: the workers choose.
"""
FIRST_PLAN = "PLAN (made by a read-only planner before step 1: what to cover and what looks wrong; a guide, not steps)"
FOLLOW_UP = """\
The finish line says the goal is not met yet. You did the earlier steps in this chat; this is what it said after your last one.

{seen}

Fix the cause in the files, then check your work again. The rules of your first message still hold: before you stop, rewrite {notes_path} in full.{fix}"""
# What differs between two runs of one failure, so it does not make a failure new: addresses, dates, clock times, timings
NOISE = re.compile(r"0x[0-9a-f]+|\d{4}-\d\d-\d\d|\d?\d(:\d\d){2}([.,]\d+)?|duration_ms:? \d[\d.]*|\d[\d.]*\s?(ms|s|sec|seconds)\b", re.I)


class StopReason(str, Enum):
    """Every run ends with exactly one of these."""
    GOAL_MET = "goal_met"
    OUT_OF_ITERATIONS = "out_of_iterations"
    OUT_OF_TIME = "out_of_time"
    OUT_OF_TOKENS = "out_of_tokens"
    PROVIDER_FAILED = "provider_failed"
    PROTECTED_FILE_CHANGED = "protected_file_changed"
    INTERRUPTED = "interrupted"
    UNCHECKED = "unchecked"
    CHECK_ALREADY_PASSES = "check_already_passes"   # before any step: it cannot tell done from not done

    @property
    def exit_code(self):
        return {"goal_met": 0, "unchecked": 0, "check_already_passes": 2, "provider_failed": 3,
                "protected_file_changed": 3, "interrupted": 130}.get(self.value, 1)


@dataclass(frozen=True)
class Limits:
    max_iterations: int = 4          # always bounded: there is no "unlimited"
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
    check: str | None        # None: no command, so a judge decides, or nothing does
    dir: Path
    protect: list
    provider: str            # provider.describe(): the tool, model and permissions used
    limits: Limits
    folder: Path             # records_home(DIR)/<run-id>/
    status: str = "running"
    stop_reason: StopReason | None = None
    error: str | None = None
    steps: int = 0
    seconds: float = 0.0
    tokens: int | None = None        # None means unknown, never 0
    cost_usd: float | None = None
    last_words: str | None = None    # the worker's last reply: recorded, never trusted
    last_check: CheckResult | None = None
    judge: str | None = None         # judge.describe(); None: no judge (with a check, it is asked once the check passes)
    judge_calls: int = 0
    last_verdict: Verdict | None = None
    same_since: int = 0              # the check has printed the same since this step (0: the pre-check)
    chats: list = field(default_factory=list)    # every session that gave an id: who, step, and the command that continues it


def run_check(check, workdir, timeout_s, step=0):
    start = time.monotonic()
    code, out, err, timed_out = run_process(check, workdir, timeout_s, shell=True)
    return CheckResult(None if timed_out else code, (out + err)[-TAIL_CAP:], round(time.monotonic() - start, 2), step)


def build_prompt(goal, check, protect, notes_path, notes, last, skills=(), plan="", judged=True):
    """The same template every step, built from files, never from chat. `last` is the last CheckResult or Verdict."""
    notes = notes[:NOTES_CAP] + f"\n[... notes cut at {NOTES_CAP:,} characters]" if len(notes) > NOTES_CAP else notes
    plan = f"{plan}\n\n" if plan else ""                         # a plan comes with its PLAN (made ...) line
    if check is None and not judged:      # the one step has no loop to explain, no notes or review to pass on
        keep = f"\nDo not edit these paths (the run stops if they change): {', '.join(protect)}\n" if protect else ""
        return f"{goal.strip()}\n\n{plan}{FINISH_NONE}\n{keep}"
    new = last is None or last.step == 0
    review = FINISH_JUDGE.format(when="After you stop" if check is None else "Once it exits 0")   # the judge comes second
    finish = review + FIX_CAUSE if check is None else FINISH_CHECK.format(check=check) + ("\n" + review) * judged
    seen = last_said(last)
    if skills:                                  # a fresh session knows no skill unless it is told
        seen += (f"\n\nSKILLS EARLIER STEPS USED: {', '.join(skills)}. Invoke them again before you change the work "
                 "(the Skill tool shows where their files are); do not search the disk for them.")
    no_notes = "(none yet: this is the first step)" if new else "(none: no notes were left)"
    return PROMPT.format(goal=goal.strip(), finish=finish, protected=", ".join(protect) or "(none)", plan=plan,
                         notes_path=notes_path, notes=notes.strip() or no_notes, last=seen)


def last_said(last):
    """What the finish line said last, as the worker reads it: the review (no check, or the check passed), else the check."""
    if last is None or isinstance(last, Verdict):
        return "LAST REVIEW (none yet: this is the first step)" if last is None or last.step == 0 else "LAST REVIEW " \
            f"(after step {last.step}: a reviewer's opinion, not a command result; at most its last {CAP:,} characters)\n{last.reasons}"
    return f"LAST CHECK (after step {last.step}, exit code {last}, last 4,000 characters of output)\n{last.output.strip() or '(no output)'}"


def safe_workdir(path):
    """Resolve DIR, and refuse $HOME and / so the loop never runs on whatever folder you are in."""
    workdir = Path(path).expanduser().resolve()
    if workdir in (Path.home().resolve(), Path("/")) or not workdir.is_dir():
        raise ValueError(f"--dir must be a project folder, not $HOME or / : {workdir}")
    return workdir


def records_home(workdir):
    """Where a folder's runs are kept, outside it: $XDG_DATA_HOME (~/.local/share) /subgoal/<its path, with - for />/"""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share", "subgoal", str(workdir).replace("/", "-"))


def run_loop(goal, check, workdir, provider: Provider, limits=Limits(), protect=(),
             clock=time.monotonic, judge: Provider | None = None, plan: Provider | None = None, same_session=False):
    """Run the loop to its end and return the Run (progress to stderr, the report to stdout). The finish line is a
    `check` command, a `judge` (a provider built with edit=False), or the check and then the judge; with neither, one step
    runs, unchecked. `plan`, such a provider too, plans before step 1 when there is no check. `same_session`: see _work()."""
    workdir = safe_workdir(workdir)
    folder = records_home(workdir) / time.strftime("%Y%m%d-%H%M%S", time.gmtime())   # run id: UTC start
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
        run.stop_reason = _steps(run, provider, judge, guarded, time_left, plan, same_session)
    except KeyboardInterrupt:
        run.stop_reason = StopReason.INTERRUPTED
    time_left()
    run.status = "ended"
    save(run)
    (folder / "report.md").write_text((text := report(run)) + "\n")
    print(text)
    return run


def _steps(run, provider, judge, guarded, time_left, planner=None, same_session=False):
    if run.check is not None:                                       # step 0: the pre-check
        run.last_check = _check(run, 0)
        if run.last_check.exit_code == 0 and judge is None:         # with a judge, a passing check guards
            return StopReason.CHECK_ALREADY_PASSES                  # alone, no step could show in it
    traces, session = {}, None              # step: its trace.md, as the loop wrote it; the worker's chat, if it is continued
    plan = _plan(run, planner, time_left) if planner and run.check is None else ""      # a check: no plan
    for i in range(1, run.limits.max_iterations + 1):
        if stop := _budget(run, time_left):                         # a: no step past a limit, a plan's tokens too
            return stop
        step, notes = run.folder / f"{i:02d}", run.folder / "notes.md"
        step.mkdir()
        skills = sorted({s[6:].strip() for t in traces.values() for s in t.splitlines() if s.startswith("Skill ")})
        prompt = build_prompt(run.goal, run.check, run.protect, notes,
                              notes.read_text() if notes.exists() else "",
                              newest(run), skills, plan, judge is not None)
        run.steps, began = i, time.monotonic()
        try:
            reply = _work(run, provider, step, prompt, session, time_left)      # c: a fresh session, or the same chat
        except ProviderError as error:
            run.error = str(error)
            write_json(step / "call.json", {"error": run.error, "seconds": round(time.monotonic() - began)})
            return StopReason.OUT_OF_TIME if time_left() <= 0 else StopReason.PROVIDER_FAILED
        worker_s = time.monotonic() - began
        (step / "reply.md").write_text(reply.text + "\n")           # d: kept for you, not the model
        if reply.trace is not None:                                 # its tool calls, as the provider saw them
            traces[i] = (reply.trace or "(no tool calls)") + "\n"
        for old in run.folder.glob("*/trace.md"):                   # the worker can edit the records, so
            old.unlink()                                            # the loop writes back its own copies
        for n, trace in traces.items():
            (run.folder / f"{n:02d}" / "trace.md").write_text(trace)
        write_json(step / "call.json", dict(asdict(reply), text="(reply.md)", seconds=round(worker_s),
                                            trace=None if reply.trace is None else "(trace.md)"))
        run.last_words, session = reply.text, reply.session if same_session else None
        run.tokens, run.cost_usd = add(run.tokens, reply.tokens), add(run.cost_usd, reply.cost_usd)
        if _moved(run, guarded, "worker"):                          # e: the finish line was moved
            return StopReason.PROTECTED_FILE_CHANGED
        if judge is None and run.check is None:
            return StopReason.UNCHECKED                             # nothing decides: one step, then it ends
        met, said = True, ""                                        # f: the loop decides, not the agent
        if run.check is not None:                                   # first the check, which no model can talk past
            prev, run.last_check = run.last_check, _check(run, i)
            met = run.last_check.exit_code == 0
            said = f"check exit {run.last_check} ({'met' if met else 'not met'})"
            run.same_since = run.same_since if same_failure(prev, run.last_check) else i
        if judge is not None and met:                               # then the judge, only once the check holds
            if stop := _budget(run, time_left):                     # no judge call past a limit
                return stop
            try:
                run.last_verdict = _judge(run, judge, step, time_left)
            except ProviderError as error:
                run.error = f"the judge failed: {error}"
                return StopReason.OUT_OF_TIME if time_left() <= 0 else StopReason.PROVIDER_FAILED
            if _moved(run, guarded, "judge"):                       # a command it ran moved it
                return StopReason.PROTECTED_FILE_CHANGED            # before its verdict counts
            met, said = run.last_verdict.met, ", ".join(filter(None, (said, f"judge: {run.last_verdict}")))
        print(f"step {i}/{run.limits.max_iterations}  worker {mmss(worker_s)}  "
              f"{count(reply.tokens)} tokens  {said}", file=sys.stderr)
        save(run)
        if met:                                                     # g
            return StopReason.GOAL_MET
        if stop := _budget(run, time_left):                         # h
            return stop
    return StopReason.OUT_OF_ITERATIONS


def _work(run, provider, step, prompt, session, time_left):
    """One worker call (NN/prompt.md). With `session`, that chat goes on with only what the finish line said; if it cannot, a fresh one."""
    if session:
        short = FOLLOW_UP.format(seen=last_said(last := newest(run)), fix=FIX_CAUSE * isinstance(last, Verdict), notes_path=step.parent / "notes.md")
        (step / "prompt.md").write_text(short)
        try:
            return _chat(run, "worker", provider, provider.run(short, run.dir, time_left(), resume=session))
        except ProviderError as error:
            print(f"step {run.steps}: the worker's chat could not be continued ({error}); a fresh session instead", file=sys.stderr)
    (step / "prompt.md").write_text(prompt)                         # b: built from files only
    return _chat(run, "worker", provider, provider.run(prompt, run.dir, time_left()))


def _chat(run, who, provider, reply):
    """Record how to continue the session behind `reply`, once per session; returns the reply."""
    if reply.session and (how := getattr(provider, "CONTINUE", None)) and reply.session not in [c["session"] for c in run.chats]:
        run.chats.append({"who": who, "step": run.steps, "session": reply.session, "continue": how.format(reply.session)})
    return reply


def newest(run):   # what the finish line said last; on one step the verdict, as the judge comes after the check
    return max(filter(None, (run.last_verdict, run.last_check)), key=lambda r: r.step, default=None)


def same_failure(a, b):   # NOISE aside; only the end, as a timing that changes width moves where the tail starts; a pass is none
    return a.exit_code == b.exit_code != 0 and NOISE.sub("", a.output)[-TAIL_CAP // 2:] == NOISE.sub("", b.output)[-TAIL_CAP // 2:]


def _moved(run, guarded, who):
    """True, with run.error set, if a --protect path changed since `guarded` was taken, at the start."""
    if changed := [p for p, digest in fingerprint(run.dir, run.protect).items() if digest != guarded[p]]:
        run.error = f"the {who} changed protected paths: " + ", ".join(changed)
    return bool(changed)


def _budget(run, time_left):
    if run.limits.max_tokens and (run.tokens or 0) >= run.limits.max_tokens:
        return StopReason.OUT_OF_TOKENS
    return StopReason.OUT_OF_TIME if time_left() <= 0 else None


def _judge(run, judge, step, time_left):
    """Ask a fresh no-edit session if the goal is met (NN/judge-prompt.md, judge.md, judge-call.json). May raise ProviderError."""
    prompt, began = judge_prompt(run.goal, run.dir, run.last_words, run.folder), time.monotonic()
    (step / "judge-prompt.md").write_text(prompt)
    run.judge_calls += 1
    reply = _chat(run, "judge", judge, judge.run(prompt, run.dir, time_left()))
    (step / "judge.md").write_text(reply.text + "\n")
    write_json(step / "judge-call.json", dict(asdict(reply), text="(judge.md)", trace=None, seconds=round(time.monotonic() - began)))
    run.tokens, run.cost_usd = add(run.tokens, reply.tokens), add(run.cost_usd, reply.cost_usd)
    return parse_verdict(reply.text, run.steps)


def _plan(run, planner, time_left):
    """One read-only session before step 1 (plan-prompt.md, plan.md); if it fails: no plan, not a failed run."""
    prompt = PLAN_PROMPT.format(goal=run.goal.strip(), workdir=run.dir)
    (run.folder / "plan-prompt.md").write_text(prompt)
    try:
        reply = _chat(run, "planner", planner, planner.run(prompt, run.dir, time_left()))
    except ProviderError as error:
        print(f"plan failed, going on without one: {error}", file=sys.stderr)
        return ""
    (run.folder / "plan.md").write_text(reply.text + "\n")
    run.tokens, run.cost_usd = add(run.tokens, reply.tokens), add(run.cost_usd, reply.cost_usd)
    print(f"plan  {count(reply.tokens)} tokens", file=sys.stderr)
    text = reply.text.strip()[:PLAN_CAP]
    return f"{FIRST_PLAN}\n{text}" if text else ""


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
    return {rel: _digest(workdir / rel) for rel in paths}


def _digest(root):
    digest = hashlib.sha256()
    for f in sorted(root.rglob("*")) if root.is_dir() else [root]:
        if not f.is_dir() and "__pycache__" not in f.relative_to(root).parts:
            digest.update(str(f).encode() + (f.read_bytes() if f.exists() else b"(missing)"))
    return digest.hexdigest()


def add(total, value):
    return total if value is None else (total or 0) + value


def save(run):
    write_json(run.folder / "run.json", asdict(run))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def count(n):
    return "unknown" if n is None else f"{n:,}"


def mmss(seconds):
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def report(run):
    """The final report, printed and saved as report.md. Only the finish line can make it say GOAL MET."""
    last, limits, judged, checked = newest(run), run.limits, run.judge is not None, run.check is not None
    if run.stop_reason is StopReason.GOAL_MET:
        said = " and ".join(["the check exited 0"] * checked + ["a judge said met"] * judged)
        caveat = " ".join(["This proves what the check tests and nothing more."] * checked +
                          ["The judge's met is a model's opinion, not a command's exit code."] * judged)
        lines = [f"subgoal: GOAL MET after step {last.step}: {said}", f"  {caveat} Read the diff."]
    elif run.stop_reason is StopReason.CHECK_ALREADY_PASSES:
        lines = ["subgoal: CHECK ALREADY PASSES before any step, so it cannot tell done from not done.",
                 "  No step ran: make it fail first (write the failing test), or the work is already done."]
    elif run.stop_reason is StopReason.UNCHECKED:
        lines = ["subgoal: DONE, NOT CHECKED: one step ran, and nothing verified it. Read the diff and run the tests."]
    else:
        lines = [f"subgoal: GOAL NOT MET ({run.stop_reason.value})"]
    limit = f"limit {limits.max_tokens:,}" if limits.max_tokens else "no limit set"
    tokens = "unknown: token limit NOT enforced (provider reported no tokens)" if run.tokens is None and limits.max_tokens \
        else f"{count(run.tokens)} ({limit}; as reported by the provider)"
    cost = "unknown" if run.cost_usd is None else \
        f"${run.cost_usd:.2f} (the provider's own estimate; a subscription is not billed per call)"
    if run.steps == 0 and run.tokens is None and run.cost_usd is None:      # a plan's numbers still show
        tokens = cost = "none (no worker calls)"
    gates, c, v = [], run.last_check, run.last_verdict                      # one line per finish line, in its order
    if checked or not judged:
        same = f", the same failure since {f'step {run.same_since}' if run.same_since else 'the pre-check'}"   # no step moved it
        gates.append(f"check     {run.check or 'none, and no judge'}      " +
                     (f"last run after step {c.step}: exit {c}{same * (c.step > run.same_since)}" if c else "not run"))
    if judged:
        gates.append(f"judge     {'once the check exits 0' if checked else 'no check command'}: an agent with no edit tools decides"
                     f"      {f'last verdict after step {v.step}: {v}' if v else 'no verdict yet'}")
    calls = f" (judge calls: {run.judge_calls})" * judged
    lines += [f"  goal      {' '.join(run.goal.split())}",
              *[f"  {gate}" for gate in gates],
              f"  steps     {run.steps} of {limits.max_iterations} worker calls{calls}",
              f"  time      {mmss(run.seconds)} of {mmss(limits.max_seconds)} "
              f"({' and '.join(['checks'] * checked + ['judge calls'] * judged) or 'all calls'} included)",
              f"  tokens    {tokens}",
              f"  cost      {cost}",
              f"  provider  {run.provider}",
              *[f"  judge via {run.judge}"] * judged, *[f"  error     {run.error}"] * bool(run.error)]
    words = f"{w[:150]} [...] {w[-150:]}" if len(w := " ".join((run.last_words or "").split())) > 300 else w   # the end: what it left
    lines += [f"  worker's last words (unverified): \"{words}\""] if run.last_words is not None else []
    detail = last and (last.reasons if isinstance(last, Verdict) else last.output).strip()
    if detail:
        lines.append("  judge's reasons (a model's opinion; last lines):" if isinstance(last, Verdict) else "  check output (tail):")
        lines += [f"    {line}".rstrip() for line in detail.splitlines()[-6:]]
    lines += [f"  {'continue' if not n else '        '}  {c['who']}, step {c['step']}: {c['continue']}" + f"  (run in {run.dir})" * (not n)
              for n, c in enumerate(run.chats)]
    return "\n".join(lines + [f"  records   {run.folder}/"])
