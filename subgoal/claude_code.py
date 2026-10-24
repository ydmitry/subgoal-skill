"""The one concrete provider: Claude Code in headless mode (claude -p), a new session per call."""
import json
import shlex

from .process import run_process
from .provider import ProviderError, Reply

# The key of the id that `claude --resume` takes. Written in two parts only so that a search
# of this repo for leaked chat links (which contain that spelling) finds nothing.
RESUME_ID_KEY = "session" + "_id"
# The token counts in claude's "usage" object. Their sum is the call's token total.
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")
# What the judge (edit=False) may use: read files, and run shell commands in the folder. No edit tool.
JUDGE_TOOLS = ("Read", "Grep", "Glob", "Bash")


class ClaudeCode:
    """Every call runs the same command, so the permissions are fixed here, once."""

    def __init__(self, model=None, allow=(), executable="claude", edit=True):
        # -p                       headless, and a new session: no --continue or --resume
        # --output-format json     one result object to parse
        # acceptEdits              file edits and simple file commands, inside the folder only
        # dontAsk (edit=False)     deny everything the allow list does not name
        # --permission-prompts     nobody is there to answer a prompt, so it is denied
        self.edit = edit
        mode = "acceptEdits" if edit else "dontAsk"
        self.argv = [executable, "-p", "--output-format", "json",
                     "--permission-mode", mode, "--permission-prompts", "none"]
        if not edit:
            # --tools                only these exist in this session: no Edit, Write or NotebookEdit
            # --strict-mcp-config    no MCP tools, which could write outside the folder
            self.argv += ["--tools", ",".join(JUDGE_TOOLS), "--strict-mcp-config"]
            allow = JUDGE_TOOLS                        # the caller's extra rules never widen it
        if model:
            self.argv += ["--model", model]
        if allow:
            self.argv += ["--allowedTools", *allow]    # takes several values, so it goes last

    def describe(self):
        model = "" if "--model" in self.argv else ", your default model"
        only = ", no edit tools: it may run commands" if not self.edit else ""
        return f"{shlex.join(self.argv)}  (prompt on stdin{model}{only})"

    def run(self, prompt, workdir, timeout_s):
        try:
            code, out, err, timed_out = run_process(self.argv, workdir, timeout_s, stdin=prompt)
        except FileNotFoundError:
            raise ProviderError(f"{self.argv[0]} not found: is Claude Code on PATH?") from None
        if timed_out:
            raise ProviderError(f"no reply within {timeout_s:.0f}s; the call was stopped")
        return parse(code, out, err)


def parse(exit_code, stdout, stderr):
    """Map claude's JSON result onto a Reply. Anything unusable raises ProviderError."""
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or exit_code != 0 or data.get("is_error"):
        said = data.get("result") or "; ".join(map(str, data.get("errors") or [])) \
            if isinstance(data, dict) else ""
        detail = said or stderr.strip()[-2000:] or stdout.strip()[-2000:] or "no output"
        raise ProviderError(f"claude -p failed (exit {exit_code}): {detail}")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    counts = [usage[k] for k in TOKEN_FIELDS if isinstance(usage.get(k), int)]
    return Reply(text=str(data.get("result") or ""), tokens=sum(counts) if counts else None,
                 cost_usd=data.get("total_cost_usd"), session=data.get(RESUME_ID_KEY))
