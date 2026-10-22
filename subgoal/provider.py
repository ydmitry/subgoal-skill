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


class ProviderError(Exception):
    """No usable reply: tool missing, login, crash, timeout, unreadable output."""


class Provider(Protocol):
    """A provider's constructor also takes `read_only=False`. With True, every session it
    starts must be unable to edit anything, and describe() says so. The judge is one."""

    def describe(self) -> str:
        """One line: which tool, model and permissions every call will use.
        Written to run.json and to the report."""

    def run(self, prompt: str, workdir: Path, timeout_s: float) -> Reply:
        """Start a NEW agent session in workdir, give it prompt, wait at most
        timeout_s, and return its final Reply. Raise ProviderError on failure."""
