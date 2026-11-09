#!/usr/bin/env python3
"""Gate a bumped checkpoint in a standing checkout of its pushed tip."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from scripts.copies import copies

from scripts.production.checkpoint_gate import (
    Stop, complete_gate, read_record, record_lock,
    run_checkpoint_test_steps, save_record,
)
from scripts.production.production_doc import ProductionDocument
from scripts.message import sessions


def run_gate(path: Path) -> int:
    with record_lock(path):
        record = read_record(path)
        pid = os.getpid()
        proc_start = sessions.process_start(pid) or ""
        if record["result"] != "pending":
            print("bump gate already completed; no tests run", flush=True)
            return 0
        if record["pid"] != 0 and (record["pid"], record["proc_start"]) != (pid, proc_start):
            owner = sessions.process_liveness(record["pid"], record["proc_start"])
            if not isinstance(owner, sessions.SessionNotLive):
                print(f"bump gate held by pid {record['pid']}; no tests run", flush=True)
                return 0
        record["pid"] = pid
        record["proc_start"] = proc_start
        save_record(path, record)
    try:
        with copies.standing(record["owner_id"], Path(record["checkout"]), copies.CommittedSource(record["tip"])) as clone:
            production = ProductionDocument(Path(record["production_doc"]), record["name"], record["merge_branch"],
                                    clone, Path(record["checkout"]) / record["production_log"], ZoneInfo(record["zone"]))
            os.environ["BUILDLOG_UNIT"] = record["unit"]
            detail = run_checkpoint_test_steps(
                production, tuple(record["paths"]), tuple(tuple(command) for command in record["merge_tests"]),
                frozenset(record["flakes"]), frozenset(record["flaky_tests"]), Path(record["log"]),
            )
        complete_gate(path, record, "green", detail)
        return 0
    except (Stop, copies.CopyError, OSError, ValueError, KeyError, TypeError) as error:
        detail = f"{error.step}: {error.detail}" if isinstance(error, Stop) else f"gate failed before completion: {error}"
        print(detail, flush=True)
        complete_gate(path, record, "red", detail)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--record", required=True, type=Path)
    args = parser.parse_args()
    return run_gate(cast(Path, args.record))


if __name__ == "__main__":
    raise SystemExit(main())
