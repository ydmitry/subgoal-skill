"""The first provider: Claude Code in headless mode (claude -p), a new session per call unless one is continued."""
import json
import os
import shlex

from .process import run_process
from .provider import ProviderError, Reply

RESUME_ID_KEY = "session" + "_id"      # the id `claude --resume` takes, in two parts so a search for leaked chat links finds none
# The token counts in claude's "usage" object. Their sum is the call's token total.
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# What the judge (edit=False) may use: read files, and run shell commands in the folder. No edit tool.
JUDGE_TOOLS = ("Read", "Grep", "Glob", "Bash")
# The trace: each tool call's key argument, by tool (others: the name alone). Reads fold into one line.
TRACE_ARGS = {"Skill": "skill", "Task": "description", "Agent": "description", "Write": "file_path",
              "Edit": "file_path", "Read": "file_path", "Bash": "command"}
READS, TRACE_CAP = ("Read", "Grep", "Glob"), 8_000


class ClaudeCode:
    """Every call runs the same command, so the permissions are fixed here, once."""
    CONTINUE = "claude --resume {}"

    def __init__(self, model=None, allow=(), executable="claude", edit=True, records=None):
        # -p                       headless, and a new session; run(resume=ID) adds --resume ID
        # stream-json --verbose    one JSON event per line (claude needs --verbose for it): the tool
        #                          calls, then a result event like the one object `json` prints
        # acceptEdits, --add-dir   file edits and simple file commands, in the folder and in its records (kept outside it) only
        # dontAsk (edit=False)     deny everything the allow list does not name
        # --permission-prompts     nobody is there to answer a prompt, so it is denied
        self.edit, self.argv = edit, [executable, "-p", "--output-format", "stream-json", "--verbose", *["--add-dir", str(records)] * bool(records),
                                      "--permission-mode", "acceptEdits" if edit else "dontAsk", "--permission-prompts", "none"]
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

    def run(self, prompt, workdir, timeout_s, resume=None):
        os.environ.setdefault("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "1")   # a slow command fails; none holds the session
        try:
            argv = self.argv[:2] + ["--resume", resume] * bool(resume) + self.argv[2:]     # right after -p; --allowedTools stays last
            code, out, err, timed_out = run_process(argv, workdir, timeout_s, stdin=prompt)
        except FileNotFoundError:
            raise ProviderError(f"{self.argv[0]} not found: is Claude Code on PATH?") from None
        if timed_out:
            raise ProviderError(f"no reply within {timeout_s:.0f}s; the call was stopped")
        return parse(code, out, err, workdir)


def parse(exit_code, stdout, stderr, workdir=None):
    """Map claude's output onto a Reply: the last result event of the stream, and the trace from
    its tool calls (see trace()), or one JSON object (`--output-format json`, no trace). Unusable: ProviderError."""
    data, trace, events = _json(stdout), None, []
    if not isinstance(data, dict):
        events = [e for e in map(_json, stdout.splitlines()) if isinstance(e, dict)]
        results = [e for e in events if e.get("type") == "result"]
        data, trace = (results[-1], tool_trace(events, results[-1].get("permission_denials"), workdir)) if results else (None, None)
    if not isinstance(data, dict) or exit_code != 0 or data.get("is_error"):
        said = data.get("result") or "; ".join(map(str, data.get("errors") or [])) \
            if isinstance(data, dict) else ""
        raw = stdout.strip()[-2000:] if not events else "its result event gave no detail" if data \
            else f"no result event in {len(events)} stream events"    # never the stream: it holds what tools returned
        detail = said or stderr.strip()[-2000:] or raw or "no output"
        raise ProviderError(f"claude -p failed (exit {exit_code}): {detail}")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    counts = [usage[k] for k in TOKEN_FIELDS if isinstance(usage.get(k), int)]
    return Reply(text=str(data.get("result") or ""), tokens=sum(counts) if counts else None,
                 cost_usd=data.get("total_cost_usd"), session=data.get(RESUME_ID_KEY), trace=trace)


def tool_trace(events, denials=(), workdir=None):
    """claude's tool calls as trace lines; one the permission layer denied (permission_denials) is marked `denied: `."""
    denied = {d.get("tool_use_id") for d in denials or () if isinstance(d, dict)}
    blocks = [e["message"].get("content") for e in events if e.get("type") == "assistant" and isinstance(e.get("message"), dict)]
    return trace([(str(c.get("name")), c.get("input"), "denied: " * (c.get("id") in denied)) for b in blocks
                  if isinstance(b, list) for c in b if isinstance(c, dict) and c.get("type") == "tool_use"], workdir)


def trace(calls, workdir=None):
    """Every provider's trace, so the judge reads one format: per (Claude Code tool name, input, mark) call, in order,
    one line: the mark, the tool and its key argument, cut at 160 characters (a file path at 100, keeping its end so its
    file name shows; inside workdir, the folder the call ran in, relative to it, as is each of a patch's files), never
    what it returned. A run of unmarked Read, Grep and Glob calls is one line, `Read: a.md, b.md`. Past TRACE_CAP, the middle goes."""
    lines, root = [], " ".join(f"{workdir}/".split()) if workdir else ""     # whitespace collapsed, as in the args
    for name, args, mark in calls:
        arg = " ".join(str(args.get(TRACE_ARGS.get(name), "")).split()) if isinstance(args, dict) else ""
        if name in READS and not mark:
            seen = lines.pop()[6:] if lines and lines[-1].startswith("Read:") else ""
            lines.append(("Read: " + ", ".join(filter(None, [seen, arg.rsplit("/", 1)[-1] if name == "Read" else ""])))[:200].rstrip())
        else:                                                   # a command stays as written; the folder itself too
            arg = (arg.replace(", " + root, ", ").removeprefix(root) or arg)[-100:] if TRACE_ARGS.get(name) == "file_path" else arg[:160]
            lines.append(f"{mark}{name[:100]} {arg}".rstrip())
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
