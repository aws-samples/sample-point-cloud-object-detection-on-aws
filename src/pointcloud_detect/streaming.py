"""Two-pass streaming ingest for large LAS/LAZ and E57 scenes (NFR-1.2).

Whole-cloud ingestion loads every point before tiling, so peak memory scales with the
scene. For scenes whose footprint exceeds the memory budget, this module instead:

  Pass 1  walk the file once for true bounds, an exact point count, and a coarse
          occupancy grid; validate the header against what the data actually says.
  Pass 2  read the planned leaves in budget-sized batches, one file walk per batch,
          so peak memory is bounded by a batch and total I/O by the budget.

IMPORTANT — coordinate frames: tile extents are planned and points are selected in the
FILE's native frame (the frame on disk). The transform to Z-up (and downsample /
channel selection) is applied per tile AFTER reading, exactly as the
whole-cloud path applies it. Planning in file space is required because the chunked
readers return file-space coordinates; transforming would need all points at once,
which is the very thing being avoided. Tile extents are mapped into the output frame
on the way out so a tile's extent and its points always agree.

Only LAS/LAZ (laspy chunked iterator), E57 (pye57 capacity buffers) and the canonical
format the parser sidecar writes (io_readers.iter_canonical_chunks) are streamable.
PLY / KITTI .bin are not and must use the whole-cloud path.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.io_readers import canonical_header, iter_canonical_chunks
from pointcloud_detect.models import PointCloud, Tile
from pointcloud_detect.preprocessing import COORD_TRANSFORMS, preprocess

STREAMABLE_FORMATS = frozenset({"las", "laz", "e57", "canonical"})

# Points read per chunk from the file in Pass 2. Bounds the transient read buffer
# independently of tile size; a tile is assembled from the chunk points that fall
# inside its extent.
DEFAULT_READ_CHUNK = 5_000_000


def is_streamable(fmt: str) -> bool:
    """Whether a detected format supports chunked spatial reads."""
    return fmt in STREAMABLE_FORMATS


# ---------------------------------------------------------------------------
# Pass 1 — cheap scene scan (bounds + count), no full materialization.
# ---------------------------------------------------------------------------


def scan_bounds_las(file_path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """Scene bounds + point count for LAS/LAZ from the header (O(1), no point read)."""
    import laspy

    with laspy.open(str(file_path)) as reader:
        h = reader.header
        mins = np.array([h.x_min, h.y_min, h.z_min], dtype=np.float64)
        maxs = np.array([h.x_max, h.y_max, h.z_max], dtype=np.float64)
        return mins, maxs, int(h.point_count)


def scan_bounds_e57(file_path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """Scene bounds + point count for E57, from the header when it carries bounds.

    Falls back to a full chunked coordinate scan when the header lacks bounds.
    """
    import pye57

    e57 = pye57.E57(str(file_path))
    header = e57.get_header(0)
    count = int(header.point_count)

    # header.xMinimum and friends return plain floats. The obvious-looking
    # np.array([header.cartesianBounds["xMinimum"], ...]) does NOT: the subscripts
    # are libe57.FloatNode objects and numpy raises
    #   TypeError: float() argument must be ... not 'pye57.libe57.FloatNode'
    # which a bare `except` previously swallowed, sending every E57 down the
    # fallback path below. Use the float accessors, and catch only what these can
    # actually raise so a real bug cannot hide here again.
    try:
        mins = np.array(
            [header.xMinimum, header.yMinimum, header.zMinimum], dtype=np.float64
        )
        maxs = np.array(
            [header.xMaximum, header.yMaximum, header.zMaximum], dtype=np.float64
        )
        if np.all(np.isfinite(mins)) and np.all(np.isfinite(maxs)):
            return mins, maxs, count
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    # Fallback: scan every point in bounded chunks. This must read the WHOLE file —
    # bounds taken from only the first chunk would exclude every later point from
    # all planned tile extents, silently dropping most of the scene.
    lo = np.full(3, np.inf, dtype=np.float64)
    hi = np.full(3, -np.inf, dtype=np.float64)
    seen = 0
    for block, n in _iter_e57_blocks(
        e57, header, ("cartesianX", "cartesianY", "cartesianZ"), DEFAULT_READ_CHUNK
    ):
        xyz = np.column_stack(
            [
                block["cartesianX"][:n],
                block["cartesianY"][:n],
                block["cartesianZ"][:n],
            ]
        )
        lo = np.minimum(lo, xyz.min(axis=0))
        hi = np.maximum(hi, xyz.max(axis=0))
        seen += n

    if seen == 0:
        raise ValueError(f"E57 file contains no readable points: {file_path}")
    return lo, hi, count


def scan_bounds_canonical(file_path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """Scene bounds + point count for a canonical file.

    The count is the validated header's. The format stores no bounds, so they are
    computed with one chunked pass; nothing the parser claimed about them is used.
    """
    count = canonical_header(file_path).count
    lo = np.full(3, np.inf, dtype=np.float64)
    hi = np.full(3, -np.inf, dtype=np.float64)
    for xyz, _rgb, _intensity in iter_canonical_chunks(file_path, DEFAULT_READ_CHUNK):
        lo = np.minimum(lo, xyz.min(axis=0))
        hi = np.maximum(hi, xyz.max(axis=0))
    if count == 0:
        # Like an empty LAS header: no extent, and nothing for the caller to stream.
        return np.zeros(3), np.zeros(3), 0
    return lo, hi, count


def scan_bounds(file_path: Path, fmt: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Dispatch a Pass-1 bounds/count scan by format."""
    if fmt in ("las", "laz"):
        return scan_bounds_las(file_path)
    if fmt == "e57":
        return scan_bounds_e57(file_path)
    if fmt == "canonical":
        return scan_bounds_canonical(file_path)
    raise ValueError(f"scan_bounds: format not streamable: {fmt}")


# ---------------------------------------------------------------------------
# Shared file walking — one code path for both passes.
# ---------------------------------------------------------------------------

ChunkT = tuple[np.ndarray, np.ndarray | None, np.ndarray | None]


def iter_chunks_las(file_path: Path, chunk: int) -> Iterator[ChunkT]:
    """Yield (xyz, rgb, intensity) blocks from a LAS/LAZ file."""
    import laspy

    with laspy.open(str(file_path)) as reader:
        # A set: dimension_names is a generator, and the rgb check below would
        # consume it past "intensity" before that is looked for, dropping the
        # intensity of every streamed LAS/LAZ.
        dims = set(reader.header.point_format.dimension_names)
        has_rgb = all(d in dims for d in ("red", "green", "blue"))
        has_intensity = "intensity" in dims

        for pts in reader.chunk_iterator(chunk):
            xyz = np.column_stack(
                [
                    np.asarray(pts.x, dtype=np.float64),
                    np.asarray(pts.y, dtype=np.float64),
                    np.asarray(pts.z, dtype=np.float64),
                ]
            )
            rgb = None
            if has_rgb:
                rgb = np.column_stack(
                    [
                        (np.asarray(pts.red) >> 8).astype(np.uint8),
                        (np.asarray(pts.green) >> 8).astype(np.uint8),
                        (np.asarray(pts.blue) >> 8).astype(np.uint8),
                    ]
                )
            intensity = (
                np.asarray(pts.intensity, dtype=np.float32) if has_intensity else None
            )
            yield xyz, rgb, intensity


def iter_chunks_e57(file_path: Path, chunk: int) -> Iterator[ChunkT]:
    """Yield (xyz, rgb, intensity) blocks from an E57 file."""
    import pye57

    e57 = pye57.E57(str(file_path))
    header = e57.get_header(0)
    fields = header.point_fields
    has_rgb = all(f in fields for f in ("colorRed", "colorGreen", "colorBlue"))
    has_intensity = "intensity" in fields

    read_fields = ["cartesianX", "cartesianY", "cartesianZ"]
    if has_rgb:
        read_fields += ["colorRed", "colorGreen", "colorBlue"]
    if has_intensity:
        read_fields.append("intensity")

    for buf, n in _iter_e57_blocks(e57, header, read_fields, chunk):
        xyz = np.column_stack(
            [buf["cartesianX"][:n], buf["cartesianY"][:n], buf["cartesianZ"][:n]]
        ).astype(np.float64)
        rgb = None
        if has_rgb:
            rgb = np.column_stack(
                [
                    buf["colorRed"][:n],
                    buf["colorGreen"][:n],
                    buf["colorBlue"][:n],
                ]
            ).astype(np.uint8)
        intensity = buf["intensity"][:n].astype(np.float32) if has_intensity else None
        yield xyz, rgb, intensity


def iter_chunks(file_path: Path, fmt: str, chunk: int) -> Iterator[ChunkT]:
    """Dispatch a chunked full-file walk by format."""
    if fmt in ("las", "laz"):
        yield from iter_chunks_las(file_path, chunk)
    elif fmt == "e57":
        yield from iter_chunks_e57(file_path, chunk)
    elif fmt == "canonical":
        yield from iter_canonical_chunks(file_path, chunk)
    else:
        raise ValueError(f"iter_chunks: format not streamable: {fmt}")


# ---------------------------------------------------------------------------
# Pass 2 — read only the points inside a region (file coordinate frame).
# ---------------------------------------------------------------------------


def read_region_las(
    file_path: Path,
    region_min: np.ndarray,
    region_max: np.ndarray,
    chunk: int = DEFAULT_READ_CHUNK,
) -> PointCloud:
    """Read only LAS/LAZ points inside [region_min, region_max], chunk by chunk.

    Peak transient memory is one chunk; the accumulated output holds only the
    in-region points.
    """
    import laspy

    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    zs: list[np.ndarray] = []
    rs: list[np.ndarray] = []
    gs: list[np.ndarray] = []
    bs: list[np.ndarray] = []
    iss: list[np.ndarray] = []
    has_rgb = False
    has_intensity = False

    with laspy.open(str(file_path)) as reader:
        point_format = reader.header.point_format
        has_rgb = all(
            d in point_format.dimension_names for d in ("red", "green", "blue")
        )
        has_intensity = "intensity" in point_format.dimension_names

        for pts in reader.chunk_iterator(chunk):
            x = np.asarray(pts.x, dtype=np.float64)
            y = np.asarray(pts.y, dtype=np.float64)
            z = np.asarray(pts.z, dtype=np.float64)
            mask = (
                (x >= region_min[0])
                & (x <= region_max[0])
                & (y >= region_min[1])
                & (y <= region_max[1])
                & (z >= region_min[2])
                & (z <= region_max[2])
            )
            if not mask.any():
                continue
            xs.append(x[mask])
            ys.append(y[mask])
            zs.append(z[mask])
            if has_rgb:
                rs.append((np.asarray(pts.red)[mask] >> 8).astype(np.uint8))
                gs.append((np.asarray(pts.green)[mask] >> 8).astype(np.uint8))
                bs.append((np.asarray(pts.blue)[mask] >> 8).astype(np.uint8))
            if has_intensity:
                iss.append(np.asarray(pts.intensity, dtype=np.float32)[mask])

    if not xs:
        empty = np.zeros((0, 3), dtype=np.float64)
        return PointCloud(xyz=empty, source_file=str(file_path))

    xyz = np.column_stack([np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)])
    rgb = (
        np.column_stack([np.concatenate(rs), np.concatenate(gs), np.concatenate(bs)])
        if has_rgb
        else None
    )
    intensity = np.concatenate(iss) if has_intensity else None
    return PointCloud(xyz=xyz, rgb=rgb, intensity=intensity, source_file=str(file_path))


def read_region_e57(
    file_path: Path,
    region_min: np.ndarray,
    region_max: np.ndarray,
    chunk: int = DEFAULT_READ_CHUNK,
) -> PointCloud:
    """Read only E57 points inside [region_min, region_max], in bounded chunks."""
    import pye57

    e57 = pye57.E57(str(file_path))
    header = e57.get_header(0)
    total = int(header.point_count)
    fields = header.point_fields
    has_rgb = all(f in fields for f in ("colorRed", "colorGreen", "colorBlue"))
    has_intensity = "intensity" in fields

    read_fields = ["cartesianX", "cartesianY", "cartesianZ"]
    if has_rgb:
        read_fields += ["colorRed", "colorGreen", "colorBlue"]
    if has_intensity:
        read_fields.append("intensity")

    keep_rgb: list[np.ndarray] = []
    keep_i: list[np.ndarray] = []
    xyz_parts: list[np.ndarray] = []

    read_so_far = 0
    for buf_data, n in _iter_e57_blocks(e57, header, read_fields, chunk):
        x = buf_data["cartesianX"][:n]
        y = buf_data["cartesianY"][:n]
        z = buf_data["cartesianZ"][:n]
        mask = (
            (x >= region_min[0])
            & (x <= region_max[0])
            & (y >= region_min[1])
            & (y <= region_max[1])
            & (z >= region_min[2])
            & (z <= region_max[2])
        )
        if mask.any():
            xyz_parts.append(
                np.column_stack([x[mask], y[mask], z[mask]]).astype(np.float64)
            )
            if has_rgb:
                keep_rgb.append(
                    np.column_stack(
                        [
                            buf_data["colorRed"][:n][mask],
                            buf_data["colorGreen"][:n][mask],
                            buf_data["colorBlue"][:n][mask],
                        ]
                    ).astype(np.uint8)
                )
            if has_intensity:
                keep_i.append(buf_data["intensity"][:n][mask].astype(np.float32))
        read_so_far += n

    if read_so_far != total:
        warnings.warn(
            f"E57 read {read_so_far:,} of {total:,} points declared by the header "
            f"for {file_path}. The file may be truncated or the header stale; "
            "detections outside the points actually read will be missing.",
            RuntimeWarning,
            stacklevel=2,
        )

    if not xyz_parts:
        return PointCloud(
            xyz=np.zeros((0, 3), dtype=np.float64), source_file=str(file_path)
        )
    xyz = np.concatenate(xyz_parts)
    rgb = np.concatenate(keep_rgb) if has_rgb and keep_rgb else None
    intensity = np.concatenate(keep_i) if has_intensity and keep_i else None
    return PointCloud(xyz=xyz, rgb=rgb, intensity=intensity, source_file=str(file_path))


def read_region_canonical(
    file_path: Path,
    region_min: np.ndarray,
    region_max: np.ndarray,
    chunk: int = DEFAULT_READ_CHUNK,
) -> PointCloud:
    """Read only canonical-file points inside [region_min, region_max]."""
    header = canonical_header(file_path)
    xyz_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray] = []
    int_parts: list[np.ndarray] = []
    for xyz, rgb, intensity in iter_canonical_chunks(file_path, chunk):
        mask = np.all((xyz >= region_min) & (xyz <= region_max), axis=1)
        if not mask.any():
            continue
        xyz_parts.append(xyz[mask])
        if rgb is not None:
            rgb_parts.append(rgb[mask])
        if intensity is not None:
            int_parts.append(intensity[mask])

    if not xyz_parts:
        return PointCloud(
            xyz=np.zeros((0, 3), dtype=np.float64), source_file=str(file_path)
        )
    return PointCloud(
        xyz=np.concatenate(xyz_parts),
        rgb=np.concatenate(rgb_parts) if header.has_rgb else None,
        intensity=np.concatenate(int_parts) if header.has_intensity else None,
        source_file=str(file_path),
    )


def _iter_e57_blocks(
    e57: object,
    header: object,
    fields: Sequence[str],
    chunk: int,
) -> Iterator[tuple[dict[str, np.ndarray], int]]:
    """Yield ``(buffers, n)`` for successive blocks of an E57 scan.

    The reader is created ONCE and ``read()`` called repeatedly. Constructing
    ``header.points.reader(buffers)`` inside the loop returns a fresh reader
    positioned at offset 0 every iteration, so the same first ``chunk`` points are
    returned over and over while the loop counter advances to the declared total:
    a 50,000-point file read at chunk=10,000 yielded 50,000 points of which only
    10,000 were unique, and 40,000 real points were never read at all.

    The buffers are reused across yields, so callers MUST copy anything they keep
    (masking with a boolean index already does).
    """
    from pye57 import libe57  # type: ignore[attr-defined]

    total = int(header.point_count)  # type: ignore[attr-defined]
    if total <= 0:
        return
    cap = max(1, min(int(chunk), total))

    data: dict[str, np.ndarray] = {}
    buffers = libe57.VectorSourceDestBuffer()
    for field in fields:
        arr, buf = e57.make_buffer(field, cap)  # type: ignore[attr-defined]
        data[field] = arr
        buffers.append(buf)

    reader = header.points.reader(buffers)  # type: ignore[attr-defined]
    try:
        while True:
            n = int(reader.read())
            if n <= 0:
                break
            yield data, n
    finally:
        # pye57 opens the whole file per reader; one is created per region read, so
        # closing explicitly keeps the fd count flat rather than relying on
        # refcount finalisation.
        reader.close()


def read_region(
    file_path: Path,
    fmt: str,
    region_min: np.ndarray,
    region_max: np.ndarray,
    chunk: int = DEFAULT_READ_CHUNK,
) -> PointCloud:
    """Dispatch a Pass-2 region read by format (file coordinate frame)."""
    if fmt in ("las", "laz"):
        return read_region_las(file_path, region_min, region_max, chunk)
    if fmt == "e57":
        return read_region_e57(file_path, region_min, region_max, chunk)
    if fmt == "canonical":
        return read_region_canonical(file_path, region_min, region_max, chunk)
    raise ValueError(f"read_region: format not streamable: {fmt}")


# ---------------------------------------------------------------------------
# Pass 1 — validate the header, then walk the file ONCE for true bounds, an
# exact point count, and a coarse occupancy grid used to plan tiles.
# ---------------------------------------------------------------------------

# The occupancy grid is sized by scene extent, so cap its cells: a corrupt or
# mis-projected header (unprojected degrees written into a projected field, say)
# would otherwise size it for the whole planet. 2M cells is 16 MB at int64.
MAX_OCCUPANCY_CELLS = 2_000_000

# Hard ceiling on planned leaves. Each leaf is one inference call, and the leaf
# list itself is held before any point is read. Without this, geometry-only
# planning on a 1280 m cube produced 16,777,216 leaves and ~4.5 GB of list.
MAX_PLANNED_TILES = 200_000


def validate_scene_bounds(
    mins: np.ndarray,
    maxs: np.ndarray,
    count: int,
    file_path: Path,
    source: str = "header",
) -> None:
    """Reject bounds that cannot describe a real scene.

    Point-cloud headers are untrusted input. ``plan_tile_extents`` partitions
    exactly the box it is given and ``read_region_*`` only ever reads inside it, so
    a bad header does not fail — it silently changes the answer. Measured on an
    otherwise valid 40,000-point file with only its header bounds patched:
    75.1% of points fell outside every planned extent and were never read, and the
    run still exited 0. Inverted and NaN bounds produced zero tiles, an empty
    detections file, and exit 0.
    """
    if not (np.all(np.isfinite(mins)) and np.all(np.isfinite(maxs))):
        raise ValueError(
            f"{source} bounds for {file_path} contain NaN or inf "
            f"(min={mins.tolist()}, max={maxs.tolist()}). Refusing to plan tiles "
            "from unusable bounds; repair the file or use the whole-cloud path."
        )
    if np.any(maxs < mins):
        bad = ["XYZ"[i] for i in range(3) if maxs[i] < mins[i]]
        raise ValueError(
            f"{source} bounds for {file_path} are inverted on axis/axes "
            f"{', '.join(bad)} (min={mins.tolist()}, max={maxs.tolist()}). This "
            "would plan zero tiles and report success having detected nothing."
        )
    if count <= 0:
        raise ValueError(
            f"{source} for {file_path} declares {count} points; nothing to stream."
        )
    if np.all(maxs - mins <= 0.0):
        raise ValueError(
            f"{source} bounds for {file_path} are degenerate — zero extent on every "
            f"axis (min={mins.tolist()}, max={maxs.tolist()})."
        )


def _occupancy_shape(extent: np.ndarray, min_cell: float) -> np.ndarray:
    """Grid shape for the occupancy scan, capped at MAX_OCCUPANCY_CELLS."""
    safe_extent = np.maximum(extent, 0.0)
    shape = np.maximum(np.ceil(safe_extent / max(min_cell, 1e-9)), 1).astype(np.int64)

    # Shrink proportionally until the cell budget is met. Multiply as Python ints:
    # np.prod on an int64 shape silently overflows negative for large scenes.
    def total(s: np.ndarray) -> int:
        out = 1
        for d in s.tolist():
            out *= int(d)
        return out

    while total(shape) > MAX_OCCUPANCY_CELLS:
        shape = np.maximum(shape // 2, 1)
        if total(shape) <= 1:
            break
    return shape


@dataclass
class ScenePass1:
    """Result of the single Pass-1 file walk."""

    data_min: np.ndarray
    data_max: np.ndarray
    points_read: int
    header_count: int
    occupancy: np.ndarray
    grid_origin: np.ndarray
    cell_size: np.ndarray


def scan_occupancy(
    file_path: Path,
    fmt: str,
    header_min: np.ndarray,
    header_max: np.ndarray,
    header_count: int,
    min_cell: float,
    chunk: int = DEFAULT_READ_CHUNK,
) -> ScenePass1:
    """Walk the file once for true bounds, exact count, and a coarse occupancy grid.

    The grid is sized from the header (needed before any point is seen) but the
    TRUE data bounds are accumulated at the same time, so a stale header is
    detectable rather than silently truncating the scene. Cell indices are clipped,
    so points outside the header box still land in an edge cell and are counted.
    """
    extent = np.asarray(header_max, float) - np.asarray(header_min, float)
    shape = _occupancy_shape(extent, min_cell)
    cell_size = np.where(shape > 0, extent / np.maximum(shape, 1), min_cell)
    cell_size = np.where(cell_size > 0, cell_size, min_cell)

    occupancy = np.zeros(tuple(int(s) for s in shape), dtype=np.int64)
    lo = np.full(3, np.inf, dtype=np.float64)
    hi = np.full(3, -np.inf, dtype=np.float64)
    seen = 0

    for xyz, _rgb, _intensity in iter_chunks(file_path, fmt, chunk):
        if xyz.shape[0] == 0:
            continue
        lo = np.minimum(lo, xyz.min(axis=0))
        hi = np.maximum(hi, xyz.max(axis=0))
        seen += int(xyz.shape[0])

        idx = np.floor((xyz - header_min) / cell_size).astype(np.int64)
        np.clip(idx, 0, shape - 1, out=idx)
        flat = (idx[:, 0] * shape[1] + idx[:, 1]) * shape[2] + idx[:, 2]
        occupancy += np.bincount(flat, minlength=occupancy.size).reshape(
            occupancy.shape
        )

    if seen == 0:
        raise ValueError(f"No readable points in {file_path}; cannot stream.")

    return ScenePass1(
        data_min=lo,
        data_max=hi,
        points_read=seen,
        header_count=int(header_count),
        occupancy=occupancy,
        grid_origin=np.asarray(header_min, float),
        cell_size=cell_size,
    )


# ---------------------------------------------------------------------------
# Tile planning (file frame) — mirrors tiling.py octree, driven by occupancy.
# ---------------------------------------------------------------------------


def build_cell_leaf_map(
    leaves: list[tuple[np.ndarray, np.ndarray]],
    leaf_indices: list[int],
    grid_origin: np.ndarray,
    cell_size: np.ndarray,
    grid_shape: np.ndarray,
    margin: float,
) -> dict[int, list[int]]:
    """Map each occupancy-grid cell to the leaves whose guard-band box overlaps it.

    This is the routing table that replaces the per-chunk x per-leaf scan in Pass 2.
    A point's cell is an O(1) computation (same arithmetic as the Pass-1 histogram),
    and this map turns that cell into the small set of candidate leaves the point
    could belong to — typically 1, up to ~8 where guard bands overlap near a corner.
    The caller then applies the exact float box test only against those candidates,
    so membership is identical to testing against every leaf, at a fraction of the
    work.

    Only ``leaf_indices`` (e.g. one batch) are mapped, so the table stays small. Each
    leaf's box is expanded by ``margin`` before being converted to the cell range it
    covers, so boundary cells correctly list every leaf whose guard band reaches them.
    Cells are keyed by their flattened index (x*Sy*Sz + y*Sz + z).
    """
    cell_map: dict[int, list[int]] = {}
    sy, sz = int(grid_shape[1]), int(grid_shape[2])
    for i in leaf_indices:
        lo, hi = leaves[i]
        elo = lo - margin
        ehi = hi + margin
        # Cells that could contain a point in the INCLUSIVE box [elo, ehi]. A point
        # at coordinate x lands in cell floor((x - origin) / cell), so the covered
        # cells run from floor(elo) to floor(ehi) INCLUSIVE. Using ceil for the upper
        # bound (as an exclusive range end) drops the cell that holds points sitting
        # exactly on ehi, which under-includes boundary cells and makes routing miss
        # the both-leaves membership the per-leaf scan produced on a shared plane.
        # This map may over-include (a superset is safe — the exact float box test in
        # the caller filters); it must never under-include.
        # The map must be a SUPERSET of the cells any point in the expanded box can
        # occupy, so the caller's exact float test never misses a point (over-
        # inclusion is harmless — the float test filters it out). floor() at cell
        # granularity plus quantised point coordinates means a point within the box
        # can sit in a cell whose index is up to one cell beyond floor(edge); pad by
        # one cell on each side to cover it. (elo/ehi already include the m margin;
        # this pad covers only the cell-vs-float rounding gap, which is < 1 cell.)
        c0 = np.clip(
            np.floor((elo - grid_origin) / cell_size).astype(np.int64) - 1,
            0,
            grid_shape - 1,
        )
        c1 = np.clip(
            np.floor((ehi - grid_origin) / cell_size).astype(np.int64) + 1,
            0,
            grid_shape - 1,
        )
        for cx in range(int(c0[0]), int(c1[0]) + 1):
            for cy in range(int(c0[1]), int(c1[1]) + 1):
                base = cx * sy * sz + cy * sz
                for cz in range(int(c0[2]), int(c1[2]) + 1):
                    cell_map.setdefault(base + cz, []).append(i)
    return cell_map


def build_neighbor_map(
    leaves: list[tuple[np.ndarray, np.ndarray]],
    margin: float,
    cell_size: np.ndarray,
) -> list[list[int]]:
    """For each tile i, the core leaves that may hold a point in i's expanded box.

    A tile's full point set is every point in ``[lo_i - margin, hi_i + margin]``.
    Under store-once each such point lives in the single core that OWNS it, so tile
    i must read its own core plus every core j that could hold one of those points.
    Returns ``neighbors[i]`` = sorted list of such j (including i itself).

    Owner assignment (``build_core_owner_map``) is CELL-granular: a point is stored
    in the owner of its occupancy cell, and a cell straddling a core boundary can
    place a point up to one cell OUTSIDE that core's nominal ``[lo, hi]``. So a core
    j can hold a point relevant to tile i even when j's nominal box does not quite
    reach i's expanded box. To never miss such a point, each core's box is padded by
    one ``cell_size`` on every side before the overlap test — a SUPERSET (the exact
    float box test in Pass 2b then filters), which must never under-include.

    Vectorised AABB overlap: two boxes overlap iff they overlap on all three axes.
    O(n) per tile via numpy broadcast, O(n^2) total — a one-time pass, fine for the
    ~10^4 leaves a large scene produces (the margin is small vs tile size, so each
    tile has only a handful of true neighbours; this just finds them exactly).
    """
    n = len(leaves)
    if n == 0:
        return []
    cs = np.asarray(cell_size, float)
    los = np.stack([lo for lo, _ in leaves]) - cs  # (n, 3), padded one cell low
    his = np.stack([hi for _, hi in leaves]) + cs  # (n, 3), padded one cell high
    raw_lo = np.stack([lo for lo, _ in leaves])
    raw_hi = np.stack([hi for _, hi in leaves])
    neighbors: list[list[int]] = []
    for i in range(n):
        elo = raw_lo[i] - margin
        ehi = raw_hi[i] + margin
        # padded core j overlaps i's expanded box iff, on every axis, his[j] >= elo
        # and los[j] <= ehi.
        overlap = np.all((his >= elo) & (los <= ehi), axis=1)
        neighbors.append(np.nonzero(overlap)[0].tolist())
    return neighbors


def plan_tile_extents(
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
    config: PipelineConfig,
    scene: ScenePass1 | None = None,
    max_points: int | None = None,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Plan octree leaf extents for the scene.

    Split PLACEMENT uses the occupancy-weighted median (see ``split_plane``): the
    plane is put where roughly half the region's points fall on each side, computed
    from the Pass-1 occupancy grid rather than per-node point coordinates. This is
    the streaming equivalent of the in-memory tiler's point-count median and cuts
    through dense clusters instead of bisecting empty volume around them. Placement
    falls back to the geometric midpoint only for the geometry-only path (no
    occupancy grid) or degenerate profiles.

    When ``scene`` is supplied the recursion becomes occupancy-driven, which is what
    makes it bounded:

    - regions containing no points are dropped instead of subdivided, so planning
      follows occupied space rather than the whole scene volume;
    - a region already within ``max_points`` stops early, so dense areas get the
      depth they need and sparse ones do not;
    - the leaf count is capped either way.

    ``min_tile_size_m`` is a hard floor, so a dense region smaller than twice it
    cannot be cut finer and is emitted over budget with a warning — the same stop
    condition the in-memory tiler uses. That is the one case where the per-tile cap
    cannot be honoured, and it is surfaced rather than hidden.

    Without occupancy (the geometry-only fallback) recursion runs to
    ``min_tile_size_m`` on every axis. That is what produced 16,777,216 leaves and
    ~4.5 GB for an ordinary 1280 m aerial cube, so the cap still applies and raises
    rather than exhausting memory.
    """
    tcfg = config.tiling
    leaves: list[tuple[np.ndarray, np.ndarray]] = []
    over_cap: list[tuple[int, tuple, tuple]] = []

    integral: np.ndarray | None = None
    if scene is not None:
        # Summed-area table so a region's point count is 8 lookups, not a slice sum.
        integral = scene.occupancy.cumsum(0).cumsum(1).cumsum(2)

    def region_points(bmin: np.ndarray, bmax: np.ndarray) -> int:
        if scene is None or integral is None:
            return -1  # unknown; disables the count-based criteria
        shape = np.array(scene.occupancy.shape, dtype=np.int64)
        i0 = np.clip(
            np.floor((bmin - scene.grid_origin) / scene.cell_size).astype(np.int64),
            0,
            shape - 1,
        )
        i1 = np.clip(
            np.ceil((bmax - scene.grid_origin) / scene.cell_size).astype(np.int64) - 1,
            0,
            shape - 1,
        )
        if np.any(i1 < i0):
            return 0

        table = integral
        assert table is not None  # guarded by the scene/integral check above

        def at(x: int, y: int, z: int) -> int:
            if x < 0 or y < 0 or z < 0:
                return 0
            return int(table[x, y, z])

        x0, y0, z0 = (int(v) - 1 for v in i0)
        x1, y1, z1 = (int(v) for v in i1)
        return (
            at(x1, y1, z1)
            - at(x0, y1, z1)
            - at(x1, y0, z1)
            - at(x1, y1, z0)
            + at(x0, y0, z1)
            + at(x0, y1, z0)
            + at(x1, y0, z0)
            - at(x0, y0, z0)
        )

    def split_plane(bmin: np.ndarray, bmax: np.ndarray, axis: int) -> float:
        """Occupancy-weighted median plane for one axis of a region.

        The geometric midpoint bisects the region's *volume*, which strands a dense
        cluster in one half and recurses into mostly-empty space around it — the
        cause of the sub-min-size over-budget tiles observed on clustered scenes.
        The occupancy median bisects the region's *points* instead: it finds the
        cell boundary where roughly half the region's points fall on each side, so
        the cut passes through the dense region and both children carry a balanced
        share. This is the streaming (occupancy-grid) equivalent of the in-memory
        tiler's point-count median; it needs only the occupancy histogram, not
        per-point coordinates, so it preserves the bounded-memory Pass-1 contract.

        Snaps to a grid-cell boundary (the finest the occupancy grid can resolve)
        and is clamped to ``[lo + min_tile_size_m, hi - min_tile_size_m]`` so both
        children clear the minimum tile size — matching the axis-qualification
        guard. Falls back to the geometric midpoint when there is no occupancy grid
        (geometry-only planning) or when the projected counts are degenerate.
        """
        lo = float(bmin[axis])
        hi = float(bmax[axis])
        midpoint = (lo + hi) / 2.0
        if scene is None:
            return midpoint

        # Cell index range this region spans on `axis`.
        shape = np.array(scene.occupancy.shape, dtype=np.int64)
        origin_a = float(scene.grid_origin[axis])
        cell_a = float(scene.cell_size[axis])
        c0 = int(np.clip(np.floor((lo - origin_a) / cell_a), 0, shape[axis] - 1))
        c1 = int(np.clip(np.ceil((hi - origin_a) / cell_a), 0, shape[axis]))
        if c1 - c0 < 2:
            return midpoint  # not enough cells to place an interior plane

        # 1-D occupancy profile along `axis` for exactly this region's other-axis
        # span, so the median reflects the region's own points, not the whole scene.
        other = tuple(a for a in range(3) if a != axis)
        # Region cell ranges on the other two axes.
        oi0, oi1 = [], []
        for a in other:
            lo_a = float(bmin[a])
            hi_a = float(bmax[a])
            org_a = float(scene.grid_origin[a])
            csz_a = float(scene.cell_size[a])
            o0 = int(np.clip(np.floor((lo_a - org_a) / csz_a), 0, shape[a] - 1))
            o1 = int(np.clip(np.ceil((hi_a - org_a) / csz_a), 0, shape[a]))
            oi0.append(o0)
            oi1.append(max(o1, o0 + 1))
        # Slice the region out, then move `axis` to the front and flatten the other
        # two, giving a 1-D profile indexed by cells along `axis` — no fragile
        # post-sum axis renumbering.
        region_slices = [slice(None)] * 3
        region_slices[axis] = slice(c0, c1)
        region_slices[other[0]] = slice(oi0[0], oi1[0])
        region_slices[other[1]] = slice(oi0[1], oi1[1])
        block = scene.occupancy[tuple(region_slices)]
        profile = np.moveaxis(block, axis, 0).reshape(block.shape[axis], -1).sum(axis=1)
        total = int(profile.sum())
        if total <= 0:
            return midpoint

        # First cell boundary at which the cumulative count reaches half the total.
        cumulative = np.cumsum(profile)
        half = total / 2.0
        # index into `profile` (0..len-1); the boundary AFTER cell `k` is the plane.
        k = int(np.searchsorted(cumulative, half))
        k = int(np.clip(k, 0, len(profile) - 2))  # keep an interior boundary
        plane = origin_a + (c0 + k + 1) * cell_a

        # Clamp to the legal window so both children stay >= min_tile_size_m.
        lo_limit = lo + tcfg.min_tile_size_m
        hi_limit = hi - tcfg.min_tile_size_m
        if lo_limit > hi_limit:
            return midpoint  # window collapsed (shouldn't happen given the guard)
        return float(np.clip(plane, lo_limit, hi_limit))

    def recurse(bmin: np.ndarray, bmax: np.ndarray, depth: int) -> None:
        if len(leaves) > MAX_PLANNED_TILES:
            raise ValueError(
                f"Planning exceeded MAX_PLANNED_TILES={MAX_PLANNED_TILES:,} leaves "
                f"for bounds {bmin.tolist()}..{bmax.tolist()}. Each leaf is one "
                "inference call. Raise tiling.min_tile_size_m, raise "
                "tiling.max_points_per_tile, or check the file header for a "
                "mis-projected extent."
            )

        count = region_points(bmin, bmax)
        if count == 0:
            return  # unoccupied: nothing to read, nothing to infer

        extent = bmax - bmin
        splittable = extent >= tcfg.min_tile_size_m * 2.0

        within_budget = max_points is not None and count >= 0 and count <= max_points
        # depth guard is a backstop for pathological bounds; it caps depth, not
        # breadth, so it is not a substitute for MAX_PLANNED_TILES.
        if not np.any(splittable) or within_budget or depth > 32:
            if max_points is not None and count > max_points:
                # The region holds more points than the budget allows but cannot be
                # cut any finer without going below min_tile_size_m. Emitting it
                # anyway matches the in-memory tiler's own stop condition, but it
                # does breach the per-tile bound, so record it — this is the one
                # case where streaming cannot honour max_points_per_tile.
                over_cap.append((count, tuple(bmin.tolist()), tuple(bmax.tolist())))
            leaves.append((bmin.copy(), bmax.copy()))
            return

        splits = {}
        for axis in range(3):
            if splittable[axis]:
                splits[axis] = split_plane(bmin, bmax, axis)
        axes = sorted(splits)
        for combo in range(2 ** len(axes)):
            cmin, cmax = bmin.copy(), bmax.copy()
            for bit, axis in enumerate(axes):
                if (combo >> bit) & 1:
                    cmin[axis] = splits[axis]
                else:
                    cmax[axis] = splits[axis]
            recurse(cmin, cmax, depth + 1)

    recurse(np.asarray(bounds_min, float), np.asarray(bounds_max, float), 0)

    if len(leaves) > MAX_PLANNED_TILES:
        raise ValueError(
            f"Planning produced {len(leaves):,} leaves, over "
            f"MAX_PLANNED_TILES={MAX_PLANNED_TILES:,}."
        )

    if over_cap:
        worst, wmin, wmax = max(over_cap, key=lambda item: item[0])
        warnings.warn(
            f"{len(over_cap)} planned tile(s) exceed max_points_per_tile="
            f"{max_points:,} because they cannot be subdivided below "
            f"min_tile_size_m={tcfg.min_tile_size_m}. Largest is {worst:,} points "
            f"at {[round(v, 2) for v in wmin]}..{[round(v, 2) for v in wmax]}. "
            "Peak memory for those tiles will exceed the per-tile budget; lower "
            "min_tile_size_m to let the planner cut them finer.",
            RuntimeWarning,
            stacklevel=2,
        )

    return leaves


def _transform_box(
    lo: np.ndarray, hi: np.ndarray, source_frame: str
) -> tuple[np.ndarray, np.ndarray]:
    """Map a file-frame box into the output (Z-up) frame.

    Extents are planned in file space but ``preprocess`` rotates the points, so
    without this a tile's own points fall outside its own extent — measured 4/4
    tiles for ``coord_frame="y-up"``. For the axis-permutation frames the rotated
    box is still axis-aligned and this is exact; for the true rotations
    (``livox``, ``leica``, ``z-rot*``) the axis-aligned hull of the rotated corners
    is a conservative superset.
    """
    if source_frame == "z-up":
        return lo.copy(), hi.copy()
    rotation = COORD_TRANSFORMS[source_frame]
    # Note the enclosing list: np.array(<generator>) builds a 0-d object array, not
    # an (8, 3) float array, and the matmul below then fails.
    corners = np.array(
        [
            [[lo[0], hi[0]][a], [lo[1], hi[1]][b], [lo[2], hi[2]][c]]
            for a in (0, 1)
            for b in (0, 1)
            for c in (0, 1)
        ],
        dtype=np.float64,
    )
    rotated = (rotation @ corners.T).T
    return rotated.min(axis=0), rotated.max(axis=0)


def stream_tiles(
    file_path: Path,
    fmt: str,
    config: PipelineConfig,
    max_points_per_tile: int | None = None,
) -> Iterator[Tile]:
    """Yield preprocessed tiles for a large scene without loading it whole.

    Three phases: validate the header, walk the file once to build occupancy and
    true bounds, then route points to on-disk per-tile extracts in a single walk
    (Option C) and read each extract back for preprocessing + inference.
    """
    if not is_streamable(fmt):
        raise ValueError(f"stream_tiles: format not streamable: {fmt}")

    frame = config.preprocessing.coord_frame
    if frame == "auto":
        # _auto_align estimates the up-axis and in-plane rotation from whatever
        # points it is handed. In streaming mode that is one tile at a time, so
        # every tile lands in its own frame and global NMS compares boxes across
        # incompatible frames. Measured: 4 tiles, 4 different rotations.
        raise ValueError(
            'coord_frame="auto" cannot be used with the streaming ingest path: the '
            "frame is estimated per tile, so each tile would be aligned "
            "differently and detections could not be compared. Set an explicit "
            "coord_frame, or raise tiling.memory_budget_gb to use the whole-cloud "
            "path."
        )

    header_min, header_max, header_count = scan_bounds(file_path, fmt)
    validate_scene_bounds(header_min, header_max, header_count, file_path, "header")

    cap = max_points_per_tile or config.tiling.max_points_per_tile
    scene = scan_occupancy(
        file_path,
        fmt,
        header_min,
        header_max,
        header_count,
        min_cell=max(config.tiling.min_tile_size_m, 1e-6),
    )
    validate_scene_bounds(
        scene.data_min, scene.data_max, scene.points_read, file_path, "point data"
    )

    # A header that under-reports its own extent is common in the wild: any tool
    # that edits points without rewriting the header produces one. Plan the union
    # so those points still land in a tile instead of being dropped.
    plan_min = np.minimum(header_min, scene.data_min)
    plan_max = np.maximum(header_max, scene.data_max)
    if np.any(scene.data_min < header_min) or np.any(scene.data_max > header_max):
        warnings.warn(
            f"Header bounds for {file_path} do not contain the point data "
            f"(header {header_min.tolist()}..{header_max.tolist()}, data "
            f"{scene.data_min.tolist()}..{scene.data_max.tolist()}). Planning over "
            "the union so no points are dropped; the file header is stale.",
            RuntimeWarning,
            stacklevel=2,
        )
    if scene.points_read != scene.header_count:
        warnings.warn(
            f"{file_path} header declares {scene.header_count:,} points but "
            f"{scene.points_read:,} were read. Continuing with the points actually "
            "present.",
            RuntimeWarning,
            stacklevel=2,
        )

    leaves = plan_tile_extents(plan_min, plan_max, config, scene=scene, max_points=cap)
    if not leaves:
        raise ValueError(
            f"Planning produced no tiles for {file_path} despite "
            f"{scene.points_read:,} points. This should not happen; please report it."
        )

    margin = config.tiling.overlap_margin_m
    core_points_emitted = 0
    grid_shape = np.array(scene.occupancy.shape, dtype=np.int64)
    sy, sz = int(grid_shape[1]), int(grid_shape[2])
    n_leaves = len(leaves)

    # Option C — single-pass routing to on-disk per-tile extracts.
    #
    # The file is walked ONCE here (plus the one Pass-1 occupancy walk), regardless
    # of tile count, instead of once per budget-sized batch. Each point is appended
    # to the extract of the tile(s) it belongs to; extracts are then read back one at
    # a time for preprocessing + inference, so peak memory is still one tile. This
    # replaces the (1 + n_batches) x file_size re-read that was the measured 50 GB
    # throughput wall.
    #
    # Byte-identical to the previous batched routed path: same cell->leaf map, same
    # exact float box test, same np.sort(...) file-order selection, appended in the
    # same chunk-walk order. Only the destination changed (on-disk file vs an in-RAM
    # per-batch list), and read-back preserves append order, so each tile's point
    # array is unchanged and voxel_downsample keeps the same representatives.

    # Channel presence is fixed for the whole scene by the reader; probe it from the
    # first non-empty chunk so extracts know their schema before any append.
    chunk_iter = iter_chunks(file_path, fmt, DEFAULT_READ_CHUNK)
    has_rgb = False
    has_intensity = False
    xyz_dtype = np.dtype(np.float64)
    rgb_dtype = np.dtype(np.uint8)
    int_dtype = np.dtype(np.float32)
    first_chunks: list[tuple[np.ndarray, np.ndarray | None, np.ndarray | None]] = []
    for xyz, rgb, intensity in chunk_iter:
        first_chunks.append((xyz, rgb, intensity))
        if xyz.shape[0] > 0:
            has_rgb = rgb is not None
            has_intensity = intensity is not None
            xyz_dtype = xyz.dtype
            if rgb is not None:
                rgb_dtype = rgb.dtype
            if intensity is not None:
                int_dtype = intensity.dtype
            break

    # Disk headroom: fail fast rather than filling the volume mid-walk. The extract
    # footprint is roughly the raw point bytes (uncompressed) plus overlap dupes,
    # which for a compressed LAZ can exceed the source file size.
    est = _estimate_extract_footprint_bytes(scene.points_read, has_rgb, has_intensity)
    # Where the on-disk extracts are written. Defaults to the system temp dir, but a
    # 50 GB+ scene needs more scratch than the root volume usually has (extracts are
    # uncompressed, so a compressed LAZ inflates several-fold), so allow pointing it
    # at a larger mounted volume (e.g. EFS/EBS) via PCD_EXTRACT_DIR.
    env_dir = os.environ.get("PCD_EXTRACT_DIR")
    tmp_root = Path(env_dir) if env_dir else None
    if tmp_root is not None and not tmp_root.is_dir():
        raise RuntimeError(
            f"PCD_EXTRACT_DIR={env_dir!r} is not an existing directory. Create it (or "
            "unset PCD_EXTRACT_DIR to use the system temp dir) before streaming."
        )
    disk_target = str(tmp_root) if tmp_root else tempfile.gettempdir()
    # Fail closed: if free space cannot be measured, do not start writing extracts
    # that may fill a volume other pods on the node depend on.
    try:
        free = shutil.disk_usage(disk_target).free
    except OSError as e:
        raise RuntimeError(
            f"Cannot measure free space on {disk_target} ({e}); refusing to stream "
            "on-disk extracts without a disk-headroom check."
        ) from e
    if est > free:
        raise RuntimeError(
            f"Streaming to on-disk tile extracts for {file_path} needs about "
            f"{est / 1e9:.1f} GB of scratch space, but only {free / 1e9:.1f} GB is "
            f"free on {disk_target}. Free space, or point PCD_EXTRACT_DIR at a larger "
            "volume, before running the streaming path on a scene this large."
        )

    # Store-once routing: each point is written to exactly ONE core extract (no
    # guard-band duplication — that is what blew the disk footprint past the source
    # size). Reconstruction below adds back each tile's overlap by reading
    # neighbouring cores.
    #
    # Owner selection uses the SAME machinery as the old duplicating routing: a
    # cell->candidate-leaves map (margin=0, so candidates are leaves whose core box
    # could contain the point) plus the EXACT float box test. This is what makes it
    # correct for points OUTSIDE the occupancy grid (a stale header under-reporting
    # the extent clips such points into an edge cell; the float test still assigns
    # them to the real core that contains them, so they are not dropped). Among the
    # cores whose [lo,hi] contains a point, the lowest index wins (deterministic).
    core_cell_map = build_cell_leaf_map(
        leaves,
        list(range(n_leaves)),
        scene.grid_origin,
        scene.cell_size,
        grid_shape,
        margin=0.0,
    )
    # Split the cell map into the common case and the rare one so routing can be
    # vectorised. Most cells have exactly ONE candidate core (interior cells), so a
    # point's owner is a pure array lookup with no per-point geometry. Only cells
    # with several candidates (boundary cells straddling cores) need the exact float
    # box test, and those are a small fraction. Precomputing these once — rather than
    # looping over every cell of every 5M-point chunk in Python — is what keeps
    # Pass-2a disk-bound instead of CPU-bound (a per-cell Python loop measured ~5
    # MB/s vs the ~250 MB/s the disk sustains).
    single_owner: dict[int, int] = {}
    multi_cells: dict[int, list[int]] = {}
    for _key, _cand in core_cell_map.items():
        if len(_cand) == 1:
            single_owner[_key] = _cand[0]
        else:
            multi_cells[_key] = sorted(_cand)
    # For each tile, the cores that may hold a point in its expanded box — the cores
    # it must read to rebuild its full [lo-margin, hi+margin] point set.
    neighbors = build_neighbor_map(leaves, margin, scene.cell_size)

    store = _TileExtractStore(range(n_leaves), has_rgb, has_intensity, tmp_dir=tmp_root)

    # Running count of points seen across the walk, i.e. each point's global source
    # row index. Stored with the point so a tile assembled from several cores can be
    # re-sorted into file order (the invariant voxel_downsample depends on).
    seen = 0

    def route_chunk(
        xyz: np.ndarray, rgb: np.ndarray | None, intensity: np.ndarray | None
    ) -> int:
        nonlocal seen
        n = xyz.shape[0]
        if n == 0:
            return 0
        base_idx = seen
        cidx = np.floor((xyz - scene.grid_origin) / scene.cell_size).astype(np.int64)
        np.clip(cidx, 0, grid_shape - 1, out=cidx)
        flat = cidx[:, 0] * sy * sz + cidx[:, 1] * sz + cidx[:, 2]

        # owner[row] = index of the single core that stores this point (lowest-index
        # core whose [lo,hi] contains it). -1 until assigned; a point left at -1 is
        # outside every core and surfaced by the reconcile check.
        owner = np.full(n, -1, dtype=np.int64)

        # Vectorised owner lookup for the common case: cells with a single candidate
        # core. Build the per-point owner in one pass over the chunk's unique cells,
        # assigning by array slice (no per-point geometry). Points whose cell has
        # several candidates, or is not in the map, are collected for the exact float
        # test below — a small minority (boundary cells / out-of-grid points).
        order = np.argsort(flat, kind="stable")
        flat_sorted = flat[order]
        uniq, starts = np.unique(flat_sorted, return_index=True)
        ends = np.append(starts[1:], len(flat_sorted))

        ambiguous_rows: list[np.ndarray] = []
        for key, s, e in zip(uniq.tolist(), starts.tolist(), ends.tolist()):
            rows = order[s:e]
            owner_i = single_owner.get(int(key))
            if owner_i is not None:
                owner[rows] = owner_i  # unambiguous: whole cell -> one core
            else:
                # multi-candidate cell, or key absent (out-of-grid clipped point):
                # defer to the exact float test.
                ambiguous_rows.append(rows)

        # Exact float box test only for the deferred points. Test each point against
        # its cell's candidate cores in ascending index; lowest-index container wins.
        if ambiguous_rows:
            amb = np.concatenate(ambiguous_rows)
            amb_pts = xyz[amb]
            amb_keys = flat[amb]
            # Group the ambiguous points by their cell key so each candidate set is
            # tested once against its own points.
            a_order = np.argsort(amb_keys, kind="stable")
            a_keys_sorted = amb_keys[a_order]
            a_uniq, a_starts = np.unique(a_keys_sorted, return_index=True)
            a_ends = np.append(a_starts[1:], len(a_keys_sorted))
            for key, s, e in zip(a_uniq.tolist(), a_starts.tolist(), a_ends.tolist()):
                cand = multi_cells.get(int(key))
                if not cand:
                    continue  # out-of-grid key with no candidate: left unowned
                local = a_order[s:e]
                rows = amb[local]
                pts = amb_pts[local]
                for i in cand:  # already sorted ascending
                    lo, hi = leaves[i]
                    inside = (
                        (pts[:, 0] >= lo[0])
                        & (pts[:, 0] <= hi[0])
                        & (pts[:, 1] >= lo[1])
                        & (pts[:, 1] <= hi[1])
                        & (pts[:, 2] >= lo[2])
                        & (pts[:, 2] <= hi[2])
                    )
                    take = inside & (owner[rows] < 0)
                    if take.any():
                        owner[rows[take]] = i

        # Append each owned group to its core. Group by owner with ONE stable
        # argsort + unique run instead of scanning the whole chunk once per owner
        # (np.nonzero(owner == i) per owner was O(chunk x owners) — quadratic at
        # ~10^4 tiles). A stable sort keeps rows ascending WITHIN each owner group,
        # so each group's rows are already in file order — no per-group re-sort.
        assigned = owner >= 0
        if assigned.any():
            rows_all = np.nonzero(assigned)[0]  # ascending row indices
            own_all = owner[rows_all]
            g = np.argsort(own_all, kind="stable")
            rows_sorted = rows_all[g]  # grouped by owner, ascending within group
            own_sorted = own_all[g]
            uniq_own, o_starts = np.unique(own_sorted, return_index=True)
            o_ends = np.append(o_starts[1:], len(own_sorted))
            for i, s, e in zip(uniq_own.tolist(), o_starts.tolist(), o_ends.tolist()):
                sel = rows_sorted[s:e]
                gidx = (base_idx + sel).astype(np.int64)
                store.append(
                    i,
                    gidx,
                    xyz[sel],
                    rgb[sel] if rgb is not None else None,
                    intensity[sel] if intensity is not None else None,
                )
        return n

    try:
        # Pass 2a — ONE walk, routing every point to its single owning core. Replay
        # the chunks consumed while probing channel presence, then continue.
        for xyz, rgb, intensity in first_chunks:
            seen += route_chunk(xyz, rgb, intensity)
        for xyz, rgb, intensity in chunk_iter:
            seen += route_chunk(xyz, rgb, intensity)
        store.flush_all()

        # A core j may be read by several tiles (itself + every tile whose expanded
        # box overlaps it), so it can only be dropped once the LAST such tile is
        # done. last_reader[j] = max tile index that will read core j; we process
        # tiles in ascending index, so core j is safe to drop after tile
        # last_reader[j] is emitted.
        last_reader = [i for i in range(n_leaves)]
        for i in range(n_leaves):
            for j in neighbors[i]:
                if i > last_reader[j]:
                    last_reader[j] = i
        readers_done: dict[int, int] = {}

        # Pass 2b — rebuild each tile from its own core plus overlapping neighbours,
        # re-sort into file order, preprocess, yield, then drop cores no longer needed.
        for i in range(n_leaves):
            lo, hi = leaves[i]
            elo, ehi = lo - margin, hi + margin

            idx_parts: list[np.ndarray] = []
            xyz_parts: list[np.ndarray] = []
            rgb_parts: list[np.ndarray] = []
            int_parts: list[np.ndarray] = []
            for j in neighbors[i]:
                c_idx, c_xyz, c_rgb, c_int = store.read_core(
                    j, xyz_dtype, rgb_dtype, int_dtype
                )
                if c_xyz.shape[0] == 0:
                    continue
                # Keep only this core's points that fall in tile i's expanded box —
                # the exact same predicate the duplicating path applied per leaf.
                m = (
                    (c_xyz[:, 0] >= elo[0])
                    & (c_xyz[:, 0] <= ehi[0])
                    & (c_xyz[:, 1] >= elo[1])
                    & (c_xyz[:, 1] <= ehi[1])
                    & (c_xyz[:, 2] >= elo[2])
                    & (c_xyz[:, 2] <= ehi[2])
                )
                if not m.any():
                    continue
                idx_parts.append(c_idx[m])
                xyz_parts.append(c_xyz[m])
                if c_rgb is not None:
                    rgb_parts.append(c_rgb[m])
                if c_int is not None:
                    int_parts.append(c_int[m])

            # Drop cores whose last reader was tile i (reclaim scratch as we go).
            for j in neighbors[i]:
                readers_done[j] = i
                if readers_done[j] >= last_reader[j]:
                    store.drop(j)

            if not xyz_parts:
                continue

            tile_idx = np.concatenate(idx_parts)
            tile_xyz = np.concatenate(xyz_parts)
            tile_rgb = np.concatenate(rgb_parts) if rgb_parts else None
            tile_i = np.concatenate(int_parts) if int_parts else None

            # Restore FILE order across the gathered cores (the invariant the
            # file-order fix protects: voxel_downsample keeps the first point per voxel
            # by input order, so the assembled tile must be in ascending source-row
            # order to be byte-identical to the single-pass masking path).
            order = np.argsort(tile_idx, kind="stable")
            tile_xyz = tile_xyz[order]
            if tile_rgb is not None:
                tile_rgb = tile_rgb[order]
            if tile_i is not None:
                tile_i = tile_i[order]

            core = np.all((tile_xyz >= lo) & (tile_xyz <= hi), axis=1)
            core_points_emitted += int(core.sum())

            region = PointCloud(
                xyz=tile_xyz,
                rgb=tile_rgb,
                intensity=tile_i,
                source_file=str(file_path),
            )
            region = preprocess(region, config.preprocessing)
            if region.num_points == 0:
                continue

            out_lo, out_hi = _transform_box(lo, hi, frame)
            yield Tile(
                xyz=region.xyz,
                rgb=region.rgb,
                intensity=region.intensity,
                normals=region.normals,
                origin=(out_lo + out_hi) / 2.0,
                extent_min=out_lo,
                extent_max=out_hi,
                point_indices=None,
            )
    finally:
        # Always remove the temp dir — a crash or KeyboardInterrupt (exactly what
        # ended the 50 GB run) must not leave the extract footprint orphaned on disk.
        store.cleanup()

    # Reconcile: every point Pass 1 saw should have landed in exactly one core.
    # Store-once means the total stored equals points_read (each point owned once),
    # so a core shortfall means planning did not cover the data.
    if core_points_emitted < scene.points_read:
        missing = scene.points_read - core_points_emitted
        pct = 100.0 * missing / max(scene.points_read, 1)
        warnings.warn(
            f"Streaming covered {core_points_emitted:,} of {scene.points_read:,} "
            f"points for {file_path} ({pct:.1f}% not assigned to any tile core). "
            "Detections in the uncovered region will be missing.",
            RuntimeWarning,
            stacklevel=2,
        )


# ---------------------------------------------------------------------------
# Option C — on-disk per-tile extracts (single-pass routing).
#
# The batched Pass 2 above re-reads the source file once per batch: total I/O is
# (1 + n_batches) x file_size. At ~10,000 tiles (a 50 GB scene) that is dozens of
# full walks and is the measured throughput wall. The store below lets Pass 2 walk
# the source ONCE, appending each point to the on-disk extract of the tile(s) it
# belongs to; inference then reads each compact extract in turn. Source reads drop
# to exactly 2 (Pass-1 occupancy + one Pass-2 routing walk), independent of tile
# count.
#
# Extracts are RAW per-channel binary, not LAS/LAZ: LAZ would re-incur the per-point
# encode/compress cost this is trying to remove. Append is O(bytes); read-back is a
# single np.frombuffer + reshape. Points are appended in file order (the caller
# hands np.sort(...)-selected rows in chunk-walk order), so read-back order equals
# file order and downstream voxel_downsample keeps the same representative per voxel
# as the in-RAM path — i.e. byte-identical tiles.
# ---------------------------------------------------------------------------

# Bytes buffered across all leaves before the fullest buffers are flushed to disk.
# Bounds Pass-2a RAM independently of tile count. Flushing opens/appends/closes a
# file, so a handle is never held between flushes -> the ~10,000-tile scene cannot
# exhaust the OS file-descriptor limit. 256 MiB is well under the ~2-3 GB the
# streaming path already uses for read buffers, so it does not move peak RSS.
EXTRACT_FLUSH_BYTES = 256 * 1024 * 1024


class _TileExtract:
    """Append-only raw-binary buffer for ONE core leaf's points.

    Store-once: a point is written to exactly one core extract (the leaf whose
    unexpanded ``[lo, hi]`` owns it), NOT duplicated into every overlapping tile.
    Alongside xyz (and optional rgb/intensity) each point carries its int64 SOURCE
    ROW INDEX — the point's position in the file walk. That index is what lets a
    tile be reconstructed in file order after gathering points from this core plus
    neighbouring cores (their append order alone is not the global file order once
    several cores are concatenated).

    Points are buffered in RAM and flushed to per-channel files under ``base`` when
    the owning store decides; ``read()`` returns everything written for this core.
    """

    def __init__(self, base: Path, has_rgb: bool, has_intensity: bool) -> None:
        self._base = base
        self._has_rgb = has_rgb
        self._has_intensity = has_intensity
        self._buf_idx: list[np.ndarray] = []
        self._buf_xyz: list[np.ndarray] = []
        self._buf_rgb: list[np.ndarray] = []
        self._buf_int: list[np.ndarray] = []
        self._buffered_bytes = 0
        self._count = 0

    @property
    def buffered_bytes(self) -> int:
        return self._buffered_bytes

    @property
    def count(self) -> int:
        return self._count

    def append(
        self,
        idx: np.ndarray,
        xyz: np.ndarray,
        rgb: np.ndarray | None,
        intensity: np.ndarray | None,
    ) -> None:
        # idx is the int64 global source-row index of each point; it is stored so the
        # reconstruction can re-sort a tile (built from several cores) back into file
        # order. rgb/intensity presence must match what the store was told the source
        # has, so the read-back reshape is unambiguous.
        self._buf_idx.append(idx)
        self._buffered_bytes += idx.nbytes
        self._buf_xyz.append(xyz)
        self._buffered_bytes += xyz.nbytes
        self._count += xyz.shape[0]
        if self._has_rgb and rgb is not None:
            self._buf_rgb.append(rgb)
            self._buffered_bytes += rgb.nbytes
        if self._has_intensity and intensity is not None:
            self._buf_int.append(intensity)
            self._buffered_bytes += intensity.nbytes

    def flush(self) -> None:
        """Append buffered points to the per-channel files, then release the buffers.

        Opens in append-binary mode and closes immediately, so no file descriptor is
        held between flushes. Raw ``ndarray.tobytes()`` preserves dtype and order.
        """
        if self._buf_idx:
            with open(f"{self._base}.idx", "ab") as f:
                for a in self._buf_idx:
                    f.write(np.ascontiguousarray(a).tobytes())
            self._buf_idx = []
        if self._buf_xyz:
            with open(f"{self._base}.xyz", "ab") as f:
                for a in self._buf_xyz:
                    f.write(np.ascontiguousarray(a).tobytes())
            self._buf_xyz = []
        if self._buf_rgb:
            with open(f"{self._base}.rgb", "ab") as f:
                for a in self._buf_rgb:
                    f.write(np.ascontiguousarray(a).tobytes())
            self._buf_rgb = []
        if self._buf_int:
            with open(f"{self._base}.int", "ab") as f:
                for a in self._buf_int:
                    f.write(np.ascontiguousarray(a).tobytes())
            self._buf_int = []
        self._buffered_bytes = 0

    def read(
        self, xyz_dtype: np.dtype, rgb_dtype: np.dtype, int_dtype: np.dtype
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None]:
        """Read this core back as (idx, xyz, rgb, intensity), in append order.

        Flushes any still-buffered points first so the on-disk files are complete,
        so a core small enough never to have flushed still reads correctly.
        """
        self.flush()
        idx_path = Path(f"{self._base}.idx")
        if idx_path.exists():
            idx = np.frombuffer(idx_path.read_bytes(), dtype=np.int64)
        else:
            idx = np.empty(0, dtype=np.int64)

        xyz_path = Path(f"{self._base}.xyz")
        if xyz_path.exists():
            xyz = np.frombuffer(xyz_path.read_bytes(), dtype=xyz_dtype).reshape(-1, 3)
        else:
            xyz = np.empty((0, 3), dtype=xyz_dtype)

        rgb: np.ndarray | None = None
        if self._has_rgb:
            rgb_path = Path(f"{self._base}.rgb")
            if rgb_path.exists():
                rgb = np.frombuffer(rgb_path.read_bytes(), dtype=rgb_dtype).reshape(
                    -1, 3
                )

        intensity: np.ndarray | None = None
        if self._has_intensity:
            int_path = Path(f"{self._base}.int")
            if int_path.exists():
                intensity = np.frombuffer(int_path.read_bytes(), dtype=int_dtype)

        return idx, xyz, rgb, intensity

    def drop(self) -> None:
        """Delete this core's files once no remaining tile can reference it."""
        for ext in (".idx", ".xyz", ".rgb", ".int"):
            p = Path(f"{self._base}{ext}")
            if p.exists():
                p.unlink()


class _TileExtractStore:
    """Owns the temp dir and one ``_TileExtract`` per leaf.

    Bounds Pass-2a RAM by flushing the fullest extracts whenever total buffered
    bytes cross ``flush_bytes``. Because flushing closes the file, at most a handful
    of descriptors are ever open at once regardless of leaf count.
    """

    def __init__(
        self,
        leaf_indices: Sequence[int],
        has_rgb: bool,
        has_intensity: bool,
        tmp_dir: Path | None = None,
        flush_bytes: int = EXTRACT_FLUSH_BYTES,
    ) -> None:
        self._dir = Path(
            tempfile.mkdtemp(
                prefix="pcd_extracts_", dir=str(tmp_dir) if tmp_dir else None
            )
        )
        self._has_rgb = has_rgb
        self._has_intensity = has_intensity
        self._flush_bytes = flush_bytes
        self._buffered_total = 0
        self._extracts: dict[int, _TileExtract] = {
            i: _TileExtract(self._dir / f"leaf_{i}", has_rgb, has_intensity)
            for i in leaf_indices
        }
        # Bounded LRU cache of decoded cores. In Pass 2b each core is read by its own
        # tile AND every neighbouring tile whose expanded box overlaps it (~a dozen
        # reads per core at scale). Neighbours are index-local, so a small cache
        # collapses those to ~one disk read + decode per core, which the profiler
        # showed was ~2/3 of the runtime.
        #
        # The cache is bounded by BYTES, not a core count: a fixed count let a few
        # huge cores (e.g. a 13.6M-point tile) push peak RSS to ~9.9 GB on the 50 GB
        # scene — under the 16 GB budget but eroding the NFR-1.1 margin, and unsafe on
        # a scene with more large cores. Capping cached bytes keeps the cache's RAM
        # contribution bounded regardless of core sizes. Keyed by leaf index; value is
        # the decoded (idx, xyz, rgb, intensity) tuple.
        self._read_cache: dict[int, tuple] = {}
        self._read_cache_order: list[int] = []
        self._read_cache_bytes = 0
        self._read_cache_max_bytes = 512 * 1024 * 1024

    @property
    def tmp_dir(self) -> Path:
        return self._dir

    def append(
        self,
        i: int,
        idx: np.ndarray,
        xyz: np.ndarray,
        rgb: np.ndarray | None,
        intensity: np.ndarray | None,
    ) -> None:
        ex = self._extracts[i]
        before = ex.buffered_bytes
        ex.append(idx, xyz, rgb, intensity)
        self._buffered_total += ex.buffered_bytes - before
        if self._buffered_total >= self._flush_bytes:
            self._flush_fullest()

    def _flush_fullest(self) -> None:
        # Flush largest buffers first until back under budget; each flush frees its
        # buffered bytes. Sorting by current buffer size keeps the number of flushed
        # files small (spill the big ones, leave many tiny ones buffered).
        for ex in sorted(
            self._extracts.values(), key=lambda e: e.buffered_bytes, reverse=True
        ):
            if self._buffered_total < self._flush_bytes:
                break
            freed = ex.buffered_bytes
            if freed == 0:
                break
            ex.flush()
            self._buffered_total -= freed

    def flush_all(self) -> None:
        for ex in self._extracts.values():
            ex.flush()
        self._buffered_total = 0

    def read_core(
        self, i: int, xyz_dtype: np.dtype, rgb_dtype: np.dtype, int_dtype: np.dtype
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None]:
        """Read one core's stored points as (idx, xyz, rgb, intensity).

        Served from a bounded LRU cache so a core read by several neighbouring tiles
        is decoded from disk once, not once per consuming tile.
        """
        cached = self._read_cache.get(i)
        if cached is not None:
            self._read_cache_order.remove(i)
            self._read_cache_order.append(i)
            return cached
        result = self._extracts[i].read(xyz_dtype, rgb_dtype, int_dtype)
        self._read_cache[i] = result
        self._read_cache_order.append(i)
        self._read_cache_bytes += _cached_tuple_bytes(result)
        # Evict least-recently-used cores until the cache is back under its BYTE
        # budget. A single core larger than the whole budget is still cached (it was
        # just read and is needed now); it is evicted on the next insert.
        while (
            self._read_cache_bytes > self._read_cache_max_bytes
            and len(self._read_cache_order) > 1
        ):
            evict = self._read_cache_order.pop(0)
            evicted = self._read_cache.pop(evict, None)
            if evicted is not None:
                self._read_cache_bytes -= _cached_tuple_bytes(evicted)
        return result

    def drop(self, i: int) -> None:
        # Evict from the read cache too, so a dropped core's arrays are released.
        if i in self._read_cache:
            evicted = self._read_cache.pop(i, None)
            self._read_cache_order.remove(i)
            if evicted is not None:
                self._read_cache_bytes -= _cached_tuple_bytes(evicted)
        self._extracts[i].drop()

    def cleanup(self) -> None:
        """Remove the whole temp dir. Safe to call on any exit path."""
        self._read_cache.clear()
        self._read_cache_order.clear()
        self._read_cache_bytes = 0
        shutil.rmtree(self._dir, ignore_errors=True)


def _cached_tuple_bytes(
    t: tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None],
) -> int:
    """Resident bytes of a decoded (idx, xyz, rgb, intensity) core tuple."""
    return sum(a.nbytes for a in t if a is not None)


def _estimate_extract_footprint_bytes(
    points_read: int, has_rgb: bool, has_intensity: bool
) -> int:
    """Uncompressed byte size of all extracts, for the disk-headroom check.

    Store-once: each point is written to exactly ONE core extract, so the footprint
    is the raw point bytes with NO guard-band duplication multiplier. Per point:
    xyz float64 (24 B) + an int64 source-row index (8 B, needed to restore file order
    when a tile is reconstructed from its own core plus neighbours) + rgb uint8 x3
    (3 B) + intensity float32 (4 B) when present.

    This is exact, not a guess. The earlier duplicating design multiplied this by an
    overlap factor that was wildly low: on the 50 GB scene it estimated ~210 GB but
    the real duplicated footprint blew past 412 GB and filled the disk. Store-once
    removes that multiplier entirely, so the estimate the disk-headroom check uses is
    now the true size.
    """
    per_point = 24 + 8  # xyz float64 + int64 source-row index
    if has_rgb:
        per_point += 3
    if has_intensity:
        per_point += 4
    return int(points_read * per_point)
