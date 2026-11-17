"""Credential-free parser sidecar (T11).

Runs in its own container of the worker pod:

    /app/.venv/bin/python -m pointcloud_detect.worker.parser_sidecar

with PCD_PARSER_SPOOL pointing at the directory it shares with the worker, as a uid
of its own in the worker's group, with no AWS environment, no IRSA token and no
Kubernetes service-account token. It is the only process in the pod that runs the
native parsers (laspy/lazrs, pye57 with libE57 and Xerces-C, trimesh) over uploaded
files. It turns each input into the canonical format described in
pointcloud_detect.io_readers, which the worker reads with numpy alone.

The loop here is a supervisor. It claims a request by renaming request.json to
request.inprogress, runs the conversion in a child process with a scrubbed
environment and its own resource limits, and writes result.json itself: success
with a manifest, or one of a fixed set of error codes when the child fails, crashes
or overruns the request's timeout. A crafted file that kills the child therefore
fails its job rather than stalling it.

Every request gets a fresh process tree. Once the child exits, whatever its exit
code, the supervisor SIGKILLs every process descended from it before it writes the
result or claims the next request: in the chart it is the container's PID 1, and
elsewhere it makes itself the child subreaper, so a process the child forked, even
one that called setsid(), is still its descendant. The child's HOME and TMPDIR are
a directory of its own, removed with it. So a parser that a crafted file took over
holds on for its own job only, and cannot wait in the container for the next
user's upload. The supervisor also snapshots its own resource limits and priority
before serving: a child shares its uid, so the kernel would let one lower them
(prlimit(2)), and that would outlive the job. If they change, the supervisor exits
and the restart serves with the container's own limits.

If the container itself dies mid-parse (the kernel's OOM killer, say), the request
is left as request.inprogress. On start the sidecar turns every such leftover into a
"crashed" result before taking new work, so a file that crash-loops the parser
fails its job on the next restart instead of hanging until the worker's timeout.

The wire format is in pointcloud_detect.worker.parser_ipc. The worker trusts none
of what is written here; see validate_result there.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from pointcloud_detect.worker.json_logging import configure_json_logging
from pointcloud_detect.worker.parser_ipc import (
    CANONICAL_NAME,
    DONE_NAME,
    ERROR_MESSAGES,
    IN_PROGRESS_NAME,
    MAX_PARSER_TIMEOUT_SECONDS,
    PARSER_SPOOL_ENV,
    PROTOCOL_VERSION,
    REQUEST_NAME,
    RESULT_NAME,
    parser_spool,
    read_json_file,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

CHILD_RESULT_NAME = ".child-result.json"

# Environment that marks credentials the parser must never hold. Any of them present
# means the pod spec did not isolate this container, and parsing here would put
# untrusted input back next to a credential: refuse to start rather than run.
CREDENTIAL_ENV_VARS = (
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
)
CREDENTIAL_ENV_PREFIXES = (
    "AWS_CONTAINER_CREDENTIALS_",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
)

# Where the credentials would be mounted if the pod spec failed to exclude this
# container: the IRSA projected token, EKS Pod Identity's token, the Kubernetes
# service-account token, and a shared credentials file.
CREDENTIAL_FILES = (
    "/var/run/secrets/eks.amazonaws.com/serviceaccount/token",
    "/var/run/secrets/pods.eks.amazonaws.com/serviceaccount/eks-pod-identity-token",
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "~/.aws/credentials",
)

# GPU device nodes. The parser has no use for one, and each is a way into the NVIDIA
# kernel driver, and onto GPUs other pods were given, from untrusted-input code.
GPU_DEVICE_GLOB = "/dev/nvidia*"

# The AWS SDK has no business in this process. Blocking the import makes that a
# property of the process rather than of today's import graph.
BLOCKED_MODULES = ("boto3", "botocore", "s3transfer", "aiobotocore")

# Variables the conversion child inherits; everything else is dropped.
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "PCD_PARSER_MEMORY_LIMIT_BYTES",
)

# Optional address-space cap for the conversion child, in bytes. The container's
# memory limit is the main bound; this one also stops a child that maps far more
# than it touches.
MEMORY_LIMIT_ENV = "PCD_PARSER_MEMORY_LIMIT_BYTES"

# Points per chunk when streaming a LAS/LAZ/E57 into the canonical file.
CONVERT_CHUNK = 5_000_000

_POLL_SECONDS = 0.5
_CHILD_POLL_SECONDS = 0.2

# Each conversion child's HOME and TMPDIR, under the supervisor's own temp directory.
_CHILD_HOME_PREFIX = "pcd-parse-"

# Rounds of SIGKILL before the supervisor gives up on a stray that keeps forking and
# exits instead, which as PID 1 takes every process in the container with it.
_SWEEP_ROUNDS = 50

# Set by isolate_supervisor(): every orphan the children leave is this process's
# descendant, so sweeping descendants reaches them all. Unset (a test calling
# handle() in its own process) there is nothing safe to sweep.
_SWEEP_ENABLED = False

# linux/prctl.h
_PR_SET_DUMPABLE = 4
_PR_SET_CHILD_SUBREAPER = 36


def credential_problems(
    environ: Mapping[str, str], files: Sequence[str] = CREDENTIAL_FILES
) -> list[str]:
    """Why this process must not parse, or an empty list if it may."""
    problems = [name for name in CREDENTIAL_ENV_VARS if environ.get(name)]
    problems += sorted(
        name
        for name in environ
        if environ[name] and name.startswith(CREDENTIAL_ENV_PREFIXES)
    )
    problems += [p for p in files if os.path.lexists(os.path.expanduser(p))]
    return problems


def gpu_devices(pattern: str = GPU_DEVICE_GLOB) -> list[str]:
    """GPU device nodes visible to this process; there should be none."""
    import glob

    return sorted(glob.glob(pattern))


def block_aws_sdk() -> None:
    """Make any later import of the AWS SDK fail in this process."""
    for name in BLOCKED_MODULES:
        # A None entry in sys.modules makes `import name` raise ImportError.
        sys.modules[name] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Conversion: runs in the child process.
# ---------------------------------------------------------------------------


def convert(input_path: Path, output_path: Path, max_points: int | None) -> dict:
    """Convert one input file to the canonical format and return its manifest.

    The streamable formats are converted chunk by chunk when the job does not cap
    its points, so the parser's memory does not grow with the scene and the worker
    can stream the canonical file exactly as it would have streamed the original.
    Otherwise the same whole-load reader the in-process path uses is applied, with
    the job's max_points, so the points handed back are the ones the in-process
    path would have kept.
    """
    from pointcloud_detect.io_readers import (
        CANONICAL_FILE_FORMAT,
        CanonicalWriter,
        UnsupportedFormatError,
        check_las_point_bytes,
        detect_format,
        read_point_cloud,
    )

    fmt = detect_format(input_path)
    if fmt == "canonical":
        raise UnsupportedFormatError("the canonical format is not an accepted input")

    writer: CanonicalWriter | None = None
    try:
        if max_points is None and fmt in ("las", "laz", "e57"):
            from pointcloud_detect.streaming import iter_chunks

            if fmt in ("las", "laz"):
                import laspy

                with laspy.open(str(input_path)) as reader:
                    check_las_point_bytes(
                        reader.header, input_path.stat().st_size, input_path
                    )
            for xyz, rgb, intensity in iter_chunks(input_path, fmt, CONVERT_CHUNK):
                if writer is None:
                    writer = CanonicalWriter(
                        output_path, rgb is not None, intensity is not None
                    )
                writer.append(xyz, rgb, intensity)
            if writer is None:
                writer = CanonicalWriter(output_path, False, False)
        else:
            cloud = read_point_cloud(input_path, max_points=max_points)
            writer = CanonicalWriter(
                output_path, cloud.rgb is not None, cloud.intensity is not None
            )
            for start in range(0, cloud.num_points, CONVERT_CHUNK):
                end = start + CONVERT_CHUNK
                writer.append(
                    cloud.xyz[start:end],
                    None if cloud.rgb is None else cloud.rgb[start:end],
                    None if cloud.intensity is None else cloud.intensity[start:end],
                )
    except BaseException:
        if writer is not None:
            writer.close()
        output_path.unlink(missing_ok=True)
        raise
    writer.close()

    names = writer.dtype.names or ()
    return {
        "format": CANONICAL_FILE_FORMAT,
        "file": output_path.name,
        "point_count": writer.count,
        "has_rgb": "red" in names,
        "has_intensity": "intensity" in names,
        "source_format": fmt,
    }


def _error_code(exc: BaseException) -> str:
    from pointcloud_detect.io_readers import (
        FormatMismatchError,
        UnsupportedFormatError,
    )

    if isinstance(exc, FormatMismatchError):
        return "format_mismatch"
    if isinstance(exc, UnsupportedFormatError):
        return "unsupported_format"
    if isinstance(exc, MemoryError):
        return "too_large"
    if isinstance(exc, (ValueError, OSError, EOFError)):
        return "invalid_input"
    # laspy and pye57 raise their own exception types for malformed files.
    if type(exc).__name__ in ("LaspyException", "E57Exception"):
        return "invalid_input"
    return "parse_failed"


def _parse_request(request: object) -> tuple[str, int | None]:
    if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
        raise ValueError("unrecognised request")
    name = request.get("input")
    if (
        not isinstance(name, str)
        or "/" in name
        or "\\" in name
        or name in ("", ".", "..")
        or name.startswith(".")
    ):
        raise ValueError("request names no usable input file")
    max_points = request.get("max_points")
    if max_points is not None and (
        not isinstance(max_points, int) or isinstance(max_points, bool)
    ):
        raise ValueError("max_points must be an integer or null")
    if max_points is not None and max_points <= 0:
        max_points = None
    return name, max_points


def _limit_own_resources() -> None:
    """Resource limits for the conversion child, applied to itself before parsing."""
    try:
        import resource
    except ImportError:  # not POSIX
        return
    # A core dump of a parser holds whatever it had parsed.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    raw = os.environ.get(MEMORY_LIMIT_ENV, "").strip()
    if raw:
        limit = int(raw)
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError) as e:
            # macOS does not support RLIMIT_AS; Linux, where this runs, does.
            logger.warning("Could not apply %s=%s: %s", MEMORY_LIMIT_ENV, raw, e)


def convert_main(job_dir: Path) -> int:
    """Child entry point: convert the claimed request's input, report, exit 0.

    Exits 0 whenever it managed to write a child result, success or failure; any
    other exit means it died, which the supervisor reports as a crash.
    """
    _limit_own_resources()
    try:
        name, max_points = _parse_request(read_json_file(job_dir / IN_PROGRESS_NAME))
        manifest = convert(job_dir / name, job_dir / CANONICAL_NAME, max_points)
        outcome: dict = {"status": "ok", "manifest": manifest}
    except Exception as e:
        code = _error_code(e)
        # The detail goes to this container's log only, bounded and JSON-escaped.
        logger.warning(
            "Conversion in %s failed (%s): %s: %.500s",
            job_dir.name,
            code,
            type(e).__name__,
            e,
        )
        outcome = {"status": "error", "code": code}
    write_json_atomic(job_dir, CHILD_RESULT_NAME, outcome)
    return 0


# ---------------------------------------------------------------------------
# Supervision: the long-running process.
# ---------------------------------------------------------------------------


def _child_env(home: Path | None = None) -> dict[str, str]:
    env = {k: v for k in _CHILD_ENV_ALLOWLIST if (v := os.environ.get(k)) is not None}
    # Forced on here, whatever the parent had: this is the untrusted-input path.
    env["XERCES_DISABLE_DTD"] = "1"
    if home is not None:
        # Nothing one child leaves in its HOME or temp files reaches the next.
        env["HOME"] = env["TMPDIR"] = str(home)
    return env


def _prctl(option: int, value: int) -> bool:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(option, value, 0, 0, 0) == 0
    except (OSError, AttributeError):  # not Linux
        return False


def isolate_supervisor() -> bool:
    """Make every process the children leave behind reachable by the sweep.

    Also marks this process undumpable, so a child running as the same uid cannot
    ptrace it or write to its memory through /proc and take over the supervisor
    instead. Returns whether the sweep is on.
    """
    global _SWEEP_ENABLED
    _prctl(_PR_SET_DUMPABLE, 0)
    _SWEEP_ENABLED = os.getpid() == 1 or _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    if not _SWEEP_ENABLED:
        logger.warning(
            "Not PID 1 and cannot become a child subreaper: processes a conversion "
            "leaves behind will not be killed"
        )
    return _SWEEP_ENABLED


def _descendants(root: int) -> list[int]:
    """Live processes descended from ``root``, from /proc; empty where there is none."""
    parents: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue
        # The command name is in parentheses and may itself hold spaces or ")".
        fields = stat[stat.rfind(b")") + 2 :].split()
        if len(fields) < 2 or fields[0] == b"Z":
            continue
        parents[int(name)] = int(fields[1])
    found: list[int] = []
    frontier = {root}
    while frontier:
        frontier = {pid for pid, ppid in parents.items() if ppid in frontier}
        found += sorted(frontier)
    return found


def _reap_zombies() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def reap_strays() -> bool:
    """SIGKILL every process descended from this one; True once none is left.

    A no-op returning True unless isolate_supervisor() turned the sweep on.
    """
    if not _SWEEP_ENABLED:
        return True
    me = os.getpid()
    for _ in range(_SWEEP_ROUNDS):
        _reap_zombies()
        strays = _descendants(me)
        if not strays:
            return True
        for pid in strays:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)  # nosemgrep: arbitrary-sleep
    _reap_zombies()
    return not _descendants(me)


class StraysSurvivedError(RuntimeError):
    """Processes a conversion left behind outlived every round of SIGKILL."""


class SupervisorStateChangedError(RuntimeError):
    """A conversion changed this process's limits or priority (job name inside)."""


def _self_state() -> tuple | None:
    """This process's resource limits and priority, as one comparable value.

    The conversion child runs as this uid, and prlimit(2) lets one process of a
    uid lower another's limits, including the hard limits, which cannot be raised
    back. A child that did so would outlive its job: later conversions would fail
    against limits this supervisor never set. serve() compares this snapshot after
    every job and exits on a change; the restart starts over with the container's
    limits. The job whose child did it has its result already, which only that
    child could shape; the jobs after it run under limits this process set. CPU
    affinity is left out: the kubelet's CPU manager may change it while the pod runs.
    """
    try:
        import resource
    except ImportError:  # not POSIX
        return None
    limits = tuple(
        (name, resource.getrlimit(getattr(resource, name)))
        for name in sorted(dir(resource))
        if name.startswith("RLIMIT_")
    )
    return (*limits, ("priority", os.getpriority(os.PRIO_PROCESS, 0)))


# Named outright: run with -m, this module's __name__ is "__main__", which -m
# cannot find again in the child.
_MODULE = "pointcloud_detect.worker.parser_sidecar"


def _child_command(job_dir: Path) -> list[str]:
    # -P: python -m would otherwise put the working directory first on sys.path, so a
    # module planted there would shadow this one. The image's /app is root-owned; -P
    # makes the child not depend on that.
    return [sys.executable, "-P", "-m", _MODULE, "--convert", str(job_dir)]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        proc.kill()
    proc.wait()


def _run_child(job_dir: Path, timeout_seconds: float, home: Path) -> str:
    """Run one conversion child; return "ok" or an error code.

    A shutdown signal does not interrupt it: the worker finishes its current job on
    SIGTERM, so the parse it is waiting for should finish too, within the pod's
    termination grace period.
    """
    (job_dir / CHILD_RESULT_NAME).unlink(missing_ok=True)
    # The parser isolation itself (T11): untrusted files are parsed in this child,
    # which holds no credential. Reviewed for the subprocess audit rule: a fixed argv
    # of this interpreter and module, no shell, an allowlisted environment, and the
    # only variable part is the job directory this process found in its own spool.
    # Enforced, not only stated: .semgrep/invariants.yaml fails CI if this call changes
    # shape or job data reaches the argv, and tests/test_parser_isolation.py checks the
    # argv and that no credential of the supervisor's reaches the child.
    # nosemgrep: dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit  # noqa: E501
    proc = subprocess.Popen(  # nosec B603
        _child_command(job_dir),
        env=_child_env(home),
        stdin=subprocess.DEVNULL,
        # Library chatter (pye57 prints errors to stdout) joins the container log.
        stdout=2,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_seconds
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            _kill_group(proc)
            return "timeout"
        if not job_dir.is_dir():
            # The worker gave up on this job and removed its directory.
            _kill_group(proc)
            return "crashed"
        time.sleep(_CHILD_POLL_SECONDS)  # nosemgrep: arbitrary-sleep
    if proc.returncode != 0:
        logger.warning(
            "Conversion child for %s exited with %s", job_dir.name, proc.returncode
        )
        return "crashed"
    try:
        outcome = read_json_file(job_dir / CHILD_RESULT_NAME)
    except (OSError, ValueError):
        return "crashed"
    if not isinstance(outcome, dict):
        return "crashed"
    if outcome.get("status") == "ok" and isinstance(outcome.get("manifest"), dict):
        return "ok"
    code = outcome.get("code")
    return code if isinstance(code, str) and code in ERROR_MESSAGES else "parse_failed"


def _finish(job_dir: Path, result: dict) -> None:
    write_json_atomic(job_dir, RESULT_NAME, result)
    os.replace(job_dir / IN_PROGRESS_NAME, job_dir / DONE_NAME)


def _error_result(code: str) -> dict:
    return {"version": PROTOCOL_VERSION, "status": "error", "code": code}


def handle(job_dir: Path) -> None:
    """Process one claimed request and write its result."""
    try:
        request = read_json_file(job_dir / IN_PROGRESS_NAME)
        raw = request.get("timeout_seconds") if isinstance(request, dict) else None
        if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
            raise ValueError("request has no usable timeout")
        timeout_seconds = min(raw, MAX_PARSER_TIMEOUT_SECONDS)
    except (OSError, ValueError):
        _finish(job_dir, _error_result("parse_failed"))
        return

    home = Path(tempfile.mkdtemp(prefix=_CHILD_HOME_PREFIX))
    try:
        code = _run_child(job_dir, timeout_seconds, home)
        # Before the result is written, so nothing the child started is still
        # running when the worker reads what it produced.
        swept = reap_strays()
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if not swept:
        code = "crashed"
    outcome = read_json_file(job_dir / CHILD_RESULT_NAME) if code == "ok" else None
    if isinstance(outcome, dict):
        result = {
            "version": PROTOCOL_VERSION,
            "status": "ok",
            "manifest": outcome["manifest"],
        }
    else:
        (job_dir / CANONICAL_NAME).unlink(missing_ok=True)
        result = _error_result(code)
    (job_dir / CHILD_RESULT_NAME).unlink(missing_ok=True)
    _finish(job_dir, result)
    logger.info("Finished %s: %s", job_dir.name, code)
    if not swept:
        raise StraysSurvivedError(job_dir.name)


def _job_dirs(spool: Path) -> list[Path]:
    dirs = []
    for entry in sorted(spool.iterdir()):
        # Only real directories; a link here is not something the worker made.
        if entry.is_dir() and not entry.is_symlink():
            dirs.append(entry)
    return dirs


def recover(spool: Path) -> int:
    """Fail every request a previous run of this container left in progress."""
    recovered = 0
    for job_dir in _job_dirs(spool):
        if not (job_dir / IN_PROGRESS_NAME).exists():
            continue
        try:
            if os.path.lexists(job_dir / RESULT_NAME):
                os.replace(job_dir / IN_PROGRESS_NAME, job_dir / DONE_NAME)
            else:
                (job_dir / CANONICAL_NAME).unlink(missing_ok=True)
                _finish(job_dir, _error_result("crashed"))
            recovered += 1
            logger.warning("Marked %s crashed: in progress at startup", job_dir.name)
        except OSError as e:
            logger.warning("Could not recover %s: %s", job_dir.name, e)
    return recovered


def claim_next(spool: Path) -> Path | None:
    """Claim the oldest pending request by renaming it; None if there is none."""
    for job_dir in _job_dirs(spool):
        try:
            os.rename(job_dir / REQUEST_NAME, job_dir / IN_PROGRESS_NAME)
        except OSError:
            # No request here, or the directory went away underneath us.
            continue
        return job_dir
    return None


def _remove_child_homes() -> None:
    """Remove the HOME directories of children a previous run of this container ran."""
    base = Path(tempfile.gettempdir())
    for entry in base.glob(f"{_CHILD_HOME_PREFIX}*"):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)


def serve(spool: Path, stop: threading.Event) -> None:
    """Claim and handle requests until ``stop`` is set.

    Raises StraysSurvivedError if a conversion left processes it could not kill,
    and SupervisorStateChangedError if one changed this process's limits or
    priority, which prlimit(2) allows across processes of one uid.
    """
    _remove_child_homes()
    recover(spool)
    baseline = _self_state()
    logger.info("Parser sidecar serving %s", spool)
    while not stop.is_set():
        job_dir = claim_next(spool)
        if job_dir is None:
            stop.wait(_POLL_SECONDS)
            continue
        try:
            handle(job_dir)
        except OSError as e:
            # Typically the worker removed the directory after giving up on it.
            logger.warning("Could not complete %s: %s", job_dir.name, e)
            if not reap_strays():
                raise StraysSurvivedError(job_dir.name) from e
        if _self_state() != baseline:
            raise SupervisorStateChangedError(job_dir.name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Credential-free parser sidecar (T11)."
    )
    parser.add_argument("--convert", metavar="JOB_DIR", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    configure_json_logging()
    block_aws_sdk()
    problems = credential_problems(os.environ)
    if problems:
        # Logs the names of the variables and files found, never their values, which
        # tests/test_parser_isolation.py asserts for every credential variable.
        # Reviewed for the credential-disclosure rule, which matches the word
        # "credentials" in the message; .semgrep/invariants.yaml fails CI if this
        # record logs more than the names found, or a log line elsewhere names one.
        # nosemgrep: python-logger-credential-disclosure, python.lang.security.audit.logging.python-logger-credential-disclosure, python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure  # noqa: E501
        logger.error(
            "Refusing to start: credentials are visible to the parser (%s). The pod "
            "spec must keep AWS credentials and service-account tokens out of this "
            "container.",
            ", ".join(problems),
        )
        return 2
    devices = gpu_devices()
    if devices:
        logger.error(
            "Refusing to start: GPU devices are visible to the parser (%s). Set "
            "NVIDIA_VISIBLE_DEVICES=void on this container.",
            ", ".join(devices),
        )
        return 2

    if args.convert:
        return convert_main(Path(args.convert))

    try:
        spool = parser_spool()
    except RuntimeError:
        logger.error("%s must name a directory whose parent exists", PARSER_SPOOL_ENV)
        return 2

    # Files this process creates are for the worker, which reads them by group.
    os.umask(0o007)
    isolate_supervisor()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        serve(spool, stop)
    except StraysSurvivedError as e:
        # Exiting ends the container, and as its PID 1 this takes the strays with
        # it. The job has its result already, or recover() fails it on restart.
        logger.error("Processes left by the conversion for %s survived SIGKILL", e)
        return 3
    except SupervisorStateChangedError as e:
        # A child shares this uid, so it could lower this process's limits or
        # priority, and a lowered hard limit cannot be raised back. Exiting ends
        # the container, and the restart serves with the container's own limits,
        # so no later job runs under the changed ones.
        logger.error(
            "The conversion for %s changed this process's resource limits or "
            "priority; exiting so the restart starts clean",
            e,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
