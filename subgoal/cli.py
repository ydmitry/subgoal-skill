"""The command line: read the arguments, build the provider, run the loop, exit with its code."""
import argparse
from pathlib import Path

from .claude_code import ClaudeCode
from .loop import CHECK_TIMEOUT_S, Limits, build_prompt, run_check, run_loop, safe_workdir

PROVIDERS = {"claude-code": ClaudeCode}     # a second provider is one more entry here

EPILOG = """exit codes: 0 goal_met | 1 out_of_iterations, out_of_time, out_of_tokens | 2 bad arguments
            3 provider_failed, protected_file_changed | 130 interrupted
records:    DIR/.subgoal/<run-id>/   (this runs a coding agent as you: use a copy, read the diff)"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python3 -m subgoal", epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Call a coding agent in fresh sessions until --check exits 0 or a budget ends.")
    p.add_argument("--dir", required=True, help="the folder the agent works in (never $HOME or /)")
    goal = p.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal", metavar="TEXT", help="the goal, as text")
    goal.add_argument("--goal-file", type=Path, metavar="PATH", help="a file that holds the goal")
    p.add_argument("--check", required=True, metavar="CMD", help="shell command run in DIR; 0 = met")
    p.add_argument("--max-iterations", type=int, default=5, metavar="N", help="default 5")
    p.add_argument("--max-minutes", type=float, default=30, metavar="M", help="default 30")
    p.add_argument("--max-tokens", type=int, metavar="T", help="default: no limit")
    p.add_argument("--protect", action="append", default=[], metavar="PATH",
                   help="a file or folder, relative to DIR, that must not change (repeatable)")
    p.add_argument("--allow", action="append", default=[], metavar="RULE",
                   help="an extra Claude Code permission rule (repeatable)")
    p.add_argument("--model", metavar="NAME", help="default: the tool's own default")
    p.add_argument("--provider", choices=PROVIDERS, default="claude-code", help="default claude-code")
    p.add_argument("--dry-run", action="store_true",
                   help="print the provider command and the first prompt; no call, no files")
    args = p.parse_args(argv)
    if min(args.max_iterations, args.max_minutes, args.max_tokens or 1) <= 0:
        p.error("limits must be above 0: every run is bounded, and there is no 'unlimited'")
    try:
        args.dir = safe_workdir(args.dir)
        args.goal = args.goal_file.read_text() if args.goal_file else args.goal
    except (OSError, ValueError) as error:
        p.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    allow = [f"Bash({args.check})", *args.allow]        # the worker may run the check itself
    provider = PROVIDERS[args.provider](model=args.model, allow=allow)
    if args.dry_run:
        last = run_check(args.check, args.dir, CHECK_TIMEOUT_S)
        print(f"DRY RUN: no agent call, no run folder. Your check ran once as the pre-check: exit {last}")
        print(f"provider: {provider.describe()}\n\nfirst prompt:\n")
        print(build_prompt(args.goal, args.check, args.protect, ".subgoal/<run-id>/notes.md", "", last))
        return 0
    limits = Limits(args.max_iterations, args.max_minutes * 60, args.max_tokens)
    run = run_loop(args.goal, args.check, args.dir, provider, limits, args.protect)
    return run.stop_reason.exit_code
