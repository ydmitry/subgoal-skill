"""opencode 2 in non-interactive mode (opencode run --standalone), a new session per call.

Its events are opencode 1's, so opencode.parse reads them. What differs is how a call is set up.
"""
import json
import shlex
import shutil

from .opencode import parse
from .process import run_process
from .provider import ProviderError

AGENT = "subgoal"    # every call runs as this agent, which the call's own config defines


def rule(action, resource, effect):
    return {"action": action, "resource": resource, "effect": effect}


# opencode 2 lets every agent reach paths outside the folder by default. The worker may not. Its
# check covers file tools, `cd` and a command's folder, but not the paths a command names, so a
# command may not name an absolute path, the parent folder or the home folder. (Not a sandbox:
# code that an allowed command runs, such as the tests, can still write anywhere.)
LEAVE = ("* /*", "*>/*", "*=/*", "*'/*", '*"/*', "*../*", "* ~*")
WORKER_RULES = [rule("external_directory", "*", "deny"), *(rule("shell", path, "deny") for path in LEAVE)]
# What a read-only session may use: look at files, and look at git. The last matching rule wins,
# so the denials for a redirect and for `git diff --output=FILE` (both write a file) come last.
READ_ONLY_RULES = [rule("*", "*", "deny"), *(rule(action, "*", "allow") for action in ("read", "glob", "grep")),
                   *(rule("shell", f"git {command} *", "allow") for command in ("status", "diff", "log", "show")),
                   rule("shell", "*>*", "deny"), rule("shell", "*--output*", "deny")]


class OpenCode2:
    """Every call runs the same command with the same config, so the permissions are fixed here, once."""

    def __init__(self, model=None, allow=(), executable="opencode2", read_only=False):
        # run --standalone         a private server that lives as long as this call. The shared
        #                          background service is never used: it would not see this config
        # env -u PWD               opencode takes its folder from $PWD before its cwd, which is DIR
        # OPENCODE_CONFIG_CONTENT  read by that server, over your own config: one agent with the
        #                          rules above, which come after every other config's rules
        # --agent                  an agent that is missing stops the call (a missing default would
        #                          fall back to another agent)
        # no project config        (read_only) no opencode config or plugins from DIR: plugin code
        #                          could write
        # allow                    Claude Code rules, unused, as for opencode 1
        self.read_only, self.executable = read_only, executable
        config = {"agents": {AGENT: {"mode": "primary",
                                     "permissions": READ_ONLY_RULES if read_only else WORKER_RULES}}}
        self.argv = ["env", "-u", "PWD", *(["OPENCODE_DISABLE_PROJECT_CONFIG=1"] if read_only else []),
                     "OPENCODE_CONFIG_CONTENT=" + json.dumps(config, separators=(",", ":")),
                     executable, "run", "--standalone", "--format", "json", "--agent", AGENT]
        if model:
            self.argv += ["--model", model]

    def describe(self):
        model = "" if "--model" in self.argv else ", your default model"
        only = ", read-only: edits are denied" if self.read_only else ""
        return f"{shlex.join(self.argv)}  (run in DIR, prompt on stdin{model}{only})"

    def run(self, prompt, workdir, timeout_s):
        if not shutil.which(self.executable):           # `env` would start, then not find it
            raise ProviderError(f"{self.executable} not found: is opencode 2 on PATH?")
        code, out, err, timed_out = run_process(self.argv, workdir, timeout_s, stdin=prompt)
        if timed_out:
            raise ProviderError(f"no reply within {timeout_s:.0f}s; the call was stopped")
        return parse(code, out, err)
