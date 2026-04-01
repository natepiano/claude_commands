#!/usr/bin/env python3
"""Render lint-watcher-flow.dot with neato -n2, then inject cluster borders.

Reuses the same approach as the nightly pipeline flowchart:
1. Parse the .dot file to extract cluster membership, labels, and colors
2. Render with neato -n2 to get perfect node positioning
3. Parse the SVG to find each node's bounding box
4. Inject dashed <rect> elements + labels for each cluster
5. Rewrite the SVG viewBox to fit all content with uniform padding

Usage: python3 render-flow.py
"""

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict

SCRIPT_DIR = Path(__file__).parent
DOT_FILE = SCRIPT_DIR / "lint-watcher-flow.dot"
SVG_FILE = SCRIPT_DIR / "lint-watcher-flow.svg"

# Layout constants
CLUSTER_PAD_X = 18.0
CLUSTER_PAD_Y = 30.0
LABEL_HEIGHT = 14.0
LABEL_FONT_SIZE = 9.0
LABEL_INSET_X = 8.0
LABEL_BASELINE_ADJUST = 2.0
LABEL_CHAR_WIDTH_FACTOR = 0.62
CLUSTER_STROKE_WIDTH = 1.5
CLUSTER_DASH_ARRAY = "6,4"
CLUSTER_CORNER_RADIUS = 4.0
SVG_OUTER_PAD = 15.0

# This diagram has vertically stacked clusters on the right, so forcing a
# shared top line causes label overlap. Leave cluster tops independent.
PHASE_CLUSTER_IDS: tuple[str, ...] = ()

SVG_NS = "http://www.w3.org/2000/svg"
Bbox = tuple[float, float, float, float]


class ClusterDef(TypedDict):
    nodes: list[str]
    label: str
    color: str
    fontcolor: str


# ── Dot file parsing ──


def _extract_brace_block(text: str, open_pos: int) -> str:
    depth = 0
    in_quotes = False
    i = open_pos
    while i < len(text):
        ch = text[i]
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_pos + 1 : i]
        i += 1
    return text[open_pos + 1 :]


_DOT_KEYWORDS = frozenset({
    "label", "style", "color", "fontcolor", "fontsize",
    "margin", "node", "edge", "graph", "subgraph",
})


def parse_dot_clusters(dot_path: Path) -> dict[str, ClusterDef]:
    text = dot_path.read_text()
    clusters: dict[str, ClusterDef] = {}

    for header_match in re.finditer(r"subgraph\s+(cluster_\w+)\s*\{", text):
        cluster_id = header_match.group(1)
        brace_pos = header_match.end() - 1
        body = _extract_brace_block(text, brace_pos)

        label_match = re.search(r'label\s*=\s*"([^"]*)"', body)
        label = label_match.group(1) if label_match else cluster_id

        color_match = re.search(r'(?<!\w)color\s*=\s*"([^"]*)"', body)
        color = color_match.group(1) if color_match else "#95a5a6"

        fontcolor_match = re.search(r'fontcolor\s*=\s*"([^"]*)"', body)
        fontcolor = fontcolor_match.group(1) if fontcolor_match else "#7f8c8d"

        node_ids: list[str] = []
        for line in body.splitlines():
            stripped = line.strip()
            node_match = re.match(r"(\w+)\s*\[", stripped)
            if node_match:
                candidate = node_match.group(1)
                if candidate not in _DOT_KEYWORDS:
                    node_ids.append(candidate)

        clusters[cluster_id] = {
            "nodes": node_ids,
            "label": label,
            "color": color,
            "fontcolor": fontcolor,
        }

    return clusters


# ── SVG helpers ──


def parse_length(value: str | None, *, default: float = 0.0) -> float:
    if value is None:
        return default
    cleaned = value.strip()
    for suffix in ("pt", "px"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return float(cleaned)


def parse_points(points_str: str) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for pair in points_str.split():
        parts = pair.split(",")
        if len(parts) == 2:
            coords.append((float(parts[0]), float(parts[1])))
    return coords


def bbox_from_points(points: list[tuple[float, float]]) -> Bbox | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def union_bboxes(bboxes: list[Bbox]) -> Bbox | None:
    if not bboxes:
        return None
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def text_bbox(x: float, y: float, text: str, font_size: float, anchor: str) -> Bbox:
    text_width = max(len(text), 1) * font_size * LABEL_CHAR_WIDTH_FACTOR
    if anchor == "middle":
        x1 = x - (text_width / 2)
    elif anchor == "end":
        x1 = x - text_width
    else:
        x1 = x
    y1 = y - font_size
    return (x1, y1, x1 + text_width, y + (font_size * 0.3))


def get_element_bbox(elem: ET.Element) -> Bbox | None:
    tag = elem.tag.replace(f"{{{SVG_NS}}}", "")

    if tag == "polygon":
        return bbox_from_points(parse_points(elem.get("points", "")))
    if tag == "ellipse":
        cx = parse_length(elem.get("cx"))
        cy = parse_length(elem.get("cy"))
        rx = parse_length(elem.get("rx"))
        ry = parse_length(elem.get("ry"))
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    if tag == "rect":
        x = parse_length(elem.get("x"))
        y = parse_length(elem.get("y"))
        w = parse_length(elem.get("width"))
        h = parse_length(elem.get("height"))
        return (x, y, x + w, y + h)
    if tag == "path":
        raw: list[str] = re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", elem.get("d", ""))
        values: list[float] = [float(s) for s in raw]
        points = [(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]
        return bbox_from_points(points)
    if tag == "text":
        text = "".join(elem.itertext()).strip()
        if not text:
            return None
        return text_bbox(
            x=parse_length(elem.get("x")),
            y=parse_length(elem.get("y")),
            text=text,
            font_size=parse_length(elem.get("font-size"), default=10.0),
            anchor=elem.get("text-anchor", "start"),
        )
    return None


def get_graph_translation(transform: str | None) -> tuple[float, float]:
    if transform is None:
        return (0.0, 0.0)
    match = re.search(
        r"translate\(\s*([-+]?(?:\d*\.\d+|\d+))(?:[ ,]+([-+]?(?:\d*\.\d+|\d+)))?\s*\)",
        transform,
    )
    if match is None:
        return (0.0, 0.0)
    return (float(match.group(1)), float(match.group(2) or "0"))


def translate_bbox(bbox: Bbox, tx: float, ty: float) -> Bbox:
    return (bbox[0] + tx, bbox[1] + ty, bbox[2] + tx, bbox[3] + ty)


def get_node_bbox(g_elem: ET.Element) -> Bbox | None:
    for poly in g_elem.iter(f"{{{SVG_NS}}}polygon"):
        return bbox_from_points(parse_points(poly.get("points", "")))
    for ellipse in g_elem.iter(f"{{{SVG_NS}}}ellipse"):
        cx = float(ellipse.get("cx", "0"))
        cy = float(ellipse.get("cy", "0"))
        rx = float(ellipse.get("rx", "0"))
        ry = float(ellipse.get("ry", "0"))
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    return None


def get_content_bbox(root: ET.Element, graph_g: ET.Element) -> Bbox | None:
    tx, ty = get_graph_translation(graph_g.get("transform"))
    bboxes: list[Bbox] = []
    for elem in graph_g.iter():
        if elem.tag == f"{{{SVG_NS}}}title":
            continue
        if elem.tag == f"{{{SVG_NS}}}polygon" and (
            elem.get("fill") == "white" and elem.get("stroke") == "none"
        ):
            continue
        bbox = get_element_bbox(elem)
        if bbox is not None:
            bboxes.append(translate_bbox(bbox, tx, ty))
    if not bboxes:
        vb = root.get("viewBox")
        if vb is None:
            return None
        x, y, w, h = [float(v) for v in vb.split()]
        return (x, y, x + w, y + h)
    return union_bboxes(bboxes)


def set_svg_viewbox(root: ET.Element, graph_g: ET.Element) -> None:
    content_bbox = get_content_bbox(root, graph_g)
    if content_bbox is None:
        return
    min_x = content_bbox[0] - SVG_OUTER_PAD
    min_y = content_bbox[1] - SVG_OUTER_PAD
    max_x = content_bbox[2] + SVG_OUTER_PAD
    max_y = content_bbox[3] + SVG_OUTER_PAD
    w = max_x - min_x
    h = max_y - min_y
    root.set("width", f"{w:.1f}pt")
    root.set("height", f"{h:.1f}pt")
    root.set("viewBox", f"{min_x:.2f} {min_y:.2f} {w:.2f} {h:.2f}")


# ── Main pipeline ──


def render_dot() -> None:
    _ = subprocess.run(
        ["neato", "-n2", "-Tsvg", str(DOT_FILE), "-o", str(SVG_FILE)],
        check=True,
    )


def inject_clusters(svg_path: Path, clusters: dict[str, ClusterDef]) -> None:
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    tree = ET.parse(svg_path)
    root = tree.getroot()

    graph_g = root.find(f".//{{{SVG_NS}}}g[@id='graph0']")
    if graph_g is None:
        print("Warning: could not find graph0 group")
        return

    node_bboxes: dict[str, Bbox] = {}
    for g_elem in graph_g.iter(f"{{{SVG_NS}}}g"):
        if g_elem.get("class") != "node":
            continue
        title_elem = g_elem.find(f"{{{SVG_NS}}}title")
        if title_elem is None or title_elem.text is None:
            continue
        node_id = title_elem.text.strip()
        bbox = get_node_bbox(g_elem)
        if bbox is not None:
            node_bboxes[node_id] = bbox

    cluster_bounds: dict[str, Bbox] = {}
    for cluster_id, info in clusters.items():
        bboxes = [
            node_bboxes[nid] for nid in info["nodes"] if nid in node_bboxes
        ]
        cb = union_bboxes(bboxes)
        if cb is not None:
            cluster_bounds[cluster_id] = cb

    phase_tops = [
        cluster_bounds[cid][1]
        for cid in PHASE_CLUSTER_IDS
        if cid in cluster_bounds
    ]
    phase_top_y = min(phase_tops) - CLUSTER_PAD_Y - LABEL_HEIGHT if phase_tops else 0.0

    cluster_rects: list[ET.Element] = []
    cluster_labels: list[ET.Element] = []

    for cluster_id, info in clusters.items():
        cb = cluster_bounds.get(cluster_id)
        if cb is None:
            continue

        x1 = cb[0] - CLUSTER_PAD_X
        x2 = cb[2] + CLUSTER_PAD_X
        y1 = (
            phase_top_y
            if cluster_id in PHASE_CLUSTER_IDS
            else cb[1] - CLUSTER_PAD_Y - LABEL_HEIGHT
        )
        y2 = cb[3] + CLUSTER_PAD_Y

        rect = ET.Element(f"{{{SVG_NS}}}rect")
        rect.set("x", f"{x1:.1f}")
        rect.set("y", f"{y1:.1f}")
        rect.set("width", f"{x2 - x1:.1f}")
        rect.set("height", f"{y2 - y1:.1f}")
        rect.set("fill", "none")
        rect.set("stroke", info["color"])
        rect.set("stroke-width", f"{CLUSTER_STROKE_WIDTH:.1f}")
        rect.set("stroke-dasharray", CLUSTER_DASH_ARRAY)
        rect.set("rx", f"{CLUSTER_CORNER_RADIUS:.1f}")
        cluster_rects.append(rect)

        lx = x1 + LABEL_INSET_X
        ly = y1 + LABEL_HEIGHT - LABEL_BASELINE_ADJUST
        text = ET.Element(f"{{{SVG_NS}}}text")
        text.set("x", f"{lx:.1f}")
        text.set("y", f"{ly:.1f}")
        text.set("font-family", "Helvetica,sans-Serif")
        text.set("font-size", f"{LABEL_FONT_SIZE:.0f}")
        text.set("fill", info["fontcolor"])
        text.text = info["label"]
        cluster_labels.append(text)

    insert_idx = 0
    for i, child in enumerate(graph_g):
        tag = child.tag.replace(f"{{{SVG_NS}}}", "")
        if tag == "g":
            insert_idx = i
            break
        insert_idx = i + 1

    for j, rect in enumerate(cluster_rects):
        graph_g.insert(insert_idx + j, rect)
    for label in cluster_labels:
        graph_g.append(label)

    set_svg_viewbox(root, graph_g)
    tree.write(svg_path, xml_declaration=True, encoding="UTF-8")


def main() -> None:
    clusters = parse_dot_clusters(DOT_FILE)
    print(f"Parsed {len(clusters)} clusters: {', '.join(clusters)}")
    render_dot()
    inject_clusters(SVG_FILE, clusters)
    print(f"Rendered: {SVG_FILE}")


if __name__ == "__main__":
    main()
