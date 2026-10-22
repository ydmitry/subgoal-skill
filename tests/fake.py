"""FakeProvider: plays scripted steps instead of calling an agent, and records what it got."""
import subprocess
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
