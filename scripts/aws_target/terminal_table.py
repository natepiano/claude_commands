"""Align terminal table columns with two spaces between them."""
from collections.abc import Collection, Sequence


def aligned_table(headers: Sequence[str], rows: Sequence[Sequence[str]], *,
                  right: Collection[int] = (), minimum_widths: Sequence[int] = ()) -> list[str]:
    """Keep text left aligned and selected numeric columns right aligned."""
    widths = [max(len(header), *(len(row[index]) for row in rows),
                  minimum_widths[index] if minimum_widths else 0) for index, header in enumerate(headers)]
    return ['  '.join(cell.rjust(widths[index]) if index in right else cell.ljust(widths[index])
                      for index, cell in enumerate(row)).rstrip() for row in (headers, *rows)]
