"""The shared terminal-cell measure for Python text layouts."""
import unicodedata


def terminal_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)
