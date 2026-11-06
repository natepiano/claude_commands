"""The shared terminal-cell measure for Python text layouts."""
from collections.abc import Sequence
import unicodedata


def terminal_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def pad_left(text: str, cells: int) -> str:
    return " " * max(0, cells - terminal_width(text)) + text


def pad_right(text: str, cells: int) -> str:
    return text + " " * max(0, cells - terminal_width(text))


def seconds(value: float) -> str:
    return f"{value:.3f}"


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]],
                empty_message: str = "no gate runs recorded yet") -> None:
    if not rows:
        print(empty_message)
        return
    widths = [max(terminal_width(header), *(terminal_width(row[column]) for row in rows))
              for column, header in enumerate(headers)]
    for row in [headers, *rows]:
        print("  ".join(pad_right(value, width)
                        for value, width in zip(row, widths, strict=True)).rstrip())

