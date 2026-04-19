#!/usr/bin/env python3
"""Deterministic style guideline admin operations."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from style_history import HISTORY_DIR, NATE_STYLE_DIR, RUST_DIR, normalize_guideline_id
from style_report import REPORT_FILE, build_blocked_view, build_coverage_view, build_style_summary, iter_rows, render_report

RUST_STYLE_DIR = NATE_STYLE_DIR / "rust"


@dataclass
class FindingSection:
    number: int
    title: str
    lines: list[str]
    guideline_id: str | None


@dataclass
class FixSection:
    number: int
    title: str
    lines: list[str]


@dataclass
class EvaluationDocument:
    header_lines: list[str]
    findings: list[FindingSection]
    tail_lines: list[str]
    fix_sections: list[FixSection]
    post_fix_lines: list[str]


def trim_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return lines[start:end]


def compact_blank_lines(lines: list[str]) -> list[str]:
    compacted: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and previous_blank:
            continue
        compacted.append(line)
        previous_blank = is_blank
    return compacted


def update_rules_checked_line(header_lines: list[str], count: int) -> list[str]:
    updated: list[str] = []
    replaced = False
    for line in header_lines:
        if line.startswith("**Rules checked**:"):
            updated.append(f"**Rules checked**: {count}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"**Rules checked**: {count}")
    return updated


def replace_wikilinks_for_rename(text: str, old_stem: str, new_stem: str) -> str:
    return text.replace(f"[[{old_stem}]]", f"[[{new_stem}]]").replace(f"[[{old_stem}|", f"[[{new_stem}|")


def replace_wikilinks_for_delete(text: str, stem: str) -> str:
    text = re.sub(rf"\[\[{re.escape(stem)}\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(rf"\[\[{re.escape(stem)}\]\]", stem, text)
    return text


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.unlink(missing_ok=True)
        return
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def parse_evaluation_document(path: Path) -> EvaluationDocument:
    lines = path.read_text().splitlines()
    try:
        improvements_index = lines.index("## Improvements")
    except ValueError:
        return EvaluationDocument(lines, [], [], [], [])

    header_lines = lines[: improvements_index + 1]
    index = improvements_index + 1
    findings: list[FindingSection] = []
    tail_lines: list[str] = []
    fix_sections: list[FixSection] = []
    post_fix_lines: list[str] = []

    while index < len(lines):
        line = lines[index]
        if line == "## Fix Summary":
            break
        if line.startswith("### "):
            heading = line.removeprefix("### ").strip()
            number_text, _, title = heading.partition(".")
            if not number_text.isdigit():
                tail_lines.append(line)
                index += 1
                continue
            index += 1
            section_lines: list[str] = []
            guideline_id: str | None = None
            while index < len(lines):
                current = lines[index]
                if current.startswith("### ") or current == "## Fix Summary":
                    break
                if current.startswith("**Style file**:"):
                    raw_path = current.split(":", 1)[1].strip().strip("`")
                    guideline_id = normalize_guideline_id(raw_path)
                section_lines.append(current)
                index += 1
            findings.append(FindingSection(int(number_text), title.strip(), section_lines, guideline_id))
            continue
        tail_lines.append(line)
        index += 1

    if index >= len(lines):
        return EvaluationDocument(header_lines, findings, tail_lines, fix_sections, post_fix_lines)

    index += 1
    while index < len(lines):
        line = lines[index]
        if line.startswith("## ") and not line.startswith("### "):
            post_fix_lines = lines[index:]
            break
        if line.startswith("### Finding "):
            match = re.match(r"### Finding (\d+):\s*(.*)", line)
            if not match:
                post_fix_lines = lines[index:]
                break
            number = int(match.group(1))
            title = match.group(2).strip()
            index += 1
            section_lines: list[str] = []
            while index < len(lines):
                current = lines[index]
                if current.startswith("### Finding ") or (current.startswith("## ") and not current.startswith("### ")):
                    break
                section_lines.append(current)
                index += 1
            fix_sections.append(FixSection(number, title, section_lines))
            continue
        index += 1

    return EvaluationDocument(header_lines, findings, tail_lines, fix_sections, post_fix_lines)


def render_evaluation_document(doc: EvaluationDocument) -> str:
    lines: list[str] = []
    lines.extend(update_rules_checked_line(doc.header_lines, len(doc.findings)))
    if doc.findings:
        if lines and lines[-1] != "":
            lines.append("")
        for idx, finding in enumerate(doc.findings, start=1):
            lines.append(f"### {idx}. {finding.title}")
            lines.append("")
            lines.extend(trim_blank_edges(finding.lines))
            if idx != len(doc.findings):
                lines.append("")
        if doc.tail_lines:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(trim_blank_edges(doc.tail_lines))
    elif doc.tail_lines:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(trim_blank_edges(doc.tail_lines))

    if doc.fix_sections:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("## Fix Summary")
        lines.append("")
        for idx, section in enumerate(doc.fix_sections, start=1):
            lines.append(f"### Finding {idx}: {section.title}")
            lines.extend(trim_blank_edges(section.lines))
            if idx != len(doc.fix_sections):
                lines.append("")

    if doc.post_fix_lines:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(trim_blank_edges(doc.post_fix_lines))

    return "\n".join(compact_blank_lines(lines)).rstrip() + "\n"


def update_history_for_rename(old_guideline_id: str, new_guideline_id: str) -> tuple[int, int]:
    files_changed = 0
    entries_updated = 0
    for path in sorted(HISTORY_DIR.glob("*.jsonl")):
        rows = load_jsonl(path)
        changed = False
        for row in rows:
            for reviewed in row.get("reviewed_units", []):
                if reviewed.get("guideline_id") == old_guideline_id:
                    reviewed["guideline_id"] = new_guideline_id
                    entries_updated += 1
                    changed = True
        if changed:
            write_jsonl(path, rows)
            files_changed += 1
    return files_changed, entries_updated


def update_history_for_delete(guideline_id: str) -> tuple[int, int, int]:
    files_changed = 0
    entries_removed = 0
    runs_removed = 0
    for path in sorted(HISTORY_DIR.glob("*.jsonl")):
        rows = load_jsonl(path)
        new_rows: list[dict[str, Any]] = []
        changed = False
        for row in rows:
            reviewed_units = row.get("reviewed_units", [])
            kept_units = [reviewed for reviewed in reviewed_units if reviewed.get("guideline_id") != guideline_id]
            removed_here = len(reviewed_units) - len(kept_units)
            if removed_here:
                entries_removed += removed_here
                changed = True
            if kept_units:
                row["reviewed_units"] = kept_units
                new_rows.append(row)
            else:
                if removed_here:
                    runs_removed += 1
                    changed = True
        if changed:
            write_jsonl(path, new_rows)
            files_changed += 1
    return files_changed, entries_removed, runs_removed


def update_evaluations_for_rename(old_guideline_id: str, new_guideline_id: str) -> int:
    old_abs = str(NATE_STYLE_DIR / old_guideline_id)
    new_abs = str(NATE_STYLE_DIR / new_guideline_id)
    updated = 0
    for path in sorted(RUST_DIR.glob("*/EVALUATION.md")):
        original = path.read_text()
        rewritten = original.replace(old_abs, new_abs).replace(old_guideline_id, new_guideline_id)
        if rewritten != original:
            path.write_text(rewritten)
            updated += 1
    return updated


def update_evaluations_for_delete(guideline_id: str) -> int:
    updated = 0
    for path in sorted(RUST_DIR.glob("*/EVALUATION.md")):
        doc = parse_evaluation_document(path)
        if not doc.findings:
            continue
        remaining_findings = [finding for finding in doc.findings if finding.guideline_id != guideline_id]
        removed_numbers = {finding.number for finding in doc.findings if finding.guideline_id == guideline_id}
        if not removed_numbers:
            continue
        remaining_fix_sections = [section for section in doc.fix_sections if section.number not in removed_numbers]
        rewritten = render_evaluation_document(
            EvaluationDocument(
                header_lines=doc.header_lines,
                findings=remaining_findings,
                tail_lines=doc.tail_lines,
                fix_sections=remaining_fix_sections,
                post_fix_lines=doc.post_fix_lines,
            )
        )
        path.write_text(rewritten)
        updated += 1
    return updated


def update_markdown_wikilinks_for_rename(old_stem: str, new_stem: str) -> int:
    updated = 0
    for path in sorted(NATE_STYLE_DIR.rglob("*.md")):
        original = path.read_text()
        rewritten = replace_wikilinks_for_rename(original, old_stem, new_stem)
        if rewritten != original:
            path.write_text(rewritten)
            updated += 1
    return updated


def update_markdown_wikilinks_for_delete(stem: str) -> int:
    updated = 0
    for path in sorted(NATE_STYLE_DIR.rglob("*.md")):
        original = path.read_text()
        rewritten = replace_wikilinks_for_delete(original, stem)
        if rewritten != original:
            path.write_text(rewritten)
            updated += 1
    return updated


def regenerate_report() -> None:
    rows = iter_rows(None, None)
    REPORT_FILE.write_text(
        render_report(
            build_style_summary(rows),
            build_coverage_view(None),
            build_blocked_view(rows),
            len(rows),
        )
    )


def run_rename(old_name: str, new_name: str) -> None:
    source = RUST_STYLE_DIR / old_name
    target = RUST_STYLE_DIR / new_name
    if not source.exists():
        raise SystemExit(f"Source style file not found: {old_name}")
    if target.exists():
        raise SystemExit(f"Target style file already exists: {new_name}")

    old_guideline_id = f"rust/{old_name}"
    new_guideline_id = f"rust/{new_name}"
    source.rename(target)
    history_files, entries_updated = update_history_for_rename(old_guideline_id, new_guideline_id)
    evaluation_files = update_evaluations_for_rename(old_guideline_id, new_guideline_id)
    wikilink_files = update_markdown_wikilinks_for_rename(Path(old_name).stem, Path(new_name).stem)
    regenerate_report()

    print(f"Renamed: {old_name} -> {new_name}")
    print(f".history files updated: {history_files}")
    print(f".history entries updated: {entries_updated}")
    print(f"EVALUATION.md files updated: {evaluation_files}")
    print(f"Obsidian files updated: {wikilink_files}")


def run_delete(style_name: str) -> None:
    source = RUST_STYLE_DIR / style_name
    if not source.exists():
        raise SystemExit(f"Style file not found: {style_name}")

    guideline_id = f"rust/{style_name}"
    source.unlink()
    history_files, entries_removed, runs_removed = update_history_for_delete(guideline_id)
    evaluation_files = update_evaluations_for_delete(guideline_id)
    wikilink_files = update_markdown_wikilinks_for_delete(Path(style_name).stem)
    regenerate_report()

    print(f"Deleted: {style_name}")
    print(f".history files updated: {history_files}")
    print(f".history entries removed: {entries_removed}")
    print(f"Run records removed: {runs_removed}")
    print(f"EVALUATION.md files updated: {evaluation_files}")
    print(f"Obsidian files updated: {wikilink_files}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    rename = subparsers.add_parser("rename")
    rename.add_argument("old_name")
    rename.add_argument("new_name")

    delete = subparsers.add_parser("delete")
    delete.add_argument("style_name")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "rename":
        run_rename(args.old_name, args.new_name)
        return
    run_delete(args.style_name)


if __name__ == "__main__":
    main()
