"""The command line: dry run, argument guards, and the size budget of the core."""
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from subgoal.claude_code import ClaudeCode
from subgoal.cli import main

CORE = Path(__file__).resolve().parent.parent / "subgoal"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                return main(list(argv)), out.getvalue()
            except SystemExit as stop:
                return stop.code, out.getvalue()

    def test_X1_dry_run(self):
        with mock.patch.object(ClaudeCode, "run", side_effect=AssertionError("a dry run calls nobody")):
            code, out = self.cli("--dir", str(self.dir), "--goal", "Say hi.", "--check", "exit 1",
                                 "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(ClaudeCode(allow=["Bash(exit 1)"]).describe(), out)
        self.assertIn("GOAL\nSay hi.", out)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_X2_argument_guards(self):
        ok, here = ["--goal", "g", "--check", "true"], ["--dir", str(self.dir)]
        for argv in (["--dir", "~", *ok], ["--dir", "/", *ok], [*here, "--goal", "g"],
                     [*here, "--check", "true"], [*here, *ok, "--goal-file", "f"],
                     [*here, *ok, "--max-iterations", "0"]):
            self.assertEqual(self.cli(*argv)[0], 2, argv)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_S1_size_budget(self):
        lines = [line for f in CORE.glob("*.py") for line in f.read_text().splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 400, "the core must stay readable in one sitting")


if __name__ == "__main__":
    unittest.main()
