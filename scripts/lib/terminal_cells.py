"""The shared terminal-cell measure for Python text layouts."""
from collections.abc import Sequence
import re
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


def is_number_cell(text: str) -> bool:
    """Decide whether a table cell belongs to a number column."""
    return text == "—" or re.fullmatch(
        r"(?:[+-]?\$?|\$[+-]?)(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[%s]?", text,
    ) is not None


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]],
                empty_message: str = "no gate runs recorded yet") -> None:
    if not rows:
        print(empty_message)
        return
    widths = [max(terminal_width(header), *(terminal_width(row[column]) for row in rows))
              for column, header in enumerate(headers)]
    padding = [pad_left if all(is_number_cell(row[column]) for row in rows) else pad_right
               for column in range(len(headers))]
    for row in [headers, *rows]:
        print("  ".join(pad(value, width)
                        for value, width, pad in zip(row, widths, padding, strict=True)).rstrip())

