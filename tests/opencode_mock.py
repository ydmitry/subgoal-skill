"""A scripted local model and a throwaway home, for the tests that run a real opencode binary.

No real model is called. The binary runs with a clean environment and a throwaway home whose
only provider is a local OpenAI-compatible server on 127.0.0.1 with a dummy key. The server
plays a script: tool calls, then a final text, streamed as chat-completion chunks.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}    # every reply's


class Model(BaseHTTPRequestHandler):
    """POST /v1/chat/completions: the next scripted reply, as one response or as a stream."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        message, finish = self.server.reply(body)
        choice = {"index": 0, "finish_reason": finish}
        if not body.get("stream"):
            return self.send(200, "application/json", json.dumps({
                "id": "mock", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [dict(choice, message=message)], "usage": USAGE}))
        chunks = [[dict(choice, delta=message, finish_reason=None)], [dict(choice, delta={})], []]
        self.send(200, "text/event-stream", "".join(
            "data: " + json.dumps({"id": "mock", "object": "chat.completion.chunk", "created": 0,
                                   "model": body["model"], "choices": choices,
                                   **({} if choices else {"usage": USAGE})}) + "\n\n"
            for choices in chunks) + "data: [DONE]\n\n")

    def send(self, status, kind, text):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    """Plays the worker's script or the judge's. Each step is a tool call (name, arguments)
    or the final text; "{outside}" in an argument becomes a folder outside the project."""

    def __init__(self, outside, worker, judge):
        super().__init__(("127.0.0.1", 0), Model)
        self.outside, self.worker, self.judge, self.requests = outside, worker, judge, []

    def reply(self, body):
        """The worker's or the judge's next step, from how many tool results the request holds."""
        if not body.get("tools"):                       # a side call, such as the session's title
            return {"role": "assistant", "content": "A title"}, "stop"
        script = self.judge if "You are a reviewer" in json.dumps(body["messages"]) else self.worker
        done = sum(m.get("role") == "tool" for m in body["messages"])
        step = script[min(done, len(script) - 1)]
        if isinstance(step, str):
            return {"role": "assistant", "content": step}, "stop"
        arguments = json.dumps(step[1]).replace("{outside}", str(self.outside))
        return {"role": "assistant", "content": None, "tool_calls": [{
            "index": 0, "id": f"call_{done}", "type": "function",
            "function": {"name": step[0], "arguments": arguments}}]}, "tool_calls"


def throwaway_home(home, binary, name, config):
    """A home whose only opencode config is `config`, with the binary on PATH as `name`.
    Returns the whole environment the binary gets: nothing of the person's own."""
    for folder in ("config/opencode", "data", "state", "cache", "tmp", "bin"):
        (home / folder).mkdir(parents=True)
    (home / "config/opencode/opencode.json").write_text(json.dumps(config))
    (home / "bin" / name).symlink_to(binary)
    return {"PATH": f"{home}/bin:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home / "tmp"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_STATE_HOME": str(home / "state"), "XDG_CACHE_HOME": str(home / "cache"),
            "OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1", "OPENCODE_DISABLE_FREE_MODELS": "1"}


class RealOpenCodeCase(unittest.TestCase):
    """A project with app.txt under git, the scripted model, a throwaway home, and a bystander.
    A subclass names the binary and the provider, gives the scripts, and writes the config."""
    binary = provider = None
    worker = judge = ()

    def config(self, port):
        """The throwaway home's opencode config, whose only provider is the model on `port`."""
        raise NotImplementedError

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        self.project, self.outside = root / "project", root / "outside"
        self.project.mkdir()
        self.outside.mkdir()
        (self.project / "app.txt").write_text("todo\n")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        self.model = Server(self.outside, self.worker, self.judge)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)
        self.env = throwaway_home(root / "home", self.binary, self.provider, self.config(self.model.server_port))
        # opencode was reported to signal all of the user's processes when a run ends (upstream
        # issue 45938). This bystander is ours, in its own session, and must outlive every run.
        self.bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900)"],
                                          start_new_session=True)
        self.addCleanup(self.bystander.wait)
        self.addCleanup(self.bystander.terminate)
        self.before = self.running()

    def running(self):
        """The pids of the processes that run this binary: by its name, its link or its real path."""
        names = {self.provider, str(Path(self.env["HOME"]) / "bin" / self.provider), str(Path(self.binary).resolve())}
        ps = subprocess.run(["ps", "-A", "-o", "pid=,args="], capture_output=True, text=True).stdout
        return {int(line.split()[0]) for line in ps.splitlines() if line.split()[1:2] and line.split()[1] in names}

    def subgoal(self, *argv):
        done = subprocess.run([sys.executable, "-m", "subgoal", "--provider", self.provider,
                               "--dir", str(self.project), "--goal", "Make app.txt say done.",
                               "--max-iterations", "1", "--max-minutes", "5", *argv],
                              cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=600)
        self.assertIsNone(self.bystander.poll(), "a process that opencode did not start was stopped")
        for _ in range(50):                             # nothing a run starts may outlive it
            left = self.running() - self.before
            if not left:
                break
            time.sleep(0.1)
        self.assertEqual(left, set(), "processes of this binary outlived the run (a service?)")
        return done.returncode, done.stdout + done.stderr

    def tool_results(self, judge):
        """What opencode sent back to the model after each tool call of the worker's or the judge's."""
        calls = [b for b in self.model.requests
                 if b.get("tools") and judge == ("You are a reviewer" in json.dumps(b["messages"]))]
        return [str(m["content"]) for m in calls[-1]["messages"] if m["role"] == "tool"], calls
