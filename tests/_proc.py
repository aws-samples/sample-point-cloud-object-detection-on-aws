"""Starting a process from the tests: the one place that does it.

A few tests need a separate OS process for what they prove: the parser sidecar run as
its own entrypoint with a scrubbed environment (threat model T11), a supervisor that
becomes a subreaper, a fresh interpreter that has never imported the AWS SDK, an E57
parse that may block on a FIFO while holding the GIL and so must be killed, the shipped
bash scripts, helm, semgrep running the repository's own rules, and git building a
throwaway repository for the export's leak check. Each goes through start() or run()
here, which allow those cases and nothing more:

- the program is chosen by name, from five: this interpreter, /bin/bash (the
  interpreter the scripts' shebang names), or the helm, semgrep or git on PATH;
- the environment is passed explicitly, never inherited, so a developer's AWS
  credentials reach a child only if a caller copies them in (minimal_env() does not);
- run() needs a timeout;
- there is no shell, and no other Popen option can be passed through.

Standard library only: the CI helm and invariants jobs run their tests with just
pytest and PyYAML, or pytest and semgrep.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

PROGRAMS = ("python", "bash", "helm", "semgrep", "git")


@dataclass(frozen=True)
class Finished:
    """What run() returns: a finished child's exit status and output."""

    returncode: int
    stdout: str
    stderr: str


def _executable(program: str) -> str:
    if program == "python":
        return sys.executable
    if program == "bash":
        return "/bin/bash"
    if program in ("helm", "semgrep", "git"):
        path = shutil.which(program)
        if path is None:
            raise FileNotFoundError(f"{program} is not on PATH")
        return path
    raise ValueError(f"the tests start only {', '.join(PROGRAMS)}, not {program!r}")


def minimal_env(**extra: str) -> dict[str, str]:
    """PATH, HOME, LANG and TMPDIR from the runner, and what the caller adds."""
    keep = ("PATH", "HOME", "LANG", "TMPDIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(extra)
    return env


def start(
    program: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.Popen[str]:
    argv = [_executable(program), *(str(a) for a in args)]
    # The tests' only process start, reviewed for the subprocess audit rule: argv[0]
    # is one of the programs above, the rest is what the calling test built from its
    # own constants and temporary paths, there is no shell, and the environment is the
    # one the test passed. Enforced, not only stated: .semgrep/invariants.yaml fails CI
    # if this call changes shape or another process start appears, and
    # tests/test_proc_helper.py tests each constraint.
    # nosemgrep: dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit  # noqa: E501
    return subprocess.Popen(  # nosec B603
        argv, env=dict(env), stdin=stdin, stdout=stdout, stderr=stderr, text=True
    )


def run(
    program: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    input: str | None = None,
    check: bool = False,
) -> Finished:
    """Run to completion, capturing output; raises TimeoutExpired after killing it."""
    with start(
        program,
        args,
        env=env,
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, out, err)
    return Finished(returncode=proc.returncode, stdout=out, stderr=err)
