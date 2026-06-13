#!/usr/bin/env python3
"""Deterministic candidate enumeration for style-eval units.

A guideline that admits mechanical enumeration declares a `candidates:` block
in its frontmatter naming a generator kind. The generator produces a superset
of the rule's violations as (file, line, text) candidates; the eval agent
judges each one (violation / exception) and never enumerates.

`style_history.py` calls `enumerate_candidates` from both `next-unit` (to hand
the agent its closed list) and `record-unit` (to verify every candidate got a
disposition), so generators must be deterministic and read-only.

Superset rule: a mechanical exclude that can suppress a real violation is
wrong even when it shrinks the list. Deliberate volume excludes (e.g. the
COMMON_TYPES list in the field-naming generators) are documented inline; the
design doc's "generator drift" mitigation covers the residual risk.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from typing import cast

# A generator returning more candidates than this is a mis-tuned spec, not a
# dirty project — refuse loudly instead of flooding the agent prompt.
MAX_CANDIDATES = 150


@dataclass(frozen=True)
class Candidate:
    file: str  # path relative to the project root
    line: int  # 1-based; 1 for file-level candidates
    text: str


@dataclass(frozen=True)
class CandidatesSpec:
    kind: str
    origin: str = ""  # style file the spec came from, for error messages
    pattern: str = ""
    exclude_pattern: str = ""
    globs: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    paths_exempt: tuple[str, ...] = ()
    structs: tuple[str, ...] = ()
    field_type_pattern: str = ""


@dataclass(frozen=True)
class Enumeration:
    candidates: tuple[Candidate, ...]
    source: str  # how the list was produced, for auditability


# ---------------------------------------------------------------------------
# Frontmatter spec parsing
# ---------------------------------------------------------------------------

_SPEC_KEY_RE = re.compile(r"^\s+([a-z_]+):\s*(.*)$")


def _parse_spec_value(raw: str) -> str | tuple[str, ...]:
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        items: list[str] = []
        for part in value[1:-1].split(","):
            item = part.strip().strip("'\"")
            if item:
                items.append(item)
        return tuple(items)
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        value = value[1:-1]
    return value


def read_candidates_spec(style_path: Path) -> CandidatesSpec | None:
    """Extract the `candidates:` block from a guideline's frontmatter.

    Sub-keys sit one indent level under `candidates:`; list values are inline
    (`[a, b]`). Returns None when the guideline has no block.
    """
    lines = style_path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    in_block = False
    values: dict[str, str | tuple[str, ...]] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.rstrip() == "candidates:":
            in_block = True
            continue
        if not in_block:
            continue
        match = _SPEC_KEY_RE.match(line)
        if match is None:
            in_block = False
            continue
        values[match.group(1)] = _parse_spec_value(match.group(2))
    if "kind" not in values:
        return None

    def str_value(key: str) -> str:
        value = values.get(key, "")
        return value if isinstance(value, str) else ""

    def tuple_value(key: str) -> tuple[str, ...]:
        value = values.get(key, ())
        if isinstance(value, tuple):
            return value
        return (value,) if value else ()

    return CandidatesSpec(
        kind=str_value("kind"),
        origin=str(style_path),
        pattern=str_value("pattern"),
        exclude_pattern=str_value("exclude_pattern"),
        globs=tuple_value("globs"),
        paths=tuple_value("paths"),
        paths_exempt=tuple_value("paths_exempt"),
        structs=tuple_value("structs"),
        field_type_pattern=str_value("field_type_pattern"),
    )


# ---------------------------------------------------------------------------
# Rust source lexing — mask strings/comments so scanners see only code shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StringLiteral:
    line: int  # 1-based
    col: int  # 0-based column of the opening quote
    text: str  # contents without quotes
    kind: str  # "string" | "char"


@dataclass(frozen=True)
class MaskedSource:
    lines: list[str]  # masked text, parallel to the original lines
    strings: list[StringLiteral]


_RAW_OPEN_RE = re.compile(r'b?r(#*)"')
_IDENT_CHAR_RE = re.compile(r"[A-Za-z0-9_]")


def mask_source(text: str) -> MaskedSource:
    """Blank out comments and string/char literal contents, keeping newlines.

    Handles line and nested block comments, plain/byte strings with escapes,
    raw strings (`r"..."`, `r#"..."#`), and char literals vs lifetimes.
    """
    out = list(text)
    n = len(text)
    literal_spans: list[tuple[int, int, str]] = []  # (start, end_exclusive, kind)

    def blank(a: int, b: int) -> None:
        for j in range(a, min(b, n)):
            if out[j] != "\n":
                out[j] = " "

    i = 0
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
            continue
        if c == "/" and nxt == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif text[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
            continue
        if c in "br":
            prev = text[i - 1] if i > 0 else " "
            raw_match = _RAW_OPEN_RE.match(text, i)
            if raw_match is not None and _IDENT_CHAR_RE.match(prev) is None:
                closer = '"' + "#" * len(raw_match.group(1))
                j = text.find(closer, raw_match.end())
                j = n if j == -1 else j + len(closer)
                literal_spans.append((raw_match.end(), max(raw_match.end(), j - len(closer)), "string"))
                blank(i, j)
                i = j
                continue
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            literal_spans.append((i + 1, min(j, n), "string"))
            blank(i, j + 1)
            i = j + 1
            continue
        if c == "'":
            if nxt == "\\":
                j = text.find("'", i + 2)
                if j != -1 and j - i <= 12:
                    literal_spans.append((i + 1, j, "char"))
                    blank(i, j + 1)
                    i = j + 1
                    continue
            elif nxt and text[i + 2 : i + 3] == "'":
                literal_spans.append((i + 1, i + 2, "char"))
                blank(i, i + 3)
                i = i + 3
                continue
        i += 1

    line_starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(idx + 1)

    def offset_line_col(offset: int) -> tuple[int, int]:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, offset - line_starts[lo]

    strings: list[StringLiteral] = []
    for start, end, kind in literal_spans:
        line, col = offset_line_col(max(start - 1, 0))
        strings.append(StringLiteral(line=line, col=col, text=text[start:end], kind=kind))
    return MaskedSource(lines="".join(out).splitlines(), strings=strings)


# ---------------------------------------------------------------------------
# File iteration and path exemptions
# ---------------------------------------------------------------------------


def rust_files(project_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(project_root.rglob("*.rs")):
        rel = path.relative_to(project_root).as_posix()
        parts = rel.split("/")
        if "target" in parts or any(part.startswith(".") for part in parts):
            continue
        files.append(path)
    return files


def path_is_exempt(rel: str, exempts: tuple[str, ...]) -> bool:
    rel_slash = "/" + rel
    for raw in exempts:
        entry = raw.strip("/")
        if not entry:
            continue
        if rel == entry or rel_slash.endswith("/" + entry) or f"/{entry}/" in rel_slash:
            return True
    return False


def is_test_file(rel: str) -> bool:
    return rel.endswith("tests.rs") or "/tests/" in "/" + rel


def read_masked(path: Path) -> MaskedSource:
    try:
        return mask_source(path.read_text())
    except (OSError, UnicodeDecodeError):
        return MaskedSource(lines=[], strings=[])


# ---------------------------------------------------------------------------
# Structural scanners (masked text in, line-anchored shapes out)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDef:
    name: str
    type_text: str
    line: int  # 1-based


@dataclass(frozen=True)
class StructDef:
    name: str
    line: int  # 1-based
    fields: tuple[FieldDef, ...]


@dataclass(frozen=True)
class EnumDef:
    name: str
    line: int  # 1-based
    variants: tuple[tuple[str, int], ...]  # (variant name, 1-based line)


_VIS_RE = r"(?:pub(?:\([^)]*\))?\s+)?"
_STRUCT_RE = re.compile(rf"^\s*{_VIS_RE}struct\s+([A-Za-z_]\w*)")
_ENUM_RE = re.compile(rf"^\s*{_VIS_RE}enum\s+([A-Za-z_]\w*)")
_FIELD_RE = re.compile(rf"^\s*{_VIS_RE}(?:r#)?([a-z_]\w*)\s*:\s*(.+?),?\s*$")
_VARIANT_RE = re.compile(r"^\s*([A-Z]\w*)\s*(?:[,({=]|$)")


def _find_body_open(lines: list[str], start_line: int, start_col: int) -> tuple[int, int] | None:
    """First `{` at/after (start_line, start_col), unless `;` or `(` comes first."""
    col = start_col
    for idx in range(start_line, len(lines)):
        segment = lines[idx][col:]
        for k, ch in enumerate(segment):
            if ch == "{":
                return idx, col + k
            if ch in ";(":
                return None
        col = 0
    return None


def _scan_block_fields(lines: list[str], open_line: int, open_col: int) -> list[FieldDef]:
    fields: list[FieldDef] = []
    depth = 0
    for idx in range(open_line, len(lines)):
        segment = lines[idx][open_col if idx == open_line else 0 :]
        if depth == 1 and idx != open_line:
            match = _FIELD_RE.match(lines[idx])
            if match is not None:
                fields.append(FieldDef(name=match.group(1), type_text=match.group(2).strip(), line=idx + 1))
        for ch in segment:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return fields
    return fields


def scan_structs(lines: list[str]) -> list[StructDef]:
    structs: list[StructDef] = []
    for idx, line in enumerate(lines):
        match = _STRUCT_RE.match(line)
        if match is None:
            continue
        opened = _find_body_open(lines, idx, match.end())
        if opened is None:
            continue
        open_line, open_col = opened
        fields = _scan_block_fields(lines, open_line, open_col)
        structs.append(StructDef(name=match.group(1), line=idx + 1, fields=tuple(fields)))
    return structs


def scan_enums(lines: list[str]) -> list[EnumDef]:
    enums: list[EnumDef] = []
    for idx, line in enumerate(lines):
        match = _ENUM_RE.match(line)
        if match is None:
            continue
        opened = _find_body_open(lines, idx, match.end())
        if opened is None:
            continue
        open_line, open_col = opened
        variants: list[tuple[str, int]] = []
        depth = 0
        for j in range(open_line, len(lines)):
            segment = lines[j][open_col if j == open_line else 0 :]
            if depth == 1 and j != open_line:
                vmatch = _VARIANT_RE.match(lines[j])
                if vmatch is not None:
                    variants.append((vmatch.group(1), j + 1))
            closed = False
            for ch in segment:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        closed = True
                        break
            if closed:
                break
        enums.append(EnumDef(name=match.group(1), line=idx + 1, variants=tuple(variants)))
    return enums


def test_spans(lines: list[str]) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of `#[cfg(test)]` items."""
    spans: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        if "#[cfg(test)]" not in line:
            continue
        col = line.index("#[cfg(test)]")
        opened = _find_body_open(lines, idx, col)
        if opened is None:
            continue
        open_line, open_col = opened
        depth = 0
        for j in range(open_line, len(lines)):
            segment = lines[j][open_col if j == open_line else 0 :]
            for ch in segment:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        spans.append((idx + 1, j + 1))
                        break
            if depth == 0 and spans and spans[-1][0] == idx + 1:
                break
    return spans


def in_spans(line: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in spans)


_CAMEL_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def camel_words(name: str) -> list[str]:
    words: list[str] = _CAMEL_WORD_RE.findall(name)
    return [word.lower() for word in words]


def snake_case(name: str) -> str:
    return "_".join(camel_words(name))


def display_line(path: Path, line: int) -> str:
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:200]
    return ""


# ---------------------------------------------------------------------------
# regex kind — rg is the enumerator
# ---------------------------------------------------------------------------


def _run_rg(spec: CandidatesSpec, project_root: Path) -> list[Candidate]:
    args = ["rg", "--line-number", "--no-heading", "--color", "never", "--glob", "!target/**"]
    if spec.globs:
        for glob in spec.globs:
            args += ["--glob", glob]
    else:
        args += ["--type", "rust"]
    args.append(spec.pattern)
    args += list(spec.paths) if spec.paths else ["."]
    result = subprocess.run(
        args,
        cwd=project_root,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(
            f"candidates regex for {spec.origin} failed (rg exit {result.returncode}): "
            + result.stderr.strip()
        )
    exclude = re.compile(spec.exclude_pattern) if spec.exclude_pattern else None
    candidates: list[Candidate] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        rel, line_str, text = parts
        rel = rel.removeprefix("./")
        if path_is_exempt(rel, spec.paths_exempt):
            continue
        if exclude is not None and exclude.search(text):
            continue
        candidates.append(Candidate(file=rel, line=int(line_str), text=text.strip()[:200]))
    candidates.sort(key=lambda c: (c.file, c.line))
    return candidates


def gen_regex(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    if not spec.pattern:
        raise SystemExit(f"candidates kind=regex in {spec.origin} has no pattern")
    return Enumeration(
        candidates=tuple(_run_rg(spec, project_root)),
        source=f"regex:{spec.pattern}",
    )


# ---------------------------------------------------------------------------
# Cargo.toml generators
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, object]:
    empty: dict[str, object] = {}
    try:
        with path.open("rb") as handle:
            data: dict[str, object] = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return empty
    return data


def _toml_table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if isinstance(value, dict):
        return cast(dict[str, object], cast(object, value))
    empty: dict[str, object] = {}
    return empty


def _dep_line(toml_path: Path, dep_name: str) -> int:
    dep_re = re.compile(rf'^\s*"?{re.escape(dep_name)}"?\s*=')
    try:
        lines = toml_path.read_text().splitlines()
    except OSError:
        return 1
    for idx, line in enumerate(lines):
        if dep_re.match(line):
            return idx + 1
    return 1


def _member_manifests(project_root: Path) -> list[Path]:
    root_manifest = project_root / "Cargo.toml"
    data = _load_toml(root_manifest)
    workspace = _toml_table(data, "workspace")
    if not workspace:
        return []
    manifests: list[Path] = []
    if "package" in data:
        manifests.append(root_manifest)
    members = workspace.get("members")
    if isinstance(members, list):
        for member in cast(list[object], members):
            if not isinstance(member, str):
                continue
            if "*" in member:
                for path in sorted(project_root.glob(member)):
                    if (path / "Cargo.toml").exists():
                        manifests.append(path / "Cargo.toml")
            elif (project_root / member / "Cargo.toml").exists():
                manifests.append(project_root / member / "Cargo.toml")
    return manifests


_DEP_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")


def gen_workspace_deps(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for manifest in _member_manifests(project_root):
        data = _load_toml(manifest)
        rel = manifest.relative_to(project_root).as_posix()
        for section in _DEP_SECTIONS:
            for dep_name, dep_value in _toml_table(data, section).items():
                if isinstance(dep_value, dict):
                    dep_table = cast(dict[str, object], cast(object, dep_value))
                    pinned = "version" in dep_table and dep_table.get("workspace") is not True
                else:
                    pinned = isinstance(dep_value, str)
                if pinned:
                    candidates.append(
                        Candidate(
                            file=rel,
                            line=_dep_line(manifest, dep_name),
                            text=f"[{section}] {dep_name} pins a version instead of workspace = true",
                        )
                    )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(candidates=tuple(candidates), source="toml:member-deps-version-pins")


def gen_bevy_kana_usage(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    manifests = [project_root / "Cargo.toml"]
    manifests += [m for m in _member_manifests(project_root) if m not in manifests]
    kana_crates: list[Path] = []
    for manifest in manifests:
        data = _load_toml(manifest)
        deps = _toml_table(data, "dependencies")
        if "bevy" in deps and "bevy_kana" not in deps:
            rel = manifest.relative_to(project_root).as_posix()
            candidates.append(
                Candidate(
                    file=rel,
                    line=_dep_line(manifest, "bevy"),
                    text="bevy crate without a bevy_kana dependency",
                )
            )
        if "bevy_kana" in deps:
            kana_crates.append(manifest.parent)
    leak_re = re.compile(
        r"^\s*pub\s.*\b(Position|Displacement|Velocity|ToF32|ToI32|ToU32|ToUsize)\b"
    )
    for crate_dir in kana_crates:
        if not (crate_dir / "src" / "lib.rs").exists():
            continue
        for path in rust_files(crate_dir / "src"):
            rel = path.relative_to(project_root).as_posix()
            masked = read_masked(path)
            for idx, line in enumerate(masked.lines):
                if leak_re.match(line) or re.match(r"^\s*pub\s+use\s+.*bevy_kana", line):
                    candidates.append(
                        Candidate(file=rel, line=idx + 1, text=display_line(path, idx + 1))
                    )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(
        candidates=tuple(candidates),
        source="toml:bevy-dep-without-kana + regex:pub-surface-kana-types",
    )


# ---------------------------------------------------------------------------
# test_allow_boilerplate — stale allow lints in scopes with zero matches
# ---------------------------------------------------------------------------

_ALLOW_RE = re.compile(r"#\[allow\(([^)]*?)\)\s*\]", re.S)
_BOILERPLATE_LINTS = {
    "clippy::expect_used": ".expect(",
    "clippy::unwrap_used": ".unwrap(",
    "clippy::panic": "panic!",
}


def _find_brace_after(lines: list[str], start_line: int, start_col: int) -> tuple[int, int] | None:
    """First `{` at/after the position, stopping only at `;` (parens are fine —
    the scope owner is usually a fn signature or a mod header)."""
    col = start_col
    for idx in range(start_line, len(lines)):
        segment = lines[idx][col:]
        for k, ch in enumerate(segment):
            if ch == "{":
                return idx, col + k
            if ch == ";":
                return None
        col = 0
    return None


def gen_allows_without_reason(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    """`#[allow(...)]` attributes (single- or multi-line) with no `reason` field."""
    _ = spec
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        masked_text = "\n".join(masked.lines)
        for match in _ALLOW_RE.finditer(masked_text):
            if re.search(r"\breason\s*=", match.group(1)):
                continue
            attr_line = masked_text.count("\n", 0, match.start()) + 1
            candidates.append(
                Candidate(file=rel, line=attr_line, text=display_line(path, attr_line))
            )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(candidates=tuple(candidates), source="parse:allow-attrs-without-reason")


def gen_test_allow_boilerplate(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        masked_text = "\n".join(masked.lines)
        for match in _ALLOW_RE.finditer(masked_text):
            lints = [part.strip() for part in match.group(1).replace("\n", " ").split(",")]
            tracked = [lint for lint in lints if lint in _BOILERPLATE_LINTS]
            if not tracked:
                continue
            attr_line = masked_text.count("\n", 0, match.start()) + 1
            attr_end_line = masked_text.count("\n", 0, match.end()) + 1
            attr_end_col = match.end() - (masked_text.rfind("\n", 0, match.end()) + 1)
            opened = _find_brace_after(masked.lines, attr_end_line - 1, attr_end_col)
            if opened is None:
                scope_text = masked_text[match.end() :]
            else:
                open_line, open_col = opened
                end_line = open_line
                depth = 0
                for j in range(open_line, len(masked.lines)):
                    segment = masked.lines[j][open_col if j == open_line else 0 :]
                    done = False
                    for ch in segment:
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                end_line = j
                                done = True
                                break
                    if done:
                        break
                else:
                    end_line = len(masked.lines) - 1
                scope_text = "\n".join(masked.lines[open_line : end_line + 1])
            for lint in tracked:
                if _BOILERPLATE_LINTS[lint] not in scope_text:
                    candidates.append(
                        Candidate(
                            file=rel,
                            line=attr_line,
                            text=f"strip {lint} — 0 matches of `{_BOILERPLATE_LINTS[lint]}` in scope",
                        )
                    )
    candidates.sort(key=lambda c: (c.file, c.line, c.text))
    return Enumeration(candidates=tuple(candidates), source="parse:allow-lints-vs-scope-matches")


# ---------------------------------------------------------------------------
# Struct/enum naming generators
# ---------------------------------------------------------------------------

# Unit suffixes are information-bearing on numeric fields per the rule's own
# exception — excluding them mechanically cannot suppress a real violation.
_UNIT_SUFFIXES = {
    "ms", "millis", "micros", "nanos", "secs", "seconds", "bytes", "px",
    "deg", "degrees", "rad", "radians", "hz",
}

_TYPE_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _type_idents(type_text: str) -> list[str]:
    return _TYPE_IDENT_RE.findall(type_text)


def _each_struct(project_root: Path) -> list[tuple[str, Path, StructDef]]:
    found: list[tuple[str, Path, StructDef]] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        for struct in scan_structs(masked.lines):
            found.append((rel, path, struct))
    return found


def gen_field_affixes(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for rel, _path, struct in _each_struct(project_root):
        if len(struct.fields) < 2:
            continue
        by_prefix: dict[str, list[str]] = {}
        by_suffix: dict[str, list[str]] = {}
        for fld in struct.fields:
            words = [word for word in fld.name.split("_") if word]
            if len(words) < 2:
                continue
            by_prefix.setdefault(words[0], []).append(fld.name)
            if words[-1] not in _UNIT_SUFFIXES:
                by_suffix.setdefault(words[-1], []).append(fld.name)
        # The rule fires when sibling fields ALL repeat the affix — a group
        # covering only part of the struct is not a violation per the rule text.
        for kind, groups in (("prefix", by_prefix), ("suffix", by_suffix)):
            for word, names in sorted(groups.items()):
                if len(names) >= 2 and len(names) == len(struct.fields):
                    candidates.append(
                        Candidate(
                            file=rel,
                            line=struct.line,
                            text=f"struct {struct.name}: shared {kind} '{word}' on {', '.join(names)}",
                        )
                    )
    candidates.sort(key=lambda c: (c.file, c.line, c.text))
    return Enumeration(candidates=tuple(candidates), source="parse:struct-fields-shared-affix")


def gen_enum_variant_stutter(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        for enum in scan_enums(masked.lines):
            enum_words = set(camel_words(enum.name))
            for variant_name, variant_line in enum.variants:
                shared = enum_words & set(camel_words(variant_name))
                if shared:
                    candidates.append(
                        Candidate(
                            file=rel,
                            line=variant_line,
                            text=f"enum {enum.name}::{variant_name} repeats '{sorted(shared)[0]}'",
                        )
                    )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(candidates=tuple(candidates), source="parse:enum-variant-shares-enum-word")


def gen_field_type_stutter(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for rel, _path, struct in _each_struct(project_root):
        struct_snake = snake_case(struct.name)
        first_word = camel_words(struct.name)[0] if camel_words(struct.name) else ""
        for fld in struct.fields:
            hits = fld.name == struct_snake or fld.name.startswith(struct_snake + "_")
            if not hits and first_word:
                hits = fld.name == first_word or fld.name.startswith(first_word + "_")
            if not hits:
                continue
            # Type-named fields win per the rule's own cross-reference.
            if any(snake_case(ident) == fld.name for ident in _type_idents(fld.type_text)):
                continue
            candidates.append(
                Candidate(
                    file=rel,
                    line=fld.line,
                    text=f"struct {struct.name}: field `{fld.name}` restates the struct name",
                )
            )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(candidates=tuple(candidates), source="parse:field-prefixed-with-struct-name")


# ---------------------------------------------------------------------------
# Trait / module-shape generators
# ---------------------------------------------------------------------------

_TRAIT_RE = re.compile(rf"^\s*{_VIS_RE}(?:unsafe\s+)?trait\s+([A-Z]\w*)")


def gen_single_impl_traits(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    decls: list[tuple[str, str, int]] = []  # (trait name, rel file, line)
    masked_texts: list[str] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        masked_texts.append("\n".join(masked.lines))
        for idx, line in enumerate(masked.lines):
            match = _TRAIT_RE.match(line)
            if match is not None:
                decls.append((match.group(1), rel, idx + 1))
    candidates: list[Candidate] = []
    for trait_name, rel, line in decls:
        impl_re = re.compile(rf"\bimpl\b[^{{;]*?\b{trait_name}\b[^{{;]*?\bfor\b", re.S)
        impl_count = sum(len(impl_re.findall(text)) for text in masked_texts)
        if impl_count <= 1:
            candidates.append(
                Candidate(
                    file=rel,
                    line=line,
                    text=f"trait {trait_name}: {impl_count} impl(s) project-wide",
                )
            )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(candidates=tuple(candidates), source="parse:trait-decls-with-impl-count<=1")


_FN_RE = re.compile(r"\bfn\s+(\w+)\s*(?:<[^>]*>)?\s*\(", re.S)


def gen_observer_guards(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        masked_text = "\n".join(masked.lines)
        for match in _FN_RE.finditer(masked_text):
            params_end = masked_text.find(")", match.end())
            if params_end == -1:
                continue
            params = masked_text[match.end() : params_end + 1]
            if "On<" not in params and "Trigger<" not in params:
                continue
            fn_line = masked_text.count("\n", 0, match.start()) + 1
            body_open = masked_text.find("{", params_end)
            if body_open == -1:
                continue
            body_start_line = masked_text.count("\n", 0, body_open) + 1
            window = masked.lines[body_start_line - 1 : body_start_line + 5]
            if any("return" in line for line in window):
                candidates.append(
                    Candidate(
                        file=rel,
                        line=fn_line,
                        text=f"observer fn {match.group(1)} opens with an early return",
                    )
                )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(candidates=tuple(candidates), source="parse:observer-fn-early-return")


_ITEM_RE = re.compile(
    rf"^\s*{_VIS_RE}(?:unsafe\s+)?(fn|struct|enum|trait|impl|const|static|type|union|macro_rules!)\b\s*(\w*)"
)


def gen_module_root_items(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        if path.name not in ("mod.rs", "lib.rs", "main.rs"):
            continue
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        depth = 0
        items: list[tuple[int, str]] = []
        for idx, line in enumerate(masked.lines):
            if depth == 0:
                match = _ITEM_RE.match(line)
                if match is not None:
                    kind, name = match.group(1), match.group(2)
                    # fn main in main.rs is structurally required, not a violation.
                    if not (path.name == "main.rs" and kind == "fn" and name == "main"):
                        items.append((idx + 1, f"{kind} {name}".strip()))
            for ch in line:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
        if items:
            preview = ", ".join(label for _line, label in items[:6])
            if len(items) > 6:
                preview += f", … (+{len(items) - 6})"
            candidates.append(
                Candidate(
                    file=rel,
                    line=items[0][0],
                    text=f"{len(items)} top-level item(s) beyond mod/use: {preview}",
                )
            )
    candidates.sort(key=lambda c: c.file)
    return Enumeration(candidates=tuple(candidates), source="parse:module-root-top-level-items")


_ANTI_PATTERN_STEMS = {"helpers", "utils", "util", "common", "misc", "types", "model"}


def gen_submodule_names(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    _ = spec
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        stem = path.stem
        parent = path.parent.name
        if stem in _ANTI_PATTERN_STEMS:
            candidates.append(
                Candidate(file=rel, line=1, text=f"anti-pattern module name `{stem}.rs`")
            )
        elif stem == "mod" and parent in _ANTI_PATTERN_STEMS:
            candidates.append(
                Candidate(file=rel, line=1, text=f"anti-pattern module directory `{parent}/`")
            )
        elif (
            stem not in ("mod", "lib", "main")
            and parent not in ("src", "")
            and stem.startswith(parent + "_")
        ):
            candidates.append(
                Candidate(
                    file=rel,
                    line=1,
                    text=f"`{parent}/{stem}.rs` stutters the parent module name",
                )
            )
    candidates.sort(key=lambda c: c.file)
    return Enumeration(candidates=tuple(candidates), source="parse:module-filename-anti-patterns")


# ---------------------------------------------------------------------------
# Literal generators (no-magic-values, derive-test-values)
# ---------------------------------------------------------------------------

_NUMERIC_RE = re.compile(r"(?<![\w.])(\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)")
_CONST_LINE_RE = re.compile(rf"^\s*{_VIS_RE}const\s")
_FORMAT_MACRO_RE = re.compile(
    r"\b(?:format|print|println|eprint|eprintln|write|writeln|panic|assert|assert_eq|assert_ne"
    + r"|debug_assert|debug_assert_eq|debug_assert_ne|todo|unimplemented|unreachable|matches|vec)!\s*\(?"
)
_INCLUDE_MACRO_RE = re.compile(r"\b(?:include_str|include_bytes|embedded_asset|env|concat)!\s*\(")
# Fixed Rust/tooling spellings the rule itself leaves inline.
_FIXED_SPELLINGS = {"Cargo.toml", "Cargo.lock", "mod.rs", "lib.rs", "main.rs", "crate", "super", "self", "src"}
_IDENTITY_NUMERICS = {"0", "1", "0.0", "1.0"}


def _significant_numeric(token: str) -> bool:
    digits = token.replace("_", "")
    if digits in _IDENTITY_NUMERICS:
        return False
    return sum(ch.isdigit() for ch in digits) >= 2


def _array_dim_position(line: str, start: int) -> bool:
    before = line[:start]
    after = line[start:]
    return re.search(r";\s*$", before) is not None and re.match(r"[\d_]*\s*\]", after) is not None


def _numeric_candidates_for_file(
    path: Path,
    rel: str,
    masked: MaskedSource,
    spans: list[tuple[int, int]],
    inside_spans: bool,
    line_filter: Callable[[str], bool] | None = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen_lines: set[int] = set()
    for idx, line in enumerate(masked.lines):
        line_no = idx + 1
        if in_spans(line_no, spans) != inside_spans:
            continue
        if _CONST_LINE_RE.match(line) or line.lstrip().startswith("#"):
            continue
        if line_filter is not None and not line_filter(line):
            continue
        for match in _NUMERIC_RE.finditer(line):
            if not _significant_numeric(match.group(1)):
                continue
            if _array_dim_position(line, match.start(1)):
                continue
            if line_no not in seen_lines:
                seen_lines.add(line_no)
                candidates.append(Candidate(file=rel, line=line_no, text=display_line(path, line_no)))
            break
    return candidates


def gen_literals(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    exempts = tuple(spec.paths_exempt) + ("examples", "benches", "build.rs")
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        if path_is_exempt(rel, exempts) or is_test_file(rel):
            continue
        masked = read_masked(path)
        spans = test_spans(masked.lines)
        candidates += _numeric_candidates_for_file(path, rel, masked, spans, inside_spans=False)
        for literal in masked.strings:
            if literal.kind != "string" or len(literal.text) < 2:
                continue
            if in_spans(literal.line, spans):
                continue
            line = masked.lines[literal.line - 1] if literal.line - 1 < len(masked.lines) else ""
            raw_line = display_line(path, literal.line)
            if _CONST_LINE_RE.match(line) or line.lstrip().startswith("#"):
                continue
            prefix = line[: literal.col]
            if _FORMAT_MACRO_RE.search(prefix) or _INCLUDE_MACRO_RE.search(prefix):
                continue
            if literal.text in _FIXED_SPELLINGS:
                continue
            candidates.append(Candidate(file=rel, line=literal.line, text=raw_line))
    deduped = sorted(set(candidates), key=lambda c: (c.file, c.line))
    return Enumeration(
        candidates=tuple(deduped),
        source="parse:literals(numeric 2+ digits, strings len>=2; const/test/format/attr/fixed-spelling excluded)",
    )


_COMPARISON_LINE_RE = re.compile(r"\bassert|[=!<>]=|matches!")


def gen_test_literals(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    """Hardcoded numerics in test assertions/comparisons — the sites where a
    value derived from a production constant breaks silently when it drifts."""
    _ = spec
    candidates: list[Candidate] = []
    for path in rust_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        masked = read_masked(path)
        if is_test_file(rel):
            spans = [(1, len(masked.lines))]
        else:
            spans = test_spans(masked.lines)
        if not spans:
            continue
        candidates += _numeric_candidates_for_file(
            path,
            rel,
            masked,
            spans,
            inside_spans=True,
            line_filter=lambda line: _COMPARISON_LINE_RE.search(line) is not None,
        )
    candidates.sort(key=lambda c: (c.file, c.line))
    return Enumeration(
        candidates=tuple(candidates),
        source="parse:test-span-numerics(2+ digits, assert/comparison lines, const/attr lines excluded)",
    )


# ---------------------------------------------------------------------------
# struct_fields — parameterized field matcher (project-local rules)
# ---------------------------------------------------------------------------


def gen_struct_fields(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    if not spec.field_type_pattern:
        raise SystemExit(f"candidates kind=struct_fields in {spec.origin} needs field_type_pattern")
    type_re = re.compile(spec.field_type_pattern)
    wanted = set(spec.structs)
    candidates: list[Candidate] = []
    for rel, _path, struct in _each_struct(project_root):
        if wanted and struct.name not in wanted:
            continue
        for fld in struct.fields:
            if type_re.search(fld.type_text):
                candidates.append(
                    Candidate(
                        file=rel,
                        line=fld.line,
                        text=f"struct {struct.name}: field `{fld.name}: {fld.type_text}`",
                    )
                )
    candidates.sort(key=lambda c: (c.file, c.line))
    scope = ",".join(sorted(wanted)) if wanted else "*"
    return Enumeration(
        candidates=tuple(candidates),
        source=f"parse:struct-fields(structs={scope}, type~{spec.field_type_pattern})",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

GENERATORS: dict[str, Callable[[CandidatesSpec, Path], Enumeration]] = {
    "regex": gen_regex,
    "workspace_deps": gen_workspace_deps,
    "allows_without_reason": gen_allows_without_reason,
    "test_allow_boilerplate": gen_test_allow_boilerplate,
    "field_affixes": gen_field_affixes,
    "enum_variant_stutter": gen_enum_variant_stutter,
    "field_type_stutter": gen_field_type_stutter,
    "single_impl_traits": gen_single_impl_traits,
    "observer_guards": gen_observer_guards,
    "module_root_items": gen_module_root_items,
    "submodule_names": gen_submodule_names,
    "bevy_kana_usage": gen_bevy_kana_usage,
    "test_literals": gen_test_literals,
    "literals": gen_literals,
    "struct_fields": gen_struct_fields,
}


def enumerate_candidates(spec: CandidatesSpec, project_root: Path) -> Enumeration:
    """Run the generator named by the spec. Unknown kinds are loud config errors."""
    generator = GENERATORS.get(spec.kind)
    if generator is None:
        raise SystemExit(
            f"candidates kind '{spec.kind}' in {spec.origin} has no generator in candidate_generators.py"
        )
    enumeration = generator(spec, project_root)
    if len(enumeration.candidates) > MAX_CANDIDATES:
        raise SystemExit(
            f"candidates generator '{spec.kind}' for {spec.origin} produced"
            + f" {len(enumeration.candidates)} candidates (cap {MAX_CANDIDATES});"
            + " tune the spec's excludes before this unit can be evaluated"
        )
    return enumeration
