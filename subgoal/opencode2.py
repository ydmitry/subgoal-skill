"""opencode 2 in non-interactive mode (opencode run --standalone), a new session per call.

Its events are opencode 1's, so opencode.parse reads them. What differs is how a call is set up.
"""
import json
import re
import shlex
import shutil

from .opencode import parse
from .process import run_process
from .provider import ProviderError

AGENT = "subgoal"    # every call runs as this agent, which the call's own config defines
VERSION_TIMEOUT_S = 15


def rule(action, resource, effect):
    return {"action": action, "resource": resource, "effect": effect}


# opencode 2 lets every agent reach paths outside the folder by default. The worker may not. Its
# check covers file tools, `cd` and a command's folder, but not the paths a command names, so a
# command may not name an absolute path, the parent folder or the home folder. (Not a sandbox:
# code that an allowed command runs, such as the tests, can still write anywhere.)
LEAVE = ("* /*", "*>/*", "*=/*", "*'/*", '*"/*', "*../*", "* ~*")
WORKER_RULES = [rule("external_directory", "*", "deny"), *(rule("shell", path, "deny") for path in LEAVE)]
# What the judge (edit=False) may use: read files, and run shell commands under the worker's path
# rule. Everything else is denied, edit (which covers write and patch) included.
JUDGE_RULES = [rule("*", "*", "deny"),
               *(rule(action, "*", "allow") for action in ("read", "glob", "grep", "shell")), *WORKER_RULES]


class OpenCode2:
    """Every call runs the same command with the same config, so the permissions are fixed here, once."""

    def __init__(self, model=None, allow=(), executable=None, edit=True):
        # run --standalone         a private server that lives as long as this call. The shared
        #                          background service is never used: it would not see this config
        # env -u PWD               opencode takes its folder from $PWD before its cwd, which is DIR
        # OPENCODE_CONFIG_CONTENT  read by that server, over your own config: one agent with the
        #                          rules above, which come after every other config's rules
        # --agent                  an agent that is missing stops the call (a missing default would
        #                          fall back to another agent)
        # no project config        (edit=False) no opencode config or plugins from DIR: plugin code
        #                          would run unasked in the judge's session
        # allow                    Claude Code rules, unused, as for opencode 1
        # executable               default: opencode2 (the installer's name) if on PATH, else opencode
        executable = executable or ("opencode2" if shutil.which("opencode2") else "opencode")
        self.edit, self.executable, self.version = edit, executable, None
        config = {"agents": {AGENT: {"mode": "primary",
                                     "permissions": WORKER_RULES if edit else JUDGE_RULES}}}
        self.argv = ["env", "-u", "PWD", *([] if edit else ["OPENCODE_DISABLE_PROJECT_CONFIG=1"]),
                     "OPENCODE_CONFIG_CONTENT=" + json.dumps(config, separators=(",", ":")),
                     executable, "run", "--standalone", "--format", "json", "--agent", AGENT]
        if model:
            self.argv += ["--model", model]

    def describe(self):
        model = "" if "--model" in self.argv else ", your default model"
        only = ", no edit tools: it may run commands" if not self.edit else ""
        return f"{shlex.join(self.argv)}  (run in DIR, prompt on stdin{model}{only})"

    def run(self, prompt, workdir, timeout_s):
        if not shutil.which(self.executable):           # `env` would start, then not find it
            raise ProviderError(f"{self.executable} not found: is opencode 2 on PATH?")
        if self.version is None:                        # before any prompt is sent, once
            self.version = check_version(self.executable, workdir)
        code, out, err, timed_out = run_process(self.argv, workdir, timeout_s, stdin=prompt)
        if timed_out:
            raise ProviderError(f"no reply within {timeout_s:.0f}s; the call was stopped")
        return parse(code, out, err)


def check_version(executable, workdir):
    """The version the binary reports, if it is opencode 2. opencode 1 takes the same flags, but
    runs a session whose agent it cannot find as another agent, which can edit: a judge that writes."""
    code, out, err, timed_out = run_process([executable, "--version"], workdir, VERSION_TIMEOUT_S)
    found = re.fullmatch(r"(?:opencode )?v?(\d+)\.\d+\.\d+\S*", out.strip())
    if timed_out or code != 0 or not found:
        said = (out + err).strip()[-200:] or "no output"
        raise ProviderError(f"{executable} --version gave no opencode version (exit {code}): {said}")
    if found.group(1) != "2":
        raise ProviderError(f"{executable} is opencode {out.strip()}, not opencode 2: "
                            "for opencode 1.x use --provider opencode")
    return out.strip()
