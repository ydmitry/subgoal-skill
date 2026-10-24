---
name: subgoal
description: Use for a task in a folder that needs more than a few tool calls and has a finish line that can be checked, either a command that exits 0 (ideally a test suite) or written-out done criteria that a judge can check (it reads the files and may run commands). Runs Claude Code headless in a bounded loop of fresh sessions until the finish line is met.
---

# subgoal: a bounded loop toward a checkable goal

`python3 -m subgoal`, in this skill's folder, calls `claude -p` in fresh sessions until the
goal is met or a budget ends. A check command decides; with none, a judge (no edit tools) does.

## 1. Turn the request into a goal, and a check when one exists

- **Goal:** a few short lines of observable done criteria, plus any constraint ("without editing the tests").
- **Check:** when a command can tell (tests, a build, a linter), add `--check`: a shell command
  or a script (`--check ./check.sh`), run in the project folder, that exits 0 only when the
  goal holds. Never one that passes trivially. It is the strongest finish line, the only one a
  model cannot talk its way past. Without it, a judge decides: it reads files and may run
  commands, but has no browser tool, so a part only a browser or a timing shows needs a check.
- **Protect the finish line:** `--protect` the test files, the check script, or any file the
  work must not change. Put a compound check (`&&`, `|`) in a script. If unclear, ask the user.

## 2. Launch: dry run first, then the real run in the background

```bash
(cd "${CLAUDE_SKILL_DIR}" && python3 -m subgoal --dir "/absolute/path/to/project" \
  --goal "Make the tests in tests/ pass without editing them." \
  --check "python3 -m unittest discover -s tests" --protect tests --dry-run)
```

Pass the project folder as an absolute `--dir`, never `$HOME` or `/`. Show the user the dry
run's provider command(s) and first prompt, and get a yes. Then run it without `--dry-run`, as
a background command. Options: `--max-iterations N` (default 5), `--max-minutes M` (30),
`--max-tokens T`, `--model NAME`, `--judge-model NAME` (no `--check`).

## 3. Read the result

You are told when the background command exits. Read its report, then act on its exit code:

| Exit | Stop reason | Next |
|---|---|---|
| 0 | goal_met | Show the diff. A check proves what it tests; a judge's "met" is an opinion. |
| 1 | out_of_iterations, out_of_time, out_of_tokens | Read `notes.md` and the last `check.txt` or `judge.md`; refine the goal or a limit; ask before another run. |
| 2 | bad arguments | Fix the command. |
| 3 | provider_failed, protected_file_changed | Quote the error. For a protected change, show the diff. |
| 130 | interrupted | Stop. |

Records are in `<dir>/.subgoal/<run-id>/`: `report.md`, `run.json`, `notes.md`, one `NN/` per step.

## Safety

The loop runs a coding agent unattended, as the user, unsandboxed, in `--dir`: it edits files and
runs the check, or, with no check, any command. The judge runs commands too: it has no edit tool,
but a command can still change files, and the loop checks only `--protect` paths. Hooks and MCP
servers there run too. Use a clean git tree or a branch, and read the diff.

## Do not

- Poll the run, or tail its files while it works. Wait for the exit.
- Add any flag that skips permission checks, here or to `claude`.
- Report the goal as met from the worker's words. Only exit code 0 counts.
