"""Tiny hand-layout SVG kit for the publication diagrams.

Graphviz auto-layout (including the `diagrams` library's) produced crossing,
unreadable edges for these figures, so they are laid out by hand on a fixed
canvas instead: every box, icon and orthogonal edge has explicit coordinates.

Icons are the official AWS Architecture Icons (plus Kubernetes and a few
open-source project marks) as vendored by the `diagrams` package. PNG export renders
in-process with resvg (`resvg-py`), so the dependencies are those two packages and no
system library.
"""

from __future__ import annotations

import base64
import html
import importlib.util
from functools import cache
from pathlib import Path

FONT = "Helvetica, Arial, sans-serif"
# Helvetica has no arrow glyphs, and resvg replaces the font of a whole text element
# that has a glyph its font lacks, with a serif fallback. Arial has them and Helvetica's
# metrics, so a label with an arrow is set in Arial.
ARROW_FONT = "Arial, Helvetica, sans-serif"
ARROWS = frozenset("\u2192\u2194")


def _font(text: str) -> str:
    return ARROW_FONT if ARROWS & set(text) else FONT


# Edge palette: one colour per concern, so a flow can be followed by colour alone.
DATA = "#1a5fb4"
SCALE = "#d35400"
PLATFORM = "#6b6b6b"
THREAT = "#c0392b"
FIXED = "#2e7d32"
PARTIAL = "#b86e00"
IDENTITY = "#8e44ad"
INK = "#232f3e"


@cache
def _resources() -> Path:
    # Located rather than imported: the icons are data files next to the package,
    # and `diagrams` is a render-time tool, not a project dependency.
    spec = importlib.util.find_spec("diagrams")
    if spec is None or spec.origin is None:
        raise SystemExit(
            "run with: uv run --no-project --with diagrams==0.25.1"
            " --with resvg-py==0.5.0 ..."
        )
    return Path(spec.origin).resolve().parent.parent / "resources"


@cache
def _icon_uri(name: str) -> str:
    data = (_resources() / f"{name}.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode()


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        # Painted in this order, so text and badges always sit above edges.
        self.layers: dict[str, list[str]] = {
            "box": [],
            "edge": [],
            "node": [],
            "text": [],
        }

    def text(
        self,
        x: float,
        y: float,
        s: str,
        *,
        size: int = 15,
        color: str = INK,
        anchor: str = "middle",
        weight: str = "normal",
        bg: bool = False,
        line: float = 1.2,
        italic: bool = False,
    ) -> None:
        lines = s.split("\n")
        if bg:  # white plate so a label stays legible where it crosses a line
            width = max(len(ln) for ln in lines) * size * 0.56 + 10
            height = len(lines) * size * line + 4
            left = {"start": x - 5, "middle": x - width / 2, "end": x - width + 5}
            self.layers["text"].append(
                f'<rect x="{left[anchor]:.1f}" y="{y - size - 1:.1f}" '
                f'width="{width:.1f}" height="{height:.1f}" rx="4" '
                'fill="white" fill-opacity="0.92"/>'
            )
        tspans = "".join(
            f'<tspan x="{x}" dy="{0 if i == 0 else size * line:.1f}">'
            f"{html.escape(ln)}</tspan>"
            for i, ln in enumerate(lines)
        )
        style = ' font-style="italic"' if italic else ""
        self.layers["text"].append(
            f'<text x="{x}" y="{y}" font-family="{_font("".join(lines))}" '
            f'font-size="{size}" '
            f'fill="{color}" text-anchor="{anchor}" font-weight="{weight}"{style}>'
            f"{tspans}</text>"
        )

    def box(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        label: str = "",
        *,
        stroke: str = INK,
        fill: str = "none",
        dash: str | None = None,
        icon: str | None = None,
        size: int = 15,
        width: float = 1.6,
        where: str = "top-left",
    ) -> None:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.layers["box"].append(
            f'<rect x="{x1}" y="{y1}" width="{x2 - x1}" height="{y2 - y1}" rx="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"{dash_attr}/>'
        )
        left = x1 + 10
        if icon:
            self.layers["box"].append(
                f'<image href="{_icon_uri(icon)}" x="{x1 + 6}" y="{y1 + 5}" '
                'width="24" height="24"/>'
            )
            left = x1 + 36
        if not label:
            return
        vertical, horizontal = where.split("-")
        y = y1 + 22 if vertical == "top" else y2 - 9
        x, anchor = (left, "start") if horizontal == "left" else (x2 - 10, "end")
        if vertical == "bottom" and horizontal == "left":
            x = x1 + 10
        self.text(x, y, label, size=size, color=stroke, anchor=anchor, weight="bold")

    def node(
        self,
        x: float,
        y: float,
        icon: str,
        label: str = "",
        *,
        size: int = 52,
        where: str = "below",
        font: int = 15,
        dy: float = 0,
    ) -> None:
        h = size / 2
        self.layers["node"].append(
            f'<image href="{_icon_uri(icon)}" x="{x - h}" y="{y - h}" '
            f'width="{size}" height="{size}"/>'
        )
        if not label:
            return
        if where == "below":
            self.text(x, y + h + font + 2 + dy, label, size=font)
            return
        # Beside the icon, vertically centred on it.
        n = label.count("\n") + 1
        ty = y - (n - 1) * font * 0.6 + font * 0.35 + dy
        if where == "right":
            self.text(x + h + 10, ty, label, size=font, anchor="start")
        else:
            self.text(x - h - 10, ty, label, size=font, anchor="end")

    def edge(
        self,
        pts: list[tuple[float, float]],
        *,
        color: str = DATA,
        dash: str | None = None,
        width: float = 2.2,
        head: bool = True,
        tail: bool = False,
        halo: bool = True,
    ) -> None:
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        if halo:  # white underlay, so crossings read as a clean gap
            self.layers["edge"].append(
                f'<path d="{d}" fill="none" stroke="white" '
                f'stroke-width="{width + 5}" stroke-linejoin="round" '
                'stroke-linecap="round"/>'
            )
        marker = f"url(#m{color.lstrip('#')})"
        attrs = f' stroke-dasharray="{dash}"' if dash else ""
        attrs += f' marker-end="{marker}"' if head else ""
        attrs += f' marker-start="{marker}"' if tail else ""
        self.layers["edge"].append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linejoin="round"{attrs}/>'
        )

    def badge(
        self,
        x: float,
        y: float,
        n: str,
        color: str = DATA,
        *,
        outline: bool = False,
        pill: bool = False,
    ) -> None:
        # outline=True draws a hollow badge: white fill, coloured ring and text.
        fill, ring, ink = (
            ("white", color, color) if outline else (color, "white", "white")
        )
        if len(n) <= 2 and not pill:
            shape = f'<circle cx="{x}" cy="{y}" r="12" fill="{fill}" '
        else:  # pill for longer ids such as "T16"
            w = 9 * max(len(n), 3) + 8
            shape = (
                f'<rect x="{x - w / 2}" y="{y - 11}" width="{w}" height="22" '
                f'rx="11" fill="{fill}" '
            )
        self.layers["text"].append(
            shape + f'stroke="{ring}" stroke-width="2"/>'
            f'<text x="{x}" y="{y + 5}" font-family="{FONT}" font-size="14" '
            f'font-weight="bold" fill="{ink}" text-anchor="middle">{n}</text>'
        )

    def save(self, stem: Path, scale: int = 2) -> None:
        # One arrowhead per colour; auto-start-reverse flips it for marker-start.
        markers = "".join(
            f'<marker id="m{c.lstrip("#")}" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
            for c in (DATA, SCALE, PLATFORM, THREAT, FIXED, IDENTITY, INK)
        )
        body = "".join("".join(self.layers[k]) for k in self.layers)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
            f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
            f"<defs>{markers}</defs>"
            f'<rect width="100%" height="100%" fill="white"/>{body}</svg>'
        )
        stem.parent.mkdir(parents=True, exist_ok=True)
        svg_path = stem.with_suffix(".svg")
        svg_path.write_text(svg)
        import resvg_py  # pyright: ignore[reportMissingImports]

        png = resvg_py.svg_to_bytes(
            svg_path=str(svg_path), zoom=scale, background="white"
        )
        stem.with_suffix(".png").write_bytes(bytes(png))
