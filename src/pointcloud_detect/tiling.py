"""Adaptive tiling engine for large point clouds.

Density-aware, axis-qualified octree subdivision. A node is split when it exceeds
the point budget; it is cut on every axis whose extent is still larger than
``min_tile_size_m``, so:

- Indoor scenes (roughly cubic, uniformly dense) split 8 ways — a true octree.
- Outdoor scenes (large XY, thin Z) degrade to quadtree-like XY splits without
  wasting tiles on the short Z axis. Strict 8-way subdivision would spend half its
  children on empty top/bottom octants every level for ground-plane-dominated data.

Split planes are placed at the point-count MEDIAN of the node's own points along
each qualifying axis, rather than the geometric midpoint, so a cut falls in the
sparse gaps between clusters instead of slicing a dense object down the middle.
(The median comes straight from the node's coordinates. ``compute_density_grid``
below is a standalone planning helper and does not feed split placement.)

This is what makes the subdivision "density-aware": dense regions get finer tiles
and objects are less likely to be fragmented across a boundary.
"""

import warnings

import numpy as np

from pointcloud_detect.config import TilingConfig
from pointcloud_detect.memory import effective_max_points_per_tile
from pointcloud_detect.models import PointCloud, Tile

# Ceiling on density-grid cells. The grid is sized by scene extent, not point
# count, so one stray coordinate can size it for the whole scene: 1000 points plus
# a single point at 5e6 m requests 888 PiB. 64M cells is 512 MB at int64 — ample
# for a coarse planning grid and small enough to fail before exhausting memory.
MAX_DENSITY_GRID_CELLS = 64_000_000


def compute_density_grid(
    xyz: np.ndarray,
    resolution: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute point counts on a coarse 3D grid.

    Returns grid counts, grid origin, and grid shape.

    This is a standalone planning helper. ``adaptive_tile`` does NOT call it — split
    planes come from the node's own coordinate median — so nothing in the pipeline
    reaches this function today. The validation below is therefore defensive
    groundwork for callers that pass it untrusted coordinates, not a guard on a
    live path.

    Raises ValueError if ``resolution`` is not positive, if the coordinates are
    not finite, or if the requested grid would exceed
    ``MAX_DENSITY_GRID_CELLS``. The cell count is driven by scene extent rather
    than point count, so a single outlier coordinate is enough to make the
    allocation unbounded.
    """
    if not resolution > 0:
        raise ValueError(f"resolution must be positive, got {resolution}")

    bounds_min = xyz.min(axis=0)
    bounds_max = xyz.max(axis=0)

    if not (np.all(np.isfinite(bounds_min)) and np.all(np.isfinite(bounds_max))):
        raise ValueError(
            "Point coordinates contain NaN or inf; cannot size a density grid."
        )

    grid_shape = np.ceil((bounds_max - bounds_min) / resolution).astype(int)
    grid_shape = np.maximum(grid_shape, 1)

    # Multiply as Python ints: np.prod on an int64 array silently overflows to a
    # negative value here, which surfaces later as a confusing
    # "minlength must not be negative" from np.bincount.
    total_cells = 1
    for dim in grid_shape.tolist():
        total_cells *= int(dim)
    if total_cells > MAX_DENSITY_GRID_CELLS:
        extent = (bounds_max - bounds_min).tolist()
        raise ValueError(
            f"Density grid would need {total_cells:,} cells "
            f"(limit {MAX_DENSITY_GRID_CELLS:,}) for scene extent {extent} at "
            f"resolution {resolution}. Check for outlier coordinates, or use a "
            "coarser resolution."
        )

    cell_indices = np.floor((xyz - bounds_min) / resolution).astype(int)
    cell_indices = np.clip(cell_indices, 0, grid_shape - 1)

    # Vectorised bincount over flattened cell indices (fast for large clouds).
    flat = (
        cell_indices[:, 0] * grid_shape[1] * grid_shape[2]
        + cell_indices[:, 1] * grid_shape[2]
        + cell_indices[:, 2]
    )
    grid = np.bincount(flat, minlength=total_cells).reshape(grid_shape)

    return grid, bounds_min, grid_shape


def _density_median_split(
    coords: np.ndarray,
    lo: float,
    hi: float,
    min_tile_size_m: float,
) -> float:
    """Choose a split plane on one axis at the point-count median.

    ``coords`` are the point coordinates on the axis being split. The median of
    the points falls in a sparse gap between clusters far more often than the
    geometric midpoint does, so a cut there is less likely to bisect a dense
    object. An out-of-range median is clamped to the nearest legal plane, so the
    cut stays as close to the true median as the minimum tile size allows.
    """
    midpoint = (lo + hi) / 2.0
    if coords.size == 0:
        return midpoint
    median = float(np.median(coords))
    # The axis-qualification guard in _subdivide (extent >= 2 * min_tile_size_m)
    # only implies both children clear the minimum when the cut is at the
    # midpoint. Restrict the median to the window where that still holds, so the
    # guard and the split placement agree; otherwise a median near a bound
    # produces a child a fraction of min_tile_size_m wide.
    #
    # Clamp rather than fall back to the midpoint. The nearest legal plane is by
    # construction at least as close to the true median as the midpoint is, so
    # clamping is never worse and is usually better. Falling back discarded the
    # density signal entirely in exactly the skewed distributions this function
    # exists to handle, and because the legal window narrows to nothing as the
    # extent approaches 2 x min_tile_size_m, that made the deepest levels of the
    # tree — the dense ones — silently revert to a midpoint splitter.
    #
    # The window is never empty here: the axis guard gives hi - lo >= 2 *
    # min_tile_size_m, hence lo_limit <= hi_limit. The midpoint above is legal for
    # the same reason.
    lo_limit = lo + min_tile_size_m
    hi_limit = hi - min_tile_size_m
    return float(np.clip(median, lo_limit, hi_limit))


def adaptive_tile(cloud: PointCloud, config: TilingConfig) -> list[Tile]:
    """Tile a point cloud adaptively based on point density.

    Density-aware, axis-qualified octree subdivision: recursively splits a node
    on every axis still larger than ``min_tile_size_m``, placing each split plane
    at the point-count median, until every leaf is within ``max_points_per_tile``
    or cannot be subdivided further.
    """
    if cloud.num_points == 0:
        return []

    # Non-finite coordinates must be rejected before any split arithmetic. NaN
    # propagates through np.median, and every comparison against a NaN plane is
    # False, so each child mask excludes all points: tiles are still emitted but
    # carry zero points, and the job reports success having silently dropped the
    # whole cloud. Infinities blow up the bounds instead. Fail loudly.
    #
    # Checked via the bounds rather than np.isfinite(cloud.xyz), which would
    # allocate an (N, 3) boolean array — ~1.5 GB on a 500M-point cloud, in the
    # one code path whose purpose is bounding memory. min/max are reductions and
    # propagate both NaN and inf, so the bounds catch every non-finite coordinate.
    lo_bounds = cloud.bounds_min
    hi_bounds = cloud.bounds_max
    if not (np.all(np.isfinite(lo_bounds)) and np.all(np.isfinite(hi_bounds))):
        bad_axes = [
            "XYZ"[i]
            for i in range(3)
            if not (np.isfinite(lo_bounds[i]) and np.isfinite(hi_bounds[i]))
        ]
        raise ValueError(
            "Point cloud contains non-finite coordinates (NaN or inf) on "
            f"axis/axes {', '.join(bad_axes)}. Clean or filter the cloud before "
            "tiling."
        )

    # Enforce the memory budget: lower the per-tile point cap so a single tile's
    # inference working set fits memory_budget_gb (NFR-1.1). Never raises the
    # configured ceiling, only lowers it.
    max_points = effective_max_points_per_tile(
        configured_max=config.max_points_per_tile,
        memory_budget_gb=config.memory_budget_gb,
        has_rgb=cloud.rgb is not None,
        has_intensity=cloud.intensity is not None,
        has_normals=cloud.normals is not None,
    )

    tiles: list[Tile] = []
    over_budget: list[tuple[int, np.ndarray]] = []
    # The root's margin-expanded region contains every point, so the root
    # candidate set is the whole cloud. Passing the same array for both is
    # deliberate — _subdivide detects the equal-length case and skips a redundant
    # (N, 3) copy.
    #
    # bounds_min/bounds_max are recomputed properties on PointCloud, so reuse the
    # values already read above for the finiteness check rather than reducing over
    # the whole cloud twice more.
    all_indices = np.arange(cloud.num_points)
    _subdivide(
        cloud=cloud,
        bounds_min=lo_bounds,
        bounds_max=hi_bounds,
        indices=all_indices,
        candidates=all_indices,
        config=config,
        max_points=max_points,
        tiles=tiles,
        over_budget=over_budget,
    )

    if over_budget:
        _warn_over_budget(over_budget, config, max_points)

    return tiles


def _warn_over_budget(
    over_budget: list[tuple[int, np.ndarray]],
    config: TilingConfig,
    effective_max: int,
) -> None:
    """Report regions that stayed over the point budget at the minimum tile size.

    One aggregated warning rather than one per tile: a dense scene can produce many
    of these, and an operator reading a batch log needs the worst case and the
    knobs that affect it, not a repeated line per occurrence.

    ``effective_max`` is the cap the recursion actually used. It is the configured
    max_points_per_tile unless memory_budget_gb lowered it, and reporting the
    configured value in that case would understate the overrun and name a number
    the operator cannot act on.
    """
    worst_count, worst_extent = max(over_budget, key=lambda r: r[0])
    budget = effective_max
    if effective_max < config.max_points_per_tile:
        budget_desc = (
            f"the effective cap of {effective_max} points "
            f"(memory_budget_gb={config.memory_budget_gb} lowered it from "
            f"max_points_per_tile={config.max_points_per_tile})"
        )
        remedy = (
            "Lower min_tile_size_m to let the tree cut finer, or raise "
            "memory_budget_gb if the host can absorb it."
        )
    else:
        budget_desc = f"max_points_per_tile={config.max_points_per_tile}"
        remedy = (
            "Lower min_tile_size_m to let the tree cut finer, or raise "
            "max_points_per_tile if inference can absorb it."
        )
    extent_str = " x ".join(f"{v:.3g}" for v in worst_extent)
    warnings.warn(
        f"{len(over_budget)} tile(s) exceed {budget_desc} and cannot be subdivided "
        f"further: their regions are already within "
        f"2 x min_tile_size_m={config.min_tile_size_m}. Worst case is "
        f"{worst_count} points ({worst_count / budget:.1f}x budget) in a region "
        f"of {extent_str} m. Each of these is one inference call carrying more "
        f"points than the budget was sized for, which risks running out of memory "
        f"during inference. {remedy}",
        RuntimeWarning,
        stacklevel=3,
    )


def _select_within(
    pts: np.ndarray,
    index_map: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
) -> np.ndarray:
    """Return entries of ``index_map`` whose ``pts`` row lies in [lo, hi].

    Builds the mask one axis at a time with in-place ``&=`` rather than
    ``np.all((pts >= lo) & (pts <= hi), axis=1)``. The vectorised form allocates
    two (M, 3) boolean arrays plus the reduction output — 6M bytes of transient
    per call. Axis-at-a-time holds one (M,) accumulator and one (M,) comparison
    temporary, so 2M bytes. On the tiling hot path this is the difference between
    a peak of 9x the cloud footprint and roughly 1x.
    """
    mask = pts[:, 0] >= lo[0]
    mask &= pts[:, 0] <= hi[0]
    mask &= pts[:, 1] >= lo[1]
    mask &= pts[:, 1] <= hi[1]
    mask &= pts[:, 2] >= lo[2]
    mask &= pts[:, 2] <= hi[2]
    return index_map[mask]


def _subdivide(
    cloud: PointCloud,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
    indices: np.ndarray,
    candidates: np.ndarray,
    config: TilingConfig,
    max_points: int,
    tiles: list[Tile],
    over_budget: list[tuple[int, np.ndarray]],
) -> None:
    """Recursively subdivide a region into tiles (axis-qualified octree).

    ``indices`` are the points strictly inside [bounds_min, bounds_max]; they
    drive the budget decision and the recursion. ``candidates`` are the points
    inside that region expanded by ``overlap_margin_m`` — the guard band — and
    become the emitted tile's contents.

    ``max_points`` is the effective (budget-capped) per-tile point limit computed by
    ``adaptive_tile``; it may be lower than ``config.max_points_per_tile``.

    ``over_budget`` accumulates ``(point_count, extent)`` for regions that hit the
    minimum-tile-size floor while still over the point budget, so the caller can
    report them together instead of one warning per occurrence.

    Threading ``candidates`` down the tree is what keeps this linear-ish. A child's
    expanded region is always contained in its parent's expanded region
    (``child_min >= parent_min`` implies ``child_min - margin >= parent_min -
    margin``), so each level can narrow the previous level's candidate set instead
    of rescanning the whole cloud. Selecting per tile from the full cloud instead
    is O(N x tiles); this is O(N x depth), and depth is a handful where tile
    counts run to hundreds of thousands.
    """
    # Stop condition 1: within the point budget. Uses the effective cap, so
    # lowering memory_budget_gb tightens tiling rather than only affecting routing.
    if len(indices) <= max_points:
        _emit_tile(cloud, bounds_min, bounds_max, candidates, config, tiles)
        return

    # Determine which axes are still large enough to split. An axis qualifies only
    # if splitting it would leave both halves >= min_tile_size_m, i.e. the axis is
    # at least 2 x min_tile_size_m. This is what makes indoor scenes split 8-way
    # while thin outdoor scenes split only in X/Y.
    extent = bounds_max - bounds_min
    splittable = extent >= config.min_tile_size_m * 2.0

    # Stop condition 2: no axis can be split further; emit as-is rather than
    # recursing forever on a dense region smaller than 2 x min_tile_size_m.
    #
    # This is the one place a tile can exceed the point budget, and it cannot be
    # fixed by splitting harder — the region is already at the floor. Capping the
    # contents instead would silently drop points, which is worse. So record it and
    # let adaptive_tile surface it: the caller needs to know that a tile is going
    # into inference carrying more points than the budget it sized itself for.
    if not np.any(splittable):
        over_budget.append((len(indices), extent.copy()))
        _emit_tile(cloud, bounds_min, bounds_max, candidates, config, tiles)
        return

    # Place a split plane on each qualifying axis at the point-count median.
    splits: dict[int, float] = {}
    for axis in range(3):
        if splittable[axis]:
            coords = cloud.xyz[indices, axis]
            splits[axis] = _density_median_split(
                coords,
                float(bounds_min[axis]),
                float(bounds_max[axis]),
                config.min_tile_size_m,
            )

    # Enumerate child octants: the Cartesian product of {low, high} over each
    # qualifying axis. Non-split axes span the full parent extent. For 1 split axis
    # this yields 2 children (kd/quadtree-like), for 2 axes 4, for 3 axes 8.
    child_bounds = _octant_bounds(bounds_min, bounds_max, splits)

    # Materialise the parent's points once, not once per child: an 8-way split
    # would otherwise copy the whole parent block eight times.
    #
    # At the root, indices is arange(num_points), so cloud.xyz[indices] copies the
    # entire cloud to produce something identical to cloud.xyz. Read the array
    # directly instead — on a 50 GB cloud that copy is 50 GB, in the code path
    # whose job is to bound memory. pts is only ever read.
    if len(indices) == cloud.num_points:
        pts = cloud.xyz
    else:
        pts = cloud.xyz[indices]

    # indices is a subset of candidates and both are ascending, so equal lengths
    # mean equal contents — reuse the block rather than taking a second (M, 3)
    # copy. This is the common case at the root, and whenever overlap_margin_m
    # admits no extra points.
    if len(candidates) == len(indices):
        cand_pts = pts
    elif len(candidates) == cloud.num_points:
        cand_pts = cloud.xyz
    else:
        cand_pts = cloud.xyz[candidates]

    margin = config.overlap_margin_m

    # Per-axis masks, computed once and shared by all children rather than
    # recomputed inside the child loop. An 8-way split previously ran 3 comparisons
    # per child (24 passes over the parent block); this is 2 per split axis (6).
    #
    # Exact membership uses a half-open interval on each split axis so every point
    # lands in exactly one child (no double-counting).
    # Only the low-side mask is stored per axis; the high side is its complement,
    # taken as a short-lived temporary in the child loop. Storing both would hold
    # an extra (M,) array per split axis live for the whole loop.
    exact_low: dict[int, np.ndarray] = {}
    # Guard-band membership: a child on the low side of an axis spans
    # [parent_min - margin, plane + margin]. Every candidate already satisfies the
    # parent's lower bound by the recursion invariant, so the only new constraint
    # is the upper one. Symmetrically for the high side. These two are genuinely
    # different comparisons rather than complements, so both are needed.
    band_low: dict[int, np.ndarray] = {}
    band_high: dict[int, np.ndarray] = {}
    for axis, plane in splits.items():
        exact_low[axis] = pts[:, axis] <= plane
        cand_col = cand_pts[:, axis]
        band_low[axis] = cand_col <= plane + margin
        band_high[axis] = cand_col >= plane - margin

    for child_min, child_max, sides in child_bounds:
        mask: np.ndarray | None = None
        cand_mask: np.ndarray | None = None
        for axis in splits:
            side_high = sides[axis]
            exact = ~exact_low[axis] if side_high else exact_low[axis]
            band = band_high[axis] if side_high else band_low[axis]
            # First axis takes ownership of a fresh array; later axes accumulate
            # in place. `~` already produced a new array on the high side, so only
            # the low side needs an explicit copy.
            if mask is None:
                mask = exact if side_high else exact.copy()
                cand_mask = band.copy()
            else:
                mask &= exact
                assert cand_mask is not None
                cand_mask &= band

        # splits is non-empty here (guarded by the np.any(splittable) check
        # above), so both masks were assigned.
        assert mask is not None and cand_mask is not None

        child_indices = indices[mask]
        if len(child_indices) == 0:
            continue
        _subdivide(
            cloud,
            child_min,
            child_max,
            child_indices,
            candidates[cand_mask],
            config,
            max_points,
            tiles,
            over_budget,
        )


def _octant_bounds(
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
    splits: dict[int, float],
) -> list[tuple[np.ndarray, np.ndarray, dict[int, bool]]]:
    """Enumerate child region bounds from the per-axis split planes.

    ``splits`` maps axis -> split plane. Produces 2**len(splits) child boxes: the
    Cartesian product of the low/high half on each split axis, with non-split axes
    spanning the full parent extent.

    Each entry is ``(child_min, child_max, sides)`` where ``sides`` maps axis ->
    True for the high half. The caller must use these bits rather than re-deriving
    the side from the child bounds: that inference only holds while the plane sits
    strictly inside the parent, and silently misclassifies every child when the
    plane coincides with a bound.
    """
    axes = sorted(splits.keys())
    result: list[tuple[np.ndarray, np.ndarray, dict[int, bool]]] = []
    for combo in range(2 ** len(axes)):
        child_min = bounds_min.copy()
        child_max = bounds_max.copy()
        sides: dict[int, bool] = {}
        for bit, axis in enumerate(axes):
            plane = splits[axis]
            is_high = bool((combo >> bit) & 1)
            sides[axis] = is_high
            if is_high:
                child_min[axis] = plane  # high half
            else:
                child_max[axis] = plane  # low half
        # Return the side bit explicitly. Re-deriving it from the child bounds
        # (child_min[axis] >= plane) is only valid while the plane sits strictly
        # inside the parent bounds; when it coincides with a bound, every child
        # infers as the high side and the point masks exclude everything.
        result.append((child_min, child_max, sides))
    return result


def _emit_tile(
    cloud: PointCloud,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
    candidates: np.ndarray,
    config: TilingConfig,
    tiles: list[Tile],
) -> None:
    """Create a tile with overlap margin applied.

    The tile's core region is [bounds_min, bounds_max]; its points are those within
    that region expanded by overlap_margin_m on every face, so neighbouring tiles
    share their boundary points (guard band). This is what lets an object
    straddling a tile edge be seen whole by at least one tile; the resulting
    duplicate detections are removed by global 3D NMS downstream.

    ``candidates`` is the caller's already-narrowed index set for this node's
    expanded region. The filter below is therefore normally a no-op — it is kept so
    the docstring contract holds for any caller, rather than depending on an
    invariant threaded through the recursion. It costs one pass over a tile-sized
    array, not over the cloud.
    """
    # Checked here rather than in _subdivide so it covers every path that emits a
    # tile, including the two early-return stop conditions. Opt-in: None means no
    # ceiling, so this cannot reject a legitimately large scene by default.
    if config.max_tiles is not None and len(tiles) >= config.max_tiles:
        raise ValueError(
            f"Tiling produced more than max_tiles={config.max_tiles} tiles. Each "
            "tile is one inference call. Raise max_tiles, raise "
            "max_points_per_tile, or raise min_tile_size_m for this scene."
        )

    margin = config.overlap_margin_m
    # Same root-copy avoidance as in _subdivide: when the candidate set is the whole
    # cloud (single-tile scenes), fancy-indexing it would duplicate the cloud.
    if len(candidates) == cloud.num_points:
        cand_pts = cloud.xyz
    else:
        cand_pts = cloud.xyz[candidates]
    tile_indices = _select_within(
        cand_pts,
        candidates,
        bounds_min - margin,
        bounds_max + margin,
    )

    tile = Tile(
        xyz=cloud.xyz[tile_indices],
        rgb=cloud.rgb[tile_indices] if cloud.rgb is not None else None,
        intensity=(
            cloud.intensity[tile_indices] if cloud.intensity is not None else None
        ),
        normals=(cloud.normals[tile_indices] if cloud.normals is not None else None),
        origin=(bounds_min + bounds_max) / 2.0,
        extent_min=bounds_min,
        extent_max=bounds_max,
        point_indices=tile_indices,
    )
    tiles.append(tile)
