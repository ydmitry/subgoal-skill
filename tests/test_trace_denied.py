"""A tool call the permission layer denied shows as `denied: ...` in the trace, from the result event's permission_denials."""
import json
import unittest

import fake  # noqa: F401  (throwaway config and records homes)
from subgoal.claude_code import parse, tool_trace


def use(tid, name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(denied_ids):
    return {"type": "result", "is_error": False, "result": "done", "usage": {"input_tokens": 1},
            "permission_denials": [{"tool_name": "Bash", "tool_use_id": t, "tool_input": {}} for t in denied_ids]}


class TraceDenied(unittest.TestCase):
    def test_denied_call_is_marked_and_the_rest_is_unchanged(self):
        events = [use("a", "Bash", command="git stash"), use("b", "Bash", command="ls"), use("c", "Skill", skill="tiny"),
                  use("d", "Read", file_path="/p/x.md"), use("e", "Read", file_path="/p/y.md")]
        self.assertEqual(tool_trace(events, result(["a", "c"])["permission_denials"]).splitlines(),
                         ["denied: Bash git stash", "Bash ls", "denied: Skill tiny", "Read: x.md, y.md"])
        self.assertEqual(tool_trace(events).splitlines(), ["Bash git stash", "Bash ls", "Skill tiny", "Read: x.md, y.md"])

    def test_a_denied_read_does_not_fold_into_the_read_line(self):
        events = [use("d", "Read", file_path="/p/x.md"), use("e", "Grep", pattern="q"), use("f", "Read", file_path="/p/z.md")]
        self.assertEqual(tool_trace(events, [{"tool_use_id": "e"}]).splitlines(), ["Read: x.md", "denied: Grep", "Read: z.md"])

    def test_parse_passes_the_result_events_denials_to_the_trace(self):
        stream = "\n".join(json.dumps(e) for e in [use("a", "Bash", command="git stash"), use("b", "Bash", command="ls"),
                                                   result(["a"])])
        self.assertEqual(parse(0, stream, "").trace, "denied: Bash git stash\nBash ls")
        no_field = stream.replace('"permission_denials"', '"other"')          # an older claude without the field
        self.assertEqual(parse(0, no_field, "").trace, "Bash git stash\nBash ls")

    def test_a_denied_skill_is_not_a_skill_earlier_steps_used(self):
        trace = tool_trace([use("c", "Skill", skill="tiny"), use("g", "Skill", skill="big")], [{"tool_use_id": "c"}])
        used = sorted({s[6:].strip() for s in trace.splitlines() if s.startswith("Skill ")})     # loop.py's rule
        self.assertEqual(used, ["big"])


if __name__ == "__main__":
    unittest.main()
