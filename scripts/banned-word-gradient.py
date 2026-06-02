#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""Render the banned-word counter report as a white->red gradient PNG.

Color maps to hit count: the largest count is pure red, the smallest is white.
A log scale (default) spreads the color across the full range so one dominant
word (e.g. `shape`) does not wash everything else to white; pass --scale linear
for a literal value mapping.

Data comes from the canonical loader in banned_words_lib (the same counter
state the hooks and `--analysis` report read), so this never diverges from the
text report.

Usage (uv auto-installs Pillow from the inline metadata above):
    uv run ~/.claude/scripts/banned-word-gradient.py
    uv run ~/.claude/scripts/banned-word-gradient.py --sort recency
    uv run ~/.claude/scripts/banned-word-gradient.py --scale linear --out /tmp/x.png

Prints the output PNG path on success.
"""

import argparse
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from PIL import Image, ImageDraw, ImageFont

# Reuse the canonical counter loader so this stays a single source of truth.
sys.path.insert(0, str(Path.home() / ".claude" / "scripts" / "hooks"))
from banned_words_lib import counter_analysis_rows  # noqa: E402

# (count) -> (r, g, b)
ColorFn = Callable[[int], tuple[int, int, int]]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    _ = p.add_argument(
        "--sort",
        choices=("count", "recency"),
        default="count",
        help="row order: by count descending (default) or most-recently-triggered first",
    )
    _ = p.add_argument(
        "--scale",
        choices=("log", "linear"),
        default="log",
        help="gradient scale: log (default, spreads color) or linear (literal value)",
    )
    _ = p.add_argument(
        "--out",
        default=os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "banned_word_gradient.png"
        ),
        help="output PNG path",
    )
    return p.parse_args()


def sorted_rows(sort: str) -> list[tuple[str, int, str]]:
    rows = counter_analysis_rows()  # [(stem, count, last_triggered_or_'never')]
    if sort == "recency":
        # newest first; 'never' (no timestamp) sorts last.
        rows.sort(key=lambda r: r[2], reverse=True)
        rows.sort(key=lambda r: r[2] == "never")
    else:
        rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def make_color_fn(counts: list[int], scale: str) -> ColorFn:
    lo, hi = min(counts), max(counts)

    def fraction(c: int) -> float:
        if hi == lo:
            return 0.0
        if scale == "log":
            return (math.log1p(c) - math.log1p(lo)) / (
                math.log1p(hi) - math.log1p(lo)
            )
        return (c - lo) / (hi - lo)

    def color(c: int) -> tuple[int, int, int]:
        x = fraction(c)  # 0 -> white, 1 -> red
        channel = round(255 * (1 - x))  # red channel stays pinned at 255
        return (255, channel, channel)

    return color


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render(rows: list[tuple[str, int, str]], color_fn: ColorFn, out: str) -> str:
    font_size = 22
    row_h = font_size + 12
    pad = 24
    header_h = row_h + 8

    name_w = max((len(w) for w, _, _ in rows), default=4)
    x_word = pad
    x_count = x_word + name_w * 12 + 30
    x_ts = x_count + 90
    width = x_ts + 230
    height = header_h + row_h * len(rows) + pad

    bg = (18, 18, 20)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    font = load_font(font_size)

    header_fill = (150, 150, 150)
    draw.text((x_word, pad), "word", font=font, fill=header_fill)
    draw.text((x_count, pad), "count", font=font, fill=header_fill)
    draw.text((x_ts, pad), "last triggered", font=font, fill=header_fill)
    draw.line((pad, pad + row_h, width - pad, pad + row_h), fill=(70, 70, 70), width=1)

    y = header_h + pad - 6
    for word, count, ts in rows:
        col = color_fn(count)
        disp = "never" if ts == "never" else ts.replace("T", " ")[:16]
        draw.text((x_word, y), word, font=font, fill=col)
        draw.text((x_count, y), str(count), font=font, fill=col)
        draw.text((x_ts, y), disp, font=font, fill=col)
        y += row_h

    img.save(out)
    return out


def main() -> None:
    args = parse_args()
    sort = cast(str, args.sort)
    scale = cast(str, args.scale)
    out_path = cast(str, args.out)
    rows = sorted_rows(sort)
    if not rows:
        print("no counter data found", file=sys.stderr)
        raise SystemExit(1)
    color_fn = make_color_fn([c for _, c, _ in rows], scale)
    out = render(rows, color_fn, out_path)
    print(out)


if __name__ == "__main__":
    main()
