#!/usr/bin/env python3
"""Run targeted tests with affected module selection in the shared suite runner."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import merge_gate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    merge_gate.add_suite_arguments(parser)
    args = cast(merge_gate.GateArguments, parser.parse_args(argv))
    return merge_gate.run_tests_only(args, merge_gate.SuiteMode.TARGETED_TEST)


if __name__ == "__main__":
    raise SystemExit(main())
