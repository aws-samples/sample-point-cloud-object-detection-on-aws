"""Generate an interactive 3D HTML visualization with click-to-zoom detections."""

import html
import json
import sys
from pathlib import Path

import numpy as np


def load_point_cloud(path: Path, max_pts: int = 500000):
    """Load point cloud, return (xyz, colors). No coordinate transforms - raw data."""
    suffix = path.suffix.lower()
    if suffix == ".bin":
        raw = np.fromfile(path, dtype=np.float32)
        cols = (
            5
            if (
                raw.size % 5 == 0
                and ("nuscenes" in str(path).lower() or raw.size % 4 != 0)
            )
            else 4
        )
        if raw.size % 6 == 0 and raw.size % 5 != 0:
            cols = 6
        data = raw.reshape(-1, cols)
        xyz = data[:, :3].astype(np.float32)
        # Always use height-based coloring for .bin (best visibility)
        z_norm = (xyz[:, 2] - xyz[:, 2].min()) / (
            xyz[:, 2].max() - xyz[:, 2].min() + 1e-6
        )
        # Turbo-like colormap: vivid blue → cyan → green → yellow → red
        r = np.clip(np.where(z_norm < 0.5, 0.0, (z_norm - 0.5) * 2.8), 0, 1)
        g = np.clip(np.where(z_norm < 0.5, z_norm * 2.5, (1 - z_norm) * 2.5), 0, 1)
        b = np.clip(np.where(z_norm < 0.5, 1.0 - z_norm * 1.5, 0.0), 0, 1)
        # Ensure minimum brightness
        brightness = np.maximum(r, np.maximum(g, b))
        boost = np.where(brightness < 0.3, 0.3 / (brightness + 1e-6), 1.0)
        r = np.clip(r * boost, 0, 1)
        g = np.clip(g * boost, 0, 1)
        b = np.clip(b * boost, 0, 1)
        colors = np.column_stack([r, g, b]).astype(np.float32)
    elif suffix == ".ply":
        import trimesh

        # trimesh.load is typed as returning the Geometry base class, which declares
        # neither vertices nor colors -- those are on the concrete PointCloud/Trimesh
        # subclasses, and which one comes back depends on the file. getattr keeps the
        # duck-typed access while giving the type checker something it can follow.
        cloud = trimesh.load(str(path), process=False)
        verts = getattr(cloud, "vertices", None)
        if verts is None:
            raise ValueError(f"PLY file does not contain vertices: {path}")
        cloud_colors = getattr(cloud, "colors", None)
        xyz = np.asarray(verts, dtype=np.float32)
        if cloud_colors is not None:
            colors = np.asarray(cloud_colors[:, :3], dtype=np.float32) / 255.0
        else:
            colors = np.full((len(xyz), 3), 0.7, dtype=np.float32)
    elif suffix in (".las", ".laz"):
        import laspy

        las = laspy.read(str(path))
        # las.x/y/z are laspy scaled-array views rather than plain ndarrays, so each
        # is materialised before stacking.
        xyz = np.column_stack(
            [np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)]
        ).astype(np.float32)
        if hasattr(las, "red"):
            colors = np.column_stack(
                [np.asarray(las.red), np.asarray(las.green), np.asarray(las.blue)]
            ).astype(np.float32)
            colors = colors / (colors.max() + 1e-6)
        else:
            colors = np.full((len(xyz), 3), 0.7, dtype=np.float32)
    elif suffix == ".e57":
        import pye57

        # libe57 is the SWIG extension module pye57 exposes at runtime but omits
        # from its type stubs -- an upstream stub gap, not a missing dependency.
        from pye57 import libe57  # pyright: ignore[reportAttributeAccessIssue]

        e57 = pye57.E57(str(path))
        header = e57.get_header(0)
        # Read up to 2M points for good spatial coverage; the display subsample is
        # taken later
        read_cap = min(header.point_count, max(max_pts * 10, 2_000_000))
        point_fields = header.point_fields
        has_rgb = all(
            f in point_fields for f in ["colorRed", "colorGreen", "colorBlue"]
        )
        fields_to_read = ["cartesianX", "cartesianY", "cartesianZ"]
        if has_rgb:
            fields_to_read += ["colorRed", "colorGreen", "colorBlue"]
        data = {}
        buffers = libe57.VectorSourceDestBuffer()
        for field in fields_to_read:
            np_array, buffer = e57.make_buffer(field, read_cap)
            data[field] = np_array
            buffers.append(buffer)
        count = header.points.reader(buffers).read()
        xyz = np.column_stack(
            [
                data["cartesianX"][:count].copy(),
                data["cartesianY"][:count].copy(),
                data["cartesianZ"][:count].copy(),
            ]
        ).astype(np.float32)
        if has_rgb:
            colors = (
                np.column_stack(
                    [
                        data["colorRed"][:count].copy(),
                        data["colorGreen"][:count].copy(),
                        data["colorBlue"][:count].copy(),
                    ]
                ).astype(np.float32)
                / 255.0
            )
        else:
            colors = np.full((len(xyz), 3), 0.7, dtype=np.float32)
    else:
        raise ValueError(f"Unsupported format: {suffix}")
    return xyz, colors


def inverse_transform(xyz: np.ndarray, coord_frame: str) -> np.ndarray:
    """Transform detection coordinates (model space) back to raw point cloud space."""
    if coord_frame == "z-up":
        return xyz
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from pointcloud_detect.preprocessing import COORD_TRANSFORMS

    if coord_frame not in COORD_TRANSFORMS:
        return xyz
    R = COORD_TRANSFORMS[coord_frame]
    # Forward: raw -> model = R @ raw
    # Inverse: model -> raw = R^-1 @ model = R.T @ model (rotations are orthogonal)
    R_inv = np.linalg.inv(R)
    return (R_inv @ xyz.T).T


SEG_COLORS = {
    "ceiling": "#ccccee",
    "floor": "#996633",
    "wall": "#aaaaaa",
    "beam": "#ff8800",
    "column": "#ffcc00",
    "window": "#44ccff",
    "door": "#00cc66",
    "table": "#cc3333",
    "chair": "#3388ff",
    "sofa": "#cc44cc",
    "bookcase": "#885522",
    "board": "#ff3399",
    "clutter": "#555555",
    "cabinet": "#880088",
    "bed": "#ffaaaa",
    "desk": "#dd4411",
    "curtain": "#aaddaa",
    "other furniture": "#777777",
}

LABEL_COLORS = {
    "car": "#ff4444",
    "truck": "#ff8800",
    "bus": "#ff6600",
    "pedestrian": "#44ff44",
    "motorcycle": "#4444ff",
    "bicycle": "#44ffff",
    "traffic cone": "#ffff44",
    "barrier": "#ff44ff",
    "chair": "#00ff88",
    "table": "#ff8800",
    "sofa": "#aa44ff",
    "bed": "#ff44aa",
    "door": "#44ffff",
    "window": "#ffff44",
    "cabinet": "#8888ff",
    "desk": "#ff8888",
    "curtain": "#88ff88",
    "picture": "#ff88ff",
    "bookshelf": "#88ffff",
    "counter": "#ffaa44",
    "refrigerator": "#44aaff",
    "toilet": "#aa88ff",
    "sink": "#ffaa88",
    "bathtub": "#88aaff",
    "other furniture": "#aaaaaa",
}


def bbox_edges_lists(mn, mx):
    c = [
        [mn[0], mn[1], mn[2]],
        [mx[0], mn[1], mn[2]],
        [mx[0], mx[1], mn[2]],
        [mn[0], mx[1], mn[2]],
        [mn[0], mn[1], mx[2]],
        [mx[0], mn[1], mx[2]],
        [mx[0], mx[1], mx[2]],
        [mn[0], mx[1], mx[2]],
    ]
    edges = [
        [0, 1],
        [1, 2],
        [2, 3],
        [3, 0],
        [4, 5],
        [5, 6],
        [6, 7],
        [7, 4],
        [0, 4],
        [1, 5],
        [2, 6],
        [3, 7],
    ]
    x, y, z = [], [], []
    for a, b in edges:
        x += [c[a][0], c[b][0], None]
        y += [c[a][1], c[b][1], None]
        z += [c[a][2], c[b][2], None]
    return x, y, z


def main(argv: list[str] | None = None) -> None:
    """Write the report. ``argv`` is a command line, program name first; the default
    is this process's own, so ``scripts/visualize_3d.py`` and the CLI share it."""
    argv = sys.argv if argv is None else argv
    if len(argv) < 3:
        print(
            "Usage: python visualize_3d.py <point_cloud> <detections.json> "
            "[output_dir] [--subsample N] [--coord-frame FRAME] "
            "[--seg segmentation.json]"
        )
        sys.exit(1)

    pc_path = Path(argv[1])
    det_path = Path(argv[2])
    out_dir = (
        Path(argv[3])
        if len(argv) > 3 and not argv[3].startswith("--")
        else Path("./output")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    max_points = None
    coord_frame = "z-up"
    seg_path = None
    for i, arg in enumerate(argv):
        if arg == "--subsample" and i + 1 < len(argv):
            max_points = int(argv[i + 1])
        if arg == "--coord-frame" and i + 1 < len(argv):
            coord_frame = argv[i + 1]
        if arg == "--seg" and i + 1 < len(argv):
            seg_path = Path(argv[i + 1])

    # Load raw point cloud (no transform)
    xyz, colors = load_point_cloud(
        pc_path, max_pts=max_points if max_points else 500000
    )

    # Apply coord transform for display if specified
    if coord_frame != "z-up":
        from pointcloud_detect.preprocessing import transform_coordinates

        xyz = transform_coordinates(xyz.astype(np.float64), coord_frame).astype(
            np.float32
        )

    if max_points and len(xyz) > max_points:
        idx = np.random.default_rng(42).choice(len(xyz), max_points, replace=False)
        xyz = xyz[idx]
        colors = colors[idx]

    with open(det_path, encoding="utf-8") as f:
        result = json.load(f)
    detections = result["detections"]

    color_strs = [
        f"rgb({int(c[0] * 255)},{int(c[1] * 255)},{int(c[2] * 255)})" for c in colors
    ]

    # Load segmentation colors if provided
    seg_color_strs = None
    if seg_path and seg_path.exists():
        with open(seg_path, encoding="utf-8") as f:
            seg = json.load(f)
        seg_classes = seg["classes"]
        seg_labels = np.array(seg["labels"])
        seg_xyz_stored = np.array(seg["xyz"]) if seg.get("xyz") else None

        # Match segmentation to display points
        # If seg has its own xyz, find nearest matches; otherwise use first N labels
        if seg_xyz_stored is not None and len(seg_xyz_stored) == len(xyz):
            seg_labels_display = seg_labels
        elif len(seg_labels) >= len(xyz):
            seg_labels_display = seg_labels[: len(xyz)]
        else:
            seg_labels_display = np.zeros(len(xyz), dtype=np.int32)

        seg_color_strs = []
        for lbl in seg_labels_display:
            cls = seg_classes[int(lbl)] if int(lbl) < len(seg_classes) else "clutter"
            hex_color = SEG_COLORS.get(cls, "#888888")
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            seg_color_strs.append(f"rgb({r},{g},{b})")

    traces = [
        {
            "type": "scatter3d",
            "x": xyz[:, 0].tolist(),
            "y": xyz[:, 1].tolist(),
            "z": xyz[:, 2].tolist(),
            "mode": "markers",
            "marker": {"size": 2, "color": color_strs, "opacity": 0.9},
            "name": "Point Cloud",
            "hoverinfo": "skip",
            "visible": True,
        }
    ]

    # Add segmentation trace (hidden by default)
    if seg_color_strs:
        traces.append(
            {
                "type": "scatter3d",
                "x": xyz[:, 0].tolist(),
                "y": xyz[:, 1].tolist(),
                "z": xyz[:, 2].tolist(),
                "mode": "markers",
                "marker": {"size": 1.5, "color": seg_color_strs, "opacity": 0.8},
                "name": "Segmentation",
                "hoverinfo": "skip",
                "visible": False,
            }
        )

    det_centers = []
    for det in detections:
        mn = np.array(det["bbox_aabb"]["min"])
        mx = np.array(det["bbox_aabb"]["max"])
        center = np.array(det["bbox_obb"]["center"])
        det_centers.append(center.tolist())
        color = LABEL_COLORS.get(det["label"], "#ffffff")
        bx, by, bz = bbox_edges_lists(mn.tolist(), mx.tolist())
        traces.append(
            {
                "type": "scatter3d",
                "x": bx,
                "y": by,
                "z": bz,
                "mode": "lines",
                "line": {"color": color, "width": 6},
                "name": f"{det['label']} ({det['confidence']:.2f})",
            }
        )

    cam_dist = 1.5 if (xyz[:, 0].max() - xyz[:, 0].min()) < 50 else 2.5

    layout = {
        "paper_bgcolor": "#1a1a2e",
        "font": {"color": "white"},
        "title": {
            "text": f"3D Detections — {pc_path.name} ({len(detections)} objects)",
            "font": {"color": "white"},
        },
        "legend": {"x": 0, "y": 1, "font": {"color": "white"}},
        "margin": {"l": 0, "r": 0, "t": 40, "b": 0},
        "scene": {
            "bgcolor": "#1a1a2e",
            "xaxis": {"color": "#aaa", "gridcolor": "#333", "title": "X (m)"},
            "yaxis": {"color": "#aaa", "gridcolor": "#333", "title": "Y (m)"},
            "zaxis": {"color": "#aaa", "gridcolor": "#333", "title": "Z (m)"},
            "aspectmode": "data",
            "camera": {
                "eye": {"x": cam_dist, "y": cam_dist, "z": cam_dist * 0.7},
            },
        },
    }

    # Labels and file names come from input files, so nothing from them reaches the page
    # as markup: they are HTML-escaped in the markup, and "<" is escaped inside the
    # inline script so that no value can close the <script> element.
    def script_json(value):
        return json.dumps(value).replace("<", "\\u003c")

    traces_json = script_json(traces)
    layout_json = script_json(layout)
    centers_json = script_json(det_centers)

    html_path = out_dir / f"{pc_path.stem}_3d.html"

    has_seg = seg_color_strs is not None
    seg_toggle = ""
    if has_seg:
        seg_toggle = """
        <button class="reset-btn" onclick="toggleView()" id="toggle-btn">
            Show Segmentation</button>"""

    sidebar_buttons = "".join(
        f'<button class="det-btn" '
        f'style="border-color: {LABEL_COLORS.get(det["label"], "#ffffff")}" '
        f'onclick="zoomTo({i})">'
        f"{html.escape(str(det['label']))} ({det['confidence']:.2f})</button>"
        for i, det in enumerate(detections)
    )

    full_html = f"""<!DOCTYPE html>
<html>
<head>
    <title>3D Detections — {html.escape(pc_path.name)}</title>
    <script src="https://cdn.plot.ly/plotly-3.5.0.min.js"
        integrity="sha384-DPvk2KODrsA0CfBr4HTwAwhdDROPqqK2PvSSswJMQMpnUkwSTg4gLBxXc3wv2e5L"
        crossorigin="anonymous"></script>
    <style>
        html, body {{
            margin: 0; padding: 0; width: 100%; height: 100%;
            background: #1a1a2e; overflow: hidden;
        }}
        #plot {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; }}
        #sidebar {{
            position: fixed; top: 10px; right: 10px; z-index: 1000;
            background: rgba(30,30,30,0.9); padding: 12px; border-radius: 8px;
            max-height: 90vh; overflow-y: auto;
        }}
        #sidebar h3 {{ color: white; margin: 0 0 8px 0; font-family: sans-serif; }}
        .det-btn {{
            display: block; width: 100%; margin: 4px 0; padding: 8px 12px;
            border: 2px solid; border-radius: 4px; background: transparent;
            color: white; font-size: 13px; cursor: pointer; text-align: left;
            font-family: sans-serif;
        }}
        .det-btn:hover {{ background: rgba(255,255,255,0.1); }}
        .reset-btn {{
            display: block; width: 100%; margin: 12px 0 4px 0; padding: 8px 12px;
            border: 1px solid #666; border-radius: 4px; background: #333;
            color: white; font-size: 13px; cursor: pointer; text-align: center;
            font-family: sans-serif;
        }}
        .reset-btn:hover {{ background: #555; }}
    </style>
</head>
<body>
    <div id="sidebar">
        <h3>Detections</h3>
        <button class="reset-btn" onclick="resetView()">Reset View</button>
        {seg_toggle}
        {sidebar_buttons}
    </div>
    <div id="plot"></div>
    <script>
        var traces = {traces_json};
        var layout = {layout_json};
        var detCenters = {centers_json};
        var defaultCamera = null;

        Plotly.newPlot('plot', traces, layout, {{responsive: true}}).then(function() {{
            defaultCamera = JSON.parse(JSON.stringify(
                document.getElementById('plot').layout.scene.camera
            ));
        }});

        var showingSeg = false;
        function toggleView() {{
            showingSeg = !showingSeg;
            var btn = document.getElementById('toggle-btn');
            if (showingSeg) {{
                Plotly.restyle('plot', {{'visible': false}}, [0]);
                Plotly.restyle('plot', {{'visible': true}}, [1]);
                btn.textContent = 'Show RGB';
            }} else {{
                Plotly.restyle('plot', {{'visible': true}}, [0]);
                Plotly.restyle('plot', {{'visible': false}}, [1]);
                btn.textContent = 'Show Segmentation';
            }}
        }}

        function zoomTo(idx) {{
            var c = detCenters[idx];
            var r = 3;
            Plotly.relayout('plot', {{
                'scene.xaxis.range': [c[0]-r, c[0]+r],
                'scene.yaxis.range': [c[1]-r, c[1]+r],
                'scene.zaxis.range': [c[2]-r, c[2]+r],
            }});
        }}

        function resetView() {{
            Plotly.relayout('plot', {{
                'scene.xaxis.range': null,
                'scene.yaxis.range': null,
                'scene.zaxis.range': null,
                'scene.camera': defaultCamera,
            }});
        }}
    </script>
</body>
</html>"""

    html_path.write_text(full_html)
    print(f"Saved: {html_path}")
    print("Open in browser — click detection buttons to zoom, 'Reset View' to zoom out")
    print("Detections:")
    for det in detections:
        print(
            f"  - {det['label']} (conf: {det['confidence']:.2f}) "
            f"at {det['bbox_obb']['center']}"
        )


if __name__ == "__main__":
    main()
