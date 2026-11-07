"""Adapt test identity strings to the shared reader's named results."""
from collections.abc import Callable

from .process_start import Gone, ProcessStart, Started


def process_reader(read: Callable[[int], str | None]) -> Callable[[int], ProcessStart]:
    def result(pid: int) -> ProcessStart:
        start = read(pid)
        return Started(start) if start is not None else Gone()
    return result
