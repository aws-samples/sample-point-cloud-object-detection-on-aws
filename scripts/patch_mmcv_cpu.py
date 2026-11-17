#!/usr/bin/env python3
"""Patch mmcv CUDA-only ops with CPU fallbacks for point-based models.

This script is run during Docker image build (Dockerfile.cpu) to inject
pure-PyTorch CPU fallback branches into mmcv's custom op source files.
It enables VoteNet, FCAF3D, and other PointNet++-based models to run
inference on CPU nodes — slower than CUDA but functionally correct.

Patched ops:
  - furthest_point_sample (FPS)
  - ball_query
  - gather_points
  - group_points
  - three_nn
  - three_interpolate
  - points_in_boxes_all / points_in_boxes_part

Usage (inside Docker build):
    RUN /app/.venv/bin/python3 scripts/patch_mmcv_cpu.py

The script auto-detects the mmcv install path from the venv.
"""

import pathlib
import sys


def find_mmcv_ops_dir() -> pathlib.Path:
    """Find the mmcv/ops directory in the current environment."""
    candidates = [
        pathlib.Path("/app/.venv/lib/python3.12/site-packages/mmcv/ops"),
        pathlib.Path(sys.prefix)
        / "lib"
        / "python3.12"
        / "site-packages"
        / "mmcv"
        / "ops",
    ]
    # Also try importing
    try:
        import mmcv.ops  # pyright: ignore[reportMissingImports]

        candidates.insert(0, pathlib.Path(mmcv.ops.__file__).parent)
    except ImportError:
        pass

    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Cannot find mmcv/ops directory")


def patch(filepath: pathlib.Path, old: str, new: str, label: str) -> bool:
    """Replace first occurrence of `old` with `new` in file."""
    if not filepath.exists():
        print(f"SKIP: {label} - file not found: {filepath}")
        return False
    t = filepath.read_text()
    if old in t:
        t = t.replace(old, new, 1)
        filepath.write_text(t)
        print(f"OK: {label}")
        return True
    else:
        print(f"ERR: {label} - pattern not found in {filepath.name}")
        return False


def main():
    ops_dir = find_mmcv_ops_dir()
    print(f"Patching mmcv ops at: {ops_dir}")
    results = []

    # -------------------------------------------------------------------------
    # 1. furthest_point_sample — replaces torch.cuda.IntTensor with CPU loop
    # -------------------------------------------------------------------------
    results.append(
        patch(
            ops_dir / "furthest_point_sample.py",
            "        output = torch.cuda.IntTensor(B, num_points)",
            "        if not points_xyz.is_cuda:\n"
            '            distances = points_xyz.new_full((B, N), float("inf"))\n'
            "            indices = torch.zeros(B, num_points, dtype=torch.int64, device=points_xyz.device)\n"
            "            farthest = torch.zeros(B, dtype=torch.int64, device=points_xyz.device)\n"
            "            for i in range(num_points):\n"
            "                indices[:, i] = farthest\n"
            "                centroid = points_xyz[torch.arange(B), farthest].unsqueeze(1)\n"
            "                dist = torch.sum((points_xyz - centroid) ** 2, dim=-1)\n"
            "                distances = torch.min(distances, dist)\n"
            "                farthest = torch.argmax(distances, dim=-1)\n"
            "            return indices.int()\n"
            "        output = torch.cuda.IntTensor(B, num_points)",
            "furthest_point_sample",
        )
    )

    # -------------------------------------------------------------------------
    # 2. ball_query — 12-space indent (inside else block)
    # -------------------------------------------------------------------------
    results.append(
        patch(
            ops_dir / "ball_query.py",
            "            ext_module.ball_query_forward(",
            "            if not center_xyz.is_cuda:\n"
            "                out = center_xyz.new_zeros(B, npoint, sample_num, dtype=torch.int32)\n"
            "                for b2 in range(B):\n"
            "                    for m2 in range(npoint):\n"
            "                        dists = ((xyz[b2] - center_xyz[b2, m2]) ** 2).sum(-1)\n"
            "                        mask = (dists >= min_radius**2) & (dists <= max_radius**2)\n"
            "                        valid = torch.where(mask)[0]\n"
            "                        if len(valid) > 0:\n"
            "                            si = valid[dists[valid].argsort()][:sample_num]\n"
            "                            out[b2, m2, :len(si)] = si.int()\n"
            "                            if len(si) < sample_num:\n"
            "                                out[b2, m2, len(si):] = si[0].int()\n"
            "                return out\n"
            "            ext_module.ball_query_forward(",
            "ball_query",
        )
    )

    # -------------------------------------------------------------------------
    # 3. gather_points — 8-space indent
    # -------------------------------------------------------------------------
    results.append(
        patch(
            ops_dir / "gather_points.py",
            "        ext_module.gather_points_forward(",
            "        if not features.is_cuda:\n"
            "            idx = indices.long().unsqueeze(1).expand(-1, C, -1)\n"
            "            output = features.gather(2, idx)\n"
            "            ctx.save_for_backward(indices)\n"
            "            ctx.N = N\n"
            "            return output\n"
            "        ext_module.gather_points_forward(",
            "gather_points",
        )
    )

    # -------------------------------------------------------------------------
    # 4. group_points — 12-space indent (inside else block)
    #    Uses gather with proper reshaping to avoid shape mismatch
    # -------------------------------------------------------------------------
    results.append(
        patch(
            ops_dir / "group_points.py",
            "            ext_module.group_points_forward(",
            "            if not features.is_cuda:\n"
            "                idx = indices.long().clamp(0, N - 1).view(B, 1, -1).expand(B, C, -1)\n"
            "                output = features.gather(2, idx).view(B, C, nfeatures, nsample)\n"
            "                ctx.save_for_backward(indices)\n"
            "                ctx.N = N\n"
            "                return output\n"
            "            ext_module.group_points_forward(",
            "group_points",
        )
    )

    # -------------------------------------------------------------------------
    # 5. three_nn — 8-space indent
    # -------------------------------------------------------------------------
    results.append(
        patch(
            ops_dir / "three_nn.py",
            "        ext_module.three_nn_forward(",
            "        if not target.is_cuda:\n"
            "            diffs = target.unsqueeze(2) - source.unsqueeze(1)\n"
            "            dists = (diffs ** 2).sum(-1)\n"
            "            d2, idx = dists.topk(3, dim=-1, largest=False)\n"
            "            return d2.sqrt(), idx.int()\n"
            "        ext_module.three_nn_forward(",
            "three_nn",
        )
    )

    # -------------------------------------------------------------------------
    # 6. three_interpolate — 8-space indent
    # -------------------------------------------------------------------------
    results.append(
        patch(
            ops_dir / "three_interpolate.py",
            "        ext_module.three_interpolate_forward(",
            "        if not features.is_cuda:\n"
            "            B2, C2, M2 = features.shape\n"
            "            _, N2, K = indices.shape\n"
            "            idx = indices.long()\n"
            "            out = features.new_zeros(B2, C2, N2)\n"
            "            for b in range(B2):\n"
            "                for n in range(N2):\n"
            "                    for k in range(K):\n"
            "                        out[b, :, n] += weight[b, n, k] * features[b, :, idx[b, n, k]]\n"
            "            ctx.save_for_backward(indices, weight)\n"
            "            ctx.M = M2\n"
            "            return out\n"
            "        ext_module.three_interpolate_forward(",
            "three_interpolate",
        )
    )

    # -------------------------------------------------------------------------
    # 7. points_in_boxes — patch both functions at the device check
    #    Insert CPU early-return before torch.cuda.current_device() call
    # -------------------------------------------------------------------------
    pib_file = ops_dir / "points_in_boxes.py"
    if pib_file.exists():
        t = pib_file.read_text()
        old_pattern = (
            "    points_device = points.get_device()\n"
            "    assert points_device == boxes.get_device()"
        )
        new_pattern = (
            "    if not points.is_cuda:\n"
            "        B, T, _ = points.shape\n"
            "        M = boxes.shape[1]\n"
            "        result = points.new_zeros((B, T, M), dtype=torch.int)\n"
            "        for b in range(B):\n"
            "            for j in range(M):\n"
            "                cx, cy, cz, dx, dy, dz, rz = boxes[b, j]\n"
            "                cos_r, sin_r = torch.cos(-rz), torch.sin(-rz)\n"
            "                px = points[b, :, 0] - cx\n"
            "                py = points[b, :, 1] - cy\n"
            "                pz = points[b, :, 2] - cz\n"
            "                lx = px * cos_r - py * sin_r\n"
            "                ly = px * sin_r + py * cos_r\n"
            "                mask = (lx.abs() <= dx/2) & (ly.abs() <= dy/2) & (pz.abs() <= dz/2)\n"
            "                result[b, mask, j] = 1\n"
            "        return result\n"
            "    points_device = points.get_device()\n"
            "    assert points_device == boxes.get_device()"
        )
        count = t.count(old_pattern)
        if count > 0:
            t = t.replace(old_pattern, new_pattern)
            pib_file.write_text(t)
            print(f"OK: points_in_boxes ({count} occurrences)")
            results.append(True)
        else:
            print("ERR: points_in_boxes - pattern not found")
            results.append(False)
    else:
        print("SKIP: points_in_boxes - file not found")
        results.append(False)

    # Summary
    ok = sum(1 for r in results if r)
    total = len(results)
    print(f"\nPatched {ok}/{total} ops successfully.")
    if ok < total:
        print("WARNING: Some patches failed. CPU inference may crash on unpatched ops.")
        sys.exit(1)


if __name__ == "__main__":
    main()
