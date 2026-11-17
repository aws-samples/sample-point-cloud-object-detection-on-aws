"""Visualize segmentation results - color points by structural class."""

import json
import sys
from html import escape
from pathlib import Path

import numpy as np


class NumpyEncoder(json.JSONEncoder):
    def default(self, o):
        # Parameter is named `o` to match json.JSONEncoder.default(self, o); pyright
        # treats a renamed positional parameter as an incompatible override.
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return super().default(o)


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
    "refrigerator": "#22dddd",
    "toilet": "#dddd33",
    "sink": "#3377dd",
    "bathtub": "#77dddd",
    "other furniture": "#777777",
}


def load_point_cloud(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".e57":
        import pye57

        e57 = pye57.E57(str(path))
        fields = e57.read_scan_raw(0)
        xyz = np.column_stack(
            [fields["cartesianX"], fields["cartesianY"], fields["cartesianZ"]]
        ).astype(np.float64)
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
        xyz = np.asarray(verts, dtype=np.float64)
    else:
        raise ValueError(f"Unsupported format: {suffix}")
    return xyz


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: python visualize_seg.py <point_cloud> <segmentation.json> [output_dir] [--subsample N] [--coord-frame FRAME]"
        )
        sys.exit(1)

    pc_path = Path(sys.argv[1])
    seg_path = Path(sys.argv[2])
    out_dir = (
        Path(sys.argv[3])
        if len(sys.argv) > 3 and not sys.argv[3].startswith("--")
        else Path("./output")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    max_points = None
    coord_frame = "z-up"
    for i, arg in enumerate(sys.argv):
        if arg == "--subsample" and i + 1 < len(sys.argv):
            max_points = int(sys.argv[i + 1])
        if arg == "--coord-frame" and i + 1 < len(sys.argv):
            coord_frame = sys.argv[i + 1]

    xyz = load_point_cloud(pc_path)

    with open(seg_path, encoding="utf-8") as f:
        seg = json.load(f)

    classes = seg["classes"]
    labels = np.array(seg["labels"])
    confidences = np.array(seg["confidences"]) if seg.get("confidences") else None

    # Use the XYZ saved alongside segmentation (preprocessed/downsampled coords)
    if seg.get("xyz") is not None:
        xyz = np.array(seg["xyz"], dtype=np.float64)
        already_transformed = True
    else:
        xyz = load_point_cloud(pc_path)
        xyz = xyz[: len(labels)]
        already_transformed = False

    n = len(labels)

    # Apply confidence threshold if available
    min_conf = 0.0
    for i, arg in enumerate(sys.argv):
        if arg == "--min-confidence" and i + 1 < len(sys.argv):
            min_conf = float(sys.argv[i + 1])

    if confidences is not None and min_conf > 0:
        mask = confidences >= min_conf
        xyz = xyz[mask]
        labels = labels[mask]
        confidences = confidences[mask]
        n = len(labels)

    # Subsample consistently
    if max_points and n > max_points:
        idx = np.random.default_rng(42).choice(n, max_points, replace=False)
        xyz = xyz[idx]
        labels = labels[idx]

    # Apply coordinate transform only if xyz is raw (not already transformed)
    if coord_frame != "z-up" and not already_transformed:
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from pointcloud_detect.preprocessing import transform_coordinates

        xyz = transform_coordinates(xyz, coord_frame)

    # Build one trace per class
    traces = []
    class_info = []
    for i, cls in enumerate(classes):
        mask = labels == i
        count = mask.sum()
        if count == 0:
            continue
        pts = xyz[mask]
        color = SEG_COLORS.get(cls, "#888888")
        traces.append(
            {
                "type": "scatter3d",
                "x": pts[:, 0].tolist(),
                "y": pts[:, 1].tolist(),
                "z": pts[:, 2].tolist(),
                "mode": "markers",
                "marker": {"size": 1.5, "color": color, "opacity": 0.9},
                "name": cls,
                "visible": True,
            }
        )
        class_info.append({"name": cls, "color": color, "count": int(count)})

    layout = {
        "paper_bgcolor": "#111",
        "font": {"color": "white"},
        "title": {"text": f"Segmentation — {pc_path.name}", "font": {"color": "white"}},
        "showlegend": False,
        "margin": {"l": 0, "r": 0, "t": 40, "b": 0},
        "scene": {
            "bgcolor": "#111",
            "xaxis": {"color": "white", "title": "X"},
            "yaxis": {"color": "white", "title": "Y"},
            "zaxis": {"color": "white", "title": "Z"},
            "aspectmode": "data",
        },
    }

    # Class names and file names come from input files, so nothing from them reaches the
    # page as markup: they are HTML-escaped in the markup, and "<" is escaped inside the
    # inline script so that no value can close the <script> element.
    def script_json(value, **kwargs):
        return json.dumps(value, **kwargs).replace("<", "\\u003c")

    traces_json = script_json(traces, cls=NumpyEncoder)
    layout_json = script_json(layout, cls=NumpyEncoder)
    class_info_json = script_json(class_info)

    sidebar_buttons = ""
    for i, info in enumerate(class_info):
        sidebar_buttons += f"""
        <button class="cls-btn" id="btn-{i}" style="border-color:{info["color"]}"
            onclick="toggleClass({i})" data-visible="1">
            <span class="dot" style="background:{info["color"]}"></span>
            {escape(str(info["name"]))} <span class="cnt">({info["count"]:,})</span>
        </button>"""

    html_path = out_dir / f"{pc_path.stem}_segmentation_3d.html"
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Segmentation — {escape(pc_path.name)}</title>
    <script src="https://cdn.plot.ly/plotly-3.5.0.min.js" integrity="sha384-DPvk2KODrsA0CfBr4HTwAwhdDROPqqK2PvSSswJMQMpnUkwSTg4gLBxXc3wv2e5L" crossorigin="anonymous"></script>
    <style>
        html, body {{ margin:0; padding:0; width:100%; height:100%; background:#111; overflow:hidden; }}
        #plot {{ position:absolute; top:0; left:0; right:0; bottom:0; }}
        #sidebar {{
            position:fixed; top:10px; right:10px; z-index:1000;
            background:rgba(20,20,20,0.95); padding:12px; border-radius:8px;
            max-height:90vh; overflow-y:auto; min-width:180px;
        }}
        #sidebar h3 {{ color:white; margin:0 0 8px 0; font-family:sans-serif; font-size:14px; }}
        .cls-btn {{
            display:flex; align-items:center; width:100%; margin:3px 0; padding:6px 10px;
            border:2px solid; border-radius:4px; background:rgba(255,255,255,0.05);
            color:white; font-size:12px; cursor:pointer; text-align:left;
            font-family:sans-serif; gap:8px;
        }}
        .cls-btn:hover {{ background:rgba(255,255,255,0.15); }}
        .cls-btn.hidden {{ opacity:0.35; background:transparent; }}
        .dot {{ width:10px; height:10px; border-radius:50%; flex-shrink:0; }}
        .cnt {{ color:#aaa; font-size:11px; }}
        .ctrl-btn {{
            display:block; width:100%; margin:8px 0 3px 0; padding:6px;
            border:1px solid #555; border-radius:4px; background:#333;
            color:white; font-size:12px; cursor:pointer; font-family:sans-serif;
        }}
        .ctrl-btn:hover {{ background:#555; }}
    </style>
</head>
<body>
    <div id="sidebar">
        <h3>Structural Classes</h3>
        <button class="ctrl-btn" onclick="showAll()">Show All</button>
        <button class="ctrl-btn" onclick="hideAll()">Hide All</button>
        {sidebar_buttons}
    </div>
    <div id="plot"></div>
    <script>
        var classInfo = {class_info_json};
        var visibility = classInfo.map(function() {{ return true; }});

        Plotly.newPlot('plot', {traces_json}, {layout_json}, {{responsive: true}});

        function toggleClass(idx) {{
            visibility[idx] = !visibility[idx];
            var btn = document.getElementById('btn-' + idx);
            btn.classList.toggle('hidden', !visibility[idx]);
            var update = {{}};
            update['visible'] = visibility[idx] ? true : 'legendonly';
            Plotly.restyle('plot', update, [idx]);
        }}

        function showAll() {{
            visibility = classInfo.map(function() {{ return true; }});
            classInfo.forEach(function(_, i) {{
                document.getElementById('btn-' + i).classList.remove('hidden');
            }});
            Plotly.restyle('plot', {{'visible': true}});
        }}

        function hideAll() {{
            visibility = classInfo.map(function() {{ return false; }});
            classInfo.forEach(function(_, i) {{
                document.getElementById('btn-' + i).classList.add('hidden');
            }});
            Plotly.restyle('plot', {{'visible': 'legendonly'}});
        }}
    </script>
</body>
</html>"""

    html_path.write_text(html)
    print(f"Saved: {html_path}")
    print("\nClass breakdown:")
    for info in class_info:
        print(f"  {info['name']:20s}: {info['count']:6,} points")


if __name__ == "__main__":
    main()
