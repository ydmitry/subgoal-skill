"""The opencode2 provider with the REAL opencode 2 binary and a scripted local model. Optional.

Skipped unless SUBGOAL_TEST_OPENCODE2 names an opencode 2 binary:

    SUBGOAL_TEST_OPENCODE2=/path/to/opencode python3 -m unittest discover -s tests -p test_opencode2_real.py

The scripted model, the throwaway home, the bystander and the check that no process of the
binary outlives a run (no background service, no private server) are in opencode_mock.py.
"""
import json
import os
import unittest

import opencode_mock

BINARY = os.environ.get("SUBGOAL_TEST_OPENCODE2")
# The worker runs a command in the folder, reaches out of it three ways (denied), reads .env
# (opencode asks, and an ask is rejected at once), then rewrites app.txt and runs a check.
WORKER = [("shell", {"command": "ls"}),
          ("shell", {"command": "cat /etc/hosts"}),
          ("shell", {"command": "touch {outside}/x-outside"}),
          ("write", {"path": "{outside}/escaped.txt", "content": "out\n"}),
          ("read", {"path": ".env"}),
          ("read", {"path": "app.txt"}),
          ("write", {"path": "app.txt", "content": "done\n"}),
          ("shell", {"command": "grep -q done app.txt && echo found"}),
          "I made app.txt say done."]
# The judge tries each edit tool (none is offered) and a command naming a path outside (denied,
# as for the worker), then runs a check of its own, reads, and looks at git, which must all work.
JUDGE = [("write", {"path": "app.txt", "content": "judged\n"}),
         ("edit", {"path": "app.txt", "oldString": "done", "newString": "judged"}),
         ("patch", {"patchText": "*** Begin Patch\n*** Add File: judge.txt\n+judged\n*** End Patch"}),
         ("shell", {"command": "cat /etc/hosts"}),
         ("shell", {"command": "grep -q done app.txt && echo passed"}),
         ("read", {"path": "app.txt"}),
         ("shell", {"command": "git status --short"}),
         "app.txt says done.\nVERDICT: met"]


@unittest.skipUnless(BINARY, "set SUBGOAL_TEST_OPENCODE2 to an opencode 2 binary to run these")
class RealOpenCode2Test(opencode_mock.RealOpenCodeCase):
    binary, provider, worker, judge = BINARY, "opencode2", WORKER, JUDGE

    def config(self, port):
        return {"$schema": "https://opencode.ai/config.json", "update": "disable", "model": "mock/scripted",
                "providers": {"mock": {"name": "Mock", "package": "@opencode/ai/providers/openai-compatible",
                                       "settings": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "dummy"},
                                       "models": {"scripted": {"name": "Scripted"}}}}}

    def setUp(self):
        super().setUp()
        # A plugin in the project, whose code runs when opencode loads it: it notes whether project
        # config was on. The worker loads it; the judge, with project config off, must not.
        (self.project / ".env").write_text("SECRET=1\n")
        self.loaded = self.project.parent / "plugin-loaded.txt"
        (self.project / ".opencode/plugins").mkdir(parents=True)
        (self.project / ".opencode/plugins/planted.js").write_text(
            'import { appendFileSync } from "node:fs"\n'
            f'appendFileSync({json.dumps(str(self.loaded))}, '
            '(process.env.OPENCODE_DISABLE_PROJECT_CONFIG ? "off" : "on") + "\\n")\n'
            'export const Planted = async () => ({})\n')

    def test_R1_the_worker_edits_inside_the_folder_only(self):
        code, out = self.subgoal("--check", "grep -q done app.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: the check exited 0", out)
        self.assertEqual((self.project / "app.txt").read_text(), "done\n")
        self.assertEqual(list(self.outside.iterdir()), [])
        (listed, hosts, touched, escaped, secret, read, wrote, checked), calls = self.tool_results(judge=False)
        self.assertIn("app.txt", listed)                         # a command in the folder runs
        for text in (hosts, touched):                            # a command naming a path outside does not
            self.assertIn("Permission denied: shell", text)
        self.assertIn("Permission denied: external_directory", escaped)
        self.assertIn("cannot ask the user for permission, so the request was rejected", secret)
        self.assertIn("1: todo", read)
        self.assertIn("Wrote file successfully", wrote)
        self.assertIn("found", checked)                          # the worker can run a check
        self.assertTrue({"write", "edit", "shell"} <= {t["function"]["name"] for t in calls[-1]["tools"]})
        self.assertEqual(self.loaded.read_text(), "on\n")    # project plugins do run in a worker session
        step = next(self.project.glob(".subgoal/*/01"))         # what the parser read from the events
        call = json.loads((step / "call.json").read_text())
        self.assertIn(call["tokens"], (8 * 110, 9 * 110))       # opencode 2.1.4 may drop the last step's
        self.assertIsNone(call["cost_usd"])
        self.assertTrue(call["session"].startswith("ses_"))
        self.assertEqual((step / "reply.md").read_text(), "I made app.txt say done.\n")

    def test_R2_the_judge_has_no_edit_tools_but_runs_commands(self):
        code, out = self.subgoal()                               # no --check: a judge decides
        self.assertEqual(code, 0, out)
        self.assertIn("GOAL MET after step 1: a judge said met", out)
        self.assertEqual((self.project / "app.txt").read_text(), "done\n")    # the worker's edit, no more
        self.assertFalse((self.project / "judge.txt").exists())
        self.assertEqual(self.loaded.read_text(), "on\n")       # the worker's load only: none by the judge
        said, calls = self.tool_results(judge=True)
        self.assertEqual({t["function"]["name"] for b in calls for t in b["tools"]}, {"glob", "grep", "read", "shell"})
        self.assertEqual(len(said), len(JUDGE) - 1)
        for text in said[:3]:                                    # write, edit, patch: not offered
            self.assertIn("is currently available", text)
        self.assertIn("Permission denied: shell", said[3])      # a path outside the folder
        self.assertIn("passed", said[4])                        # a command of its own runs
        self.assertIn("1: done", said[5])                       # reading works
        self.assertNotIn("Permission denied", said[6])          # and so does git status
        step = next(self.project.glob(".subgoal/*/01"))
        self.assertTrue((step / "judge.md").read_text().endswith("VERDICT: met\n"))


if __name__ == "__main__":
    unittest.main()
