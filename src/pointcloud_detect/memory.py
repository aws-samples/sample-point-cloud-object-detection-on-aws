"""Memory-budget accounting for the tiling pipeline (NFR-1.1 / NFR-1.2).

Turns ``memory_budget_gb`` from an unused config field into an enforced constraint:

- ``bytes_per_point`` — how many bytes one point occupies given its channels.
- ``budget_derived_max_points`` — the largest per-tile point count whose inference
  working set fits the budget. Used to lower ``max_points_per_tile`` so a single tile
  cannot blow the budget.
- ``estimate_cloud_footprint_bytes`` / ``modelled_whole_load_peak_bytes`` /
  ``exceeds_budget`` — whether loading the WHOLE cloud would exceed the budget,
  which is the signal to use the streaming ingest path instead.

These are deliberately simple, explicit models rather than live memory probes, so they
are unit-testable without real data. Read the constants below for which of them are
measured and which are estimates — they are not all on the same footing.
"""

from __future__ import annotations

# Raw bytes per point per channel in the unified PointCloud representation.
_XYZ_BYTES = 8 * 3  # float64 x 3
_RGB_BYTES = 1 * 3  # uint8 x 3
_INTENSITY_BYTES = 4  # float32
_NORMALS_BYTES = 4 * 3  # float32 x 3

# Two INDEPENDENT safety fractions. These were a single DEFAULT_SAFETY_FRACTION
# gating both the per-tile cap and the whole-vs-stream routing decision, so an
# operator lowering it to fix a per-tile overshoot silently changed which files
# stream: 0.5 -> 0.1 moved the streaming threshold from 258 M points to 52 M.
# Splitting them makes each remedy do only what it says.
TILE_SAFETY_FRACTION = 0.5
ROUTING_SAFETY_FRACTION = 0.8

# Retained for backward compatibility with callers that passed the old name.
DEFAULT_SAFETY_FRACTION = TILE_SAFETY_FRACTION

# Multiple of the raw point footprint at which the WHOLE-LOAD path actually peaks.
# This one is measured, on a clustered scene with tracemalloc: adaptive_tile peaks
# at 8.92x the cloud footprint at the default overlap_margin_m=2.0 (6.64x retained
# tiles, since a point near a boundary legitimately belongs to several, plus 2.28x
# transient), and laspy's read peaks at ~3.4x because it materialises the whole
# file before max_points is applied.
#
# Routing previously compared the RAW footprint against half the budget — a 2x
# allowance against a measured ~9x — so at a 16 GB budget it whole-loaded anything
# up to 258 M points, an 8.0 GB footprint that peaks near 29 GB in tiling alone,
# before inference.
WHOLE_LOAD_PEAK_FACTOR = 9.0

# Multiple of raw tile bytes that inference is expected to materialise (features,
# voxelisation, network activations).
#
# NOT measured. The on-hardware benchmark that would calibrate this has not been
# run (see docs/diagrams/memory-budget-enforcement.md), so this is a conservative
# placeholder, not an empirical figure. Earlier comments here and in that doc
# claimed it was "tuned against the benchmark", which was not true.
DEFAULT_WORKING_SET_FACTOR = 4.0

GB = 1_000_000_000


def bytes_per_point(
    has_rgb: bool = False,
    has_intensity: bool = False,
    has_normals: bool = False,
) -> int:
    """Bytes one point occupies given which channels are present."""
    total = _XYZ_BYTES
    if has_rgb:
        total += _RGB_BYTES
    if has_intensity:
        total += _INTENSITY_BYTES
    if has_normals:
        total += _NORMALS_BYTES
    return total


def budget_derived_max_points(
    memory_budget_gb: float,
    has_rgb: bool = False,
    has_intensity: bool = False,
    has_normals: bool = False,
    safety_fraction: float = TILE_SAFETY_FRACTION,
    working_set_factor: float = DEFAULT_WORKING_SET_FACTOR,
) -> int:
    """Largest per-tile point count whose working set fits the budget.

    working_set_bytes(n) = n * bytes_per_point * working_set_factor
    Solve working_set_bytes(n) <= budget * safety_fraction for n.
    """
    if memory_budget_gb <= 0:
        raise ValueError("memory_budget_gb must be positive")
    bpp = bytes_per_point(has_rgb, has_intensity, has_normals)
    usable = memory_budget_gb * GB * safety_fraction
    n = int(usable // (bpp * working_set_factor))
    return max(n, 1)


def effective_max_points_per_tile(
    configured_max: int,
    memory_budget_gb: float,
    has_rgb: bool = False,
    has_intensity: bool = False,
    has_normals: bool = False,
    safety_fraction: float = TILE_SAFETY_FRACTION,
    working_set_factor: float = DEFAULT_WORKING_SET_FACTOR,
) -> int:
    """Reconcile the configured cap with the budget-derived cap.

    The budget can only ever LOWER the configured ceiling, never raise it: an operator
    who set a small ``max_points_per_tile`` keeps it; the budget just prevents a large
    (or default) value from exceeding what memory allows.

    Be aware of the regime this actually binds in. At shipped defaults
    (``max_points_per_tile=100_000``, ``memory_budget_gb=16.0``) the derived cap is
    83,333,333 — about 833x the configured value — so ``min()`` returns the
    configured one and this changes nothing. The derived cap only drops below
    100,000 once the budget falls under roughly 19.2 MB.

    That is arithmetically correct rather than broken: one 100,000-point tile is
    ~3 MB raw, so no 16 GB budget needs to shrink it. But it means this function
    alone does NOT enforce NFR-1.1 at any realistic configuration — the routing
    decision in ``exceeds_budget`` and the streaming path are what bound peak
    memory on a large scene. ``test_derived_cap_binding_threshold`` pins the
    crossover so this stays visible instead of being assumed.
    """
    derived = budget_derived_max_points(
        memory_budget_gb,
        has_rgb,
        has_intensity,
        has_normals,
        safety_fraction,
        working_set_factor,
    )
    return min(configured_max, derived)


def estimate_cloud_footprint_bytes(
    num_points: int,
    has_rgb: bool = False,
    has_intensity: bool = False,
    has_normals: bool = False,
) -> int:
    """Raw bytes to hold the whole cloud in memory (excludes inference working set)."""
    return num_points * bytes_per_point(has_rgb, has_intensity, has_normals)


def modelled_whole_load_peak_bytes(
    num_points: int,
    has_rgb: bool = False,
    has_intensity: bool = False,
    has_normals: bool = False,
    peak_factor: float = WHOLE_LOAD_PEAK_FACTOR,
) -> int:
    """Modelled PEAK bytes for the whole-load path, not just the resident cloud.

    The raw footprint is what the points occupy once; peak is what the process
    actually reaches while reading and tiling them. Measurement puts that at ~9x
    the footprint, so the two differ by nearly an order of magnitude.
    """
    footprint = estimate_cloud_footprint_bytes(
        num_points, has_rgb, has_intensity, has_normals
    )
    return int(footprint * peak_factor)


def exceeds_budget(
    num_points: int,
    memory_budget_gb: float,
    has_rgb: bool = False,
    has_intensity: bool = False,
    has_normals: bool = False,
    safety_fraction: float = ROUTING_SAFETY_FRACTION,
    peak_factor: float = WHOLE_LOAD_PEAK_FACTOR,
) -> bool:
    """Whether the whole-load path would exceed the budget, so we should stream.

    Compares the modelled PEAK against the budget. Comparing the raw footprint
    instead — as this did — under-estimates by the measured ~9x materialisation
    factor, so scenes were whole-loaded that then peaked far over budget: at 16 GB
    the worst permitted whole-load was an 8.0 GB footprint peaking near 29 GB in
    tiling, before inference had allocated anything.
    """
    peak = modelled_whole_load_peak_bytes(
        num_points, has_rgb, has_intensity, has_normals, peak_factor
    )
    usable = memory_budget_gb * GB * safety_fraction
    return peak > usable
