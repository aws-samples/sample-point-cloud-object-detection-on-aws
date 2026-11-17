"""CPU fallback implementations for mmcv CUDA-only custom ops.

mmcv's furthest_point_sample and related ops use torch.cuda.IntTensor
unconditionally, crashing on CPU-only systems. This module provides pure
PyTorch CPU implementations and monkey-patches them into mmcv at import time.

Usage:
    import pointcloud_detect.backends.mmcv_cpu_ops  # patches applied on import
"""

import logging

import torch  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)


def furthest_point_sample_cpu(
    points_xyz: torch.Tensor, num_points: int
) -> torch.Tensor:
    """CPU implementation of furthest point sampling (FPS).

    Iteratively selects the point that is farthest from all previously
    selected points. O(N * num_points) — slower than CUDA but correct.

    Args:
        points_xyz: (B, N, 3) point coordinates
        num_points: number of points to sample

    Returns:
        (B, num_points) indices of sampled points
    """
    B, N, _ = points_xyz.shape
    device = points_xyz.device
    dtype = points_xyz.dtype

    indices = torch.zeros(B, num_points, dtype=torch.int64, device=device)
    distances = torch.full((B, N), float("inf"), dtype=dtype, device=device)

    # Start from point 0
    farthest = torch.zeros(B, dtype=torch.int64, device=device)

    for i in range(num_points):
        indices[:, i] = farthest
        centroid = points_xyz[torch.arange(B), farthest].unsqueeze(1)  # (B, 1, 3)
        dist = torch.sum((points_xyz - centroid) ** 2, dim=-1)  # (B, N)
        distances = torch.min(distances, dist)
        farthest = torch.argmax(distances, dim=-1)  # (B,)

    return indices.int()


def _patch_mmcv_fps():
    """Monkey-patch mmcv's furthest_point_sample to work on CPU."""
    try:
        import sys

        import mmcv.ops  # pyright: ignore[reportMissingImports]
        import mmcv.ops.furthest_point_sample as fps_module  # pyright: ignore[reportMissingImports]

        # Replace the entire FurthestPointSampling class with one that handles CPU
        class CPUFurthestPointSampling(torch.autograd.Function):
            @staticmethod
            def forward(ctx, points_xyz: torch.Tensor, num_points: int) -> torch.Tensor:
                if points_xyz.is_cuda:
                    # Use original CUDA kernel
                    B, N, _ = points_xyz.shape
                    output = torch.cuda.IntTensor(B, num_points)
                    from mmcv.ops.furthest_point_sample import (  # pyright: ignore[reportMissingImports]
                        ext_module,
                    )

                    ext_module.furthest_point_sampling_forward(
                        points_xyz, output, points_xyz.new_zeros(B, N).float()
                    )
                    return output
                return furthest_point_sample_cpu(points_xyz, num_points)

        # Replace the class
        fps_module.FurthestPointSampling = CPUFurthestPointSampling

        # Replace the wrapper function
        def _patched_fps(points_xyz: torch.Tensor, num_points: int) -> torch.Tensor:
            return CPUFurthestPointSampling.apply(points_xyz, num_points)

        fps_module.furthest_point_sample = _patched_fps
        mmcv.ops.furthest_point_sample = _patched_fps

        # Patch in all loaded modules that reference the original
        for mod_name, mod in list(sys.modules.items()):
            if mod is None:
                continue
            try:
                if hasattr(mod, "furthest_point_sample") and mod is not fps_module:
                    setattr(mod, "furthest_point_sample", _patched_fps)
            except Exception:
                pass

        logger.info("Patched mmcv furthest_point_sample with CPU fallback")

    except (ImportError, AttributeError) as e:
        logger.debug("Could not patch mmcv FPS (may not be installed): %s", e)


def _patch_mmcv_ball_query():
    """Monkey-patch mmcv's ball_query to use CPU fallback when needed."""
    try:
        import mmcv.ops  # pyright: ignore[reportMissingImports]
        import mmcv.ops.ball_query as bq_module  # pyright: ignore[reportMissingImports]

        _original_bq = bq_module.ball_query

        def _patched_bq(min_radius, max_radius, sample_num, xyz, center_xyz):
            if xyz.is_cuda:
                return _original_bq(min_radius, max_radius, sample_num, xyz, center_xyz)
            return _ball_query_cpu(min_radius, max_radius, sample_num, xyz, center_xyz)

        bq_module.ball_query = _patched_bq
        mmcv.ops.ball_query = _patched_bq

        # Patch in query_and_group which imports ball_query
        try:
            from mmcv.ops import (  # pyright: ignore[reportMissingImports]
                query_and_group as qg_module,
            )

            qg_module.ball_query = _patched_bq
        except (ImportError, AttributeError):
            pass

        logger.info("Patched mmcv ball_query with CPU fallback")

    except (ImportError, AttributeError) as e:
        logger.debug("Could not patch mmcv ball_query: %s", e)


def _ball_query_cpu(
    min_radius: float,
    max_radius: float,
    sample_num: int,
    xyz: torch.Tensor,
    center_xyz: torch.Tensor,
) -> torch.Tensor:
    """CPU implementation of ball query.

    Args:
        min_radius: minimum search radius
        max_radius: maximum search radius
        sample_num: max neighbors to return per query point
        xyz: (B, N, 3) all points
        center_xyz: (B, M, 3) query centers

    Returns:
        (B, M, sample_num) neighbor indices (-1 for missing)
    """
    B, N, _ = xyz.shape
    _, M, _ = center_xyz.shape

    result = torch.zeros(B, M, sample_num, dtype=torch.int32, device=xyz.device)

    for b in range(B):
        for m in range(M):
            dists = torch.sum((xyz[b] - center_xyz[b, m].unsqueeze(0)) ** 2, dim=-1)
            mask = (dists >= min_radius**2) & (dists <= max_radius**2)
            valid_indices = torch.where(mask)[0]

            if len(valid_indices) == 0:
                # No neighbors found — fill with first point
                result[b, m, :] = 0
            else:
                # Take up to sample_num neighbors (sorted by distance)
                sorted_idx = valid_indices[dists[valid_indices].argsort()]
                n_take = min(sample_num, len(sorted_idx))
                result[b, m, :n_take] = sorted_idx[:n_take].int()
                # Pad remaining with first found neighbor
                if n_take < sample_num:
                    result[b, m, n_take:] = sorted_idx[0].int()

    return result


def _patch_mmcv_group_points():
    """Monkey-patch mmcv's group_points (QueryAndGroup uses it)."""
    try:
        import mmcv.ops  # pyright: ignore[reportMissingImports]
        import mmcv.ops.group_points as gp_module  # pyright: ignore[reportMissingImports]

        _original_gp = gp_module.group_points

        def _patched_gp(features, indices):
            if features.is_cuda:
                return _original_gp(features, indices)
            return _group_points_cpu(features, indices)

        gp_module.group_points = _patched_gp
        mmcv.ops.group_points = _patched_gp

        # Patch in query_and_group
        try:
            from mmcv.ops import (  # pyright: ignore[reportMissingImports]
                query_and_group as qg_module,
            )

            qg_module.group_points = _patched_gp
        except (ImportError, AttributeError):
            pass

        logger.info("Patched mmcv group_points with CPU fallback")

    except (ImportError, AttributeError) as e:
        logger.debug("Could not patch mmcv group_points: %s", e)


def _group_points_cpu(features: torch.Tensor, indices: torch.Tensor) -> torch.Tensor:
    """CPU implementation of grouping operation.

    Args:
        features: (B, C, N) point features
        indices: (B, npoint, nsample) grouping indices

    Returns:
        (B, C, npoint, nsample) grouped features
    """
    B, C, N = features.shape
    _, npoint, nsample = indices.shape

    indices_long = indices.long().clamp(0, N - 1)
    # Gather: for each batch, each query point, each sample
    # features[:, :, indices] → (B, C, npoint, nsample)
    output = features[:, :, indices_long.view(B, -1)].view(B, C, npoint, nsample)
    return output


def apply_all_patches():
    """Apply all CPU fallback patches for mmcv ops."""
    if not torch.cuda.is_available():
        _patch_mmcv_fps()
        _patch_mmcv_ball_query()
        _patch_mmcv_group_points()
        logger.info("All mmcv CPU fallback patches applied (no CUDA detected)")
    else:
        logger.debug("CUDA available — mmcv CPU patches not needed")


# Auto-apply on import
apply_all_patches()
