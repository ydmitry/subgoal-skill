"""The contract between the loop and a coding agent. This file is all of it."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Reply:
    text: str                      # the agent's final message: recorded, never trusted
    tokens: int | None = None      # tokens the call used, if the tool reports them
    cost_usd: float | None = None  # the tool's own estimate, if it gives one
    session: str | None = None     # an id to look the call up later, if any
    trace: str | None = None       # one short line per tool call, in order: tool and key argument, never the result


class ProviderError(Exception):
    """No usable reply: tool missing, login, crash, timeout, unreadable output."""


class Provider(Protocol):
    """The constructor also takes `edit=True`. With False, no session has a tool that edits files, though it
    may run shell commands, and describe() says so (the judge and the planner). Reply.trace may be None. CONTINUE,
    if the class has it, is the command a person types in the folder to go on with a session: "tool --flag {}"."""

    def describe(self) -> str:
        """One line: which tool, model and permissions every call uses (run.json and the report)."""

    def run(self, prompt: str, workdir: Path, timeout_s: float, resume: str | None = None) -> Reply:
        """A NEW session in workdir, or `resume` (a Reply.session) continued: prompt in, at most timeout_s, its Reply or ProviderError."""
