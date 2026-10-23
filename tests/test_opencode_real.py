"""The opencode provider with the REAL opencode binary and a scripted local model. Optional.

Skipped unless SUBGOAL_TEST_OPENCODE names an opencode binary:

    SUBGOAL_TEST_OPENCODE=/path/to/opencode python3 -m unittest tests/test_opencode_real.py

No real model is called. The binary runs with a clean environment and a throwaway home whose
only provider is a local OpenAI-compatible server on 127.0.0.1 with a dummy key. The server
plays a script: tool calls, then a final text, streamed as chat-completion chunks.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BINARY = os.environ.get("SUBGOAL_TEST_OPENCODE")
ROOT = Path(__file__).resolve().parent.parent
USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}    # every reply's
# Each step is a tool call (name, arguments) or the final text. The worker reaches out of the
# folder (denied), then reads app.txt and rewrites it.
WORKER = [("write", {"filePath": "{outside}/escaped.txt", "content": "out\n"}),
          ("read", {"filePath": "app.txt"}),
          ("write", {"filePath": "app.txt", "content": "done\n"}),
          "I made app.txt say done."]
# The judge tries every way to write it can name, then reads, which must still work.
JUDGE = [("write", {"filePath": "app.txt", "content": "judged\n"}),
         ("edit", {"filePath": "app.txt", "oldString": "done", "newString": "judged"}),
         ("apply_patch", {"patchText": "*** Begin Patch\n*** Add File: judge.txt\n+judged\n*** End Patch"}),
         ("bash", {"command": "git status > app.txt", "description": "Write with a redirect"}),
         ("bash", {"command": "git diff --output=app.txt", "description": "Write with git"}),
         ("bash", {"command": "git status && touch judge.txt", "description": "Make a file"}),
         ("read", {"filePath": "app.txt"}),
         ("bash", {"command": "git status --short", "description": "See what changed"}),
         "app.txt says done.\nVERDICT: met"]


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
    def __init__(self, outside):
        super().__init__(("127.0.0.1", 0), Model)
        self.outside, self.requests = outside, []

    def reply(self, body):
        """The worker's or the judge's next step, from how many tool results the request holds."""
        if not body.get("tools"):                       # a side call, such as the session's title
            return {"role": "assistant", "content": "A title"}, "stop"
        script = JUDGE if "You are a reviewer" in json.dumps(body["messages"]) else WORKER
        done = sum(m.get("role") == "tool" for m in body["messages"])
        step = script[min(done, len(script) - 1)]
        if isinstance(step, str):
            return {"role": "assistant", "content": step}, "stop"
        arguments = json.dumps(step[1]).replace("{outside}", str(self.outside))
        return {"role": "assistant", "content": None, "tool_calls": [{
            "index": 0, "id": f"call_{done}", "type": "function",
            "function": {"name": step[0], "arguments": arguments}}]}, "tool_calls"


def throwaway_home(home, port):
    """A home with one config, whose only provider is the local server. Returns the clean env."""
    config = {"$schema": "https://opencode.ai/config.json", "autoupdate": False,
              "enabled_providers": ["mock"], "model": "mock/scripted", "small_model": "mock/scripted",
              "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "Mock",
                                    "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "dummy"},
                                    "models": {"scripted": {"name": "Scripted", "tool_call": True}}}}}
    for name in ("config/opencode", "data", "state", "cache", "tmp", "bin"):
        (home / name).mkdir(parents=True)
    (home / "config/opencode/opencode.json").write_text(json.dumps(config))
    (home / "bin/opencode").symlink_to(BINARY)
    return {"PATH": f"{home}/bin:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home / "tmp"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_STATE_HOME": str(home / "state"), "XDG_CACHE_HOME": str(home / "cache"),
            "OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"}


@unittest.skipUnless(BINARY, "set SUBGOAL_TEST_OPENCODE to an opencode binary to run these")
class RealOpenCodeTest(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        self.project, outside = root / "project", root / "outside"
        self.project.mkdir()
        outside.mkdir()
        (self.project / "app.txt").write_text("todo\n")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        self.model = Server(outside)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)
        self.env = throwaway_home(root / "home", self.model.server_port)
        # opencode was reported to signal all of the user's processes when a run ends (upstream
        # issue 45938). This bystander is ours, in its own session, and must outlive every run.
        self.bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900)"],
                                          start_new_session=True)
        self.addCleanup(self.bystander.wait)
        self.addCleanup(self.bystander.terminate)

    def subgoal(self, *argv):
        done = subprocess.run([sys.executable, "-m", "subgoal", "--provider", "opencode",
                               "--dir", str(self.project), "--goal", "Make app.txt say done.",
                               "--max-iterations", "1", "--max-minutes", "5", *argv],
                              cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=600)
        self.assertIsNone(self.bystander.poll(), "a process that opencode did not start was stopped")
        return done.returncode, done.stdout + done.stderr

    def tool_results(self, judge):
        """What opencode sent back to the model after each tool call of the worker's or the judge's."""
        calls = [b for b in self.model.requests
                 if b.get("tools") and judge == ("You are a reviewer" in json.dumps(b["messages"]))]
        return [str(m["content"]) for m in calls[-1]["messages"] if m["role"] == "tool"], calls

    def test_R1_the_worker_edits_inside_the_folder_only(self):
        code, out = self.subgoal("--check", "grep -q done app.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: the check exited 0", out)
        self.assertEqual((self.project / "app.txt").read_text(), "done\n")
        self.assertEqual(list(self.model.outside.iterdir()), [])
        (escaped, read, wrote), calls = self.tool_results(judge=False)
        self.assertIn("prevents you from using this specific tool call", escaped)
        self.assertIn("1: todo", read)
        self.assertIn("Wrote file successfully", wrote)
        self.assertTrue({"write", "edit", "bash"} <= {t["function"]["name"] for t in calls[-1]["tools"]})
        step = next(self.project.glob(".subgoal/*/01"))         # what the parser read from the events
        call = json.loads((step / "call.json").read_text())
        self.assertEqual((call["tokens"], call["cost_usd"]), (4 * 110, None))
        self.assertTrue(call["session"].startswith("ses_"))
        self.assertEqual((step / "reply.md").read_text(), "I made app.txt say done.\n")

    def test_R2_the_judge_cannot_write(self):
        code, out = self.subgoal()                               # no --check: a read-only judge decides
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: a judge said met", out)
        self.assertEqual((self.project / "app.txt").read_text(), "done\n")    # the worker's edit, no more
        self.assertFalse((self.project / "judge.txt").exists())
        said, calls = self.tool_results(judge=True)
        self.assertEqual({t["function"]["name"] for b in calls for t in b["tools"]}, {"bash", "glob", "grep", "read"})
        self.assertEqual(len(said), len(JUDGE) - 1)
        for text in said[:3]:                                    # write, edit, apply_patch: not offered
            self.assertIn("unavailable tool", text)
        for text in said[3:6]:                                   # the redirect, --output, touch
            self.assertIn("prevents you from using this specific tool call", text)
        self.assertIn("1: done", said[6])                       # reading still works
        self.assertNotIn("prevents you", said[7])               # and so does git status
        step = next(self.project.glob(".subgoal/*/01"))
        self.assertTrue((step / "judge.md").read_text().endswith("VERDICT: met\n"))


if __name__ == "__main__":
    unittest.main()
