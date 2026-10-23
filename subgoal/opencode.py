"""A second provider: opencode in non-interactive mode (opencode run), a new session per call."""
import json
import shlex
import shutil

from .process import run_process
from .provider import ProviderError, Reply

AGENT = "subgoal"    # every call runs as this agent, which the call's own config defines
# opencode asks before these by default. `opencode run` rejects every ask, and a rejection ends
# the session, so the worker is told no instead and goes on: no paths outside the folder.
WORKER_RULES = {"external_directory": "deny", "doom_loop": "deny"}
# What a read-only session may use: look at files, and look at git. The last matching rule wins,
# so the denials for a redirect and for `git diff --output=FILE` (both write a file) come last.
READ_ONLY_RULES = {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow",
                   "bash": {"*": "deny", "git status *": "allow", "git diff *": "allow",
                            "git log *": "allow", "git show *": "allow", "*>*": "deny",
                            "*--output*": "deny"}}


class OpenCode:
    """Every call runs the same command with the same config, so the permissions are fixed here, once."""

    def __init__(self, model=None, allow=(), executable="opencode", read_only=False):
        # run --format json        a new session, one JSON event per line: no --continue or --session
        # OPENCODE_CONFIG_CONTENT  merged over your own config: one agent with the rules above, made
        #                          the default, so no session starts as another agent (a missing
        #                          default agent is an error, not a fallback)
        # allow                    Claude Code rules, unused: the CLI refuses --allow for opencode, and
        #                          the worker may already run any command in the folder, the check too
        # --pure (read_only)       no plugins, whose code could write
        self.read_only, self.executable = read_only, executable
        config = {"default_agent": AGENT, "agent": {AGENT: {
            "mode": "primary", "permission": READ_ONLY_RULES if read_only else WORKER_RULES}}}
        self.argv = ["env", "OPENCODE_CONFIG_CONTENT=" + json.dumps(config, separators=(",", ":")),
                     executable, "run", "--format", "json"]
        if read_only:
            self.argv.append("--pure")
        if model:
            self.argv += ["--model", model]

    def describe(self):
        model = "" if "--model" in self.argv else ", your default model"
        only = ", read-only: edits are denied" if self.read_only else ""
        return f"{shlex.join(self.argv)} --dir DIR  (prompt on stdin{model}{only})"

    def run(self, prompt, workdir, timeout_s):
        if not shutil.which(self.executable):           # `env` would start, then not find it
            raise ProviderError(f"{self.executable} not found: is opencode on PATH?")
        # --dir: opencode takes its folder from $PWD, which a child's cwd does not change
        code, out, err, timed_out = run_process([*self.argv, "--dir", str(workdir)], workdir,
                                                timeout_s, stdin=prompt)
        if timed_out:
            raise ProviderError(f"no reply within {timeout_s:.0f}s; the call was stopped")
        if code and "Unrecognized flag: --dir" in err:
            raise ProviderError("this looks like opencode 2, which has no --dir: use --provider opencode2")
        return parse(code, out, err)


def parse(exit_code, stdout, stderr):
    """Map opencode's JSON events, one per line, onto a Reply. Anything unusable raises ProviderError."""
    try:
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    except ValueError:
        events = None
    if events is None or not all(isinstance(e, dict) for e in events):
        detail = stderr.strip()[-2000:] or stdout.strip()[-2000:]
        raise ProviderError(f"opencode run printed lines that are not JSON events (exit {exit_code}): {detail}")
    text, tokens, cost, errors = [], [], 0.0, []
    for event in events:
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "step_start":
            text = []                                   # the reply is the last step's words only
        elif event.get("type") == "text" and isinstance(part.get("text"), str):
            text.append(part["text"])
        elif event.get("type") == "step_finish":
            tokens.append(step_tokens(part.get("tokens")))
            cost += part.get("cost") if isinstance(part.get("cost"), (int, float)) else 0
        elif event.get("type") == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            errors.append(str(data.get("message") or error.get("message") or error.get("name")
                              or event.get("error")))           # opencode 1 nests it in data, 2 does not
    reply = "\n".join(text).strip()
    if exit_code != 0 or errors or not reply:
        detail = "; ".join(errors) or (stderr.strip()[-2000:] if exit_code else "") or "no final text"
        raise ProviderError(f"opencode run failed (exit {exit_code}): {detail}")
    counted = [n for n in tokens if n is not None]
    return Reply(text=reply, tokens=sum(counted) if counted else None,
                 cost_usd=cost or None,                 # 0 is what opencode says when it has no price
                 session=next((e["sessionID"] for e in events if e.get("sessionID")), None))


def step_tokens(tokens):
    """One step's tokens: input, output, reasoning, cache reads and writes. None if it gave none."""
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    counts = [n for n in (tokens.get("input"), tokens.get("output"), tokens.get("reasoning"),
                          cache.get("read"), cache.get("write")) if isinstance(n, int)]
    return sum(counts) if counts else None
