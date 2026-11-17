"""Show point cloud with labeled X/Y/Z axes for coordinate system identification."""

import json
import sys
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


def load_cloud(path: Path, max_pts: int):
    suffix = path.suffix.lower()
    if suffix == ".e57":
        import pye57

        e57 = pye57.E57(str(path))
        fields = e57.read_scan_raw(0)
        xyz = np.column_stack(
            [fields["cartesianX"], fields["cartesianY"], fields["cartesianZ"]]
        ).astype(np.float64)
        if all(k in fields for k in ("colorRed", "colorGreen", "colorBlue")):
            colors = (
                np.column_stack(
                    [fields["colorRed"], fields["colorGreen"], fields["colorBlue"]]
                ).astype(np.float32)
                / 255.0
            )
        else:
            colors = np.full((len(xyz), 3), 0.7, dtype=np.float32)
    else:
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
        cloud_colors = getattr(cloud, "colors", None)
        colors = (
            np.asarray(cloud_colors[:, :3], dtype=np.float32) / 255.0
            if cloud_colors is not None
            else np.full((len(xyz), 3), 0.7, dtype=np.float32)
        )

    if len(xyz) > max_pts:
        idx = np.random.default_rng(42).choice(len(xyz), max_pts, replace=False)
        xyz, colors = xyz[idx], colors[idx]
    return xyz, colors


def main():
    path = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("output_axes.html")
    max_pts = int(sys.argv[3]) if len(sys.argv) > 3 else 50000

    coord_frame = "z-up"
    for i, arg in enumerate(sys.argv):
        if arg == "--coord-frame" and i + 1 < len(sys.argv):
            coord_frame = sys.argv[i + 1]

    xyz, colors = load_cloud(path, max_pts)

    if coord_frame != "z-up":
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from pointcloud_detect.preprocessing import transform_coordinates

        xyz = transform_coordinates(xyz, coord_frame)

    color_strs = [
        f"rgb({int(c[0] * 255)},{int(c[1] * 255)},{int(c[2] * 255)})" for c in colors
    ]
    origin = xyz.mean(axis=0)
    L = float((xyz.max(axis=0) - xyz.min(axis=0)).max()) * 0.3

    traces = [
        {
            "type": "scatter3d",
            "x": xyz[:, 0].tolist(),
            "y": xyz[:, 1].tolist(),
            "z": xyz[:, 2].tolist(),
            "mode": "markers",
            "marker": {"size": 1.5, "color": color_strs, "opacity": 0.8},
            "name": "Points",
            "hoverinfo": "skip",
        },
        {
            "type": "scatter3d",
            "x": [float(origin[0]), float(origin[0] + L)],
            "y": [float(origin[1]), float(origin[1])],
            "z": [float(origin[2]), float(origin[2])],
            "mode": "lines+text",
            "line": {"color": "red", "width": 10},
            "text": ["", "X"],
            "textfont": {"color": "red", "size": 18},
            "textposition": "top center",
            "name": "X (RED)",
        },
        {
            "type": "scatter3d",
            "x": [float(origin[0]), float(origin[0])],
            "y": [float(origin[1]), float(origin[1] + L)],
            "z": [float(origin[2]), float(origin[2])],
            "mode": "lines+text",
            "line": {"color": "lime", "width": 10},
            "text": ["", "Y"],
            "textfont": {"color": "lime", "size": 18},
            "textposition": "top center",
            "name": "Y (GREEN)",
        },
        {
            "type": "scatter3d",
            "x": [float(origin[0]), float(origin[0])],
            "y": [float(origin[1]), float(origin[1])],
            "z": [float(origin[2]), float(origin[2] + L)],
            "mode": "lines+text",
            "line": {"color": "#4af", "width": 10},
            "text": ["", "Z"],
            "textfont": {"color": "#4af", "size": 18},
            "textposition": "top center",
            "name": "Z (BLUE)",
        },
    ]

    layout = {
        "paper_bgcolor": "#111",
        "font": {"color": "white"},
        "title": {
            "text": f"Axes Viewer — {path.name} [{coord_frame}]",
            "font": {"color": "white"},
        },
        "legend": {"font": {"color": "white"}},
        "margin": {"l": 0, "r": 0, "t": 40, "b": 0},
        "scene": {
            "bgcolor": "#111",
            "xaxis": {"color": "white", "title": "X"},
            "yaxis": {"color": "white", "title": "Y"},
            "zaxis": {"color": "white", "title": "Z"},
            "aspectmode": "data",
        },
    }

    html = f"""<!DOCTYPE html><html><head><title>Axes Viewer</title>
<script src="https://cdn.plot.ly/plotly-3.5.0.min.js" integrity="sha384-DPvk2KODrsA0CfBr4HTwAwhdDROPqqK2PvSSswJMQMpnUkwSTg4gLBxXc3wv2e5L" crossorigin="anonymous"></script>
<style>html,body{{margin:0;padding:0;width:100%;height:100%;background:#111;overflow:hidden}}
#plot{{position:absolute;top:0;left:0;right:0;bottom:0}}
#info{{position:fixed;top:10px;left:10px;z-index:1000;background:rgba(0,0,0,0.85);padding:14px;border-radius:8px;color:white;font-family:sans-serif;font-size:14px;line-height:1.8}}
</style></head>
<body>
<div id="info"><b>Which axis is UP (ceiling)?</b><br>
<span style="color:red">X axis (RED)</span><br>
<span style="color:lime">Y axis (GREEN)</span><br>
<span style="color:#4af">Z axis (BLUE)</span></div>
<div id="plot"></div>
<script>Plotly.newPlot('plot',{json.dumps(traces, cls=NumpyEncoder)},{json.dumps(layout, cls=NumpyEncoder)},{{responsive:true}});</script>
</body></html>"""

    out.write_text(html)
    print(f"Saved: {out}")
    print(f"Serve: python -m http.server 8080 --directory {out.parent}/")
    print(f"Open:  http://localhost:8080/{out.name}")


if __name__ == "__main__":
    main()
