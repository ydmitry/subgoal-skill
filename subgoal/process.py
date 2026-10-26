"""Run a command with a time limit, and stop everything it started when time runs out."""
import os
import signal
import subprocess

GRACE_S = 5   # seconds between SIGTERM (please stop) and SIGKILL (stop now)


def run_process(cmd, cwd, timeout_s, stdin=None, shell=False):
    """Return (exit_code, stdout, stderr, timed_out). The child's whole process group is stopped on timeout
    (timed_out=True) and on Ctrl+C (re-raised)."""
    child = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, start_new_session=True, text=True,
        encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE)
    try:
        out, err = child.communicate(stdin, timeout=max(timeout_s, 0.01))
        return child.returncode, out, err, False
    except subprocess.TimeoutExpired:
        out, err = _stop(child)
        return child.returncode, out, err, True
    except KeyboardInterrupt:
        _stop(child)
        raise


def _stop(child):
    """SIGTERM the group, wait up to GRACE_S, then SIGKILL whatever is left of it."""
    _signal_group(child, signal.SIGTERM)
    try:
        out, err = child.communicate(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        out = err = None
    _signal_group(child, signal.SIGKILL)
    return (out, err) if out is not None else child.communicate()


def _signal_group(child, sig):
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass    # the group is already gone
