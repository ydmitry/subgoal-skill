---
name: subgoal
description: Use for a task in a folder that needs more than a few tool calls. With a check command that exits 0 (ideally a test suite) it runs Claude Code headless in a bounded loop of fresh sessions until the command passes. With no check it makes a plan and runs one step that nothing verifies, unless --judge asks a model for its opinion after each step.
---

# subgoal: a bounded loop toward a checkable goal

`python3 -m subgoal`, in this skill's folder, calls `claude -p` in fresh sessions until a check command exits 0 or a budget ends.

## 1. Pick the finish line, then write the goal

| The work | Use | What happens |
|---|---|---|
| A command can tell it is done (tests, a build, a script) | `--check CMD` | Up to 5 fresh sessions until CMD exits 0: the only finish line a model cannot talk past. |
| A bug, or a feature a test can pin | Write the failing test first, then `--check` and `--protect` it | The same loop, now with a check. |
| Nothing can check it (a page, a refactor, prose) | No flag; add `--no-plan` if the change is small and exact | A read-only plan (none with `--no-plan`), then one step; ends `unchecked`. Read the diff yourself. |
| The goal names a skill or a way of working | `--judge` | Loops on a model's opinion, drawn from the files and the tool calls (`NN/trace.md`). Not for bugs: it accepted wrong fixes. |

- **Goal:** a few short lines of observable done criteria, plus any constraint ("without editing the tests").
- **Check:** a command or script (`--check ./check.sh`) that exits 0 only when the goal holds, never trivially; put `&&` or `|` in a script.
- **Protect:** `--protect` the tests, the check script, any file the work must not change. If unclear, ask the user.

## 2. Launch: dry run first, then the real run in the background

```bash
(cd "${CLAUDE_SKILL_DIR}" && python3 -m subgoal --dir "/absolute/path/to/project" \
  --goal "Make the tests in tests/ pass without editing them." \
  --check "python3 -m unittest discover -s tests" --protect tests --dry-run)
```

Pass the project folder as an absolute `--dir`, never `$HOME` or `/`. Show the user the dry run's provider command(s) and
first prompt, and get a yes. Then run it without `--dry-run`, as a background command. Options: `--max-iterations N` (5),
`--max-minutes M` (30), `--max-tokens T`, `--model NAME`; with no `--check`: `--no-plan`, `--judge`, `--judge-model NAME`.

## 3. Read the result

You are told when the background command exits. Read its report, then act on its exit code:

| Exit | Stop reason | Next |
|---|---|---|
| 0 | goal_met, unchecked | Show the diff. A check proves what it tests; a judge's "met" is an opinion; unchecked means nothing verified it, so run the tests yourself. |
| 1 | out_of_iterations, out_of_time, out_of_tokens | Read `notes.md` and the last `check.txt` or `judge.md`; refine the goal or a limit; ask before another run. |
| 2 | bad arguments | Fix the command. |
| 3 | provider_failed, protected_file_changed | Quote the error. For a protected change, show the diff. |
| 130 | interrupted | Stop. |

Records are in `<dir>/.subgoal/<run-id>/`: `report.md`, `run.json`, `notes.md`, one `NN/` per step.

## Safety

The loop runs a coding agent unattended, as the user, unsandboxed, in `--dir`: it edits files and
runs the check, or, with no check, any command. The planner and the judge have no edit tool, but a command
can still change files, and the loop checks only `--protect` paths. Hooks and MCP servers there run too.
Use a clean git tree or a branch, and read the diff.

## Do not

- Poll the run, or tail its files while it works. Wait for the exit.
- Add any flag that skips permission checks, here or to `claude`.
- Report the goal as met from the worker's words, or from `unchecked`. Only `goal_met` counts.
