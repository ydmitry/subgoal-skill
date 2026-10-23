# Contributing

This is a small teaching project, so the main thing to protect is that it stays small and
readable. Here is how to find your way around, run the tests, and add a provider.

## Reading the code

Read in this order:

| File | What is in it |
|---|---|
| [`subgoal/provider.py`](subgoal/provider.py) | The whole contract between the loop and an agent, about 30 lines. |
| [`subgoal/loop.py`](subgoal/loop.py) | The loop itself: `run_loop()`, the prompt template, the limits, the stop reasons, the report. |
| [`subgoal/judge.py`](subgoal/judge.py) | The judge's prompt and the strict parse of its verdict line. |
| [`subgoal/claude_code.py`](subgoal/claude_code.py) | The `claude -p` command line, its read-only variant, and how its JSON becomes a `Reply`. |
| [`subgoal/opencode.py`](subgoal/opencode.py) | The same for `opencode run` (opencode 1): permissions as one config, JSON events as a `Reply`. |
| [`subgoal/opencode2.py`](subgoal/opencode2.py) | opencode 2: another command line and rule format, the same events (it reuses `opencode.parse`). |
| [`subgoal/cli.py`](subgoal/cli.py) | The arguments, the `PROVIDERS` registry, `--dry-run`. |
| [`subgoal/process.py`](subgoal/process.py) | When curious: a time limit that also stops everything the child started. |

## Running the tests

```bash
python3 -m unittest discover -s tests
```

They need no model and take a few seconds. The loop and the judge are driven by a
`FakeProvider` ([`tests/fake.py`](tests/fake.py)) that plays scripted steps; the docstrings of
[`tests/test_loop.py`](tests/test_loop.py) and [`tests/test_judge.py`](tests/test_judge.py) map
every stop reason to the tests that reach it. The provider tests parse recorded real outputs
from [`tests/fixtures`](tests/fixtures), and the opencode tests start a fake `opencode`
program: a small script that records how it was called and plays back those outputs.

Two optional test files, one per opencode major version, run the real opencode binary against a scripted local model, never a real
one, to show that the worker edits inside the folder only and that the judge cannot write:

```bash
SUBGOAL_TEST_OPENCODE=/path/to/opencode1 python3 -m unittest discover -s tests -p test_opencode_real.py
SUBGOAL_TEST_OPENCODE2=/path/to/opencode2 python3 -m unittest discover -s tests -p test_opencode2_real.py
```

It gives the binary a clean environment and a throwaway home whose only model provider is a
local server on 127.0.0.1 that plays a script. It is skipped when the variable is not set.

## The size rule

The loop core is capped at 550 non-blank lines, not counting the provider modules, and each
provider module at 100. `test_S1_size_budget` in [`tests/test_cli.py`](tests/test_cli.py)
enforces it. If a change needs more lines, something else should get simpler first.

## Adding a provider

A provider is a class that satisfies [`subgoal/provider.py`](subgoal/provider.py):

- Its constructor takes `model`, `allow` and `read_only=False`. With `read_only=True`, no
  session it starts may edit anything, whatever `allow` says; the judge is built this way.
- `describe()` returns one line: the tool, model and permissions every call will use.
- `run(prompt, workdir, timeout_s)` starts a **new** session in `workdir`, never resumes an
  old one, enforces the timeout, and returns a `Reply`. Anything unusable raises
  `ProviderError`. A number the tool does not report is `None`, never 0.

Then register it in `PROVIDERS` in [`subgoal/cli.py`](subgoal/cli.py). The loop does not
change. [`subgoal/opencode.py`](subgoal/opencode.py) is a good model to copy: permissions
fixed once in the constructor, and a `parse()` function that maps the tool's output onto a
`Reply`. The CLI refuses `--allow` for every provider but `claude-code`, since those are Claude
Code rules; decide what yours should do with it.

Test it the way [`tests/test_opencode.py`](tests/test_opencode.py) does: put a fake executable
on `PATH`, a script that records its arguments, working folder, stdin and environment and plays
back a recorded output, so the tests show exactly what the real tool would be asked to do.
Record one real output once and keep it in `tests/fixtures`.

## An exercise

Stop when the check output repeats N times in a row: a run that is stuck should end with its
own stop reason instead of spending the whole budget.

## Proposing a change

Issues and pull requests are welcome. Keep the tests green and the size rule intact, and say
in one or two lines what the change makes clearer.
