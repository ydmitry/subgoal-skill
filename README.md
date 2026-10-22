# subgoal

A small Python program (about 540 lines, standard library only) that hands a goal to
a coding agent and keeps calling it, in a fresh session each time, until a finish line
that the agent does not control says the goal holds, or until a budget ends the run.
The finish line is a check command when a command can tell. When none can, it is a
judge: one more fresh, read-only agent session that reads the files and decides.

It is a teaching example, not a product or a framework. Every part of the loop is
in plain sight: where the finish line is checked, where state is kept on disk, how
each call starts with an empty context, and how every run is bounded and ends with
one honest, named reason. The one agent it ships with is Claude Code in headless
mode (`claude -p`), behind a six-rule interface that another agent could plug into.

```text
setup      refuse DIR == $HOME or "/"; create DIR/.subgoal/<run-id>/; write run.json
           hash every --protect path
step 0     only with --check: run the check (pre-check). If it exits 0, stop: goal_met,
           with 0 worker calls. With no --check there is no step 0: the first step is a worker.
for i in 1..max_iterations:
  a  if no time is left, stop: out_of_time
  b  prompt = build_prompt(goal, finish line, notes.md, last result)   # from files only
     save as NN/prompt.md       # last result: the last check output, or the last judge reasons
  c  reply = provider.run(prompt, DIR, timeout_s = time left)          # fresh session
       ProviderError     -> stop: out_of_time if the clock ran out, else provider_failed
       KeyboardInterrupt -> stop: interrupted
  d  save NN/reply.md and NN/call.json; add reply.tokens to the total if known
  e  if any protected path changed, stop: protected_file_changed  (no check, no judge is run)
  f  with --check:     result = run_check(check, DIR); save NN/check.txt
     without --check:  if a limit is reached, stop: out_of_tokens or out_of_time (no judge call)
                       reply = judge.run(judge_prompt(goal, DIR, worker's last words), DIR,
                                         timeout_s = time left)        # fresh, read-only session
                         ProviderError -> stop: out_of_time if the clock ran out, else provider_failed
                       save NN/judge-prompt.md, NN/judge.md, NN/judge-call.json; add its tokens and cost
                       verdict = parse_verdict(reply.text): met only if the LAST non-empty line
                       is exactly "VERDICT: met"; anything else, even silence, is not met
  g  if the check exited 0, or the verdict is met, stop: goal_met
  h  if a token limit is set and known tokens >= it, stop: out_of_tokens
     if no time is left, stop: out_of_time
after the loop: stop: out_of_iterations
always     write run.json (ended), write report.md, print the report, exit with its code
```

| # | Principle | In code |
|---|---|---|
| 1 | **Checkable finish line.** The goal comes with a shell command, and exit code 0 means done. When no command can tell, the finish line is a judge's verdict on written-out done criteria. | `--check`, [`run_check()`](subgoal/loop.py); without it, [`judge.py`](subgoal/judge.py) |
| 2 | **Independent checker.** The loop runs the check, or starts the judge, not the agent. The agent's "I'm done" is recorded but never trusted: the report labels it "unverified", and the judge is shown it only as an "unverified claim". | step f in [`_steps()`](subgoal/loop.py) |
| 3 | **State on disk.** The goal, the notes, every prompt, every reply and every check result or judge reply are files. Each call is built from those files, not from a growing chat. | the run folder, [`build_prompt()`](subgoal/loop.py) |
| 4 | **Fresh context per call.** Every call, the judge's too, starts a new agent session; only what is on disk carries over. | the [`Provider.run()`](subgoal/provider.py) contract |
| 5 | **Bounded run.** Iterations and wall-clock time are always limited. Tokens are limited too when the tool reports them. | [`Limits`](subgoal/loop.py), steps a and h |
| 6 | **Honest ending.** Every run ends with exactly one named stop reason. It says "met" only when the check passed or the judge said so, a judge's "met" is reported as a model's opinion, and any number it does not know is printed as unknown. | [`StopReason`](subgoal/loop.py), [`report()`](subgoal/loop.py) |
| 7 | **A judge is read-only, and silence is not success.** The judge session cannot edit, runs no project code, and must end with an exact verdict line. A missing, malformed or empty verdict counts as "not met". | `read_only=True` in [`ClaudeCode`](subgoal/claude_code.py), [`parse_verdict()`](subgoal/judge.py) |

## Read this first: safety

- This program runs a coding agent unattended, **as you**, on your machine. Each call
  can edit any file in `--dir` and run the check command. In `acceptEdits` mode Claude
  Code also lets it run `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp` and `sed` on paths
  inside that folder without asking. The agent can edit the code the check runs, so
  allowing the check means it can effectively run any code you could run. That code
  sees your files, your environment variables (including any secrets in them) and the
  network.
- With `--check`, the loop also runs the check itself after every call. That is code the
  agent wrote, run by you.
- Without `--check`, a judge decides, and it is a model. It can be wrong, and it reads
  files the worker wrote, which could contain text aimed at it. It can read every file in
  `--dir` and run read-only `git` commands, but it **does not run the project's code**, so
  it cannot test anything: it reads. A command check is stronger whenever one exists.
  Each step also costs a second call, the judge's.
- The judge's read-only limit comes from Claude Code's permission rules (only `Read`,
  `Grep`, `Glob` and Bash `git status`, `diff`, `log` and `show`; no edit tools; no MCP
  tools; any command containing `--output` denied, because `git diff --output=FILE`
  writes a file), not from the operating system. Your own settings and hooks still apply
  to it. Use a copy here too.
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
  for `--permission-prompts`. It was last tried with 2.1.307. The judge's read-only flags
  (`dontAsk`, `--tools`, `--strict-mcp-config`, `--disallowedTools`) have been tried with
  2.1.307 only.

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

### A goal no command can check: the judge

Some goals have no test: a README, a review, a summary. Leave out `--check` and a judge
decides. [`examples/write-the-docs`](examples/write-the-docs) is such a task: write a
README for a small module, with five done criteria that a reader can verify by reading
the files. `--protect` still guards the module itself, and the loop, not the judge,
enforces that.

```bash
mkdir -p /tmp/docs && cp examples/write-the-docs/* /tmp/docs/
python3 -m subgoal --dir /tmp/docs --goal-file /tmp/docs/GOAL.md --protect slugify.py --dry-run
```

The dry run prints both provider commands, the first worker prompt and the judge prompt
with a placeholder where the worker's last words will go. It makes no call. Run it
without `--dry-run` to try it, with `--model NAME` for a small model, and `--judge-model
NAME` if the judge should use another. A judge run has no step 0 and no `check.txt`.
Each step folder holds, in addition to the worker's files:

```text
  01/judge-prompt.md  exactly what the judge received: the goal, the folder, and the
                      worker's last words, labelled as an unverified claim
  01/judge.md         the judge's reply: its reasons, then the verdict line
  01/judge-call.json  the judge call's tokens, cost, session id and seconds
```

`run.json` then has `"check": null` and a `"judge"` field with the judge's command
(otherwise `null`), plus `judge_calls` and `last_verdict`.

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
checkable finish line. The skill turns your request into a goal with observable done
criteria and, when a command can tell, a check; shows you the dry run; starts the real
run in the background once you say yes; and reads the report when the run ends. It runs the same `python3 -m subgoal` command from its own
folder (`${CLAUDE_SKILL_DIR}`), with your project as an absolute `--dir`. Everything in
the safety section above still applies.

## How a run ends

| Stop reason | Meaning | Exit code |
|---|---|---|
| `goal_met` | The check exited 0, either at the pre-check or after step N. Without `--check`: the judge's last line was `VERDICT: met` after step N. | 0 |
| `out_of_iterations` | All N worker calls were made and the finish line never held. | 1 |
| `out_of_time` | The wall-clock limit was reached. | 1 |
| `out_of_tokens` | The token limit was reached. Only when the provider reports tokens. | 1 |
| `provider_failed` | The tool was missing, the login failed, it crashed, or its output was unreadable, for the worker or for the judge. The message is quoted. | 3 |
| `protected_file_changed` | The worker changed a path listed with `--protect`. Neither the check nor the judge runs. | 3 |
| `interrupted` | You pressed Ctrl+C. | 130 |
| (bad arguments) | argparse's usage error. No run folder is created. | 2 |

Time can overshoot by one check (a check may take up to 10 minutes), because the check
always runs after a call that returned: the verdict comes from the check, never from the
clock. Tokens can overshoot by one call. A judge is different, because it is a model
call: it gets the time that is left, and no judge is asked once a time or token limit is
reached, so a run can end `out_of_time` or `out_of_tokens` with the work actually done.
Read the diff in that case too. This is a real report from the fizzbuzz example, run on
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

A run with a judge reports its finish line differently: the title says a judge said met,
the report calls that a model's opinion, and the judge's reasons replace the check
output. This is a real report from `examples/write-the-docs` on a small model, run with
`--max-iterations 2 --max-minutes 10` (the goal, the commands and the long texts are
shortened):

```text
subgoal: GOAL MET after step 1: a judge said met
  That is a model's opinion of the files, not a test result. Read the diff.
  goal      Write README.md for the `slugify` function in slugify.py, for a developer who has never seen it. Do not change slugify.py. ...
  judge     no check command: a read-only agent decides      last verdict after step 1: met
  steps     1 of 2 worker calls (judge calls: 1)
  time      0m 55s of 10m 00s (judge calls included)
  tokens    256,817 (no limit set; as reported by the provider)
  cost      $0.01 (the provider's own estimate; a subscription is not billed per call)
  provider  claude -p --output-format json --permission-mode acceptEdits --permission-prompts none --model claude-haiku-5-5  (prompt on stdin)
  judge via claude -p --output-format json --permission-mode dontAsk --permission-prompts none --tools Read,Grep,Glob,Bash --strict-mcp-config --disallowedTools 'Bash(*--output*)' --model claude-haiku-5-5 --allowedTools Read Grep Glob ...  (prompt on stdin, read-only: edits are denied)
  worker's last words (unverified): "I rewrote `README.md` with the three required sections, but I did not run any of its examples. ..."
  judge's reasons (a model's opinion; first lines):
    All five parts hold. I traced the code by hand, since I'm read-only and can't run Python.

    - **Sections (1):** README.md has `## Overview`, `## Usage`, `## Limits` in that order. ...
  records   /tmp/docs/.subgoal/20261022-075212/
```

The worker could not run Python, and the judge, which is read-only, cannot either, so
both traced the examples by hand. Running every example in the README that run wrote,
afterwards, gave exactly the printed strings, so the judge was right this time. That is
one run, not a guarantee, and it is why the report says "opinion".

## Reading the code

Read in this order. The core has a hard budget of 550 non-blank lines, checked by a
test, so it can be read in one sitting. (It was 400 before the judge; the judge, its
read-only provider mode and its records took about 150.)

| File | Lines (`wc -l`) | Non-blank | Look for |
|---|---|---|---|
| [`subgoal/provider.py`](subgoal/provider.py) | 29 | 21 | The whole contract: `Reply`, `ProviderError`, `Provider`. |
| [`subgoal/loop.py`](subgoal/loop.py) | 354 | 299 | `run_loop()` and `_steps()` (steps a to h are marked), the prompt templates, `_judge()`, `report()`. |
| [`subgoal/judge.py`](subgoal/judge.py) | 65 | 50 | The judge's prompt and the strict parse of its verdict line. |
| [`subgoal/claude_code.py`](subgoal/claude_code.py) | 74 | 65 | The `claude -p` command line, its read-only variant, and how its JSON maps onto a `Reply`. |
| [`subgoal/cli.py`](subgoal/cli.py) | 72 | 65 | The `PROVIDERS` registry, the one allow rule for the check, `--dry-run`. |
| [`subgoal/process.py`](subgoal/process.py) | 45 | 37 | When curious: a time limit that also stops the child's own children. |

The tests use a `FakeProvider` ([`tests/fake.py`](tests/fake.py)) that plays scripted
steps, for the worker and for the judge. The docstrings of
[`tests/test_loop.py`](tests/test_loop.py) and [`tests/test_judge.py`](tests/test_judge.py)
map every stop reason to the tests that reach it.

## Writing a good goal, check and done criteria

The Claude Code `/goal` docs list what a good completion condition has, and the advice
carries over:

- **One measurable end state.** "The tests in `test_x.py` pass", not "improve the code".
- **A stated finish line.** Use a check command whenever one can tell: exit code 0 is
  the only way to "met". Tests make the best checks. In Willison's words, "The value you
  can get from coding agents and other LLM coding tools is massively amplified by a good,
  cleanly passing test suite." When no command can tell, write the done criteria so that
  a reviewer holding only the files could answer yes or no to each one: numbered, and
  observable ("README.md has three sections, in this order"), never "good documentation".
  A vague goal gets a vague verdict. Whatever the judge says is an opinion, so read the
  diff.
- **Constraints that matter.** Protect the finish line with `--protect`. If the worker
  changes a protected file or folder, the run stops before the check or the judge runs,
  because a passing result would mean nothing. With a judge, protect what the criteria
  say must not change: the loop checks that for certain, and the judge only reads.

With `--check`, the worker may run the check itself; the CLI adds one permission rule
that allows exactly that command. Claude Code matches a rule against each part of a
compound command separately, so with a check like `make test && make lint` the worker
cannot run it as one command (the loop still can). Put such a check in a script,
`--protect` the script, and use the script as the check. Without `--check` there is no
such rule, and the worker is told that a separate reviewer decides and that it cannot
declare the goal met. After a "not met", the next worker prompt carries the judge's
reasons, under a heading that says they are a reviewer's opinion, not a command result.
Without `--check`, the worker also has no permission to run the project's code (a real
run's worker said so, and marked its examples as unchecked). If the task needs that, give
it an `--allow` rule, for example `--allow 'Bash(python3 *)'`.

If the worker keeps making the same mistake, change the goal text, as Ralph's author
puts it, by "adding a sign": a short line that says what not to do.

## Adding another provider

A provider is any class with `describe()` and `run()`. A sketch:

```python
# subgoal/other_agent.py  (not part of this repo)
class OtherAgent:
    def __init__(self, model=None, allow=(), read_only=False): ...
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
does not change. The contract has six rules:

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
6. **Read-only on request.** The constructor takes `read_only=False`. With `True`, no
   session it starts can edit anything, whatever `allow` says, and `describe()` says so.
   The CLI builds the judge this way, from the same `PROVIDERS` entry as the worker.
   `ClaudeCode` does it with `--permission-mode dontAsk`, a tool list without the edit
   tools (`--tools Read,Grep,Glob,Bash`), no MCP tools (`--strict-mcp-config`), an allow
   list of `Read`, `Grep`, `Glob` and four read-only `git` commands, and a deny rule for
   any command containing `--output`. The tests assert that command line. A one-off
   probe (Haiku, Claude Code 2.1.307) asked such a session to write a file with a tool,
   with a shell redirect and with `git diff --output=FILE`: no file appeared.

## When to use something else

If you just want this behaviour inside Claude Code, use its built-in `/goal`, also in
headless mode: `claude -p "/goal ..."`. There, completion "is decided by a fresh model
rather than the one doing the work", and that evaluator reads the conversation: it
"doesn't run commands or read files independently". With `--check`, this project's checker
runs a command instead. Without it, its judge is a second fresh session that does read
the files, though it still does not run the project's code. The other baseline is the plain Ralph loop,
`while :; do cat PROMPT.md | claude-code ; done`. This project exists to show the moving
parts between those two.

## Exercises

- Detect a stuck run: stop when the check output is the same N times in a row.
- Ask two judges, on different models, and call the goal met only if both say so.
- With `--check`, add the judge as a second opinion: the check must exit 0 and the
  judge must agree.
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
