"""Fakes that record what they got. FakeProvider plays scripted steps instead of calling an
agent; fake_opencode writes an opencode binary that plays scripted output."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from subgoal.provider import Reply


class FakeProvider:
    def __init__(self, steps):
        self.steps = list(steps)    # each step: a function(workdir) -> Reply, or one that raises
        self.prompts, self.timeouts = [], []

    def describe(self):
        return "fake provider (scripted steps, no agent)"

    def run(self, prompt, workdir, timeout_s):
        self.prompts.append(prompt)
        self.timeouts.append(timeout_s)
        return self.steps.pop(0)(Path(workdir))


def say(text="I worked on it.", tokens=None, cost=None):
    """A step that changes nothing and only talks."""
    return lambda workdir: Reply(text, tokens, cost)


def judges(met, reasons="Reasons.", tokens=None):
    """A judge's reply: short reasons, then the verdict line."""
    return say(f"{reasons}\nVERDICT: {'met' if met else 'not met'}", tokens)


def edit(path, content, text="I edited a file.", tokens=None):
    """A step that writes one file (relative to the workdir), then replies."""
    def step(workdir):
        (workdir / path).write_text(content)
        return Reply(text, tokens)
    return step


def notes(content, text="I left notes."):
    """A step that rewrites notes.md in the current run folder, as the prompt asks."""
    def step(workdir):
        run_folder = sorted((workdir / ".subgoal").iterdir())[-1]
        (run_folder / "notes.md").write_text(content)
        return Reply(text)
    return step


def fail(error):
    """A step that raises, like a tool that crashed or a user who pressed Ctrl+C."""
    def step(workdir):
        raise error
    return step


def gone(marker):
    """True once no running process has `marker` (a unique temp path) in its command line."""
    for _ in range(50):
        ps = subprocess.run(["ps", "-A", "-o", "args="], capture_output=True, text=True)
        if marker not in ps.stdout:
            return True
        time.sleep(0.02)
    return False


FAKE_OPENCODE = """#!{python}
import json, os, sys, time
from pathlib import Path
here = Path(__file__).parent
calls = json.loads((here / "calls.json").read_text()) if (here / "calls.json").exists() else []
env = {{k: v for k, v in os.environ.items() if k.startswith("OPENCODE_") or k == "PWD"}}
calls.append({{"name": Path(sys.argv[0]).name, "argv": sys.argv[1:], "cwd": os.getcwd(),
               "stdin": sys.stdin.read(), "env": env}})
(here / "calls.json").write_text(json.dumps(calls))
if sys.argv[1:] == ["--version"]:
    version = json.loads((here / "version.json").read_text())
    if version is None:
        sys.exit(1)
    sys.stdout.write(version)
    sys.exit(0)
reply = json.loads((here / "replies.json").read_text())[sum(c["argv"] != ["--version"] for c in calls) - 1]
if "--dir" in sys.argv:
    os.chdir(sys.argv[sys.argv.index("--dir") + 1])
for path, content in reply.get("write", {{}}).items():
    Path(path).write_text(content)
time.sleep(reply.get("sleep", 0))
sys.stdout.write(reply.get("stdout", ""))
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("exit", 0))
"""


def fake_opencode(folder, name, replies, version="opencode v2.1.4\n"):
    """Write folder/name, a fake opencode that plays these replies, one per call: stdout,
    stderr, exit, sleep, and files to write in its folder. It answers --version with `version`
    (None: exit 1). It records every call, --version too, in folder/calls.json."""
    (folder / "replies.json").write_text(json.dumps(replies))
    (folder / "version.json").write_text(json.dumps(version))
    (folder / name).write_text(FAKE_OPENCODE.format(python=sys.executable))
    (folder / name).chmod(0o755)
    return str(folder / name)


def opencode_match(text, glob):
    """opencode's wildcard (docs: Permissions): * is any run of characters and ? is one, and a
    trailing " *" also matches nothing, so `git status *` covers plain `git status`."""
    tail = glob.endswith(" *")
    regex = "".join({"*": ".*", "?": "."}.get(c) or re.escape(c) for c in (glob[:-2] if tail else glob))
    return re.fullmatch(regex + ("( .*)?" if tail else ""), text, re.S) is not None
