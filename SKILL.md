---
name: subgoal
description: Use for a task in a folder that needs more than a few tool calls. With a check command that exits 0 (ideally a test suite) it runs Claude Code headless in a bounded loop of fresh sessions until the command passes. With no check it makes a plan and runs one step that nothing verifies, unless --judge asks a model for its opinion after each step. With both, the judge is asked once the check passes.
---

# subgoal: a bounded loop toward a checkable goal

`python3 -m subgoal`, in this skill's folder, calls `claude -p` in fresh sessions until a check command exits 0 or a budget ends.

## 1. Pick the finish line, then write the goal

| The work | Use | What happens |
|---|---|---|
| A command can tell it is done (tests, a build, a script) | `--check CMD` | Up to 4 fresh worker sessions until CMD exits 0: the only finish line a model cannot talk past. The same failure twice in a row brings a read-only re-plan. |
| A bug, or a feature a test can pin | The failing test first: write it, or have a run write it (`--no-plan`, `--protect` the code, goal: "add a test for X that fails now"). Run it, see it fail for the right reason, show the user the test and get a yes. Then `--check` it and `--protect` it | The same loop, aimed at a bar the user approved, not one the worker set. A test written from a short request has its gaps, and the loop stops once it passes: in the maintainer's evaluations, about a third of the runs whose written test passed still failed the real fix's tests. If the user approves no test, use the next row. |
| Nothing can check it, or no test the user approved (a refactor, an exact change, a bug fix) | No flag; add `--no-plan` if the change is small and exact | A read-only plan (none with `--no-plan`), then one step; ends `unchecked`. Read the diff yourself. In the maintainer's evaluations, on bug fixes with no test, this did as well as any loop. |
| A page or a document, a goal that names a skill or a way of working, or parts no command can tell | `--judge`, plus `--check` for the parts a command can tell (for a page, a check script its skill gives, if any); for a page or a document, `--judge-model claude-opus-5-5` (measured on pages) | Loops on a model's opinion, drawn from the files and the tool calls (`NN/trace.md`); with a check, asked only once it exits 0, so met needs both. Not for bugs: it accepted wrong fixes. On pages a Sonnet judge did no better than the check alone in the maintainer's evaluations. |
| Stages of different kinds (find out, then build, then explain) | One run per stage, each with its own finish line from the rows above, one after another | A stage that finds things out writes them to a file. Read each report and diff before the next run, and name the files the last stage made in the next goal. Runs at the same time need a folder each (a git worktree). |

- **Goal:** a few short lines of observable done criteria, plus any constraint ("without editing the tests").
- **Check:** a command or script (`--check ./check.sh`; `&&` or `|` go in a script) that fails now and exits 0 only when the goal holds. One that already passes ends the run before any step (`check_already_passes`), unless `--judge` follows (then it guards); the dry run says so. The worker may run only that exact command: `--allow 'Bash(pytest:*)'` (your test runner) lets it look for regressions.
- **Protect:** `--protect` the tests, the check script, any file the work must not change. If unclear, ask the user.

## 2. Launch: dry run first, then the real run in the background

```bash
(cd "${CLAUDE_SKILL_DIR}" && python3 -m subgoal --dir "/absolute/path/to/project" \
  --goal "Make the tests in tests/ pass without editing them." \
  --check "python3 -m unittest discover -s tests" --protect tests --dry-run)
```

Pass the project folder as an absolute `--dir`, never `$HOME` or `/`. Show the user the dry run's provider command(s) and
first prompt, and get a yes. Then run it without `--dry-run`, as a background command. Options: `--max-iterations N` (4),
`--max-minutes M` (30), `--max-tokens T`, `--model NAME`, `--provider P`, `--judge-model NAME` and `--judge-provider P` (judge and planner; default: the worker's), `--no-plan`.

## 3. Read the result

You are told when the background command exits. Read its report (the records are in `<dir>/.subgoal/<run-id>/`:
`report.md`, `run.json`, `notes.md`, one `NN/` per step), then act on its exit code:

| Exit | Stop reason | Next |
|---|---|---|
| 0 | goal_met, unchecked | Show the diff and the worker's `notes.md` (what it guessed, chose or could not run; an unchecked run writes none: read `01/reply.md`). A check proves what it tests; a judge's "met" is an opinion; unchecked means nothing verified it, so run the tests yourself. |
| 1 | out_of_iterations, out_of_time, out_of_tokens, stuck | Read `notes.md`, the last `check.txt` or `judge.md`, and any `NN/plan.md` (`stuck`: the step after a re-plan failed the same way). If the check line says the same failure since the pre-check, no step changed what the check tests: fix the check or the environment, not the goal (`notes.md` says what the worker verified instead). Otherwise refine the goal or a limit. Ask before another run. |
| 2 | bad arguments, check_already_passes | Fix the command. A check that passes before any step cannot tell done from not done: make it fail first. |
| 3 | provider_failed, protected_file_changed | Quote the error. For a protected change, show the diff. |
| 130 | interrupted | Stop. |

## Safety

The loop runs a coding agent unattended, as the user, unsandboxed, in `--dir`: it edits files and
runs the check, or, with no check, any command. The planner (with a check, only to re-plan) and the judge have no
edit tool, but a command can still change files, and the loop checks only `--protect` paths. Hooks and MCP servers
there run too. Use a clean git tree or a branch, and read the diff.

## Do not

- Poll the run, or tail its files while it works. Wait for the exit.
- Add any flag that skips permission checks, here or to `claude`.
- Report the goal as met from the worker's words, or from `unchecked`. Only `goal_met` counts.
