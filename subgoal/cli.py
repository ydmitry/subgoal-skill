"""The command line: read the arguments, build the provider, run the loop, exit with its code."""
import argparse
import os
import shlex
import signal
import sys
from pathlib import Path

from .claude_code import ClaudeCode
from .judge import judge_prompt
from .loop import CHECK_TIMEOUT_S, Limits, build_prompt, records_home, run_check, run_loop, safe_workdir
from .opencode import OpenCode
from .opencode2 import OpenCode2

PROVIDERS = {"claude-code": ClaudeCode, "opencode": OpenCode, "opencode2": OpenCode2}   # one entry each

EPILOG = """exit codes: 0 goal_met, unchecked | 1 out_of_iterations, out_of_time, out_of_tokens
            2 bad arguments, check_already_passes | 3 provider_failed, protected_file_changed | 130 interrupted
records:    ~/.local/share/subgoal/<DIR's path, with - for />/<run-id>/   ($XDG_DATA_HOME, if set)
defaults:   ~/.config/subgoal/flags, flags as typed here, # for a comment; what you type comes after it and wins
safety:     the worker edits DIR and runs commands there as you, unsandboxed; the planner and the judge have no
            edit tools but run commands too (README, "Safety"). Use a clean git tree or a branch, and read the diff."""


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m subgoal", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Call a coding agent, a fresh session per step, until --check exits 0, --judge says met, or both, or a budget ends.")
    p.add_argument("--dir", required=True, help="the folder the agent works in (never $HOME or /)")
    goal = p.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal", metavar="TEXT", help="the goal, as text")
    goal.add_argument("--goal-file", type=Path, metavar="PATH", help="a file that holds the goal")
    p.add_argument("--check", metavar="CMD", help="shell command run in DIR; 0 = met. Without it (and without --judge) "
                   "one planned step runs and nothing verifies it")
    p.add_argument("--judge", action="store_true", help="a judge (an agent with no edit tools that may run commands in DIR) "
                   "decides after each step, with --check only once it exits 0; its 'met' is an opinion")
    p.add_argument("--max-iterations", type=int, default=4, metavar="N", help="default 4")
    p.add_argument("--max-minutes", type=float, default=30, metavar="M", help="default 30")
    p.add_argument("--max-tokens", type=int, metavar="T", help="default: no limit")
    p.add_argument("--protect", action="append", default=[], metavar="PATH", help="a path in DIR that must not change (repeatable)")
    p.add_argument("--allow", action="append", default=[], metavar="RULE", help="an extra Claude Code permission rule (repeatable)")
    p.add_argument("--model", metavar="NAME", help="default: the tool's own default (opencode: provider/model)")
    p.add_argument("--judge-model", metavar="NAME", help="model for the judge and the planner; default: --model, on the same tool")
    p.add_argument("--provider", choices=PROVIDERS, default="claude-code", help="the worker's tool; default claude-code")
    p.add_argument("--judge-provider", choices=PROVIDERS, help="the judge's and the planner's tool; default: --provider")
    p.add_argument("--no-plan", action="store_true", help="no read-only plan before step 1 (a run with --check never has one)")
    p.add_argument("--same-session", action="store_true", help="later steps continue the worker's chat with what the check or the judge said")
    p.add_argument("--dry-run", action="store_true", help="print the provider commands and the prompts; no call, no run folder")
    file = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config", "subgoal", "flags")
    found = shlex.split(file.read_text(), comments=True) if file.is_file() else []
    if {"--dir", "--goal", "--goal-file"} & {flag.split("=")[0] for flag in found}:
        p.error(f"{file}: a default --dir or goal makes no sense; type it")
    args = p.parse_args(found + (sys.argv[1:] if argv is None else list(argv)))
    args.defaults = f"defaults from {file}: {shlex.join(found)}\n" * bool(found)
    if min(args.max_iterations, args.max_minutes, args.max_tokens or 1) <= 0:
        p.error("limits must be above 0: every run is bounded, and there is no 'unlimited'")
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
    records = records_home(args.dir)
    provider = PROVIDERS[args.provider](model=args.model, allow=allow + args.allow, records=records)
    tool = args.judge_provider or args.provider                     # the judge's and the planner's; a model name is one tool's
    readonly = PROVIDERS[tool](model=args.judge_model or (args.model if tool == args.provider else None), edit=False, records=records)
    judge = readonly if args.judge else None
    if args.dry_run:
        last = None if args.check is None else run_check(args.check, args.dir, CHECK_TIMEOUT_S)
        warn = ". It already passes, so a run would stop before any step (check_already_passes): make it fail first." \
            if last and last.exit_code == 0 and not judge else ""
        print(args.defaults + "DRY RUN: no agent call, no run folder." + (f" Your check ran once as the pre-check: exit {last}{warn}" if last else ""))
        print(f"provider: {provider.describe()}\n\nfirst prompt:\n")
        print(build_prompt(args.goal, args.check, args.protect, records / "<run-id>" / "notes.md", "", last, judged=bool(judge)))
        print("\nplan: none (--no-plan)" if args.no_plan else "\nplan: none (a run with a check has no planner)" if last else
              f"\nplan: a read-only session before step 1\nplanner: {readonly.describe()}")
        print("later steps: the worker's own chat is continued (--same-session)" * args.same_session)
        if judge:
            print(f"\njudge: {judge.describe()}\n\njudge prompt, after each step{' whose check exits 0' * bool(last)}:\n")
            print(judge_prompt(args.goal, args.dir, "<the worker's last words go here>", records / "<run-id>"))
        return 0
    limits = Limits(args.max_iterations if args.check is not None or judge else 1, args.max_minutes * 60, args.max_tokens)
    return run_loop(args.goal, args.check, args.dir, provider, limits, args.protect, judge=judge,
                    plan=None if args.no_plan else readonly, same_session=args.same_session).stop_reason.exit_code
