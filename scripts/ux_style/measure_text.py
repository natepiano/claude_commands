#!/usr/bin/env python3
"""Measure one glyph in a screenshot: its cap height and its contrast.

Box one capital letter or digit, tight, with a little background around it and
nothing else inside. The box is in the full-size shot's pixels, not a shrunk
copy. The tool reports:

- cap height: the rows that hold ink, divided by the display scale, so the
  number is in logical pixels (the readable-size rule's unit);
- contrast: the WCAG 2.2 ratio between the ink's core colour and the
  background behind it (the readable-contrast rule's unit).

The background is the median colour of the box. The farthest colour is the
fifth farthest pixel, so a few stray specks cannot set it. Ink is every pixel
at least half that far from the background with another ink pixel beside it,
so an isolated speck neither counts as ink nor stretches the height. The ink's
core is the 90th percentile of its distance from the background: what the eye
reads as the text colour, anti-aliased edges included at small sizes.

Usage:
  measure_text.py <shot.png> --box X,Y,W,H [--scale 2]
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

INK_SHARE_OF_FARTHEST = 0.5
INK_CORE_PERCENTILE = 0.9
STRAY_PIXELS_IGNORED = 4


class Arguments(argparse.Namespace):
    shot: str = ""
    box: str = ""
    scale: float = 1.0


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Pixel:
    red: int
    green: int
    blue: int

    def luminance(self) -> float:
        def linear(channel: int) -> float:
            value = channel / 255
            return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

        return 0.2126 * linear(self.red) + 0.7152 * linear(self.green) + 0.0722 * linear(self.blue)

    def hex(self) -> str:
        return f"#{self.red:02x}{self.green:02x}{self.blue:02x}"


def parse_box(text: str) -> Box:
    parts = text.split(",")
    if len(parts) != 4:
        raise ValueError(f"--box wants X,Y,W,H, got {text!r}")
    x, y, width, height = (int(part) for part in parts)
    if width < 3 or height < 3:
        raise ValueError("--box must be at least 3x3 pixels")
    return Box(x, y, width, height)


def read_pixels(shot: Path, box: Box) -> list[list[Pixel]]:
    crop = f"{box.width}x{box.height}+{box.x}+{box.y}"
    result = subprocess.run(
        ["magick", str(shot), "-crop", crop, "+repage", "-alpha", "off", "-depth", "8", "RGB:-"],
        capture_output=True,
        check=True,
    )
    raw = result.stdout
    if len(raw) != box.width * box.height * 3:
        raise ValueError(f"the box {crop} runs past the edge of {shot.name}")
    rows: list[list[Pixel]] = []
    for row in range(box.height):
        start = row * box.width * 3
        rows.append(
            [Pixel(raw[start + 3 * column], raw[start + 3 * column + 1], raw[start + 3 * column + 2]) for column in range(box.width)]
        )
    return rows


def has_marked_neighbour(marked: list[list[bool]], row: int, column: int) -> bool:
    for neighbour_row in range(max(0, row - 1), min(len(marked), row + 2)):
        for neighbour_column in range(max(0, column - 1), min(len(marked[neighbour_row]), column + 2)):
            if (neighbour_row, neighbour_column) != (row, column) and marked[neighbour_row][neighbour_column]:
                return True
    return False


def contrast(first: float, second: float) -> float:
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def measure(rows: list[list[Pixel]], scale: float) -> str:
    pixels = [pixel for row in rows for pixel in row]
    background = Pixel(
        int(statistics.median(pixel.red for pixel in pixels)),
        int(statistics.median(pixel.green for pixel in pixels)),
        int(statistics.median(pixel.blue for pixel in pixels)),
    )
    background_luminance = background.luminance()
    distances = [[abs(pixel.luminance() - background_luminance) for pixel in row] for row in rows]
    ranked = sorted((distance for row in distances for distance in row), reverse=True)
    farthest = ranked[min(len(ranked) - 1, STRAY_PIXELS_IGNORED)]
    if farthest == 0:
        return "no ink: the box holds one flat colour"
    threshold = farthest * INK_SHARE_OF_FARTHEST
    marked = [[distance >= threshold for distance in row] for row in distances]
    ink_cells = [
        (row, column)
        for row in range(len(rows))
        for column in range(len(rows[row]))
        if marked[row][column] and has_marked_neighbour(marked, row, column)
    ]
    if not ink_cells:
        return "no ink: only isolated specks differ from the background"
    ink = sorted((rows[row][column] for row, column in ink_cells), key=lambda pixel: abs(pixel.luminance() - background_luminance))
    core = ink[min(len(ink) - 1, int(len(ink) * INK_CORE_PERCENTILE))]
    ink_rows = sorted({row for row, _ in ink_cells})
    cap_height = (ink_rows[-1] - ink_rows[0] + 1) / scale
    ratio = contrast(core.luminance(), background_luminance)
    return f"cap height {cap_height:.1f} logical px; contrast {ratio:.2f}:1 (ink {core.hex()} on {background.hex()})"


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure one glyph's cap height and contrast in a screenshot.")
    _ = parser.add_argument("shot", help="the full-size screenshot")
    _ = parser.add_argument("--box", required=True, help="X,Y,W,H around one capital letter or digit, in shot pixels")
    _ = parser.add_argument("--scale", type=float, default=1.0, help="shot pixels per logical pixel (2 on a Retina display)")
    arguments = parser.parse_args(namespace=Arguments())
    try:
        box = parse_box(arguments.box)
        rows = read_pixels(Path(arguments.shot), box)
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(measure(rows, arguments.scale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
