"""Tests for the reader hardening: format choice, PLY textures, E57 DTDs and sizes.

- T54: the validated extension picks the parser and the magic bytes must agree;
  the PLY reader never resolves a texture path; Xerces-C refuses DTDs in E57 XML.
- T12: an uncompressed LAS whose header declares more points than the file holds
  is refused before anything is allocated from the header.
- T53: a PLY or .bin larger than the memory budget is refused before loading.
- The canonical reader subsamples exactly as read_las does.
"""

from __future__ import annotations

import importlib
import os
import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

import pointcloud_detect
from pointcloud_detect import io_readers
from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.io_readers import (
    CanonicalWriter,
    FormatMismatchError,
    UnsupportedFormatError,
    detect_format,
    iter_canonical_chunks,
    read_canonical,
    read_las,
    read_ply,
    read_point_cloud,
)
from pointcloud_detect.pipeline import _plan_ingest, check_whole_load_file_size
from tests._proc import Finished, minimal_env, run
from tests.test_parser_isolation import write_bin, write_e57, write_las, write_ply

# ---------------------------------------------------------------------------
# T54: the extension chooses the parser, and the content must agree
# ---------------------------------------------------------------------------


class TestFormatChoice:
    @pytest.mark.parametrize(
        ("name", "writer", "fmt"),
        [
            ("a.las", write_las, "las"),
            ("A.LAS", write_las, "las"),
            ("a.laz", write_las, "laz"),
            ("a.ply", write_ply, "ply"),
            ("a.e57", write_e57, "e57"),
            ("a.bin", write_bin, "kitti_bin"),
        ],
    )
    def test_matching_content_is_accepted(
        self, tmp_path: Path, name: str, writer, fmt: str
    ) -> None:
        assert detect_format(writer(tmp_path / name)) == fmt

    @pytest.mark.parametrize(
        ("real", "writer", "disguise"),
        [
            # The case T54 names: a ".ply" that is really an E57 must not reach
            # libE57 and Xerces-C.
            ("real.e57", write_e57, "a.ply"),
            ("real.e57", write_e57, "a.las"),
            ("real.e57", write_e57, "a.bin"),
            ("real.las", write_las, "a.ply"),
            ("real.las", write_las, "a.e57"),
            ("real.las", write_las, "a.bin"),
            ("real.ply", write_ply, "a.e57"),
            ("real.ply", write_ply, "a.bin"),
            # A ".las" holding LASzip data must not reach the LAZ decompressor.
            ("real.laz", write_las, "a.las"),
        ],
    )
    def test_disagreeing_content_is_refused(
        self, tmp_path: Path, real: str, writer, disguise: str
    ) -> None:
        source = writer(tmp_path / real)
        target = tmp_path / disguise
        target.write_bytes(source.read_bytes())
        with pytest.raises(FormatMismatchError):
            detect_format(target)
        with pytest.raises(FormatMismatchError):
            read_point_cloud(target)

    def test_laz_named_file_with_plain_points_reads_as_las(
        self, tmp_path: Path
    ) -> None:
        source = write_las(tmp_path / "real.las")
        target = tmp_path / "plain.laz"
        target.write_bytes(source.read_bytes())
        assert detect_format(target) == "laz"
        assert read_point_cloud(target).num_points == read_las(source).num_points

    def test_file_with_no_signature_is_refused_for_signed_formats(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "a.e57"
        target.write_bytes(b"\x00" * 200)
        with pytest.raises(FormatMismatchError):
            detect_format(target)

    def test_unknown_extension(self, tmp_path: Path) -> None:
        target = tmp_path / "a.xyz"
        target.write_bytes(b"0 0 0\n")
        with pytest.raises(UnsupportedFormatError):
            detect_format(target)

    def test_fifo_is_refused_without_blocking(self, tmp_path: Path) -> None:
        fifo = tmp_path / "a.las"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="Not a regular file"):
            detect_format(fifo)


# ---------------------------------------------------------------------------
# T54: the PLY reader never resolves a texture path
# ---------------------------------------------------------------------------


def _ply_with_texture(path: Path, texture: str) -> Path:
    path.write_text(
        "ply\n"
        "format ascii 1.0\n"
        f"comment TextureFile {texture}\n"
        "element vertex 3\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "end_header\n"
        "0 0 0\n1 0 0\n0 1 0\n"
    )
    return path


@pytest.fixture
def resolver_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from trimesh import resolvers

    calls: list[str] = []
    for cls in (resolvers.FilePathResolver, resolvers.ZipResolver):
        original = cls.get

        def spy(self, name, _original=original, _cls=cls.__name__):  # noqa: ANN001
            calls.append(f"{_cls}:{name}")
            return _original(self, name)

        monkeypatch.setattr(cls, "get", spy)
    return calls


class TestPlyTextures:
    def test_trimesh_default_would_open_the_texture(
        self, tmp_path: Path, resolver_calls: list[str]
    ) -> None:
        # The control: without the reader's options, trimesh follows the comment
        # to a file next to the upload, which is the path T54 is about.
        import trimesh

        (tmp_path / "secret.png").write_bytes(b"not an image")
        ply = _ply_with_texture(tmp_path / "a.ply", "secret.png")
        trimesh.load(str(ply))
        assert any("secret.png" in c for c in resolver_calls)

    @pytest.mark.parametrize("texture", ["secret.png", "/etc/passwd", "../../x.png"])
    def test_reader_never_resolves_the_texture(
        self, tmp_path: Path, resolver_calls: list[str], texture: str
    ) -> None:
        (tmp_path / "secret.png").write_bytes(b"not an image")
        ply = _ply_with_texture(tmp_path / "a.ply", texture)
        cloud = read_ply(ply)
        assert cloud.num_points == 3
        assert resolver_calls == []

    def test_texture_pointing_at_a_fifo_does_not_hang(self, tmp_path: Path) -> None:
        os.mkfifo(tmp_path / "tex.png")
        ply = _ply_with_texture(tmp_path / "a.ply", "tex.png")
        assert read_ply(ply).num_points == 3


# ---------------------------------------------------------------------------
# T54: Xerces-C refuses DTDs in E57 XML
# ---------------------------------------------------------------------------


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def _e57_with_doctype(src: Path, dst: Path, doctype: bytes) -> Path:
    """Copy an E57 with a DOCTYPE inserted into its XML section.

    E57 stores data in 1024-byte pages, each ending in a big-endian CRC-32C of
    its first 1020 bytes. The XML is rewritten at the end of the logical stream
    and the header's XML offset, XML length and file length updated to match.
    """
    raw = src.read_bytes()
    fields = struct.unpack("<8sIIQQQQ", raw[:48])
    sig, major, minor, phys_len, xml_off, xml_len, page = fields
    logical = bytearray(b"".join(raw[i : i + 1020] for i in range(0, phys_len, 1024)))
    start = (xml_off // 1024) * 1020 + xml_off % 1024
    xml = bytes(logical[start : start + xml_len])
    decl_end = xml.index(b"?>") + 2
    new_xml = xml[:decl_end] + b"\n" + doctype + xml[decl_end:]
    new_start = len(logical)
    logical += new_xml
    logical += b"\0" * ((-len(logical)) % 1020)
    pages = len(logical) // 1020
    new_off = (new_start // 1020) * 1024 + new_start % 1020
    struct.pack_into(
        "<8sIIQQQQ",
        logical,
        0,
        sig,
        major,
        minor,
        pages * 1024,
        new_off,
        len(new_xml),
        page,
    )
    out = bytearray()
    for i in range(pages):
        chunk = bytes(logical[i * 1020 : (i + 1) * 1020])
        out += chunk + struct.pack(">I", _crc32c(chunk))
    dst.write_bytes(bytes(out))
    return dst


def _read_e57_in_subprocess(
    path: Path, xerces: str | None, timeout: float
) -> Finished | None:
    env = minimal_env()
    if xerces is not None:
        env["XERCES_DISABLE_DTD"] = xerces
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from pointcloud_detect.io_readers import read_point_cloud\n"
        "try:\n"
        f"    read_point_cloud(Path({str(path)!r}))\n"
        "except Exception as e:\n"
        "    print('refused', type(e).__name__)\n"
        "else:\n"
        "    print('loaded')\n"
    )
    try:
        return run("python", ["-c", code], env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


class TestE57Dtd:
    # The package's __init__ sets the variable; reloading it re-runs exactly that code,
    # and monkeypatch puts the environment back afterwards.
    def test_package_import_disables_dtds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XERCES_DISABLE_DTD", raising=False)
        importlib.reload(pointcloud_detect)
        assert os.environ["XERCES_DISABLE_DTD"] == "1"

    def test_an_operators_explicit_value_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XERCES_DISABLE_DTD", "0")
        importlib.reload(pointcloud_detect)
        assert os.environ["XERCES_DISABLE_DTD"] == "0"

    def test_plain_e57_still_loads(self, tmp_path: Path) -> None:
        source = write_e57(tmp_path / "a.e57")
        result = _read_e57_in_subprocess(source, None, timeout=60)
        assert result is not None
        assert result.stdout.strip() == "loaded", result.stderr

    @pytest.fixture
    def dtd_e57(self, tmp_path: Path) -> Path:
        # The DOCTYPE names a FIFO as its external DTD: a parser that tries to
        # read it blocks, which is the hang an uploaded E57 could cause.
        fifo = tmp_path / "dtd.fifo"
        os.mkfifo(fifo)
        source = write_e57(tmp_path / "a.e57")
        return _e57_with_doctype(
            source,
            tmp_path / "dtd.e57",
            f'<!DOCTYPE e57Root SYSTEM "file://{fifo}">'.encode(),
        )

    def test_e57_with_external_dtd_is_refused_without_reading_it(
        self, dtd_e57: Path
    ) -> None:
        result = _read_e57_in_subprocess(dtd_e57, None, timeout=60)
        assert result is not None, "the reader blocked on the DTD"
        assert result.stdout.strip().startswith("refused"), result.stderr

    def test_control_without_the_setting_xerces_opens_the_dtd(
        self, dtd_e57: Path
    ) -> None:
        # Proves the file above exercises the DTD path: with DTDs allowed, the
        # bundled Xerces-C opens the FIFO and blocks.
        assert _read_e57_in_subprocess(dtd_e57, "0", timeout=5) is None


# ---------------------------------------------------------------------------
# T12: LAS headers are checked against the file before allocation
# ---------------------------------------------------------------------------

# LAS 1.2: the point count is a uint32 at byte 107 of the header.
_LAS12_POINT_COUNT_OFFSET = 107


def _las_declaring(path: Path, count: int) -> Path:
    write_las(path, n=50)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<I", raw, _LAS12_POINT_COUNT_OFFSET, count)
    path.write_bytes(bytes(raw))
    return path


class TestLasHeaderSize:
    def test_inflated_point_count_is_refused(self, tmp_path: Path) -> None:
        path = _las_declaring(tmp_path / "a.las", 2**31)
        with pytest.raises(ValueError, match="declares"):
            read_las(path)

    def test_one_point_too_many_is_refused(self, tmp_path: Path) -> None:
        path = _las_declaring(tmp_path / "a.las", 51)
        with pytest.raises(ValueError, match="declares"):
            read_las(path)

    def test_the_sidecar_conversion_checks_it_too(self, tmp_path: Path) -> None:
        from pointcloud_detect.worker.parser_sidecar import convert

        path = _las_declaring(tmp_path / "a.las", 2**31)
        with pytest.raises(ValueError, match="declares"):
            convert(path, tmp_path / "points.npy", None)
        assert not (tmp_path / "points.npy").exists()

    def test_honest_las_and_laz_read_in_chunks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        las = write_las(tmp_path / "a.las", n=500)
        laz = write_las(tmp_path / "a.laz", n=500)
        monkeypatch.setattr(io_readers, "LAS_READ_CHUNK", 64)
        a, b = read_las(las), read_las(laz)
        assert a.num_points == b.num_points == 500
        np.testing.assert_allclose(a.xyz, b.xyz)
        assert a.rgb is not None and b.rgb is not None
        np.testing.assert_array_equal(a.rgb, b.rgb)


# ---------------------------------------------------------------------------
# T53: whole-load-only formats are refused above the budget
# ---------------------------------------------------------------------------


class TestWholeLoadBudget:
    @pytest.mark.parametrize("writer", [write_ply, write_bin])
    def test_file_larger_than_budget_is_refused(self, tmp_path: Path, writer) -> None:
        name = "a.ply" if writer is write_ply else "a.bin"
        path = writer(tmp_path / name)
        fmt = detect_format(path)
        tiny_gb = (path.stat().st_size - 1) / 1e9
        with pytest.raises(ValueError, match="cannot be streamed"):
            check_whole_load_file_size(path, fmt, tiny_gb)
        check_whole_load_file_size(path, fmt, (path.stat().st_size + 10) / 1e9)

    def test_streamable_formats_are_never_refused_here(self, tmp_path: Path) -> None:
        path = write_las(tmp_path / "a.las")
        check_whole_load_file_size(path, "las", 1e-9)

    def test_plan_ingest_refuses_before_loading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_ply(tmp_path / "a.ply")
        config = PipelineConfig()
        config.tiling.memory_budget_gb = 1e-6
        monkeypatch.setattr(
            io_readers, "read_ply", lambda *a, **k: pytest.fail("must not load")
        )
        with pytest.raises(ValueError, match="cannot be streamed"):
            _plan_ingest(path, config)


# ---------------------------------------------------------------------------
# The canonical format
# ---------------------------------------------------------------------------


def _canonical_of(source: Path, dest: Path) -> Path:
    cloud = read_point_cloud(source)
    writer = CanonicalWriter(dest, cloud.rgb is not None, cloud.intensity is not None)
    # Several appends, as the streaming conversion does.
    for start in range(0, cloud.num_points, 97):
        end = start + 97
        writer.append(
            cloud.xyz[start:end],
            None if cloud.rgb is None else cloud.rgb[start:end],
            None if cloud.intensity is None else cloud.intensity[start:end],
        )
    writer.close()
    return dest


class TestCanonical:
    @pytest.mark.parametrize("max_points", [None, 10, 333, 1000, 5000])
    def test_subsampling_matches_read_las(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_points: int | None
    ) -> None:
        las = write_las(tmp_path / "a.las", n=1000)
        canonical = _canonical_of(las, tmp_path / "a.npy")
        monkeypatch.setattr(io_readers, "CANONICAL_READ_CHUNK", 128)
        expected = read_las(las, max_points=max_points)
        got = read_canonical(canonical, max_points=max_points)
        np.testing.assert_allclose(got.xyz, expected.xyz)
        assert got.rgb is not None and expected.rgb is not None
        np.testing.assert_array_equal(got.rgb, expected.rgb)
        assert got.intensity is not None and expected.intensity is not None
        np.testing.assert_allclose(got.intensity, expected.intensity)

    def test_auto_cap_matches_read_las(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        las = write_las(tmp_path / "a.las", n=1000)
        canonical = _canonical_of(las, tmp_path / "a.npy")
        monkeypatch.setattr(io_readers, "AUTO_CAP_THRESHOLD_POINTS", 500)
        monkeypatch.setattr(io_readers, "AUTO_CAP_TARGET_POINTS", 120)
        with pytest.warns(ResourceWarning):
            expected = read_las(las)
        with pytest.warns(ResourceWarning):
            got = read_canonical(canonical)
        assert got.num_points == 120
        np.testing.assert_allclose(got.xyz, expected.xyz)

    def test_chunks_cover_every_point_once(self, tmp_path: Path) -> None:
        las = write_las(tmp_path / "a.las", n=1000)
        canonical = _canonical_of(las, tmp_path / "a.npy")
        chunks = list(iter_canonical_chunks(canonical, chunk=300))
        assert [len(c[0]) for c in chunks] == [300, 300, 300, 100]
        np.testing.assert_allclose(
            np.concatenate([c[0] for c in chunks]), read_las(las).xyz
        )

    def test_empty_canonical_file(self, tmp_path: Path) -> None:
        path = tmp_path / "e.npy"
        CanonicalWriter(path, False, False).close()
        assert read_canonical(path).num_points == 0

    def test_writer_refuses_channels_it_was_not_opened_with(
        self, tmp_path: Path
    ) -> None:
        writer = CanonicalWriter(tmp_path / "a.npy", False, False)
        with pytest.raises(ValueError):
            writer.append(np.zeros((1, 3)), np.zeros((1, 3), dtype=np.uint8))
        writer.close()

    def test_writer_never_follows_or_replaces_an_existing_file(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"
        target.write_text("keep")
        (tmp_path / "a.npy").symlink_to(target)
        with pytest.raises(OSError):
            CanonicalWriter(tmp_path / "a.npy", False, False)
        assert target.read_text() == "keep"

    def test_file_truncated_after_validation_raises_not_crashes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        las = write_las(tmp_path / "a.las", n=1000)
        canonical = _canonical_of(las, tmp_path / "a.npy")
        f, header = io_readers.open_canonical(canonical)
        with f:
            os.truncate(canonical, header.data_offset + 10)
            with pytest.raises(ValueError, match="ended before"):
                io_readers._read_records(f, header.dtype, header.count)


@pytest.mark.parametrize("name", ["a.las", "a.laz"])
def test_las_intensity_survives_whole_load_and_streaming(
    tmp_path: Path, name: str
) -> None:
    # laspy's dimension_names is a generator; checking rgb first used to consume
    # it past "intensity", so both readers silently dropped the channel.
    from pointcloud_detect.streaming import iter_chunks

    path = write_las(tmp_path / name, n=100)
    whole = read_las(path)
    assert whole.intensity is not None
    streamed: list[np.ndarray] = []
    for _, _, intensity in iter_chunks(path, detect_format(path), 40):
        assert intensity is not None
        streamed.append(intensity)
    np.testing.assert_allclose(np.concatenate(streamed), whole.intensity)
