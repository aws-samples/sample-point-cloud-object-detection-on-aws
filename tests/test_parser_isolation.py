"""Tests for the credential-free parser sidecar (T11).

In sidecar mode the worker never runs a native parser over an upload. It drops the
file into a shared spool, the sidecar converts it to the canonical format, and the
worker reads that with numpy alone. These tests cover both halves:

- the round trip through a real sidecar process, started with a scrubbed
  environment, gives the points the in-process readers give;
- the worker in sidecar mode calls none of laspy, pye57, trimesh or np.fromfile;
- a hostile result (paths, links, dtypes, shapes, sizes, counts) is refused;
- a crashed, hung or restarted parser fails the job with a fixed message;
- the sidecar refuses to start next to credentials, and cannot import boto3.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pointcloud_detect import io_readers
from pointcloud_detect.io_readers import (
    CanonicalWriter,
    canonical_dtype,
    read_point_cloud,
)
from pointcloud_detect.worker import parser_ipc, parser_sidecar
from pointcloud_detect.worker.parser_ipc import (
    CANONICAL_NAME,
    DONE_NAME,
    ERROR_MESSAGES,
    IN_PROGRESS_NAME,
    MAX_JSON_BYTES,
    PROTOCOL_VERSION,
    REQUEST_NAME,
    RESULT_NAME,
    ParserError,
    SidecarJob,
    validate_result,
    write_json_atomic,
)
from tests._proc import minimal_env, run, start

N_POINTS = 400
JOB_ID = "3f2a9c1e-7b4d-4e8a-9f60-2d1c5b7a8e93"


# ---------------------------------------------------------------------------
# Synthetic inputs
# ---------------------------------------------------------------------------


def _points(n: int = N_POINTS) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(7)
    xyz = rng.uniform(-5, 5, size=(n, 3))
    rgb = rng.integers(0, 256, size=(n, 3)).astype(np.uint8)
    intensity = rng.uniform(0, 1000, size=n).astype(np.float32)
    return xyz, rgb, intensity


def write_las(path: Path, n: int = N_POINTS) -> Path:
    import laspy

    xyz, rgb, intensity = _points(n)
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.array([0.001, 0.001, 0.001])
    header.offsets = np.zeros(3)
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.red = rgb[:, 0].astype(np.uint16) << 8
    las.green = rgb[:, 1].astype(np.uint16) << 8
    las.blue = rgb[:, 2].astype(np.uint16) << 8
    las.intensity = intensity.astype(np.uint16)
    las.write(str(path))
    return path


def write_ply(path: Path, n: int = N_POINTS) -> Path:
    xyz, rgb, _ = _points(n)
    lines = [
        "ply",
        "format ascii 1.0",
        f"element vertex {n}",
        "property float x",
        "property float y",
        "property float z",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        "end_header",
    ]
    lines += [
        f"{x:.6f} {y:.6f} {z:.6f} {r} {g} {b}"
        for (x, y, z), (r, g, b) in zip(xyz, rgb, strict=True)
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def write_e57(path: Path, n: int = N_POINTS) -> Path:
    import pye57

    xyz, rgb, intensity = _points(n)
    e57 = pye57.E57(str(path), mode="w")
    e57.write_scan_raw(
        {
            "cartesianX": xyz[:, 0],
            "cartesianY": xyz[:, 1],
            "cartesianZ": xyz[:, 2],
            "colorRed": rgb[:, 0],
            "colorGreen": rgb[:, 1],
            "colorBlue": rgb[:, 2],
            "intensity": intensity,
        }
    )
    e57.close()
    return path


def write_bin(path: Path, n: int = N_POINTS) -> Path:
    xyz, _, intensity = _points(n)
    np.column_stack([xyz, intensity]).astype(np.float32).tofile(path)
    return path


WRITERS = {
    "scene.las": write_las,
    "scene.laz": write_las,  # laspy compresses when the name ends in .laz
    "scene.ply": write_ply,
    "scene.e57": write_e57,
    "scene.bin": write_bin,
}


def _assert_same_cloud(a: Any, b: Any) -> None:
    np.testing.assert_allclose(a.xyz, b.xyz)
    assert (a.rgb is None) == (b.rgb is None)
    if a.rgb is not None:
        np.testing.assert_array_equal(a.rgb, b.rgb)
    assert (a.intensity is None) == (b.intensity is None)
    if a.intensity is not None:
        np.testing.assert_allclose(a.intensity, b.intensity)


# ---------------------------------------------------------------------------
# A real sidecar, in its own process with a scrubbed environment
# ---------------------------------------------------------------------------


def _scrubbed_env(spool: Path, home: Path) -> dict[str, str]:
    # Nothing from the test runner's environment but PATH: in particular no AWS_*
    # variables, and a HOME with no ~/.aws in it.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PCD_PARSER_SPOOL": str(spool),
    }


@pytest.fixture
def sidecar(tmp_path: Path) -> Iterator[Path]:
    spool = tmp_path / "spool"
    spool.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    proc = start(
        "python",
        ["-m", "pointcloud_detect.worker.parser_sidecar"],
        env=_scrubbed_env(spool, home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # Fail fast, with its log, if it refused to start.
        time.sleep(0.5)
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"sidecar exited with {proc.returncode}: {out}")
        yield spool
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _through_sidecar(
    spool: Path, source: Path, max_points: int | None = None, timeout: int = 120
) -> Any:
    with SidecarJob(spool, JOB_ID, source.name) as job:
        shutil.copyfile(source, job.input_path)
        canonical = job.parse(max_points=max_points, timeout_seconds=timeout)
        assert canonical.name == CANONICAL_NAME
        # The download is gone once the canonical file is validated.
        assert not job.input_path.exists()
        cloud = read_point_cloud(canonical, max_points=max_points)
        job_dir = job.dir
    # The job directory, and everything the parser wrote in it, is removed.
    assert not job_dir.exists()
    return cloud


class TestSidecarRoundTrip:
    @pytest.mark.parametrize("name", sorted(WRITERS))
    def test_matches_in_process_reader(
        self, sidecar: Path, tmp_path: Path, name: str
    ) -> None:
        source = WRITERS[name](tmp_path / name)
        expected = read_point_cloud(source)
        got = _through_sidecar(sidecar, source)
        assert got.num_points == N_POINTS
        _assert_same_cloud(got, expected)

    @pytest.mark.parametrize("name", ["scene.las", "scene.ply"])
    def test_max_points_subsamples_like_in_process(
        self, sidecar: Path, tmp_path: Path, name: str
    ) -> None:
        source = WRITERS[name](tmp_path / name)
        expected = read_point_cloud(source, max_points=50)
        got = _through_sidecar(sidecar, source, max_points=50)
        assert got.num_points == 50
        _assert_same_cloud(got, expected)

    def test_malformed_input_fails_with_a_fixed_message(
        self, sidecar: Path, tmp_path: Path
    ) -> None:
        source = tmp_path / "broken.las"
        source.write_bytes(b"LASF" + b"\x00" * 300)
        with pytest.raises(ParserError) as err:
            _through_sidecar(sidecar, source)
        assert err.value.code == "invalid_input"
        assert str(err.value).startswith(ERROR_MESSAGES["invalid_input"])

    def test_extension_and_content_disagreeing_fails(
        self, sidecar: Path, tmp_path: Path
    ) -> None:
        # T54: a ".ply" carrying an E57 must not reach libE57.
        source = write_e57(tmp_path / "real.e57")
        disguised = tmp_path / "disguised.ply"
        shutil.copyfile(source, disguised)
        with pytest.raises(ParserError) as err:
            _through_sidecar(sidecar, disguised)
        assert err.value.code == "format_mismatch"


# ---------------------------------------------------------------------------
# The worker in sidecar mode runs no native parser
# ---------------------------------------------------------------------------


def _forbid(name: str):
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{name} was called in the worker in sidecar mode")

    return _raise


def block_native_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    import laspy
    import pye57
    import trimesh

    monkeypatch.setattr(laspy, "open", _forbid("laspy.open"))
    monkeypatch.setattr(laspy, "read", _forbid("laspy.read"))
    monkeypatch.setattr(pye57, "E57", _forbid("pye57.E57"))
    monkeypatch.setattr(trimesh, "load", _forbid("trimesh.load"))
    monkeypatch.setattr(np, "fromfile", _forbid("np.fromfile"))
    for reader in ("read_las", "read_e57", "read_ply", "read_kitti_bin"):
        monkeypatch.setattr(io_readers, reader, _forbid(reader))


def _write_ok_result(
    job_dir: Path,
    count: int,
    has_rgb: bool,
    has_intensity: bool,
    source_format: str,
    overrides: dict[str, Any] | None = None,
) -> None:
    manifest: dict[str, Any] = {
        "format": io_readers.CANONICAL_FILE_FORMAT,
        "file": CANONICAL_NAME,
        "point_count": count,
        "has_rgb": has_rgb,
        "has_intensity": has_intensity,
        "source_format": source_format,
    }
    manifest.update(overrides or {})
    write_json_atomic(
        job_dir,
        RESULT_NAME,
        {"version": PROTOCOL_VERSION, "status": "ok", "manifest": manifest},
    )


def _write_canonical(path: Path, cloud: Any) -> None:
    writer = CanonicalWriter(path, cloud.rgb is not None, cloud.intensity is not None)
    writer.append(cloud.xyz, cloud.rgb, cloud.intensity)
    writer.close()


@pytest.mark.parametrize("job_type", ["detection", "annotation"])
@pytest.mark.parametrize("name", sorted(WRITERS))
def test_worker_in_sidecar_mode_never_runs_a_native_parser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, job_type: str
) -> None:
    from pointcloud_detect.worker import processor

    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv("PCD_PARSER_MODE", "sidecar")
    monkeypatch.setenv("PCD_PARSER_SPOOL", str(spool))

    # Converted here, before the native readers are blocked, and handed back the
    # way the sidecar hands it back: the worker sees only the validated result.
    source = WRITERS[name](tmp_path / name)
    cloud = read_point_cloud(source)
    prepared = tmp_path / "prepared.npy"
    _write_canonical(prepared, cloud)

    def fake_parse(job: SidecarJob, max_points: Any, timeout_seconds: int) -> Path:
        assert job.dir.parent == spool
        assert job.input_path.read_bytes() == source.read_bytes()
        shutil.copyfile(prepared, job.dir / CANONICAL_NAME)
        _write_ok_result(
            job.dir,
            cloud.num_points,
            cloud.rgb is not None,
            cloud.intensity is not None,
            job.source_format,
        )
        return validate_result(job.dir, job.source_format, max_points)

    uploads: dict[str, Any] = {}
    statuses: list[str] = []
    tiled: list[int] = []
    monkeypatch.setattr(SidecarJob, "parse", fake_parse)
    monkeypatch.setattr(
        processor,
        "download_from_s3",
        lambda client, bucket, key, dest: shutil.copyfile(source, dest),
    )
    monkeypatch.setattr(
        processor,
        "upload_json_to_s3",
        lambda client, bucket, key, data: uploads.__setitem__(key, data),
    )

    def fake_tiles(**kwargs: Any) -> int:
        tiled.append(kwargs["cloud"].num_points)
        return 1

    monkeypatch.setattr(processor, "generate_lod_tiles", fake_tiles)
    monkeypatch.setattr(
        processor,
        "_update_status",
        lambda job_id, status, **kw: statuses.append(status),
    )
    monkeypatch.setattr(
        processor, "fetch_job_record", lambda job_id: {"status": "processing"}
    )
    block_native_parsers(monkeypatch)

    processor.process_job(
        job_id=JOB_ID,
        s3_input_key=f"uploads/{JOB_ID}/{name}",
        config={"job_type": job_type, "downsample_voxel_m": 0.0},
        s3_client=object(),
    )

    assert statuses == ["processing", "complete"]
    assert f"results/{JOB_ID}/detections.json" in uploads
    assert tiled and tiled[0] > 0
    # Nothing is left in the spool.
    assert list(spool.iterdir()) == []


def test_in_process_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PCD_PARSER_MODE", raising=False)
    assert parser_ipc.parser_mode() == "in-process"


def test_unknown_parser_mode_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo must not quietly put the parsers back in the credentialed process.
    monkeypatch.setenv("PCD_PARSER_MODE", "sidcar")
    with pytest.raises(RuntimeError, match="not a recognised parser mode"):
        parser_ipc.parser_mode()


def test_process_job_refuses_a_bad_mode_before_touching_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pointcloud_detect.worker import processor

    monkeypatch.setenv("PCD_PARSER_MODE", "bogus")
    statuses: list[str] = []
    monkeypatch.setattr(
        processor,
        "_update_status",
        lambda job_id, status, **kw: statuses.append(status),
    )
    with pytest.raises(RuntimeError):
        processor.process_job(JOB_ID, f"uploads/{JOB_ID}/a.las", {}, object())
    assert statuses == []


def test_sidecar_mode_needs_an_existing_spool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PCD_PARSER_SPOOL", raising=False)
    with pytest.raises(RuntimeError, match="PCD_PARSER_SPOOL"):
        parser_ipc.parser_spool()
    monkeypatch.setenv("PCD_PARSER_SPOOL", str(tmp_path / "missing" / "ipc"))
    with pytest.raises(RuntimeError, match="not an existing directory"):
        parser_ipc.parser_spool()
    (tmp_path / "target").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "target")
    monkeypatch.setenv("PCD_PARSER_SPOOL", str(tmp_path / "link"))
    with pytest.raises(RuntimeError, match="not an existing directory"):
        parser_ipc.parser_spool()


def test_spool_is_created_group_writable_on_the_empty_scratch_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The chart points both containers at /scratch/ipc on a fresh emptyDir, so
    # whichever starts first must create it, writable by the other's uid.
    monkeypatch.setenv("PCD_PARSER_SPOOL", str(tmp_path / "ipc"))
    old = os.umask(0o022)
    try:
        spool = parser_ipc.parser_spool()
    finally:
        os.umask(old)
    assert spool == tmp_path / "ipc"
    assert stat.S_IMODE(spool.stat().st_mode) == 0o770
    assert parser_ipc.parser_spool() == spool


def test_sidecar_main_creates_the_spool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PCD_PARSER_SPOOL", str(tmp_path / "ipc"))
    served: list[Path] = []
    monkeypatch.setattr(
        parser_sidecar, "serve", lambda spool, stop: served.append(spool)
    )
    monkeypatch.setattr(parser_sidecar, "configure_json_logging", lambda: None)
    monkeypatch.setattr(parser_sidecar, "block_aws_sdk", lambda: None)
    monkeypatch.setattr(parser_sidecar, "credential_problems", lambda env: [])
    monkeypatch.setattr(parser_sidecar, "gpu_devices", lambda: [])
    monkeypatch.setattr(parser_sidecar.signal, "signal", lambda *a: None)
    monkeypatch.setattr(parser_sidecar.os, "umask", lambda mask: 0o022)
    # Not in the test runner's own process: on Linux it would make pytest a child
    # subreaper and turn on the sweep for every later handle() in this session.
    isolated: list[bool] = []
    monkeypatch.setattr(
        parser_sidecar, "isolate_supervisor", lambda: isolated.append(True) or True
    )
    assert parser_sidecar.main([]) == 0
    assert isolated == [True]
    assert served == [tmp_path / "ipc"]
    assert (tmp_path / "ipc").is_dir()


def test_serve_exits_when_a_job_changes_the_supervisors_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prlimit tripwire: a conversion lowering this process's limits ends serve().

    The child shares the supervisor's uid, so prlimit(2) would let a compromised
    parser lower the supervisor's limits, and the damage would outlive its job. The
    test stands in for that child by lowering the soft RLIMIT_NOFILE inside
    handle(), the only point where a child runs, and restores it afterwards (the
    hard limit is untouched, since lowering it is irreversible).
    """
    resource = pytest.importorskip("resource")
    spool = tmp_path / "ipc"
    job = spool / "job-1"
    job.mkdir(parents=True)
    (job / parser_ipc.REQUEST_NAME).write_text("{}")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft > 1

    def lower_limits(job_dir: Path) -> None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft - 1, hard))

    monkeypatch.setattr(parser_sidecar, "handle", lower_limits)
    stop = threading.Event()
    try:
        with pytest.raises(parser_sidecar.SupervisorStateChangedError) as excinfo:
            parser_sidecar.serve(spool, stop)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert str(excinfo.value) == "job-1"


def test_serve_snapshot_covers_every_limit_and_the_priority() -> None:
    """_self_state() is one comparable value naming each limit, so a diff is exact."""
    resource = pytest.importorskip("resource")
    state = parser_sidecar._self_state()
    assert state is not None
    names = [name for name, _ in state]
    assert "priority" in names
    rlimits = {n for n in dir(resource) if n.startswith("RLIMIT_")}
    assert rlimits == set(names) - {"priority"}
    assert state == parser_sidecar._self_state()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", parser_ipc.DEFAULT_PARSER_TIMEOUT_SECONDS),
        ("60", 60),
        # Capped an hour short of SQS's 12 hour visibility limit.
        ("999999", parser_ipc.MAX_PARSER_TIMEOUT_SECONDS),
    ],
)
def test_parser_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("PCD_PARSER_TIMEOUT_SECONDS", raw)
    assert parser_ipc.parser_timeout_seconds() == expected
    assert parser_ipc.MAX_PARSER_TIMEOUT_SECONDS < parser_ipc.SQS_VISIBILITY_CAP_SECONDS


@pytest.mark.parametrize("raw", ["0", "-5", "soon"])
def test_parser_timeout_rejects_nonsense(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("PCD_PARSER_TIMEOUT_SECONDS", raw)
    with pytest.raises(RuntimeError):
        parser_ipc.parser_timeout_seconds()


# ---------------------------------------------------------------------------
# Hostile results are refused
# ---------------------------------------------------------------------------


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    d = tmp_path / "job"
    d.mkdir()
    return d


def _good_canonical(job_dir: Path, n: int = 10) -> None:
    xyz, rgb, _ = _points(n)
    writer = CanonicalWriter(job_dir / CANONICAL_NAME, True, False)
    writer.append(xyz, rgb, None)
    writer.close()


def _refused(job_dir: Path, max_points: int | None = None) -> str:
    with pytest.raises(ParserError) as err:
        validate_result(job_dir, "las", max_points)
    return err.value.code


class TestHostileResults:
    def test_a_good_result_validates(self, job_dir: Path) -> None:
        _good_canonical(job_dir)
        _write_ok_result(job_dir, 10, True, False, "las")
        assert validate_result(job_dir, "las") == job_dir / CANONICAL_NAME

    @pytest.mark.parametrize(
        "overrides",
        [
            {"file": "../../etc/passwd"},
            {"file": "/etc/passwd"},
            {"file": "other.npy"},
            {"format": "pickle"},
            {"point_count": 11},
            {"point_count": -1},
            {"point_count": True},
            {"point_count": 10.0},
            {"point_count": 10**12},
            {"has_rgb": False},
            {"has_rgb": "yes"},
            {"has_intensity": True},
            {"source_format": "e57"},
            {"extra": "field"},
        ],
    )
    def test_manifest_fields_are_checked(
        self, job_dir: Path, overrides: dict[str, Any]
    ) -> None:
        _good_canonical(job_dir)
        _write_ok_result(job_dir, 10, True, False, "las", overrides)
        assert _refused(job_dir) == "invalid_result"

    def test_more_points_than_max_points_is_refused(self, job_dir: Path) -> None:
        _good_canonical(job_dir)
        _write_ok_result(job_dir, 10, True, False, "las")
        assert _refused(job_dir, max_points=5) == "invalid_result"

    def test_missing_result(self, job_dir: Path) -> None:
        assert _refused(job_dir) == "invalid_result"

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b'{"version": 2, "status": "ok"}',
            b'{"version": 1, "status": "maybe"}',
            b" " * (MAX_JSON_BYTES + 1),
        ],
    )
    def test_malformed_result_json(self, job_dir: Path, body: bytes) -> None:
        _good_canonical(job_dir)
        (job_dir / RESULT_NAME).write_bytes(body)
        assert _refused(job_dir) == "invalid_result"

    def test_result_json_as_a_symlink_is_not_followed(
        self, job_dir: Path, tmp_path: Path
    ) -> None:
        _good_canonical(job_dir)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _write_ok_result(elsewhere, 10, True, False, "las")
        (job_dir / RESULT_NAME).symlink_to(elsewhere / RESULT_NAME)
        assert _refused(job_dir) == "invalid_result"

    def test_result_json_as_a_fifo_does_not_hang(self, job_dir: Path) -> None:
        os.mkfifo(job_dir / RESULT_NAME)
        assert _refused(job_dir) == "invalid_result"

    def test_canonical_file_as_a_symlink(self, job_dir: Path, tmp_path: Path) -> None:
        _good_canonical(tmp_path)
        (job_dir / CANONICAL_NAME).symlink_to(tmp_path / CANONICAL_NAME)
        _write_ok_result(job_dir, 10, True, False, "las")
        assert _refused(job_dir) == "invalid_result"

    def test_canonical_file_as_a_hard_link(self, job_dir: Path, tmp_path: Path) -> None:
        # A hard link could name any file the parser's uid can read.
        _good_canonical(tmp_path)
        os.link(tmp_path / CANONICAL_NAME, job_dir / CANONICAL_NAME)
        _write_ok_result(job_dir, 10, True, False, "las")
        assert _refused(job_dir) == "invalid_result"

    def test_canonical_file_as_a_fifo_does_not_hang(self, job_dir: Path) -> None:
        os.mkfifo(job_dir / CANONICAL_NAME)
        _write_ok_result(job_dir, 10, True, False, "las")
        assert _refused(job_dir) == "invalid_result"

    def test_pickled_object_array_is_refused(self, job_dir: Path) -> None:
        np.save(
            job_dir / CANONICAL_NAME,
            np.array([{"x": 1}] * 10, dtype=object),
            allow_pickle=True,
        )
        _write_ok_result(job_dir, 10, False, False, "las")
        assert _refused(job_dir) == "invalid_result"

    @pytest.mark.parametrize(
        "array",
        [
            np.zeros((10, 3), dtype=np.float64),  # plain 2-D, not the record layout
            np.zeros(10, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4")]),
            np.zeros(10, dtype=canonical_dtype(False, False).newbyteorder(">")),
            np.zeros((10, 1), dtype=canonical_dtype(False, False)),
        ],
    )
    def test_unexpected_dtype_or_shape(self, job_dir: Path, array: np.ndarray) -> None:
        np.save(job_dir / CANONICAL_NAME, array, allow_pickle=False)
        _write_ok_result(job_dir, 10, False, False, "las")
        assert _refused(job_dir) == "invalid_result"

    def test_fortran_order_is_refused(self, job_dir: Path) -> None:
        from numpy.lib import format as npy_format

        with open(job_dir / CANONICAL_NAME, "wb") as f:
            npy_format.write_array_header_1_0(
                f,
                {
                    "descr": npy_format.dtype_to_descr(canonical_dtype(False, False)),
                    "fortran_order": True,
                    "shape": (10,),
                },
            )
            f.write(b"\x00" * 24 * 10)
        _write_ok_result(job_dir, 10, False, False, "las")
        assert _refused(job_dir) == "invalid_result"

    @pytest.mark.parametrize("delta", [-1, 1])
    def test_file_size_must_match_header(self, job_dir: Path, delta: int) -> None:
        _good_canonical(job_dir)
        path = job_dir / CANONICAL_NAME
        data = path.read_bytes()
        path.write_bytes(data[:-1] if delta < 0 else data + b"\x00")
        _write_ok_result(job_dir, 10, True, False, "las")
        assert _refused(job_dir) == "invalid_result"

    def test_header_declaring_billions_of_points_is_refused(
        self, job_dir: Path
    ) -> None:
        from numpy.lib import format as npy_format

        with open(job_dir / CANONICAL_NAME, "wb") as f:
            npy_format.write_array_header_1_0(
                f,
                {
                    "descr": npy_format.dtype_to_descr(canonical_dtype(False, False)),
                    "fortran_order": False,
                    "shape": (io_readers.MAX_CANONICAL_POINTS + 1,),
                },
            )
        _write_ok_result(job_dir, 10, False, False, "las")
        assert _refused(job_dir) == "invalid_result"

    def test_reported_error_uses_the_fixed_message(self, job_dir: Path) -> None:
        write_json_atomic(
            job_dir,
            RESULT_NAME,
            {"version": PROTOCOL_VERSION, "status": "error", "code": "too_large"},
        )
        with pytest.raises(ParserError) as err:
            validate_result(job_dir, "las")
        assert err.value.code == "too_large"
        assert str(err.value) == f"{ERROR_MESSAGES['too_large']} (too_large)"

    def test_parser_chosen_text_never_reaches_the_message(self, job_dir: Path) -> None:
        # A compromised parser picks at most a code; unknown codes collapse.
        write_json_atomic(
            job_dir,
            RESULT_NAME,
            {
                "version": PROTOCOL_VERSION,
                "status": "error",
                "code": "<script>alert(1)</script>",
            },
        )
        with pytest.raises(ParserError) as err:
            validate_result(job_dir, "las")
        assert err.value.code == "parse_failed"
        assert "script" not in str(err.value)


class TestSidecarJob:
    @pytest.mark.parametrize("job_id", ["../x", "a/b", "", ".hidden"])
    def test_job_id_must_be_a_plain_name(self, tmp_path: Path, job_id: str) -> None:
        with pytest.raises(ValueError):
            SidecarJob(tmp_path, job_id, "a.las")

    @pytest.mark.parametrize("filename", ["../a.las", "a/b.las", "..", "a.npy", "a"])
    def test_filename_must_be_one_input_segment(
        self, tmp_path: Path, filename: str
    ) -> None:
        with pytest.raises(ValueError):
            SidecarJob(tmp_path, JOB_ID, filename)

    def test_directory_is_group_shared_and_removed(self, tmp_path: Path) -> None:
        with SidecarJob(tmp_path, JOB_ID, "a.las") as job:
            assert job.dir.parent == tmp_path
            assert (job.dir.stat().st_mode & 0o777) == 0o770
            job.input_path.write_bytes(b"x")
            path = job.dir
        assert not path.exists()

    def test_request_is_written_atomically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parser_ipc, "_TIMEOUT_GRACE_SECONDS", 0)
        with SidecarJob(tmp_path, JOB_ID, "a.las") as job:
            job.input_path.write_bytes(b"x")
            with pytest.raises(ParserError):
                job.parse(max_points=7, timeout_seconds=1, poll_seconds=0.05)
            request = json.loads((job.dir / REQUEST_NAME).read_text())
            assert request == {
                "version": PROTOCOL_VERSION,
                "input": "a.las",
                "max_points": 7,
                "timeout_seconds": 1,
            }
            # No temporary files are left beside it.
            assert sorted(p.name for p in job.dir.iterdir()) == ["a.las", REQUEST_NAME]

    def test_input_is_made_group_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parser_ipc, "_TIMEOUT_GRACE_SECONDS", 0)
        with SidecarJob(tmp_path, JOB_ID, "a.las") as job:
            job.input_path.write_bytes(b"x")
            job.input_path.chmod(0o600)
            with pytest.raises(ParserError):
                job.parse(max_points=None, timeout_seconds=0, poll_seconds=0.01)
            assert (job.input_path.stat().st_mode & 0o777) == 0o640

    def test_a_planted_link_is_not_followed_by_the_chmod(self, tmp_path: Path) -> None:
        # A parser left over from another job swaps the download for a link to a
        # file in the worker's container: the worker must not change that file.
        target = tmp_path / "worker-file"
        target.write_bytes(b"secret")
        target.chmod(0o600)
        spool = tmp_path / "spool"
        spool.mkdir()
        with SidecarJob(spool, JOB_ID, "a.las") as job:
            job.input_path.symlink_to(target)
            with pytest.raises(ParserError) as err:
                job.parse(max_points=None, timeout_seconds=1, poll_seconds=0.01)
            assert err.value.code == "parse_failed"
            assert not (job.dir / REQUEST_NAME).exists()
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_no_parser_means_a_timeout_not_a_hang(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parser_ipc, "_TIMEOUT_GRACE_SECONDS", 0)
        with SidecarJob(tmp_path, JOB_ID, "a.las") as job:
            job.input_path.write_bytes(b"x")
            start = time.monotonic()
            with pytest.raises(ParserError) as err:
                job.parse(max_points=None, timeout_seconds=1, poll_seconds=0.05)
        assert err.value.code == "timeout"
        assert time.monotonic() - start < 10


# ---------------------------------------------------------------------------
# Crashes, hangs and restarts fail the job
# ---------------------------------------------------------------------------


def _claimed_job(spool: Path, timeout: int = 30) -> Path:
    job_dir = spool / f"{JOB_ID}.abc"
    job_dir.mkdir()
    write_las(job_dir / "scene.las", n=20)
    write_json_atomic(
        job_dir,
        REQUEST_NAME,
        {
            "version": PROTOCOL_VERSION,
            "input": "scene.las",
            "max_points": None,
            "timeout_seconds": timeout,
        },
    )
    assert parser_sidecar.claim_next(spool) == job_dir
    assert (job_dir / IN_PROGRESS_NAME).exists()
    return job_dir


def _result_code(job_dir: Path) -> str:
    result = json.loads((job_dir / RESULT_NAME).read_text())
    assert result["version"] == PROTOCOL_VERSION
    return result["code"] if result["status"] == "error" else "ok"


class TestParserFailures:
    def test_a_real_conversion_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The child inherits HOME; one with ~/.aws/credentials in it would make the
        # child refuse to run, which is the point, but not this test's.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        job_dir = _claimed_job(tmp_path)
        parser_sidecar.handle(job_dir)
        assert _result_code(job_dir) == "ok"
        assert (job_dir / DONE_NAME).exists()
        assert not (job_dir / IN_PROGRESS_NAME).exists()
        assert validate_result(job_dir, "las").exists()

    @pytest.mark.parametrize(
        ("script", "code"),
        [
            ("import os; os.abort()", "crashed"),
            ("import sys; sys.exit(3)", "crashed"),
            ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", "crashed"),
            ("import time; time.sleep(60)", "timeout"),
        ],
    )
    def test_child_crash_or_hang_is_a_failed_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        script: str,
        code: str,
    ) -> None:
        job_dir = _claimed_job(tmp_path, timeout=2)
        monkeypatch.setattr(
            parser_sidecar,
            "_child_command",
            lambda d: [sys.executable, "-c", script],
        )
        start = time.monotonic()
        parser_sidecar.handle(job_dir)
        assert time.monotonic() - start < 20
        assert _result_code(job_dir) == code
        # The worker then fails the job with the fixed message for that code.
        with pytest.raises(ParserError) as err:
            validate_result(job_dir, "las")
        assert err.value.code == code
        assert str(err.value).startswith(ERROR_MESSAGES[code])

    def test_child_writing_nonsense_is_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_dir = _claimed_job(tmp_path)
        target = job_dir / parser_sidecar.CHILD_RESULT_NAME
        monkeypatch.setattr(
            parser_sidecar,
            "_child_command",
            lambda d: [
                sys.executable,
                "-c",
                f"open({str(target)!r}, 'w').write('garbage')",
            ],
        )
        parser_sidecar.handle(job_dir)
        assert _result_code(job_dir) == "crashed"

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="needs /proc and prctl (Linux)"
    )
    @pytest.mark.parametrize("exit_code", [0, 3])
    def test_processes_the_child_leaves_are_killed_before_the_result(
        self, tmp_path: Path, exit_code: int
    ) -> None:
        # A child that a crafted file took over forks a grandchild that calls
        # setsid(), escaping killpg, then exits normally. The grandchild must be dead
        # by the time the result is written, or it would wait in the container for
        # the next user's upload. Run in a process of its own, as the sidecar runs,
        # so that the sweep only ever sees that process's descendants.
        job_dir = _claimed_job(tmp_path)
        pid_file = tmp_path / "stray.pid"
        child = (
            "import os, sys, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(600)\n"
            f"while not os.path.exists({str(pid_file)!r}): time.sleep(0.01)\n"
            f"sys.exit({exit_code})\n"
        )
        supervisor = (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "from pointcloud_detect.worker import parser_sidecar as ps\n"
            "assert ps.isolate_supervisor()\n"
            f"ps._child_command = lambda d: [sys.executable, '-c', {child!r}]\n"
            f"ps.handle(Path({str(job_dir)!r}))\n"
            f"pid = int(Path({str(pid_file)!r}).read_text())\n"
            f"print(json.dumps({{'stray_alive': os.path.exists(f'/proc/{{pid}}')}}))\n"
        )
        out = run(
            "python", ["-c", supervisor], env=minimal_env(), timeout=60, check=True
        )
        pid = int(pid_file.read_text())
        try:
            assert json.loads(out.stdout.splitlines()[-1]) == {"stray_alive": False}
            assert _result_code(job_dir) == "crashed"
        finally:
            # Belt and braces, so a failure here does not leave the sleeper behind.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_sweep_is_off_unless_the_supervisor_is_isolated(self) -> None:
        # handle() called in a test's own process must not kill the test runner's
        # other children.
        assert parser_sidecar._SWEEP_ENABLED is False
        assert parser_sidecar.reap_strays() is True

    def test_each_child_gets_a_fresh_home(self, tmp_path: Path) -> None:
        env = parser_sidecar._child_env(tmp_path)
        assert env["HOME"] == env["TMPDIR"] == str(tmp_path)

    def test_restart_fails_requests_left_in_progress(self, tmp_path: Path) -> None:
        # The container died mid-parse (say the OOM killer took it): on restart the
        # request is failed rather than retried, so a file that crash-loops the
        # parser fails its job instead of hanging it.
        job_dir = _claimed_job(tmp_path)
        (job_dir / CANONICAL_NAME).write_bytes(b"half written")
        assert parser_sidecar.recover(tmp_path) == 1
        assert _result_code(job_dir) == "crashed"
        assert (job_dir / DONE_NAME).exists()
        assert not (job_dir / CANONICAL_NAME).exists()
        # And it is not picked up again.
        assert parser_sidecar.claim_next(tmp_path) is None

    def test_restart_keeps_a_result_that_was_already_written(
        self, tmp_path: Path
    ) -> None:
        job_dir = _claimed_job(tmp_path)
        write_json_atomic(
            job_dir,
            RESULT_NAME,
            {"version": PROTOCOL_VERSION, "status": "error", "code": "too_large"},
        )
        parser_sidecar.recover(tmp_path)
        assert _result_code(job_dir) == "too_large"

    def test_unusable_request_is_failed(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "j.1"
        job_dir.mkdir()
        (job_dir / REQUEST_NAME).write_text('{"version": 1, "timeout_seconds": "x"}')
        assert parser_sidecar.claim_next(tmp_path) == job_dir
        parser_sidecar.handle(job_dir)
        assert _result_code(job_dir) == "parse_failed"

    @pytest.mark.parametrize("name", ["../x.las", ".hidden.las", "a/b.las", 5])
    def test_request_input_must_be_a_plain_name(self, name: object) -> None:
        with pytest.raises(ValueError):
            parser_sidecar._parse_request(
                {"version": PROTOCOL_VERSION, "input": name, "max_points": None}
            )

    def test_links_in_the_spool_are_not_job_directories(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / REQUEST_NAME).write_text("{}")
        spool = tmp_path / "spool"
        spool.mkdir()
        (spool / "link").symlink_to(real)
        assert parser_sidecar.claim_next(spool) is None

    def test_child_command_is_this_interpreter_and_module(self, tmp_path: Path) -> None:
        # Fixed but for the job directory; -P keeps the working directory off sys.path.
        job_dir = tmp_path / "job"
        assert parser_sidecar._child_command(job_dir) == [
            sys.executable,
            "-P",
            "-m",
            "pointcloud_detect.worker.parser_sidecar",
            "--convert",
            str(job_dir),
        ]

    def test_child_environment_is_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        monkeypatch.setenv("S3_INPUT_BUCKET", "bucket")
        monkeypatch.setenv("XERCES_DISABLE_DTD", "0")
        env = parser_sidecar._child_env()
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "S3_INPUT_BUCKET" not in env
        assert env["XERCES_DISABLE_DTD"] == "1"

    @pytest.mark.parametrize("leak", [False, True])
    def test_supervisor_credentials_never_reach_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leak: bool
    ) -> None:
        # The property, end to end through the real call: the supervisor holds every
        # credential the child refuses on, and the child's own refusal is the check.
        # leak=True hands the child the supervisor's whole environment, which shows
        # the check fails when a credential gets through.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        planted = [
            *parser_sidecar.CREDENTIAL_ENV_VARS,
            *(
                p + "FULL_URI" if p.endswith("_") else p
                for p in parser_sidecar.CREDENTIAL_ENV_PREFIXES
            ),
        ]
        for name in planted:
            monkeypatch.setenv(name, f"planted-{name.lower()}")
        if leak:
            monkeypatch.setattr(
                parser_sidecar, "_child_env", lambda home=None: dict(os.environ)
            )
        job_dir = _claimed_job(tmp_path)
        parser_sidecar.handle(job_dir)
        assert _result_code(job_dir) == ("crashed" if leak else "ok")


# ---------------------------------------------------------------------------
# The sidecar refuses credentials and the AWS SDK
# ---------------------------------------------------------------------------


class TestCredentialRefusal:
    @pytest.mark.parametrize(
        "name",
        [
            "AWS_ROLE_ARN",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        ],
    )
    def test_credential_env_is_a_problem(self, name: str) -> None:
        assert parser_sidecar.credential_problems({name: "x"}, files=()) == [name]

    def test_clean_env_has_no_problems(self) -> None:
        env = {"PATH": "/bin", "AWS_REGION": "us-east-1", "AWS_ROLE_ARN": ""}
        assert parser_sidecar.credential_problems(env, files=()) == []

    def test_token_file_is_a_problem(self, tmp_path: Path) -> None:
        token = tmp_path / "token"
        token.write_text("eyJ...")
        assert parser_sidecar.credential_problems({}, files=(str(token),)) == [
            str(token)
        ]

    def test_default_list_covers_irsa_pod_identity_and_sa_tokens(self) -> None:
        files = " ".join(parser_sidecar.CREDENTIAL_FILES)
        assert "eks.amazonaws.com/serviceaccount/token" in files
        assert "eks-pod-identity-token" in files
        assert "kubernetes.io/serviceaccount/token" in files

    @pytest.mark.parametrize(
        "extra",
        [
            {"AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/worker"},
            {"AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token"},
            {"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1"},
            # The variables whose values are themselves secrets.
            {
                "AWS_ACCESS_KEY_ID": "planted-access-key-id",
                "AWS_SECRET_ACCESS_KEY": "planted-secret-access-key",
                "AWS_SESSION_TOKEN": "planted-session-token",
            },
            {"AWS_CONTAINER_AUTHORIZATION_TOKEN": "planted-authorization-token"},
        ],
    )
    def test_process_refuses_to_start(
        self, tmp_path: Path, extra: dict[str, str]
    ) -> None:
        spool = tmp_path / "spool"
        spool.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        env = {**_scrubbed_env(spool, home), **extra}
        proc = run(
            "python",
            ["-m", "pointcloud_detect.worker.parser_sidecar"],
            env=env,
            timeout=60,
        )
        assert proc.returncode == 2
        assert "Refusing to start" in proc.stderr
        # Names only: the value of a credential variable is never logged.
        for value in extra.values():
            assert value not in proc.stderr

    def test_gpu_devices_are_found(self, tmp_path: Path) -> None:
        (tmp_path / "nvidia0").touch()
        (tmp_path / "nvidiactl").touch()
        (tmp_path / "null").touch()
        assert parser_sidecar.gpu_devices(str(tmp_path / "nvidia*")) == [
            str(tmp_path / "nvidia0"),
            str(tmp_path / "nvidiactl"),
        ]
        assert parser_sidecar.gpu_devices(str(tmp_path / "none*")) == []

    def test_process_refuses_to_start_with_a_gpu(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # In this process, so everything main() does to it first is stubbed out.
        for name in ("configure_json_logging", "block_aws_sdk"):
            monkeypatch.setattr(parser_sidecar, name, lambda: None)
        monkeypatch.setattr(parser_sidecar, "credential_problems", lambda env: [])
        monkeypatch.setattr(parser_sidecar, "gpu_devices", lambda: ["/dev/nvidia0"])
        assert parser_sidecar.main([]) == 2

    def test_process_refuses_a_shared_credentials_file(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        spool.mkdir()
        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        (home / ".aws" / "credentials").write_text("[default]\n")
        proc = run(
            "python",
            ["-m", "pointcloud_detect.worker.parser_sidecar"],
            env=_scrubbed_env(spool, home),
            timeout=60,
        )
        assert proc.returncode == 2

    def test_aws_sdk_cannot_be_imported_and_is_never_loaded(self) -> None:
        code = (
            "import sys\n"
            "from pointcloud_detect.worker import parser_sidecar\n"
            "from pointcloud_detect import io_readers, streaming\n"
            "loaded = [m for m in ('boto3', 'botocore') if sys.modules.get(m)]\n"
            "assert not loaded, loaded\n"
            "parser_sidecar.block_aws_sdk()\n"
            "try:\n"
            "    import boto3\n"
            "except ImportError:\n"
            "    print('blocked')\n"
        )
        proc = run("python", ["-c", code], env=minimal_env(), timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "blocked"
