"""The opencode provider with the REAL opencode binary and a scripted local model. Optional.

Skipped unless SUBGOAL_TEST_OPENCODE names an opencode binary (1.x):

    SUBGOAL_TEST_OPENCODE=/path/to/opencode python3 -m unittest discover -s tests -p test_opencode_real.py

The scripted model, the throwaway home and the bystander are in opencode_mock.py.
"""
import json
import os
import unittest

import opencode_mock

BINARY = os.environ.get("SUBGOAL_TEST_OPENCODE")
# The worker reaches out of the folder (denied), then reads app.txt and rewrites it.
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


@unittest.skipUnless(BINARY, "set SUBGOAL_TEST_OPENCODE to an opencode binary to run these")
class RealOpenCodeTest(opencode_mock.RealOpenCodeCase):
    binary, provider, worker, judge = BINARY, "opencode", WORKER, JUDGE

    def config(self, port):
        return {"$schema": "https://opencode.ai/config.json", "autoupdate": False,
                "enabled_providers": ["mock"], "model": "mock/scripted", "small_model": "mock/scripted",
                "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "Mock",
                                      "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "dummy"},
                                      "models": {"scripted": {"name": "Scripted", "tool_call": True}}}}}

    def test_R1_the_worker_edits_inside_the_folder_only(self):
        code, out = self.subgoal("--check", "grep -q done app.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: the check exited 0", out)
        self.assertEqual((self.project / "app.txt").read_text(), "done\n")
        self.assertEqual(list(self.outside.iterdir()), [])
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
