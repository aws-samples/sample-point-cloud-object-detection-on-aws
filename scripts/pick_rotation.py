"""Generate axes viewers for all rotations so user can pick the correct one."""

import json
import sys
from pathlib import Path

import numpy as np
import trimesh


class NumpyEncoder(json.JSONEncoder):
    def default(self, o):
        # Parameter is named `o` to match json.JSONEncoder.default(self, o); pyright
        # treats a renamed positional parameter as an incompatible override.
        if isinstance(o, (np.floating, float)):
            return float(o)
        if isinstance(o, (np.integer, int)):
            return int(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return super().default(o)


def rot_x(deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_y(deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_z(deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def make_viewer(xyz, colors, title, out_path):
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
            "line": {"color": "red", "width": 8},
            "text": ["", "X"],
            "textfont": {"color": "red", "size": 16},
            "textposition": "top center",
            "name": "X",
        },
        {
            "type": "scatter3d",
            "x": [float(origin[0]), float(origin[0])],
            "y": [float(origin[1]), float(origin[1] + L)],
            "z": [float(origin[2]), float(origin[2])],
            "mode": "lines+text",
            "line": {"color": "lime", "width": 8},
            "text": ["", "Y"],
            "textfont": {"color": "lime", "size": 16},
            "textposition": "top center",
            "name": "Y",
        },
        {
            "type": "scatter3d",
            "x": [float(origin[0]), float(origin[0])],
            "y": [float(origin[1]), float(origin[1])],
            "z": [float(origin[2]), float(origin[2] + L)],
            "mode": "lines+text",
            "line": {"color": "#4af", "width": 8},
            "text": ["", "Z"],
            "textfont": {"color": "#4af", "size": 16},
            "textposition": "top center",
            "name": "Z",
        },
    ]
    layout = {
        "paper_bgcolor": "#111",
        "font": {"color": "white"},
        "title": {"text": title, "font": {"color": "white", "size": 14}},
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
    html = f"""<!DOCTYPE html><html><head><title>{title}</title>
<script src="https://cdn.plot.ly/plotly-3.5.0.min.js" integrity="sha384-DPvk2KODrsA0CfBr4HTwAwhdDROPqqK2PvSSswJMQMpnUkwSTg4gLBxXc3wv2e5L" crossorigin="anonymous"></script>
<style>html,body{{margin:0;padding:0;width:100%;height:100%;background:#111;overflow:hidden}}
#plot{{position:absolute;top:0;left:0;right:0;bottom:0}}</style></head>
<body><div id="plot"></div>
<script>Plotly.newPlot('plot',{json.dumps(traces, cls=NumpyEncoder)},{json.dumps(layout, cls=NumpyEncoder)},{{responsive:true}});</script>
</body></html>"""
    out_path.write_text(html)


def main():
    ply_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("output_custom")
    max_pts = int(sys.argv[3]) if len(sys.argv) > 3 else 30000

    # trimesh.load is typed as returning the Geometry base class, which declares
    # neither vertices nor colors -- those are on the concrete PointCloud/Trimesh
    # subclasses, and which one comes back depends on the file. getattr keeps the
    # duck-typed access while giving the type checker something it can follow.
    cloud = trimesh.load(str(ply_path), process=False)
    verts = getattr(cloud, "vertices", None)
    if verts is None:
        raise ValueError(f"PLY file does not contain vertices: {ply_path}")
    xyz_raw = np.asarray(verts, dtype=np.float64)
    cloud_colors = getattr(cloud, "colors", None)
    colors_raw = (
        np.asarray(cloud_colors[:, :3], dtype=np.float32) / 255.0
        if cloud_colors is not None
        else np.full((len(xyz_raw), 3), 0.7, dtype=np.float32)
    )

    # Subsample
    idx = np.random.default_rng(42).choice(
        len(xyz_raw), min(max_pts, len(xyz_raw)), replace=False
    )
    xyz_raw = xyz_raw[idx]
    colors_raw = colors_raw[idx]

    # Generate all candidate rotations
    candidates = {
        "raw (no transform)": np.eye(3),
        "X+45": rot_x(45),
        "X-45": rot_x(-45),
        "X+90": rot_x(90),
        "X-90": rot_x(-90),
        "Y+45": rot_y(45),
        "Y-45": rot_y(-45),
        "Y+90": rot_y(90),
        "Y-90": rot_y(-90),
        "Z+45": rot_z(45),
        "Z-45": rot_z(-45),
        "Z+90": rot_z(90),
    }

    links = []
    for name, R in candidates.items():
        xyz_t = (R @ xyz_raw.T).T
        fname = f"rot_{name.replace(' ', '_').replace('(', '').replace(')', '').replace('/', '')}.html"
        make_viewer(xyz_t, colors_raw, name, out_dir / fname)
        links.append((name, fname))
        print(f"Generated: {fname}")

    # Index page
    index = """<!DOCTYPE html><html><head><title>Pick Rotation</title>
<style>body{background:#111;color:white;font-family:sans-serif;padding:20px}
a{color:#4af;display:block;margin:8px 0;font-size:16px}
h2{color:white}</style></head><body>
<h2>Pick the rotation where the floor is flat and ceiling is up</h2>\n"""
    for name, fname in links:
        index += f'<a href="{fname}" target="_blank">{name}</a>\n'
    index += "</body></html>"
    (out_dir / "pick_rotation.html").write_text(index)
    print("\nOpen: http://localhost:8080/pick_rotation.html")
    print("Click each link and find the one where the room is upright with floor flat.")


if __name__ == "__main__":
    main()
