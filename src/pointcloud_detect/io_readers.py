"""Point cloud file readers for E57, LAS/LAZ, PLY, KITTI .bin and the canonical format.

The canonical format is what the parser sidecar hands back to the worker (T11): a
NumPy ``.npy`` file holding one structured record per point. It exists so that a
worker running with credentials never has to run a native parser over an uploaded
file. It is read here without pickle support, without memory mapping and without
following symlinks, because in the sidecar deployment it is written by the process
that has just parsed untrusted input.
"""

import io
import os
import stat
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib import format as npy_format

from pointcloud_detect.models import PointCloud

MAGIC_E57 = b"ASTM-E57"
MAGIC_LAS = b"LASF"
MAGIC_PLY_ASCII = b"ply"
MAGIC_NPY = npy_format.MAGIC_PREFIX

# KITTI velodyne .bin: 4 float32 per point (x, y, z, reflectance)
KITTI_POINT_BYTES = 16

# The reader for a file is chosen by its extension, which the API has validated
# against ALLOWED_EXTENSIONS. ".npy" is the canonical format; the API does not accept
# it for upload, so on the service path only the parser sidecar produces it.
FORMAT_BY_EXTENSION = {
    ".e57": "e57",
    ".las": "las",
    ".laz": "laz",
    ".ply": "ply",
    ".bin": "kitti_bin",
    ".npy": "canonical",
}

# Byte 104 of a LAS header is the point data record format. LASzip marks compressed
# point data by setting bit 7 and clearing bit 6; this is the test laspy applies
# (laspy/_compression/format.py) to decide whether the LAZ backend reads the file.
_LAS_POINT_FORMAT_OFFSET = 104

# Points per laspy chunk when whole-loading a LAS or LAZ. It bounds the one
# allocation that laspy sizes from the file, see read_las.
LAS_READ_CHUNK = 5_000_000


class UnsupportedFormatError(ValueError):
    """The file's extension is not one this pipeline reads."""


class FormatMismatchError(ValueError):
    """The file's content does not match the format its extension names."""


def _open_regular(file_path: Path, *, nofollow: bool = False) -> io.BufferedReader:
    """Open a file for reading, refusing anything that is not a regular file.

    O_NONBLOCK makes the open itself return for a FIFO instead of blocking until a
    writer appears, so a FIFO or device named like a point cloud is refused rather
    than hanging the reader. It has no effect on reads from a regular file.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(file_path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Not a regular file: {file_path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _sniff_format(head: bytes) -> str | None:
    """The format the leading bytes of a file identify, or None for no known magic."""
    if head[:8] == MAGIC_E57:
        return "e57"
    if head[:4] == MAGIC_LAS:
        if len(head) > _LAS_POINT_FORMAT_OFFSET:
            point_format = head[_LAS_POINT_FORMAT_OFFSET]
            if point_format & 0x80 and not point_format & 0x40:
                return "laz"
        return "las"
    if head[:3] == MAGIC_PLY_ASCII:
        return "ply"
    if head[: len(MAGIC_NPY)] == MAGIC_NPY:
        return "canonical"
    return None


def detect_format(file_path: Path) -> str:
    """Choose the reader from the extension, and refuse content that disagrees.

    The magic bytes used to take precedence over the extension, so a file uploaded
    under any allowed extension could reach any parser: a ".ply" that began with
    "ASTM-E57" went to libE57 and Xerces-C (T54). Now the validated extension alone
    decides, and the magic only has to agree with it. A ".bin" has no magic of its
    own, so it is refused when it carries another format's. A ".las" holding
    LASzip-compressed points is refused too, so that only ".laz" reaches the LAZ
    decompressor; a ".laz" whose points are not compressed is read as plain LAS.
    """
    suffix = Path(file_path).suffix.lower()
    fmt = FORMAT_BY_EXTENSION.get(suffix)
    if fmt is None:
        raise UnsupportedFormatError(
            f"Unsupported point cloud extension {suffix!r} for {file_path}. "
            f"Supported: {', '.join(sorted(FORMAT_BY_EXTENSION))}"
        )

    with _open_regular(file_path) as f:
        head = f.read(_LAS_POINT_FORMAT_OFFSET + 1)
    found = _sniff_format(head)

    if fmt == "kitti_bin":
        agrees = found is None
    elif fmt == "laz":
        agrees = found in ("las", "laz")
    else:
        agrees = found == fmt
    if not agrees:
        raise FormatMismatchError(
            f"{file_path} has the extension {suffix!r} but its content "
            f"{'is ' + found if found else 'carries no ' + fmt + ' signature'}. "
            "Refusing to parse it as either."
        )
    return fmt


# Above this many points the readers auto-subsample rather than materialise the
# whole file. The routing decision in pipeline._plan_ingest must know this number:
# a file between this threshold and the streaming threshold would otherwise be
# whole-loaded and then have ~90% of its points discarded behind a ResourceWarning.
AUTO_CAP_THRESHOLD_POINTS = 100_000_000
AUTO_CAP_TARGET_POINTS = 20_000_000


def e57_buffer_capacity(
    declared_points: int, max_points: int | None, file_size_bytes: int
) -> int:
    """Points to allocate buffers for when whole-loading an E57.

    ``declared_points`` comes from the file's own header, so it is untrusted: a few
    kilobytes can declare 99 million points and have us allocate buffers for all of
    them before a single point is read. No real scan stores a point in less than one
    byte, so the file's size is an upper bound on how many it can hold. Clamping to
    it costs nothing for honest files and bounds the allocation by what was actually
    uploaded.
    """
    capacity = min(declared_points, max_points) if max_points else declared_points
    return max(0, min(capacity, file_size_bytes))


def read_e57(file_path: Path, max_points: int | None = None) -> PointCloud:
    """Read an E57 file with memory-safe capacity-limited reading.

    Args:
        max_points: Maximum points to load. For files >100M points,
                    defaults to 20M to prevent OOM crashes.
    """
    try:
        import pye57

        # libe57 is the SWIG-generated extension module pye57 exposes at runtime but
        # omits from its type stubs, so this is an upstream stub gap rather than a
        # missing dependency -- pye57 itself resolves fine on the line above.
        from pye57 import libe57  # pyright: ignore[reportAttributeAccessIssue]
    except ImportError as e:
        raise ImportError(
            "pye57 is required for E57 support. Install with: uv add pye57"
        ) from e

    e57 = pye57.E57(str(file_path))
    header = e57.get_header(0)
    total_points = header.point_count

    # Safety cap for large files
    if max_points is None and total_points > AUTO_CAP_THRESHOLD_POINTS:
        max_points = AUTO_CAP_TARGET_POINTS
        warnings.warn(
            f"E57 has {total_points:,} points. Auto-capping at {max_points:,}. "
            f"Use --max-points to override.",
            ResourceWarning,
            stacklevel=2,
        )

    capacity = e57_buffer_capacity(
        total_points, max_points, Path(file_path).stat().st_size
    )

    # Determine available fields
    point_fields = header.point_fields
    has_rgb = all(f in point_fields for f in ["colorRed", "colorGreen", "colorBlue"])
    has_intensity = "intensity" in point_fields

    fields_to_read = ["cartesianX", "cartesianY", "cartesianZ"]
    if has_rgb:
        fields_to_read += ["colorRed", "colorGreen", "colorBlue"]
    if has_intensity:
        fields_to_read.append("intensity")

    # Create capacity-limited buffers - pye57 will read at most `capacity` points
    data = {}
    buffers = libe57.VectorSourceDestBuffer()
    for field in fields_to_read:
        np_array, buffer = e57.make_buffer(field, capacity)
        data[field] = np_array
        buffers.append(buffer)

    count = header.points.reader(buffers).read()

    xyz = np.column_stack(
        [
            data["cartesianX"][:count].copy(),
            data["cartesianY"][:count].copy(),
            data["cartesianZ"][:count].copy(),
        ]
    ).astype(np.float64)

    rgb = None
    if has_rgb:
        rgb = np.column_stack(
            [
                data["colorRed"][:count].copy(),
                data["colorGreen"][:count].copy(),
                data["colorBlue"][:count].copy(),
            ]
        ).astype(np.uint8)

    intensity = None
    if has_intensity:
        intensity = data["intensity"][:count].copy().astype(np.float32)

    return PointCloud(xyz=xyz, rgb=rgb, intensity=intensity, source_file=str(file_path))


def check_las_point_bytes(header: Any, file_size: int, file_path: Path) -> None:
    """Refuse an uncompressed LAS whose header declares more point bytes than it has.

    ``laspy.read`` asks for every declared point at once, and its uncompressed reader
    allocates ``point_count * point_size`` bytes before reading any, so a few
    hundred bytes of header could ask for terabytes (T12). For uncompressed data the
    header, the record size and the file size must agree, so the check is exact and
    costs nothing. LAZ has no such relation; its readers go chunk by chunk instead.
    """
    if getattr(header, "are_points_compressed", False):
        return
    declared = int(header.point_count)
    record = int(header.point_format.size)
    offset = int(header.offset_to_point_data)
    needed = offset + declared * record
    if needed > file_size:
        raise ValueError(
            f"{file_path} declares {declared:,} points of {record} bytes from offset "
            f"{offset:,}, which needs {needed:,} bytes, but the file has "
            f"{file_size:,}. The header is corrupt or the file is truncated."
        )


def read_las(file_path: Path, max_points: int | None = None) -> PointCloud:
    """Read a LAS/LAZ file and return a unified PointCloud.

    Opens with ``laspy.open`` rather than ``laspy.read`` so the header can be checked
    against the file before anything is allocated from it, then reads in bounded
    chunks. For LAZ the chunking is what bounds memory by the data actually
    decompressed rather than by the header's point count.
    """
    try:
        import laspy
    except ImportError as e:
        raise ImportError(
            "laspy is required for LAS/LAZ support. Install with: uv add 'laspy[lazrs]'"
        ) from e

    file_size = Path(file_path).stat().st_size
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    zs: list[np.ndarray] = []
    rs: list[np.ndarray] = []
    gs: list[np.ndarray] = []
    bs: list[np.ndarray] = []
    iss: list[np.ndarray] = []
    with laspy.open(str(file_path)) as reader:
        header = reader.header
        check_las_point_bytes(header, file_size, file_path)
        # A set: dimension_names is a generator, and the rgb check below would
        # consume it past "intensity" before that is looked for.
        dims = set(header.point_format.dimension_names)
        has_rgb = all(d in dims for d in ("red", "green", "blue"))
        has_intensity = "intensity" in dims
        for pts in reader.chunk_iterator(LAS_READ_CHUNK):
            xs.append(np.asarray(pts.x, dtype=np.float64))
            ys.append(np.asarray(pts.y, dtype=np.float64))
            zs.append(np.asarray(pts.z, dtype=np.float64))
            if has_rgb:
                rs.append(np.asarray(pts.red) >> 8)
                gs.append(np.asarray(pts.green) >> 8)
                bs.append(np.asarray(pts.blue) >> 8)
            if has_intensity:
                iss.append(np.asarray(pts.intensity, dtype=np.float32))

    def _cat(parts: list[np.ndarray], dtype: type) -> np.ndarray:
        return np.concatenate(parts) if parts else np.zeros(0, dtype=dtype)

    x, y, z = _cat(xs, np.float64), _cat(ys, np.float64), _cat(zs, np.float64)
    total = len(x)

    # Safety cap for large files
    if max_points is None and total > AUTO_CAP_THRESHOLD_POINTS:
        max_points = AUTO_CAP_TARGET_POINTS
        warnings.warn(
            f"LAS/LAZ has {total:,} points. Auto-capping at {max_points:,}. "
            f"Use --max-points to override.",
            ResourceWarning,
            stacklevel=2,
        )

    idx = None
    if max_points and total > max_points:
        idx = np.random.default_rng(42).choice(total, max_points, replace=False)
        idx.sort()
        x, y, z = x[idx], y[idx], z[idx]

    xyz = np.column_stack([x, y, z]).astype(np.float64)

    rgb = None
    if has_rgb:
        r, g, b = _cat(rs, np.uint16), _cat(gs, np.uint16), _cat(bs, np.uint16)
        if idx is not None:
            r, g, b = r[idx], g[idx], b[idx]
        rgb = np.column_stack([r, g, b]).astype(np.uint8)

    intensity = None
    if has_intensity:
        i = _cat(iss, np.float32)
        if idx is not None:
            i = i[idx]
        intensity = i

    return PointCloud(xyz=xyz, rgb=rgb, intensity=intensity, source_file=str(file_path))


def read_ply(file_path: Path, max_points: int | None = None) -> PointCloud:
    """Read a PLY file and return a unified PointCloud."""
    try:
        import trimesh
        from trimesh.resolvers import ZipResolver
    except ImportError as e:
        raise ImportError(
            "trimesh is required for PLY support. Install with: uv add trimesh"
        ) from e

    # Given a path (or a file object with a name), trimesh builds a resolver for the
    # file's directory and, unless told otherwise, opens the file named by a
    # "comment TextureFile <name>" header line, joined to that directory with no
    # confinement: an uploaded PLY could make the worker read any path, /dev/zero
    # included (T54). skip_materials stops the PLY loader asking for the texture at
    # all (trimesh/exchange/ply.py, load_ply). The empty in-memory resolver is the
    # second lock: if anything did ask, there is no filesystem behind it. The format
    # is fixed rather than guessed from the name.
    with _open_regular(file_path) as f:
        cloud = trimesh.load(
            f,
            file_type="ply",
            resolver=ZipResolver(),
            process=False,
            skip_materials=True,
        )

    # trimesh.load is typed as returning the Geometry base class, which declares
    # neither vertices nor colors -- those live on the concrete PointCloud/Trimesh
    # subclasses, and which one comes back depends on the file. getattr keeps the
    # original duck-typed check while giving the type checker something it can follow,
    # so this needs no suppression.
    vertices = getattr(cloud, "vertices", None)
    if vertices is None:
        raise ValueError(f"PLY file does not contain vertices: {file_path}")
    xyz = np.asarray(vertices, dtype=np.float64)

    # Safety cap for large files
    total = len(xyz)
    if max_points is None and total > AUTO_CAP_THRESHOLD_POINTS:
        max_points = AUTO_CAP_TARGET_POINTS
        warnings.warn(
            f"PLY has {total:,} points. Auto-capping at {max_points:,}. "
            f"Use --max-points to override.",
            ResourceWarning,
            stacklevel=2,
        )

    idx = None
    if max_points and total > max_points:
        idx = np.random.default_rng(42).choice(total, max_points, replace=False)
        idx.sort()
        xyz = xyz[idx]

    rgb = None
    raw_colors = getattr(cloud, "colors", None)
    if raw_colors is not None:
        colors = np.asarray(raw_colors)
        if colors.ndim == 2 and colors.shape[1] >= 3:
            rgb = colors[:, :3].astype(np.uint8)
            if idx is not None:
                rgb = rgb[idx]

    return PointCloud(xyz=xyz, rgb=rgb, source_file=str(file_path))


def read_kitti_bin(file_path: Path) -> PointCloud:
    """Read a .bin point cloud file (KITTI N×4 or nuScenes N×5 float32)."""
    raw = np.fromfile(file_path, dtype=np.float32)
    # Auto-detect: nuScenes has 5 floats per point, KITTI has 4
    if raw.size % 5 == 0 and raw.size % 4 != 0:
        data = raw.reshape(-1, 5)
    elif raw.size % 5 == 0 and raw.size % 4 == 0:
        # Ambiguous — check if file path hints at nuScenes
        if "nuscenes" in str(file_path).lower() or "LIDAR_TOP" in str(file_path):
            data = raw.reshape(-1, 5)
        else:
            data = raw.reshape(-1, 4)
    else:
        data = raw.reshape(-1, 4)

    xyz = data[:, :3].astype(np.float64)
    intensity = data[:, 3]
    return PointCloud(
        xyz=xyz,
        intensity=intensity,
        source_file=str(file_path),
    )


# ---------------------------------------------------------------------------
# Canonical format: the parser sidecar's output (T11).
# ---------------------------------------------------------------------------

CANONICAL_FILE_FORMAT = "pcd-canonical-v1"

# The widest record is 31 bytes: three float64 coordinates, three uint8 colours and
# a float32 intensity, packed with no padding. Narrower records drop the channels
# the source did not have. The four layouts below are the only ones accepted.
_XYZ_FIELDS = [("x", "<f8"), ("y", "<f8"), ("z", "<f8")]
_RGB_FIELDS = [("red", "u1"), ("green", "u1"), ("blue", "u1")]
_INTENSITY_FIELDS = [("intensity", "<f4")]

# A canonical file declaring more points than this is refused. At 31 bytes a point
# it is ~93 GB, which is already beyond what the worker's scratch volume can hold
# next to the input; anything larger is a corrupt or hostile header, not a scene.
MAX_CANONICAL_POINTS = 3_000_000_000

# numpy's own default ceiling for an .npy header. The canonical header is 256 bytes.
_MAX_NPY_HEADER_BYTES = 10_000

# Records read per chunk when walking a canonical file (~150 MB at 31 bytes).
CANONICAL_READ_CHUNK = 5_000_000


def canonical_dtype(has_rgb: bool, has_intensity: bool) -> np.dtype:
    """The record layout for a canonical file with the given channels."""
    fields = list(_XYZ_FIELDS)
    if has_rgb:
        fields += _RGB_FIELDS
    if has_intensity:
        fields += _INTENSITY_FIELDS
    return np.dtype(fields)


_CANONICAL_DTYPES = tuple(
    canonical_dtype(rgb, intensity)
    for rgb in (False, True)
    for intensity in (False, True)
)


@dataclass(frozen=True)
class CanonicalHeader:
    """What a validated canonical header says, and where its records start."""

    dtype: np.dtype
    count: int
    data_offset: int

    @property
    def has_rgb(self) -> bool:
        return "red" in (self.dtype.names or ())

    @property
    def has_intensity(self) -> bool:
        return "intensity" in (self.dtype.names or ())


def open_canonical(file_path: Path) -> tuple[io.BufferedReader, CanonicalHeader]:
    """Open a canonical file and validate its header; the caller closes the file.

    Every structural fact is checked before any record is read: a regular file
    that is not a symlink and has one link, an .npy version 1 or 2 header of at most
    numpy's default size, one of the four record layouts exactly (so no object or
    pickled field can appear), a one-dimensional C-order shape, a point count within
    MAX_CANONICAL_POINTS, and a file size equal to the header plus the declared
    records, so a short or padded file is refused rather than half read.
    """
    try:
        f = _open_regular(file_path, nofollow=True)
    except OSError as e:
        raise ValueError(f"Cannot open canonical file {file_path}: {e}") from e
    try:
        st = os.fstat(f.fileno())
        if st.st_nlink != 1:
            raise ValueError(f"{file_path} has {st.st_nlink} links; expected 1")
        try:
            version = npy_format.read_magic(f)
            if version == (1, 0):
                shape, fortran, dtype = npy_format.read_array_header_1_0(
                    f, max_header_size=_MAX_NPY_HEADER_BYTES
                )
            elif version == (2, 0):
                shape, fortran, dtype = npy_format.read_array_header_2_0(
                    f, max_header_size=_MAX_NPY_HEADER_BYTES
                )
            else:
                raise ValueError(f"unsupported .npy version {version}")
        except (ValueError, SyntaxError, TypeError, OverflowError) as e:
            raise ValueError(f"{file_path} is not a valid .npy file: {e}") from e

        if dtype.hasobject or not any(dtype == d for d in _CANONICAL_DTYPES):
            raise ValueError(f"{file_path} has an unexpected record layout {dtype}")
        if fortran or not (isinstance(shape, tuple) and len(shape) == 1):
            raise ValueError(f"{file_path} has an unexpected shape {shape!r}")
        count = shape[0]
        if not isinstance(count, int) or count < 0 or count > MAX_CANONICAL_POINTS:
            raise ValueError(f"{file_path} declares an invalid point count {count!r}")
        offset = f.tell()
        expected = offset + count * dtype.itemsize
        if st.st_size != expected:
            raise ValueError(
                f"{file_path} is {st.st_size:,} bytes; its header implies {expected:,}"
            )
        return f, CanonicalHeader(dtype=dtype, count=count, data_offset=offset)
    except BaseException:
        f.close()
        raise


def _read_records(f: io.BufferedReader, dtype: np.dtype, n: int) -> np.ndarray:
    """Read exactly ``n`` records into a fresh array, or raise if the file is short.

    Plain reads, not a memory map: if the file shrinks after validation a read
    comes back short and raises here, where a mapped page past the new end would
    kill the whole process with SIGBUS.
    """
    records = np.empty(n, dtype=dtype)
    view = memoryview(records.view(np.uint8))
    filled = 0
    while filled < len(view):
        got = f.readinto(view[filled:])
        if not got:
            raise ValueError("canonical file ended before its declared point count")
        filled += got
    return records


def _record_columns(
    records: np.ndarray,
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
    names = records.dtype.names or ()
    xyz = np.column_stack([records["x"], records["y"], records["z"]])
    rgb = (
        np.column_stack([records["red"], records["green"], records["blue"]])
        if "red" in names
        else None
    )
    intensity = records["intensity"].copy() if "intensity" in names else None
    return xyz, rgb, intensity


def canonical_header(file_path: Path) -> CanonicalHeader:
    """Validate a canonical file and return its header."""
    f, header = open_canonical(file_path)
    f.close()
    return header


def iter_canonical_chunks(
    file_path: Path, chunk: int = CANONICAL_READ_CHUNK
) -> Iterator[tuple[np.ndarray, np.ndarray | None, np.ndarray | None]]:
    """Yield (xyz, rgb, intensity) blocks of at most ``chunk`` points."""
    f, header = open_canonical(file_path)
    with f:
        remaining = header.count
        while remaining > 0:
            n = min(chunk, remaining)
            yield _record_columns(_read_records(f, header.dtype, n))
            remaining -= n


def read_canonical(file_path: Path, max_points: int | None = None) -> PointCloud:
    """Whole-load a canonical file, subsampling exactly as read_las does.

    The points to keep are chosen from the header's count before any record is
    read, with the same generator and seed read_las uses, so the result is the one
    the in-process path would have produced from the original file. Only the kept
    points are held, one chunk of records at a time, which bounds memory by
    max_points rather than by whatever count the parser wrote.
    """
    f, header = open_canonical(file_path)
    total = header.count

    if max_points is None and total > AUTO_CAP_THRESHOLD_POINTS:
        max_points = AUTO_CAP_TARGET_POINTS
        warnings.warn(
            f"Point cloud has {total:,} points. Auto-capping at {max_points:,}. "
            f"Use --max-points to override.",
            ResourceWarning,
            stacklevel=2,
        )
    idx = None
    if max_points and total > max_points:
        idx = np.random.default_rng(42).choice(total, max_points, replace=False)
        idx.sort()
    kept = total if idx is None else len(idx)

    xyz = np.empty((kept, 3), dtype=np.float64)
    rgb = np.empty((kept, 3), dtype=np.uint8) if header.has_rgb else None
    intensity = np.empty(kept, dtype=np.float32) if header.has_intensity else None
    with f:
        pos = 0
        out = 0
        while pos < total:
            n = min(CANONICAL_READ_CHUNK, total - pos)
            records = _read_records(f, header.dtype, n)
            if idx is not None:
                lo, hi = np.searchsorted(idx, (pos, pos + n))
                records = records[idx[lo:hi] - pos]
            cx, crgb, ci = _record_columns(records)
            m = len(cx)
            xyz[out : out + m] = cx
            if rgb is not None and crgb is not None:
                rgb[out : out + m] = crgb
            if intensity is not None and ci is not None:
                intensity[out : out + m] = ci
            pos += n
            out += m

    return PointCloud(xyz=xyz, rgb=rgb, intensity=intensity, source_file=str(file_path))


class CanonicalWriter:
    """Write a canonical file incrementally, so a large scene never sits in memory.

    The header is written first with a count of zero and rewritten with the real
    count on close. numpy pads the shape field so that the header length does not
    depend on the count (``GROWTH_AXIS_MAX_DIGITS`` in numpy.lib.format), which is
    what allows the rewrite in place; close() checks it held.
    """

    def __init__(self, file_path: Path, has_rgb: bool, has_intensity: bool) -> None:
        self.dtype = canonical_dtype(has_rgb, has_intensity)
        self.count = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        # Group-readable: in the sidecar deployment the worker reads it by group.
        self._f = os.fdopen(os.open(file_path, flags, 0o640), "wb")
        self._write_header(0)
        self._data_offset = self._f.tell()

    def _write_header(self, count: int) -> None:
        npy_format.write_array_header_1_0(
            self._f,
            {
                "descr": npy_format.dtype_to_descr(self.dtype),
                "fortran_order": False,
                "shape": (count,),
            },
        )

    def append(
        self,
        xyz: np.ndarray,
        rgb: np.ndarray | None = None,
        intensity: np.ndarray | None = None,
    ) -> None:
        names = self.dtype.names or ()
        if ("red" in names) != (rgb is not None) or ("intensity" in names) != (
            intensity is not None
        ):
            raise ValueError("channels differ from the ones this file was opened with")
        n = len(xyz)
        if self.count + n > MAX_CANONICAL_POINTS:
            raise MemoryError(
                f"more than {MAX_CANONICAL_POINTS:,} points; refusing to write more"
            )
        records = np.empty(n, dtype=self.dtype)
        records["x"], records["y"], records["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        if rgb is not None:
            records["red"], records["green"], records["blue"] = (
                rgb[:, 0],
                rgb[:, 1],
                rgb[:, 2],
            )
        if intensity is not None:
            records["intensity"] = intensity
        self._f.write(memoryview(records.view(np.uint8)))
        self.count += n

    def close(self) -> None:
        self._f.seek(0)
        self._write_header(self.count)
        if self._f.tell() != self._data_offset:
            self._f.close()
            raise RuntimeError("canonical header changed length on rewrite")
        self._f.close()


def read_point_cloud(file_path: Path, max_points: int | None = None) -> PointCloud:
    """Read a point cloud file, choosing the reader from its extension."""
    fmt = detect_format(file_path)

    if fmt == "e57":
        return read_e57(file_path, max_points=max_points)
    if fmt in ("las", "laz"):
        return read_las(file_path, max_points=max_points)
    if fmt == "ply":
        return read_ply(file_path, max_points=max_points)
    if fmt == "kitti_bin":
        return read_kitti_bin(file_path)
    if fmt == "canonical":
        return read_canonical(file_path, max_points=max_points)

    raise UnsupportedFormatError(f"Unsupported format: {fmt}")
