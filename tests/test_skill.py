"""SKILL.md, the repo as a Claude Code skill, promises only flags and exit codes that exist."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

from subgoal.loop import StopReason

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / "SKILL.md").read_text()
HELP = subprocess.run([sys.executable, "-m", "subgoal", "--help"], cwd=ROOT,
                      capture_output=True, text=True).stdout


class SkillTest(unittest.TestCase):
    def test_frontmatter_and_size(self):
        self.assertTrue(SKILL.startswith("---\nname: subgoal\ndescription: "))
        self.assertLessEqual(len(SKILL.splitlines()), 60)

    def test_every_flag_exists(self):
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", SKILL))
        self.assertIn("--dry-run", flags)
        for flag in flags:
            self.assertIn(flag, HELP, f"SKILL.md mentions {flag}, which --help does not list")

    def test_every_exit_code_is_true(self):
        readme, codes = (ROOT / "README.md").read_text(), {r.value: r.exit_code for r in StopReason}
        rows = re.findall(r"^\| (\d+) \| ([a-z_, ]+) \|", SKILL, re.M)
        listed = {reason for _, reasons in rows for reason in reasons.split(", ")}
        self.assertEqual(listed, set(codes) | {"bad arguments"})
        for code, reasons in rows:
            self.assertIn(f"{code} {reasons}", HELP)
            for reason in set(reasons.split(", ")) - {"bad arguments"}:
                self.assertEqual(codes[reason], int(code))
                self.assertRegex(readme, rf"\| `{reason}` \|[^\n]*\| {code} \|")


if __name__ == "__main__":
    unittest.main()
