#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run targeted tests with affected module selection in the shared suite runner."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Sequence
from typing import cast

from scripts.production import merge_gate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    merge_gate.add_suite_arguments(parser)
    args = cast(merge_gate.GateArguments, parser.parse_args(argv))
    return merge_gate.run_tests_only(args, merge_gate.SuiteMode.TARGETED_TEST)


if __name__ == "__main__":
    raise SystemExit(main())
