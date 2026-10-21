---
name: subgoal
description: Use for a coding task with a checkable finish line (a command such as a test suite that exits 0 once the work is done) that needs more than a few tool calls. Runs Claude Code headless in a bounded loop of fresh sessions until that command passes.
---

# subgoal: a bounded loop toward a checkable goal

`python3 -m subgoal`, in this skill's folder, calls `claude -p` in a fresh session each
time and runs the check itself after every call, until it exits 0 or a budget runs out.

## 1. Turn the request into a goal and a check

- **Goal:** one measurable end state, in one or two sentences, plus any constraint
  that matters ("without editing the tests").
- **Check:** one shell command, run in the project folder, that exits 0 only when the
  goal holds. Tests make the best checks. Never use a check that passes trivially.
- **Protect the finish line:** `--protect` the test files, or the check script, so the
  worker cannot change them. Put a compound check (`&&`, `|`) in a script. If the goal
  or the check is unclear, ask the user before going on.

## 2. Launch: dry run first, then the real run in the background

Pass the project folder as an absolute `--dir`. Never `$HOME` or `/`.

```bash
(cd "${CLAUDE_SKILL_DIR}" && python3 -m subgoal --dir "/absolute/path/to/project" \
  --goal "Make the tests in tests/ pass without editing them." \
  --check "python3 -m unittest discover -s tests" --protect tests --dry-run)
```

Show the user the dry run's provider command and first prompt, and get a yes. Then run
the same command without `--dry-run`, as a background command. Options:
`--max-iterations N` (default 5), `--max-minutes M` (30), `--max-tokens T`, `--model NAME`.

## 3. Read the result

You are told when the background command exits. Read its report, then act on its exit code:

| Exit | Stop reason | Next |
|---|---|---|
| 0 | goal_met | Show the diff. The check proves what it tests, nothing more. |
| 1 | out_of_iterations, out_of_time, out_of_tokens | Read `notes.md` and the last `check.txt`; refine the goal or a limit; ask before another run. |
| 2 | bad arguments | Fix the command. |
| 3 | provider_failed, protected_file_changed | Quote the error. For a protected change, show the diff. |
| 130 | interrupted | Stop. |

Records are in `<dir>/.subgoal/<run-id>/`: `report.md`, `run.json`, `notes.md`, and per
step `NN/prompt.md`, `NN/reply.md`, `NN/call.json`, `NN/check.txt`.

## Safety

The loop runs a coding agent unattended, as the user: it edits files in `--dir` and can
run any code the check runs; hooks and MCP servers in that folder run too. Prefer a
throwaway copy or a clean git state, and read the diff before trusting it.

## Do not

- Poll the run, or tail its files while it works. Wait for the exit.
- Add any flag that skips permission checks, here or to `claude`.
- Report the goal as met from the worker's words. Only exit code 0 counts.
