"""Point cloud preprocessing: downsampling, channel selection, cropping."""

import numpy as np

from pointcloud_detect.config import PreprocessingConfig
from pointcloud_detect.models import PointCloud


def voxel_downsample(xyz: np.ndarray, voxel_size: float) -> np.ndarray:
    """Return indices of points to keep after voxel grid downsampling."""
    voxel_coords = np.floor(xyz / voxel_size).astype(np.int64)
    _, unique_indices = np.unique(voxel_coords, axis=0, return_index=True)
    return np.sort(unique_indices)


def crop_to_range(xyz: np.ndarray, point_cloud_range: list[float]) -> np.ndarray:
    """Return indices of points within the specified spatial range."""
    x_min, y_min, z_min, x_max, y_max, z_max = point_cloud_range
    mask = (
        (xyz[:, 0] >= x_min)
        & (xyz[:, 0] <= x_max)
        & (xyz[:, 1] >= y_min)
        & (xyz[:, 1] <= y_max)
        & (xyz[:, 2] >= z_min)
        & (xyz[:, 2] <= z_max)
    )
    return np.where(mask)[0]


# Coordinate system transforms to Z-up
# Each maps (source_x, source_y, source_z) -> (target_x, target_y, target_z)
# where target Z = up
COORD_TRANSFORMS: dict[str, np.ndarray] = {
    # Z-up (default, no transform)
    "z-up": np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64),
    # Y-up: swap Y and Z
    "y-up": np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float64),
    # Y-down / colmap
    "colmap": np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]], dtype=np.float64),
    # X-up
    "x-up": np.array([[0, 0, -1], [0, 1, 0], [1, 0, 0]], dtype=np.float64),
    # ROS (X-forward, Y-left, Z-up)
    "ros": np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=np.float64),
}

# Z-up with rotation around Z axis (for data that is already Z-up but rotated)
for _deg in [45, 90, 135, 180, -45, -90, -135]:
    _a = np.radians(_deg)
    _c, _s = np.cos(_a), np.sin(_a)
    COORD_TRANSFORMS[f"z-rot{_deg:+d}"] = np.array(
        [[_c, -_s, 0], [_s, _c, 0], [0, 0, 1]], dtype=np.float64
    )

# X-axis rotations (for data that is Z-up but tilted around X)
for _deg in [45, 90, -45, -90]:
    _a = np.radians(_deg)
    _c, _s = np.cos(_a), np.sin(_a)
    COORD_TRANSFORMS[f"x-rot{_deg:+d}"] = np.array(
        [[1, 0, 0], [0, _c, -_s], [0, _s, _c]], dtype=np.float64
    )

# Livox Mid-360: Z-up, rotated +27 degrees around Y axis
_livox_a = np.radians(27)
_livox_c, _livox_s = np.cos(_livox_a), np.sin(_livox_a)
COORD_TRANSFORMS["livox"] = np.array(
    [[_livox_c, 0, _livox_s], [0, 1, 0], [-_livox_s, 0, _livox_c]], dtype=np.float64
)

# Leica RTC360: Z-up, rotated +60 degrees around Z axis
_leica_a = np.radians(60)
_leica_c, _leica_s = np.cos(_leica_a), np.sin(_leica_a)
COORD_TRANSFORMS["leica"] = np.array(
    [[_leica_c, -_leica_s, 0], [_leica_s, _leica_c, 0], [0, 0, 1]], dtype=np.float64
)


def transform_coordinates(xyz: np.ndarray, source_frame: str) -> np.ndarray:
    """Transform point cloud coordinates to Z-up frame."""
    if source_frame == "z-up":
        return xyz
    if source_frame == "auto":
        return _auto_align(xyz)
    if source_frame not in COORD_TRANSFORMS:
        raise ValueError(
            f"Unknown coordinate frame: {source_frame}. "
            f"Available: {list(COORD_TRANSFORMS.keys()) + ['auto']}"
        )
    rotation = COORD_TRANSFORMS[source_frame]
    return (rotation @ xyz.T).T


def _auto_align(xyz: np.ndarray) -> np.ndarray:
    """Automatically align point cloud so floor is on XY plane (Z=up).

    1. Find which axis has the flattest extremes (floor/ceiling) -> that's up
    2. Rotate so that axis becomes Z
    3. Rotate around Z to align walls with X/Y axes
    """
    # Step 1: Find up direction using PCA on bottom points of each axis
    best_axis = 1  # default Y
    best_std = float("inf")
    flip = False

    for axis in range(3):
        vals = xyz[:, axis]
        # Check bottom 5%
        bottom = xyz[vals < np.percentile(vals, 5)]
        std_bottom = bottom[:, axis].std()
        if std_bottom < best_std:
            best_std = std_bottom
            best_axis = axis
            flip = False
        # Check top 5%
        top = xyz[vals > np.percentile(vals, 95)]
        std_top = top[:, axis].std()
        if std_top < best_std:
            best_std = std_top
            best_axis = axis
            flip = True

    # Step 2: Rotate so best_axis becomes Z
    if best_axis == 2:
        xyz_up = xyz.copy()
    elif best_axis == 1:
        # Y -> Z: swap Y and Z
        xyz_up = xyz[:, [0, 2, 1]].copy()
    else:
        # X -> Z: swap X and Z
        xyz_up = xyz[:, [2, 1, 0]].copy()

    if flip:
        xyz_up[:, 2] = -xyz_up[:, 2]

    # Check if floor is at bottom (negative Z) — if not, flip
    bottom_z = xyz_up[xyz_up[:, 2] < np.percentile(xyz_up[:, 2], 5)]
    top_z = xyz_up[xyz_up[:, 2] > np.percentile(xyz_up[:, 2], 95)]
    if bottom_z[:, 2].std() > top_z[:, 2].std():
        xyz_up[:, 2] = -xyz_up[:, 2]

    # Step 3: Rotate around Z to align walls with X/Y
    floor_mask = xyz_up[:, 2] < np.percentile(xyz_up[:, 2], 5)
    floor_xy = xyz_up[floor_mask][:, :2]
    centered = floor_xy - floor_xy.mean(axis=0)
    cov = np.cov(centered.T)
    _, eigvecs = np.linalg.eigh(cov)
    principal = eigvecs[:, 1]  # largest eigenvalue direction
    angle = np.arctan2(principal[1], principal[0])

    # Rotate to align principal direction with X-axis
    cos_a = np.cos(-angle)
    sin_a = np.sin(-angle)
    rot = np.array([[cos_a, -sin_a, 0], [sin_a, cos_a, 0], [0, 0, 1]])
    xyz_aligned = (rot @ xyz_up.T).T

    return xyz_aligned


def preprocess(cloud: PointCloud, config: PreprocessingConfig) -> PointCloud:
    """Apply preprocessing steps to a point cloud."""
    indices = np.arange(cloud.num_points)

    # Apply coordinate transform if needed
    xyz = cloud.xyz
    if config.coord_frame != "z-up":
        xyz = transform_coordinates(xyz, config.coord_frame)

    if config.point_cloud_range is not None:
        range_indices = crop_to_range(xyz[indices], config.point_cloud_range)
        indices = indices[range_indices]

    if config.downsample:
        ds_indices = voxel_downsample(xyz[indices], config.voxel_size_m)
        indices = indices[ds_indices]

    xyz = xyz[indices]
    rgb = cloud.rgb[indices] if cloud.rgb is not None and config.use_color else None
    intensity = (
        cloud.intensity[indices]
        if cloud.intensity is not None and config.use_intensity
        else None
    )
    normals = (
        cloud.normals[indices]
        if cloud.normals is not None and config.use_normals
        else None
    )

    return PointCloud(
        xyz=xyz,
        rgb=rgb,
        intensity=intensity,
        normals=normals,
        source_file=cloud.source_file,
    )
