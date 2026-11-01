"""The command line: read the arguments, build the provider, run the loop, exit with its code."""
import argparse
import signal
from pathlib import Path

from .claude_code import ClaudeCode
from .judge import judge_prompt
from .loop import CHECK_TIMEOUT_S, Limits, build_prompt, run_check, run_loop, safe_workdir
from .opencode import OpenCode
from .opencode2 import OpenCode2

PROVIDERS = {"claude-code": ClaudeCode, "opencode": OpenCode, "opencode2": OpenCode2}   # one entry each

EPILOG = """exit codes: 0 goal_met, unchecked | 1 out_of_iterations, out_of_time, out_of_tokens, stuck
            2 bad arguments, check_already_passes | 3 provider_failed, protected_file_changed | 130 interrupted
records:    DIR/.subgoal/<run-id>/
safety:     the worker edits DIR and runs commands there as you, unsandboxed; the planner and the judge have no
            edit tools but run commands too (README, "Safety"). Use a clean git tree or a branch, and read the diff."""


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m subgoal", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Call a coding agent in fresh sessions until --check exits 0 (or --judge says met) or a budget ends.")
    p.add_argument("--dir", required=True, help="the folder the agent works in (never $HOME or /)")
    goal = p.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal", metavar="TEXT", help="the goal, as text")
    goal.add_argument("--goal-file", type=Path, metavar="PATH", help="a file that holds the goal")
    p.add_argument("--check", metavar="CMD", help="shell command run in DIR; 0 = met. Without it (and without --judge) "
                   "one planned step runs and nothing verifies it")
    p.add_argument("--judge", action="store_true", help="no --check: a judge (an agent with no edit tools that may run "
                   "commands in DIR) decides after each step; its 'met' is an opinion")
    p.add_argument("--max-iterations", type=int, default=4, metavar="N", help="default 4")
    p.add_argument("--max-minutes", type=float, default=30, metavar="M", help="default 30")
    p.add_argument("--max-tokens", type=int, metavar="T", help="default: no limit")
    p.add_argument("--protect", action="append", default=[], metavar="PATH", help="a path in DIR that must not change (repeatable)")
    p.add_argument("--allow", action="append", default=[], metavar="RULE", help="an extra Claude Code permission rule (repeatable)")
    p.add_argument("--model", metavar="NAME", help="default: the tool's own default (opencode: provider/model)")
    p.add_argument("--judge-model", metavar="NAME", help="model for the judge and the planner; default: --model")
    p.add_argument("--provider", choices=PROVIDERS, default="claude-code", help="default claude-code")
    p.add_argument("--no-plan", action="store_true", help="no read-only planner: no plan before step 1 (no --check), "
                   "no re-plan when the check fails the same way twice (--check)")
    p.add_argument("--dry-run", action="store_true", help="print the provider commands and the prompts; no call, no run folder")
    args = p.parse_args(argv)
    if min(args.max_iterations, args.max_minutes, args.max_tokens or 1) <= 0:
        p.error("limits must be above 0: every run is bounded, and there is no 'unlimited'")
    if args.judge and args.check is not None:
        p.error("--judge is for runs with no --check")
    if args.allow and args.provider != "claude-code":
        p.error("--allow takes Claude Code rules; the opencode worker may already run any command in DIR")
    try:
        args.dir = safe_workdir(args.dir)
        args.goal = args.goal_file.read_text() if args.goal_file else args.goal
    except (OSError, ValueError) as error:
        p.error(str(error))
    return args


def main(argv=None):
    signal.signal(signal.SIGTERM, signal.default_int_handler)      # kill or a supervisor's stop ends the run as Ctrl+C does
    args = parse_args(argv)
    allow = [f"Bash({args.check})"] if args.check is not None else ["Bash", "Read"]     # no check: any command
    provider = PROVIDERS[args.provider](model=args.model, allow=allow + args.allow)
    readonly = PROVIDERS[args.provider](model=args.judge_model or args.model, edit=False)   # the judge, the planner
    judge = readonly if args.judge else None
    if args.dry_run:
        last = None if args.check is None else run_check(args.check, args.dir, CHECK_TIMEOUT_S)
        warn = ". It already passes, so a run would stop before any step (check_already_passes): make it fail first." \
            if last and last.exit_code == 0 else ""
        print("DRY RUN: no agent call, no run folder." + (f" Your check ran once as the pre-check: exit {last}{warn}" if last else ""))
        print(f"provider: {provider.describe()}\n\nfirst prompt:\n")
        print(build_prompt(args.goal, args.check, args.protect, ".subgoal/<run-id>/notes.md", "", last, judged=bool(judge)))
        when = " before step 1" if last is None else ", only to re-plan before step 3 or later, after the same failure twice"
        print("\nplan: none (--no-plan)" if args.no_plan else f"\nplan: a read-only session{when}\nplanner: {readonly.describe()}")
        if judge:
            print(f"\njudge: {judge.describe()}\n\njudge prompt, after each step:\n")
            print(judge_prompt(args.goal, args.dir, "<the worker's last words go here>", args.dir / ".subgoal" / "<run-id>"))
        return 0
    limits = Limits(args.max_iterations if args.check is not None or judge else 1, args.max_minutes * 60, args.max_tokens)
    return run_loop(args.goal, args.check, args.dir, provider, limits, args.protect, judge=judge,
                    plan=None if args.no_plan else readonly).stop_reason.exit_code
