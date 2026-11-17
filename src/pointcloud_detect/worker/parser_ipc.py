"""The worker's side of the parser sidecar, and the wire format both sides share.

T11: the worker holds IRSA credentials, and used to run laspy/lazrs, pye57 (libE57 and
Xerces-C), trimesh and numpy.fromfile over uploaded files in the same process. In
sidecar mode those parsers run in a second container of the worker pod that has no
AWS credentials, no Kubernetes token and a different uid
(pointcloud_detect.worker.parser_sidecar). The two containers share one directory, the
spool, and nothing else:

    <spool>/<job_id>.<random>/          created by the worker, mode 0770
        <input file name>               the untrusted download (worker writes)
        request.json                    written atomically by the worker
        request.inprogress              the request, renamed by the parser to claim it
        points.npy                      the canonical point file (parser writes)
        result.json                     written atomically by the parser
        request.done                    the request, once its result is written

The canonical file is described in pointcloud_detect.io_readers. Everything the parser
writes is untrusted here: a parser that a crafted file has taken over can write any
bytes, any file type and any link into the job directory. So the worker reads
result.json without following links and with a size cap, accepts only a fixed file
name, validates the canonical header before use and at every later open, and turns
every parser failure into one of a few fixed messages rather than repeating the
parser's own text into the job record.

This module must not import boto3; the sidecar imports it too.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path

from pointcloud_detect.io_readers import (
    CANONICAL_FILE_FORMAT,
    FORMAT_BY_EXTENSION,
    MAX_CANONICAL_POINTS,
    canonical_header,
)

PROTOCOL_VERSION = 1

REQUEST_NAME = "request.json"
IN_PROGRESS_NAME = "request.inprogress"
DONE_NAME = "request.done"
RESULT_NAME = "result.json"
CANONICAL_NAME = "points.npy"

# result.json and request.json are a few hundred bytes. The cap keeps a hostile
# parser from making the worker read, or json-parse, anything large.
MAX_JSON_BYTES = 64 * 1024

# Formats the parser may report converting from. The canonical format itself is
# never accepted as an input.
SOURCE_FORMATS = frozenset(FORMAT_BY_EXTENSION.values()) - {"canonical"}

PARSER_MODE_ENV = "PCD_PARSER_MODE"
PARSER_SPOOL_ENV = "PCD_PARSER_SPOOL"
PARSER_TIMEOUT_ENV = "PCD_PARSER_TIMEOUT_SECONDS"
PARSER_MODES = ("in-process", "sidecar")

# SQS stops honouring visibility extensions 12 hours after a message's first
# receive, after which the job can be handed to a second worker. The parse has to
# finish well inside that and leave time for inference, so the timeout is capped an
# hour short of it; the default leaves most of the window for inference.
SQS_VISIBILITY_CAP_SECONDS = 12 * 3600
MAX_PARSER_TIMEOUT_SECONDS = SQS_VISIBILITY_CAP_SECONDS - 3600
DEFAULT_PARSER_TIMEOUT_SECONDS = 2 * 3600

# How long past the request's timeout the worker keeps waiting, so the parser's own
# "timeout" result normally arrives before the worker gives up on its own.
_TIMEOUT_GRACE_SECONDS = 30

# The messages a job record can carry for a parser failure. The parser only picks
# the code; the text is fixed here, so a compromised parser cannot write its own
# words into the record the dashboard shows.
ERROR_MESSAGES = {
    "unsupported_format": "The input file's format is not supported.",
    "format_mismatch": "The input file's content does not match its extension.",
    "invalid_input": "The input file is malformed or truncated and could not be read.",
    "too_large": "The input file is larger than the parser's limits allow.",
    "timeout": "Reading the input file took longer than the parser's time limit.",
    "crashed": "The parser stopped unexpectedly while reading the input file.",
    "parse_failed": "The input file could not be read.",
    "invalid_result": "The parser returned a result that failed validation.",
}

_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ParserError(RuntimeError):
    """A parse that did not produce a usable canonical file, with a safe message."""

    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_MESSAGES else "parse_failed"
        super().__init__(f"{ERROR_MESSAGES[self.code]} ({self.code})")


def parser_mode() -> str:
    """The configured parser mode; in-process unless PCD_PARSER_MODE says otherwise.

    An unrecognised value is an error rather than a fallback, for the same reason
    as JOB_STORE_BACKEND: a typo must not quietly put the native parsers back into
    the credentialed process.
    """
    raw = os.environ.get(PARSER_MODE_ENV, "").strip().lower()
    mode = raw or "in-process"
    if mode not in PARSER_MODES:
        raise RuntimeError(
            f"{PARSER_MODE_ENV}={raw!r} is not a recognised parser mode. "
            f"Valid values: {', '.join(PARSER_MODES)}."
        )
    return mode


def parser_spool() -> Path:
    """The spool directory for sidecar mode, created if its parent exists.

    The chart points both containers at a subdirectory of the shared scratch
    volume. The parser mounts only that subdirectory, so the kubelet creates it,
    with the mode of the volume's root; where nothing has, whichever container
    starts first creates it here, group-writable explicitly rather than through
    the umask, so the other container (same group, different uid) can use it. A
    symlink is refused.
    """
    raw = os.environ.get(PARSER_SPOOL_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"{PARSER_MODE_ENV}=sidecar requires {PARSER_SPOOL_ENV}")
    spool = Path(raw)
    if not spool.parent.is_dir():
        raise RuntimeError(f"{PARSER_SPOOL_ENV}={raw!r} is not an existing directory")
    try:
        spool.mkdir()
    except FileExistsError:
        pass
    else:
        spool.chmod(0o770)
    if spool.is_symlink() or not spool.is_dir():
        raise RuntimeError(f"{PARSER_SPOOL_ENV}={raw!r} is not an existing directory")
    return spool


def parser_timeout_seconds() -> int:
    """Seconds the worker waits for a parse, bounded below the SQS visibility cap."""
    raw = os.environ.get(PARSER_TIMEOUT_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_PARSER_TIMEOUT_SECONDS
    except ValueError as e:
        raise RuntimeError(f"{PARSER_TIMEOUT_ENV}={raw!r} is not an integer") from e
    if value <= 0:
        raise RuntimeError(f"{PARSER_TIMEOUT_ENV} must be positive, got {value}")
    return min(value, MAX_PARSER_TIMEOUT_SECONDS)


def write_json_atomic(directory: Path, name: str, data: dict) -> None:
    """Write ``directory/name`` so a reader sees either nothing or the whole file.

    The temporary name is exclusive and never followed as a link, and the rename is
    atomic within a directory. Mode 0640: the other container reads it by group.
    """
    tmp = directory / f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, directory / name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_json_file(path: Path, max_bytes: int = MAX_JSON_BYTES) -> object:
    """Read a small JSON file without following links, blocking or reading much.

    Raises ValueError for anything but a regular file of at most ``max_bytes``
    holding valid JSON.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise FileNotFoundError(str(path)) from e
        raise ValueError(f"cannot open {path.name}: {e.strerror}") from e
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{path.name} is larger than {max_bytes} bytes")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"{path.name} is not valid JSON") from e


def _plain_int(value: object) -> bool:
    # bool is an int subclass; a JSON true must not pass as a count of one.
    return isinstance(value, int) and not isinstance(value, bool)


def validate_result(
    job_dir: Path, expected_source_format: str, max_points: int | None = None
) -> Path:
    """Check the parser's result and return the canonical file it describes.

    Raises ParserError, with the parser's code for a reported failure and
    "invalid_result" for anything that does not validate. Every check is on the
    worker's side of the boundary: the parser's word is taken for nothing that
    could make the worker read outside the job directory or allocate beyond caps.
    """
    try:
        result = read_json_file(job_dir / RESULT_NAME)
    except (OSError, ValueError) as e:
        raise ParserError("invalid_result") from e
    if not isinstance(result, dict) or result.get("version") != PROTOCOL_VERSION:
        raise ParserError("invalid_result")

    status = result.get("status")
    if status == "error":
        code = result.get("code")
        raise ParserError(code if isinstance(code, str) else "parse_failed")
    if status != "ok":
        raise ParserError("invalid_result")

    manifest = result.get("manifest")
    if not isinstance(manifest, dict) or set(manifest) != {
        "format",
        "file",
        "point_count",
        "has_rgb",
        "has_intensity",
        "source_format",
    }:
        raise ParserError("invalid_result")
    count = manifest["point_count"]
    has_rgb = manifest["has_rgb"]
    has_intensity = manifest["has_intensity"]
    if (
        manifest["format"] != CANONICAL_FILE_FORMAT
        # One fixed name, so the manifest cannot steer the worker to another path.
        or manifest["file"] != CANONICAL_NAME
        or manifest["source_format"] != expected_source_format
        or not _plain_int(count)
        or not 0 <= count <= MAX_CANONICAL_POINTS
        # The parser applies the job's max_points, so more is not an honest result.
        or (max_points is not None and max_points > 0 and count > max_points)
        or not isinstance(has_rgb, bool)
        or not isinstance(has_intensity, bool)
    ):
        raise ParserError("invalid_result")

    path = job_dir / CANONICAL_NAME
    # The name is fixed, but the directory could have been swapped for a link.
    if os.path.realpath(path.parent) != os.path.realpath(job_dir) or path.is_symlink():
        raise ParserError("invalid_result")
    try:
        header = canonical_header(path)
    except (OSError, ValueError) as e:
        raise ParserError("invalid_result") from e
    if (
        header.count != count
        or header.has_rgb != has_rgb
        or header.has_intensity != has_intensity
    ):
        raise ParserError("invalid_result")
    return path


def _chmod_regular_file(directory: Path, name: str, mode: int) -> None:
    """chmod ``directory/name`` without following a link at either level.

    The job directory is writable by the parser's uid, so the name could by now be a
    link a parser planted, and os.chmod on the path would change whatever file in
    this container the link names. Anything but a regular file fails the job.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    except OSError as e:
        raise ParserError("parse_failed") from e
    try:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=dir_fd,
            )
        except OSError as e:
            raise ParserError("parse_failed") from e
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ParserError("parse_failed")
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


class SidecarJob:
    """One job's directory in the spool, from download to canonical file.

    Used as a context manager; the directory and everything in it are removed on
    exit, whether the parse succeeded, failed or timed out.
    """

    def __init__(self, spool: Path, job_id: str, filename: str) -> None:
        if not _FILE_NAME.fullmatch(job_id):
            raise ValueError("job_id is not usable as a directory name")
        # validate_input_key has already confined the name to one segment with an
        # allowed extension; this re-checks the parts that matter for a local path.
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise ValueError("input filename must be a single path segment")
        suffix = Path(filename).suffix.lower()
        source_format = FORMAT_BY_EXTENSION.get(suffix)
        if source_format not in SOURCE_FORMATS:
            raise ValueError(f"unsupported input extension {suffix!r}")
        self.source_format: str = source_format
        self.dir = Path(tempfile.mkdtemp(prefix=f"{job_id}.", dir=spool))
        # The parser container shares the worker's group and nothing else, and has
        # to create its outputs here. Group, not other: 0770 is the narrowest mode
        # that lets a second uid write, and the spool itself is a pod-local volume.
        # nosemgrep: insecure-file-permissions, python.lang.security.audit.insecure-file-permissions, python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
        os.chmod(self.dir, 0o770)  # nosec B103
        # The upload's own name is kept, as the in-process path keeps it: the .bin
        # reader tells nuScenes from KITTI by name. It cannot collide with a
        # protocol file, because it must end in one of the input extensions.
        self.input_path = self.dir / filename

    def __enter__(self) -> SidecarJob:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        # rmtree does not follow links, so whatever the parser left here, only this
        # directory's contents are removed.
        shutil.rmtree(self.dir, ignore_errors=True)

    def parse(
        self,
        max_points: int | None,
        timeout_seconds: int,
        poll_seconds: float = 0.25,
    ) -> Path:
        """Ask the sidecar to convert the downloaded input and wait for it.

        Returns the validated canonical file. Raises ParserError on a reported
        failure, an invalid result, or no result within the timeout.
        """
        # Readable by the parser's group, which is the worker's.
        _chmod_regular_file(self.dir, self.input_path.name, 0o640)
        write_json_atomic(
            self.dir,
            REQUEST_NAME,
            {
                "version": PROTOCOL_VERSION,
                "input": self.input_path.name,
                "max_points": max_points,
                "timeout_seconds": timeout_seconds,
            },
        )
        deadline = time.monotonic() + timeout_seconds + _TIMEOUT_GRACE_SECONDS
        result = self.dir / RESULT_NAME
        while not os.path.lexists(result):
            if time.monotonic() >= deadline:
                raise ParserError("timeout")
            time.sleep(poll_seconds)  # nosemgrep: arbitrary-sleep

        path = validate_result(self.dir, self.source_format, max_points)
        # The canonical file is all the worker reads from here on. Dropping the
        # input now frees its scratch space before the streaming path writes
        # extracts, so the peak is the larger of the two rather than their sum
        # plus the extracts.
        self.input_path.unlink(missing_ok=True)
        return path
