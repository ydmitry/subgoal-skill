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
# The trace: each tool call's key argument, by tool (others: the name alone). Reads fold into one line.
TRACE_ARGS = {"Skill": "skill", "Task": "description", "Agent": "description", "Write": "file_path",
              "Edit": "file_path", "Bash": "command"}
READS, TRACE_CAP = ("Read", "Grep", "Glob"), 4_000


class ClaudeCode:
    """Every call runs the same command, so the permissions are fixed here, once."""

    def __init__(self, model=None, allow=(), executable="claude", edit=True):
        # -p                       headless, and a new session: no --continue or --resume
        # stream-json --verbose    one JSON event per line (claude needs --verbose for it): the tool
        #                          calls, then a result event like the one object `json` prints
        # acceptEdits              file edits and simple file commands, inside the folder only
        # dontAsk (edit=False)     deny everything the allow list does not name
        # --permission-prompts     nobody is there to answer a prompt, so it is denied
        self.edit = edit
        mode = "acceptEdits" if edit else "dontAsk"
        self.argv = [executable, "-p", "--output-format", "stream-json", "--verbose",
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
    """Map claude's output onto a Reply: the last result event of the stream, and the trace from
    its tool calls, or one JSON object (`--output-format json`, no trace). Unusable: ProviderError."""
    data, trace = _json(stdout), None
    if not isinstance(data, dict):
        events = [e for e in map(_json, stdout.splitlines()) if isinstance(e, dict)]
        results = [e for e in events if e.get("type") == "result"]
        data, trace = (results[-1], tool_trace(events)) if results else (None, None)
    if not isinstance(data, dict) or exit_code != 0 or data.get("is_error"):
        said = data.get("result") or "; ".join(map(str, data.get("errors") or [])) \
            if isinstance(data, dict) else ""
        detail = said or stderr.strip()[-2000:] or stdout.strip()[-2000:] or "no output"
        raise ProviderError(f"claude -p failed (exit {exit_code}): {detail}")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    counts = [usage[k] for k in TOKEN_FIELDS if isinstance(usage.get(k), int)]
    return Reply(text=str(data.get("result") or ""), tokens=sum(counts) if counts else None,
                 cost_usd=data.get("total_cost_usd"), session=data.get(RESUME_ID_KEY), trace=trace)


def tool_trace(events):
    """One line per tool call, in order: the tool and its key argument, never what it returned.
    A run of Read, Grep and Glob calls is one `Read xN` line. Past TRACE_CAP characters, the middle goes."""
    lines = []
    for event in events:
        message = event.get("message") if event.get("type") == "assistant" else None
        content = message.get("content") if isinstance(message, dict) else None
        for call in content if isinstance(content, list) else []:
            if not isinstance(call, dict) or call.get("type") != "tool_use":
                continue
            name, args = str(call.get("name")), call.get("input")
            arg = " ".join(str(args.get(TRACE_ARGS.get(name), "")).split()) if isinstance(args, dict) else ""
            if name in READS:
                reads = int(lines.pop()[6:]) if lines and lines[-1].startswith("Read x") else 0
                lines.append(f"Read x{reads + 1}")
            else:
                lines.append(f"{name} {arg[:100] if name == 'Bash' else arg}".rstrip())
    text, half = "\n".join(lines), TRACE_CAP // 2
    if len(text) <= TRACE_CAP:
        return text
    head, tail = text[:half].rsplit("\n", 1)[0], text[-half:].split("\n", 1)[-1]
    left_out = len(lines) - head.count("\n") - tail.count("\n") - 2
    return f"{head}\n... {left_out} lines left out ...\n{tail}"


def _json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None
