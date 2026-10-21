# subgoal

A small Python program (about 390 lines, standard library only) that hands a goal to
a coding agent and keeps calling it, in a fresh session each time, until a check
command that the agent does not control says the goal holds, or until a budget ends
the run.

It is a teaching example, not a product or a framework. Every part of the loop is
in plain sight: where the finish line is checked, where state is kept on disk, how
each call starts with an empty context, and how every run is bounded and ends with
one honest, named reason. The one agent it ships with is Claude Code in headless
mode (`claude -p`), behind a five-rule interface that another agent could plug into.

```text
setup      refuse DIR == $HOME or "/"; create DIR/.subgoal/<run-id>/; write run.json
           hash every --protect path
step 0     run the check (pre-check). If it exits 0, stop: goal_met, with 0 worker calls.
for i in 1..max_iterations:
  a  if no time is left, stop: out_of_time
  b  prompt = build_prompt(goal, check, notes.md, last check result)   # from files only
     save as NN/prompt.md
  c  reply = provider.run(prompt, DIR, timeout_s = time left)          # fresh session
       ProviderError     -> stop: out_of_time if the clock ran out, else provider_failed
       KeyboardInterrupt -> stop: interrupted
  d  save NN/reply.md and NN/call.json; add reply.tokens to the total if known
  e  if any protected path changed, stop: protected_file_changed  (no check is run)
  f  result = run_check(check, DIR); save NN/check.txt
  g  if result.exit_code == 0, stop: goal_met
  h  if a token limit is set and known tokens >= it, stop: out_of_tokens
     if no time is left, stop: out_of_time
after the loop: stop: out_of_iterations
always     write run.json (ended), write report.md, print the report, exit with its code
```

| # | Principle | In code |
|---|---|---|
| 1 | **Checkable finish line.** The goal comes with a shell command, and exit code 0 means done. | `--check`, [`run_check()`](subgoal/loop.py) |
| 2 | **Independent checker.** The loop runs the check, not the agent. The agent's "I'm done" is recorded but never trusted. | step f in [`_steps()`](subgoal/loop.py); the report labels the claim "unverified" |
| 3 | **State on disk.** The goal, the notes, every prompt, every reply and every check result are files. Each call is built from those files, not from a growing chat. | the run folder, [`build_prompt()`](subgoal/loop.py) |
| 4 | **Fresh context per call.** Every call starts a new agent session; only what is on disk carries over. | the [`Provider.run()`](subgoal/provider.py) contract |
| 5 | **Bounded run.** Iterations and wall-clock time are always limited. Tokens are limited too when the tool reports them. | [`Limits`](subgoal/loop.py), steps a and h |
| 6 | **Honest ending.** Every run ends with exactly one named stop reason. It says "met" only when the check passed, and any number it does not know is printed as unknown. | [`StopReason`](subgoal/loop.py), [`report()`](subgoal/loop.py) |

## Read this first: safety

- This program runs a coding agent unattended, **as you**, on your machine. Each call
  can edit any file in `--dir` and run the check command. In `acceptEdits` mode Claude
  Code also lets it run `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp` and `sed` on paths
  inside that folder without asking. The agent can edit the code the check runs, so
  allowing the check means it can effectively run any code you could run. That code
  sees your files, your environment variables (including any secrets in them) and the
  network.
- The loop also runs the check itself after every call. That is code the agent wrote,
  run by you.
- `claude -p` skips the folder-trust prompt. Hooks and MCP servers configured in the
  target folder **will run**. Point the loop only at folders you trust.
- Your own Claude Code settings still apply. Allow rules in your user settings widen
  what the worker may do.
- Work on a throwaway copy, or from a clean git state you can reset. A container or VM
  with no secrets and no network is better still.
- The project never passes `--dangerously-skip-permissions`. If you add it yourself, do
  so only in an isolated container with no internet access, which is Anthropic's own
  advice as quoted by Simon Willison.
- Each step uses your Claude subscription's usage limits (or your API budget). The
  budgets bound that use; they don't remove it.
- "Goal met" proves what the check tests, nothing more. Read the diff.

## Requirements

- Python 3.10 or later, on macOS or Linux. Nothing to install: no packages, no build.
- Claude Code installed and logged in (`claude` on your PATH), version 2.1.259 or later
  for `--permission-prompts`. It was last tried with 2.1.306.

## Quick start

Run the tests first. They make no model calls and take a few seconds.

```bash
git clone https://github.com/ydmitry/subgoal-skill
cd subgoal-skill
python3 -m unittest discover -s tests
```

Then do a dry run on a copy of the example (copying again resets the stub). It prints
the exact `claude` command and the first prompt. It runs your check once to show the
starting state, but it calls no agent and creates no run folder.

```bash
mkdir -p /tmp/fizz && cp examples/fizzbuzz/* /tmp/fizz/
python3 -m subgoal --dir /tmp/fizz --goal-file /tmp/fizz/GOAL.md \
  --check "python3 -m unittest -q test_fizzbuzz" --protect test_fizzbuzz.py --dry-run
```

Then run the same command without `--dry-run`. Add `--model NAME` to pick a model; a
small, fast model is plenty for this example. One step prints one line to stderr, and
the report goes to stdout. Then look inside the run folder:

```text
/tmp/fizz/.subgoal/20261021-150625/
  run.json      settings, the provider command, limits, status, stop reason, steps,
                seconds, tokens, cost, the last check; rewritten after every step
  notes.md      the worker's handoff notes: the only file it is asked to write here
  00/check.txt  the pre-check: command, exit code, seconds, output tail
  01/prompt.md  exactly what the worker received
  01/reply.md   the worker's final message (never fed to the next step)
  01/call.json  tokens, cost, session id, seconds, error (if any)
  01/check.txt  the check after step 1
  report.md     the final report, identical to what was printed
```

The loop writes nothing outside `--dir`. Claude Code keeps its own transcript of each
call in its config folder, which is outside this program. To read one, run
`claude --resume <session id from call.json>` from the same folder.

## Use it as a Claude Code skill

The repo root holds a `SKILL.md`, so a clone in your personal skills folder is a Claude
Code skill:

```bash
git clone https://github.com/ydmitry/subgoal-skill ~/.claude/skills/subgoal-skill
```

Claude Code then offers it as `/subgoal`, and may pick it on its own for a task with a
checkable finish line. The skill turns your request into a goal and a check, shows you
the dry run, starts the real run in the background once you say yes, and reads the
report when the run ends. It runs the same `python3 -m subgoal` command from its own
folder (`${CLAUDE_SKILL_DIR}`), with your project as an absolute `--dir`. Everything in
the safety section above still applies.

## How a run ends

| Stop reason | Meaning | Exit code |
|---|---|---|
| `goal_met` | The check exited 0, either at the pre-check or after step N. | 0 |
| `out_of_iterations` | All N worker calls were made and the check never passed. | 1 |
| `out_of_time` | The wall-clock limit was reached. | 1 |
| `out_of_tokens` | The token limit was reached. Only when the provider reports tokens. | 1 |
| `provider_failed` | The tool was missing, the login failed, it crashed, or its output was unreadable. The message is quoted. | 3 |
| `protected_file_changed` | The worker changed a path listed with `--protect`. | 3 |
| `interrupted` | You pressed Ctrl+C. | 130 |
| (bad arguments) | argparse's usage error. No run folder is created. | 2 |

Time can overshoot by one check (a check may take up to 10 minutes), because the check
always runs after a call that returned: the verdict comes from the check, never from the
clock. Tokens can overshoot by one call. This is a real report from the example, run on
a small model (only the worker's last words are shortened):

```text
subgoal: GOAL MET after step 1: the check exited 0
  This proves what the check tests and nothing more. Read the diff.
  goal      Make the tests in test_fizzbuzz.py pass without editing that file.
  check     python3 -m unittest -q test_fizzbuzz      last run after step 1: exit 0
  steps     1 of 5 worker calls
  time      0m 18s of 30m 00s (checks included)
  tokens    174,667 (no limit set; as reported by the provider)
  cost      $0.01 (the provider's own estimate; a subscription is not billed per call)
  provider  claude -p --output-format json --permission-mode acceptEdits --permission-prompts none --model claude-haiku-5-5 --allowedTools 'Bash(python3 -m unittest -q test_fizzbuzz)'  (prompt on stdin)
  worker's last words (unverified): "The goal is met: `python3 -m unittest -q test_fizzbuzz` runs 4 tests and reports OK. ..."
  check output (tail):
    ----------------------------------------------------------------------
    Ran 4 tests in 0.000s

    OK
  records   /private/tmp/fizz/.subgoal/20261021-150625/
```

The token count is the sum of input, output, cache-write and cache-read tokens, as
Claude Code reports them. Cache reads (the agent's own instructions, read again on every
turn) make up most of it, which is why even this small fix counts six figures.

**What "unknown" means.** A number the provider did not report is printed as `unknown`,
never as 0. If you set `--max-tokens` and the provider reported no tokens, the report
says `token limit NOT enforced (provider reported no tokens)`. The run was still bounded
by its iteration and time limits.

## Reading the code

Read in this order. The core has a hard budget of 400 non-blank lines, checked by a
test, so it can be read in one sitting.

| File | Lines (`wc -l`) | Non-blank | Look for |
|---|---|---|---|
| [`subgoal/provider.py`](subgoal/provider.py) | 26 | 19 | The whole contract: `Reply`, `ProviderError`, `Provider`. |
| [`subgoal/loop.py`](subgoal/loop.py) | 276 | 229 | `run_loop()` and `_steps()` (steps a to h are marked), the prompt template, `report()`. |
| [`subgoal/claude_code.py`](subgoal/claude_code.py) | 59 | 50 | The `claude -p` command line and how its JSON maps onto a `Reply`. |
| [`subgoal/cli.py`](subgoal/cli.py) | 59 | 52 | The `PROVIDERS` registry, the one allow rule for the check, `--dry-run`. |
| [`subgoal/process.py`](subgoal/process.py) | 45 | 37 | When curious: a time limit that also stops the child's own children. |

The tests use a `FakeProvider` ([`tests/fake.py`](tests/fake.py)) that plays scripted
steps. The docstring of [`tests/test_loop.py`](tests/test_loop.py) maps every stop
reason to the tests that reach it.

## Writing a good goal and check

The Claude Code `/goal` docs list what a good completion condition has, and the advice
carries over:

- **One measurable end state.** "The tests in `test_x.py` pass", not "improve the code".
- **A stated check.** Here the check is a real command, and exit code 0 is the only way
  to "met". Tests make the best checks. In Willison's words, "The value you can get from
  coding agents and other LLM coding tools is massively amplified by a good, cleanly
  passing test suite."
- **Constraints that matter.** Protect the finish line with `--protect`. If the worker
  changes a protected file or folder, the run stops before the check runs, because a
  passing check would mean nothing.

The worker may run the check itself; the CLI adds one permission rule that allows
exactly that command. Claude Code matches a rule against each part of a compound
command separately, so with a check like `make test && make lint` the worker cannot run
it as one command (the loop still can). Put such a check in a script, `--protect` the
script, and use the script as the check.

If the worker keeps making the same mistake, change the goal text, as Ralph's author
puts it, by "adding a sign": a short line that says what not to do.

## Adding another provider

A provider is any class with `describe()` and `run()`. A sketch:

```python
# subgoal/other_agent.py  (not part of this repo)
class OtherAgent:
    def __init__(self, model=None, allow=()): ...
    def describe(self): return "other-agent exec --json, sandbox=workspace-write, ..."
    def run(self, prompt, workdir, timeout_s):
        code, out, err, timed_out = run_process(["other-agent", "exec", "--json"],
                                                workdir, timeout_s, stdin=prompt)
        if timed_out or code != 0: raise ProviderError(err[-2000:] or "timed out")
        data = json.loads(out)                      # map ITS output onto Reply
        return Reply(text=data["message"], tokens=data.get("usage", {}).get("total"))
```

Then add one entry to `PROVIDERS` in `cli.py`, a parse test with a recorded output, and
a paragraph here on its permission flags, checked against its own `--help`. The loop
does not change. The contract has five rules:

1. **Fresh context.** Every `run()` is a new session. A provider never resumes or
   continues an earlier one. It may keep sessions on disk and return their id.
2. **Permissions are fixed at construction.** They are not passed per call, and
   `describe()` states them.
3. **The provider enforces the timeout,** and it stops the child process and everything
   the child started.
4. **Failure has one shape.** Tool failures raise `ProviderError`. `KeyboardInterrupt`
   passes through unchanged.
5. **Unknown is `None`.** A provider never reports 0 or a guess in place of a missing
   number.

## When to use something else

If you just want this behaviour inside Claude Code, use its built-in `/goal`, also in
headless mode: `claude -p "/goal ..."`. There, completion "is decided by a fresh model
rather than the one doing the work", and that evaluator reads the conversation: it
"doesn't run commands or read files independently". This project's checker runs a
command instead. The other baseline is the plain Ralph loop,
`while :; do cat PROMPT.md | claude-code ; done`. This project exists to show the moving
parts between those two.

## Exercises

- Detect a stuck run: stop when the check output is the same N times in a row.
- Add a read-only model judge as a second checker, through the same `Provider` interface.
- Retry once when the provider reports an overloaded error.
- Commit after every step, so each step is one diff.
- Record each run somewhere else as well (a database, a logbook).

## Sources

- Anthropic, [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents):
  start with "the simplest solution possible", let agents gain "ground truth" from the
  environment at each step, and "include stopping conditions (such as a maximum number
  of iterations)".
- Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents):
  "each new session begins with no memory of what came before", so a progress file
  carries state; work on "only one feature at a time"; agents can declare victory "too
  early".
- Claude Code docs, [Keep Claude working toward a goal](https://code.claude.com/docs/en/goal):
  a completion condition with one measurable end state, a stated check and constraints
  that matter, judged by a fresh model.
- Simon Willison, [Designing agentic loops](https://simonwillison.net/2025/Sep/30/designing-agentic-loops/):
  an agent "runs tools in a loop to achieve a goal"; the risks of unattended runs; "If a
  credential can spend money, set a tight budget limit."
- Geoffrey Huntley, [Ralph](https://ghuntley.com/ralph/): "In its purest form, Ralph is
  a Bash loop", with the same prompt every time and "Only one thing" per loop.
- Addy Osmani, [Plan-Act-Observe Loop](https://addyosmani.com/agentic-engineering/plan-act-observe/):
  "think about what to do, do it, check the result, repeat", until the agent "hits a
  limit (token budget, time limit, or max iterations)".
- Claude Code docs used for the flags: [headless mode](https://code.claude.com/docs/en/headless),
  [permission modes](https://code.claude.com/docs/en/permission-modes) and
  [permission rules](https://code.claude.com/docs/en/permissions).

## License and affiliation

MIT, see [LICENSE](LICENSE). A personal educational project, not affiliated with any
employer or with Anthropic.
