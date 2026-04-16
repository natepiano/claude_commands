#!/usr/bin/env python3
"""Merge per-run style usage logs into the permanent log with normalization."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def normalize_reason_category(reason: str) -> str:
    lowered = reason.lower()
    if any(token in lowered for token in ("public interface", "private_interfaces", "e0446", "e0364", "re-exported", "structurally exposed")):
        return "public_api_exposure"
    if any(token in lowered for token in ("cargo mend", "toolchain", "compiler errors", "include!()", "test harness")):
        return "tooling_conflict"
    if any(token in lowered for token in ("not const fn", "type mismatch", "lifetimes", "cannot be re-exported")):
        return "language_constraint"
    if any(token in lowered for token in ("style guide explicitly", "style guide exempts", "exception")):
        return "style_exception"
    if any(token in lowered for token in ("contradicts style guide", "policy", "binary crate intentionally")):
        return "guide_conflict"
    if any(token in lowered for token in ("too large", "too risky", "major structural refactoring", "concurrent findings")):
        return "too_large_for_automation"
    return "other"


def enrich_entry(entry: dict[str, object]) -> dict[str, object]:
    findings = entry.get("findings")
    if not isinstance(findings, list):
        return entry

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        status = finding.get("status")
        reason = finding.get("reason")
        if status in {"partial", "skipped"} and isinstance(reason, str) and reason.strip():
            finding.setdefault("reason_category", normalize_reason_category(reason))
    return entry


def merge(run_dir: Path, log_file: Path) -> tuple[int, int]:
    merged = 0
    rejected = 0
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with log_file.open("a") as out_handle:
        for path in sorted(run_dir.glob("style_usage_*.jsonl")):
            for idx, raw_line in enumerate(path.read_text().splitlines()):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"WARN: skipping malformed line {idx} in {path.name}",
                        file=sys.stderr,
                    )
                    rejected += 1
                    continue
                out_handle.write(json.dumps(enrich_entry(payload), sort_keys=True) + "\n")
                merged += 1

    return merged, rejected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--log-file", required=True)
    args = parser.parse_args()

    merged, rejected = merge(
        Path(args.run_dir).expanduser().resolve(),
        Path(args.log_file).expanduser().resolve(),
    )
    print(f"Merged {merged} entries ({rejected} rejected)")


if __name__ == "__main__":
    main()
