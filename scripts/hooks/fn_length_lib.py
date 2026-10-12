"""Measure Rust function bodies using clippy's too_many_lines rules."""

from __future__ import annotations

import bisect
import os
import re
from enum import Enum


class FunctionLength:
    name: str
    line: int
    lines: int
    exempt: bool
    __slots__: tuple[str, ...] = ("name", "line", "lines", "exempt")

    def __init__(self, name: str, line: int, lines: int, exempt: bool) -> None:
        self.name = name
        self.line = line
        self.lines = lines
        self.exempt = exempt


class TooManyLinesLintScope:
    enabled: bool
    threshold: int
    __slots__: tuple[str, ...] = ("enabled", "threshold")

    def __init__(self, enabled: bool, threshold: int) -> None:
        self.enabled = enabled
        self.threshold = threshold


class RustToken:
    text: str
    start: int
    end: int

    __slots__: tuple[str, str, str] = ("text", "start", "end")

    def __init__(self, text: str, start: int, end: int) -> None:
        self.text = text
        self.start = start
        self.end = end


_TOKENS = re.compile(
    r'//[^\n]*|/\*|"(?:\\.|[^"\\])*"|["\']|#!?\[|'
    + r'[{}()\[\];!]|\b(?:fn|impl|trait|mod)\b'
)
_CHAR = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|.)|[^'\\])'")
_NAME_START = re.compile(r"\s*(r#)?([^\W\d]\w*)", re.UNICODE)
_EXEMPT_LINT = re.compile(r"clippy::(?:too_many_lines|pedantic)\b")
_ATTRIBUTE_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_OPEN = {"{": "}", "(": ")", "[": "]", "#[": "]", "#![": "]"}
_CLOSE = frozenset("})]")
_RUST_KEYWORDS = frozenset(
    ("as async await break const continue crate dyn else enum extern false fn for if impl in "
     + "let loop match mod move mut pub ref return self Self static struct super trait true "
     + "type unsafe use where while abstract become box do final gen macro override priv try "
     + "typeof union unsized virtual yield").split()
)


type Replacement = tuple[str, str]  # the (old, new) text of one applied replacement


class AppliedRustEdits:
    """Rust files left by one successful edit, in payload order."""

    __slots__: tuple[str, ...] = ("agent", "tool", "cwd", "files", "replacements")
    agent: str
    tool: str
    cwd: str
    files: tuple[str, ...]
    replacements: tuple[tuple[Replacement, ...], ...]

    def __init__(
        self, agent: str, tool: str, cwd: str, files: tuple[str, ...],
        replacements: tuple[tuple[Replacement, ...], ...],
    ) -> None:
        self.agent = agent
        self.tool = tool
        self.cwd = cwd
        self.files = files
        self.replacements = replacements


class IgnoredEdit:
    """A payload with no Rust file whose final contents can be checked."""

    __slots__: tuple[str, ...] = ()


def _patch_update_replacements(lines: list[str]) -> tuple[Replacement, ...]:
    pairs: list[Replacement] = []
    old: list[str] = []
    new: list[str] = []
    for line in [*lines, "*** End Patch"]:
        if not line or line[0] in " -+":
            text = line[1:] if line else ""
            if not line or line[0] in " -":
                old.append(text)
            if not line or line[0] in " +":
                new.append(text)
        elif old or new:
            pairs.append(("\n".join(old), "\n".join(new)))
            old.clear()
            new.clear()
    return tuple(pairs)


def _patch_file_replacements(patch: str) -> dict[str, tuple[Replacement, ...]]:
    sections: list[tuple[str, bool, list[str]]] = []
    section_index = -1
    update_index = -1
    for line in patch.replace("\r\n", "\n").removesuffix("\n").split("\n"):
        if line.startswith("*** Add File: ") or line.startswith("*** Update File: "):
            added = line.startswith("*** Add File: ")
            sections.append((line.split(": ", 1)[1], added, []))
            section_index = len(sections) - 1
            update_index = -1 if added else section_index
        elif line.startswith("*** Move to: ") and update_index >= 0:
            _, added, lines = sections[update_index]
            lines.append(line)
            sections[update_index] = (line.removeprefix("*** Move to: "), added, lines)
            update_index = -1
        elif line.startswith("*** Delete File: "):
            section_index = -1
            update_index = -1
        elif section_index >= 0:
            sections[section_index][2].append(line)
    replacements: dict[str, tuple[Replacement, ...]] = {}
    for path, added, lines in sections:
        if not path.endswith(".rs"):
            continue
        pairs = (("", "\n".join(line[1:] for line in lines if line.startswith("+"))),) if added else (
            _patch_update_replacements(lines)
        )
        replacements[path] = replacements.get(path, ()) + pairs
    return replacements


def applied_patch_rust_files(patch: str) -> tuple[str, ...]:
    return tuple(_patch_file_replacements(patch))


def applied_rust_edits(raw: str) -> AppliedRustEdits | IgnoredEdit:
    import json
    from typing import cast

    data = cast(object, json.loads(raw))
    if not isinstance(data, dict):
        return IgnoredEdit()
    fields = cast(dict[str, object], data)
    tool = fields.get("tool_name")
    cwd = fields.get("cwd")
    tool_input = fields.get("tool_input")
    if not isinstance(tool, str) or not isinstance(cwd, str) or not isinstance(tool_input, dict):
        return IgnoredEdit()
    inputs = cast(dict[str, object], tool_input)
    if tool == "apply_patch":
        patch = inputs.get("command")
        replacements = _patch_file_replacements(patch) if isinstance(patch, str) else {}
        return AppliedRustEdits(
            "codex", tool, cwd, tuple(replacements), tuple(replacements.values()),
        ) if replacements else IgnoredEdit()
    file_path = inputs.get("file_path")
    if tool in {"Edit", "MultiEdit", "Write"} and isinstance(file_path, str) and file_path.endswith(".rs"):
        pairs: list[Replacement] = []
        if tool == "Write":
            content = inputs.get("content")
            if isinstance(content, str):
                pairs.append(("", content))
        else:
            entries = inputs.get("edits") if tool == "MultiEdit" else [inputs]
            if isinstance(entries, list):
                for entry in cast(list[object], entries):
                    if not isinstance(entry, dict):
                        continue
                    fields = cast(dict[str, object], entry)
                    old, new = fields.get("old_string"), fields.get("new_string")
                    if isinstance(old, str) and isinstance(new, str):
                        pairs.append((old, new))
        return AppliedRustEdits("claude", tool, cwd, (file_path,), (tuple(pairs),))
    return IgnoredEdit()


def written_lines(source: str, replacements: tuple[Replacement, ...]) -> frozenset[int]:
    """1-based numbers of the lines of source that the edit wrote."""
    written: set[int] = set()
    for old, new in replacements:
        old_lines, new_lines = old.split("\n"), new.split("\n")
        prefix = 0
        limit = min(len(old_lines), len(new_lines))
        while prefix < limit and old_lines[prefix] == new_lines[prefix]:
            prefix += 1
        suffix = 0
        while suffix < limit - prefix and old_lines[-suffix - 1] == new_lines[-suffix - 1]:
            suffix += 1
        changed_offsets = tuple(
            offset for offset in range(prefix, len(new_lines) - suffix) if new_lines[offset]
        )
        if not changed_offsets:
            continue
        at = source.find(new)
        if at < 0:
            added_lines = {line.strip() for line in new_lines} - {line.strip() for line in old_lines}
            written.update(
                line_number for line_number, line in enumerate(source.split("\n"), 1)
                if line.strip() and line.strip() in added_lines
            )
            continue
        line_number = 1
        previous_at = 0
        while at >= 0:
            line_number += source.count("\n", previous_at, at)
            written.update(line_number + offset for offset in changed_offsets)
            previous_at = at
            at = source.find(new, at + len(new))
    return frozenset(written)


def count_body_lines(body: str) -> int:
    """Count body lines exactly as clippy does, including its comment quirks."""
    trimmed = body.strip()
    if not trimmed:
        return 0
    if "/*" not in trimmed and "//" not in trimmed:
        return sum(bool(line.strip()) for line in trimmed.split("\n"))
    count = 0
    in_comment = False
    for raw_line in trimmed.split("\n"):
        line = raw_line.removesuffix("\r").lstrip()
        code_in_line = False
        while line:
            if in_comment:
                end = line.find("*/")
                if end < 0:
                    break
                line = line[end + 2 :].lstrip()
                in_comment = False
                continue
            block = line.find("/*")
            slash = line.find("//")
            block_at = len(line) if block < 0 else block
            slash_at = len(line) if slash < 0 else slash
            code_in_line |= block_at > 0 and slash_at > 0
            if block_at < slash_at:
                line = line[block_at + 2 :]
                in_comment = True
                continue
            break
        count += code_in_line
    return count


def tokenize_rust(source: str) -> tuple[list[RustToken], dict[int, int]]:
    found: list[RustToken] = []
    comments: dict[int, int] = {}
    position = 0
    matches = iter(_TOKENS.finditer(source))
    while (match := next(matches, None)) is not None:
        start = match.start()
        if start < position:
            matches = iter(_TOKENS.finditer(source, position))
            continue
        opening = match.group()
        end = match.end()
        first = opening[0]
        if first == "/" and opening.startswith("//"):
            comments[end] = start
            position = end
            continue
        if first == "/" and opening == "/*":
            depth = 1
            while depth and end < len(source):
                next_open = source.find("/*", end)
                next_close = source.find("*/", end)
                if next_close < 0:
                    end = len(source)
                    break
                if 0 <= next_open < next_close:
                    depth += 1
                    end = next_open + 2
                else:
                    depth -= 1
                    end = next_close + 2
            position = end
            comments[end] = start
            continue
        if first == '"':
            prefix = start
            while prefix > 0 and source[prefix - 1] == "#":
                prefix -= 1
            if prefix > 0 and source[prefix - 1] == "r":
                suffix = '"' + "#" * (start - prefix)
                close = source.find(suffix, start + 1)
                position = len(source) if close < 0 else close + len(suffix)
            else:
                if opening == '"':
                    while end < len(source):
                        if source[end] == "\\":
                            end += 2
                        elif source[end] == '"':
                            end += 1
                            break
                        else:
                            end += 1
                position = end
            continue
        if first == "'":
            char = _CHAR.match(source, start)
            position = char.end() if char else end
            continue
        found.append(RustToken(opening, start, end))
        position = end
    return found, comments


def pair_delimiters(tokens: list[RustToken]) -> dict[int, int]:
    pairs: dict[int, int] = {}
    stack: list[tuple[int, str]] = []
    for index, token in enumerate(tokens):
        if closing := _OPEN.get(token.text):
            stack.append((index, closing))
        elif token.text in _CLOSE and stack and stack[-1][1] == token.text:
            opening, _ = stack.pop()
            pairs[opening] = index
    return pairs


def _is_exempt(attribute: str) -> bool:
    compact = re.sub(r"\s+", "", _ATTRIBUTE_STRING.sub('""', attribute))
    if compact.startswith("#!["):
        compact = compact[3:-1]
    elif compact.startswith("#["):
        compact = compact[2:-1]
    else:
        return False
    if not _EXEMPT_LINT.search(compact):
        return False
    return bool(re.search(r"(?:^|,)\s*(?:allow|expect)\(", compact)) or (
        compact.startswith("cfg_attr(") and bool(re.search(r"(?:allow|expect)\(", compact))
    )


def body_opener(tokens: list[RustToken], pairs: dict[int, int], start: int) -> int:
    index = start
    while index < len(tokens):
        token = tokens[index].text
        if token in {"(", "["}:
            if index not in pairs:
                return -1
            index = pairs[index] + 1
        elif token == "{":
            return index
        elif token in {";", "}"}:
            return -1
        else:
            index += 1
    return -1


def _function_name(segment: str) -> str:
    if match := _NAME_START.match(segment):
        return _written_name(segment, match)
    # Rust permits comments between `fn` and the name. This slower path is
    # needed only for that uncommon spelling.
    plain: list[str] = []
    position = 0
    while position < len(segment):
        if segment.startswith("/*", position):
            depth = 1
            position += 2
            while depth and position < len(segment):
                if segment.startswith("/*", position):
                    depth += 1
                    position += 2
                elif segment.startswith("*/", position):
                    depth -= 1
                    position += 2
                else:
                    position += 1
        elif segment.startswith("//", position):
            newline = segment.find("\n", position)
            position = len(segment) if newline < 0 else newline + 1
        else:
            plain.append(segment[position])
            position += 1
    match = _NAME_START.match("".join(plain))
    return _written_name("".join(plain), match) if match else ""


def _written_name(segment: str, match: re.Match[str]) -> str:
    name = match.group(2)
    index = match.end()
    while index < len(segment) and (name + segment[index]).isidentifier():
        name += segment[index]
        index += 1
    if not name.isidentifier():
        return ""
    return f"r#{name}" if match.group(1) else name


def macro_before(source: str, bang: int, comments: dict[int, int]) -> str:
    if source[bang + 1 : bang + 2] == "=":
        return ""
    end = bang
    while end:
        while end and source[end - 1].isspace():
            end -= 1
        if end in comments:
            end = comments[end]
            continue
        break
    start = end
    while start and (source[start - 1].isalnum() or source[start - 1] == "_"):
        start -= 1
    name = source[start:end]
    return name if name.isidentifier() and name not in _RUST_KEYWORDS else ""


def measure_functions(source: str, *, skip_example_tests: bool = False) -> list[FunctionLength]:
    tokens, comments = tokenize_rust(source)
    pairs = pair_delimiters(tokens)
    newlines = [match.start() for match in re.finditer("\n", source)]
    inherited = [False]
    pending_exempt = False
    pending_test = False
    block_exemption: dict[int, bool] = {}
    results: list[FunctionLength] = []
    signature_end = -1
    index = 0
    while index < len(tokens):
        token = tokens[index]
        word = token.text
        if word in {"#[", "#!["}:
            close = pairs.get(index)
            if close is not None:
                exempt = _is_exempt(source[token.start : tokens[close].end])
                if word == "#![":
                    inherited[-1] |= exempt
                else:
                    pending_exempt |= exempt
                    pending_test |= skip_example_tests and re.fullmatch(
                        r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]",
                        source[token.start : tokens[close].end],
                    ) is not None
                index = close + 1
                continue
        if word == "!" and macro_before(source, token.start, comments):
            opener = index + 1
            while opener < len(tokens) and tokens[opener].text not in _OPEN:
                if tokens[opener].text in {";", "}"}:
                    break
                opener += 1
            if opener in pairs and tokens[opener].text in _OPEN:
                index = pairs[opener] + 1
                continue
        if word in {"fn", "impl", "trait", "mod"} and index >= signature_end:
            opener = body_opener(tokens, pairs, index + 1)
            if word == "fn" and index + 1 < len(tokens):
                name = _function_name(source[token.end : tokens[index + 1].start])
                if name and opener in pairs:
                    body = source[tokens[opener].end : tokens[pairs[opener]].start]
                    results.append(
                        FunctionLength(
                            name,
                            bisect.bisect_left(newlines, token.start) + 1,
                            count_body_lines(body),
                            inherited[-1] or pending_exempt,
                        )
                    )
            if opener >= 0:
                block_exemption[opener] = pending_exempt or (word == "mod" and pending_test)
                signature_end = opener
            pending_exempt = False
            pending_test = False
        if word == "{":
            inherited.append(inherited[-1] or block_exemption.get(index, False))
            pending_exempt = False
            pending_test = False
        elif word == "}":
            if len(inherited) > 1:
                _ = inherited.pop()
            pending_exempt = False
            pending_test = False
        elif word == ";":
            pending_exempt = False
            pending_test = False
        index += 1
    return results


def read_toml(source: str) -> dict[str, object]:
    import tomllib
    from typing import cast

    return cast(dict[str, object], tomllib.loads(source))


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as source:
        return source.read()


def ancestor_directories(directory: str) -> list[str]:
    parents: list[str] = []
    while True:
        parents.append(directory)
        parent = os.path.dirname(directory)
        if parent == directory:
            return parents
        directory = parent


def toml_table(value: object) -> dict[str, object]:
    from typing import cast

    if not isinstance(value, dict):
        return {}
    return cast(dict[str, object], value)


class ClippyLintLevel(Enum):
    ALLOW = "allow"
    EXPECT = "expect"
    WARN = "warn"
    DENY = "deny"
    FORBID = "forbid"
    UNSET = "unset"


class ConfiguredClippyLint:
    """The named lint's effective level in a discovered package."""

    __slots__: tuple[str, ...] = ("level", "package_dir")
    level: ClippyLintLevel
    package_dir: str

    def __init__(self, level: ClippyLintLevel, package_dir: str) -> None:
        self.level = level
        self.package_dir = package_dir


class UnavailableClippyLint(Enum):
    NO_PACKAGE = "no_package"
    UNREADABLE = "unreadable"


def _lint_level(value: object) -> ClippyLintLevel:
    level = value if isinstance(value, str) else toml_table(value).get("level")
    if isinstance(level, str):
        return next((named for named in ClippyLintLevel if named.value == level), ClippyLintLevel.UNSET)
    return ClippyLintLevel.UNSET


def read_clippy_lint(
    rs_file: os.PathLike[str] | str, lint: str, group: str
) -> ConfiguredClippyLint | UnavailableClippyLint:
    """Read a package's or inherited workspace's named Clippy lint."""
    try:
        for parent in ancestor_directories(os.path.dirname(os.path.abspath(rs_file))):
            manifest = os.path.join(parent, "Cargo.toml")
            if not os.path.isfile(manifest):
                continue
            raw = read_text(manifest)
            if "package" not in raw:
                continue
            if "lints" not in raw:
                return ConfiguredClippyLint(ClippyLintLevel.UNSET, parent)
            data = read_toml(raw)
            if "package" in data:
                package_dir = parent
                package = data
                break
        else:
            return UnavailableClippyLint.NO_PACKAGE
        lints = toml_table(package.get("lints"))
        if lints.get("workspace") is True:
            package_table = toml_table(package.get("package"))
            workspace_path = package_table.get("workspace")
            if isinstance(workspace_path, str):
                workspace = read_toml(read_text(
                    os.path.realpath(os.path.join(package_dir, workspace_path, "Cargo.toml"))
                ))
            else:
                workspace = {}
                for parent in ancestor_directories(package_dir):
                    manifest = os.path.join(parent, "Cargo.toml")
                    if os.path.isfile(manifest):
                        candidate = read_toml(read_text(manifest))
                        if "workspace" in candidate:
                            workspace = candidate
                            break
            clippy = toml_table(toml_table(toml_table(workspace.get("workspace")).get("lints")).get("clippy"))
        else:
            clippy = toml_table(lints.get("clippy"))
        value = clippy[lint] if lint in clippy else clippy.get(group)
        return ConfiguredClippyLint(_lint_level(value), package_dir)
    except (OSError, ValueError, UnicodeError):
        return UnavailableClippyLint.UNREADABLE


def lint_scope(rs_file: os.PathLike[str] | str) -> TooManyLinesLintScope:
    disabled = TooManyLinesLintScope(False, 100)
    configured = read_clippy_lint(rs_file, "too_many_lines", "pedantic")
    if not isinstance(configured, ConfiguredClippyLint) or configured.level not in {
        ClippyLintLevel.WARN, ClippyLintLevel.DENY, ClippyLintLevel.FORBID
    }:
        return disabled
    try:
        for parent in ancestor_directories(configured.package_dir):
            for name in ("clippy.toml", ".clippy.toml"):
                config = os.path.join(parent, name)
                if not os.path.isfile(config):
                    continue
                raw = read_text(config)
                if "too-many-lines-threshold" not in raw:
                    continue
                value = read_toml(raw).get("too-many-lines-threshold")
                if value is not None:
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        return disabled
                    return TooManyLinesLintScope(True, value)
        return TooManyLinesLintScope(True, 100)
    except (OSError, ValueError, UnicodeError):
        return disabled


def target_may_be_no_std(rs_file: os.PathLike[str] | str, package_dir: str) -> bool:
    """Pass when the edited target is no_std or its crate root is uncertain."""
    path = os.path.abspath(rs_file)
    relative = os.path.relpath(path, package_dir)
    parts = relative.split(os.sep)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return True
    src = os.path.join(package_dir, "src")
    roots: list[str]
    if parts[:1] == ["src"]:
        if len(parts) == 2 and parts[1] in {"lib.rs", "main.rs"}:
            roots = [path]
        elif len(parts) > 2 and parts[1] == "bin":
            if len(parts) == 3:
                roots = [path]
            elif len(parts) == 4 and parts[3] == "main.rs":
                roots = [path]
            else:
                bin_dir = os.path.join(src, "bin", parts[2])
                roots = [os.path.join(bin_dir, "main.rs"), os.path.join(src, "bin", parts[2] + ".rs")]
        else:
            roots = [os.path.join(src, name) for name in ("lib.rs", "main.rs")]
    elif parts[0] in {"tests", "examples", "benches"}:
        if len(parts) == 2:
            roots = [path]
        elif len(parts) == 3 and parts[2] == "main.rs":
            roots = [path]
        else:
            target_dir = os.path.join(package_dir, parts[0], parts[1])
            roots = [os.path.join(target_dir, "main.rs"), os.path.join(package_dir, parts[0], parts[1] + ".rs")]
    else:
        return True
    existing = [root for root in roots if os.path.isfile(root)]
    if not existing:
        return True
    try:
        return any(_root_is_no_std(root) for root in existing)
    except (OSError, UnicodeError):
        return True


def _root_is_no_std(root: str) -> bool:
    source = read_text(root)
    tokens, comments = tokenize_rust(source)
    pairs = pair_delimiters(tokens)
    openings = {match.end() for match in re.finditer(r"#\s*!\s*\[", source)}
    for index, token in enumerate(tokens):
        if token.text not in {"#![", "["} or token.end not in openings or index not in pairs:
            continue
        end = tokens[pairs[index]].start
        attribute = source[token.end:end]
        for comment_end, comment_start in comments.items():
            start = max(token.end, comment_start)
            stop = min(end, comment_end)
            if start < stop:
                attribute = attribute[:start - token.end] + " " * (stop - start) + attribute[stop - token.end:]
        attribute = re.sub(r'"(?:\\.|[^"\\])*"', '""', attribute).strip()
        if re.fullmatch(r"no_std", attribute) or re.fullmatch(
            r"cfg_attr\s*\(.+,\s*no_std\s*\)", attribute, re.DOTALL
        ):
            return True
    return False


def long_functions(rs_file: os.PathLike[str] | str) -> tuple[TooManyLinesLintScope, list[FunctionLength]]:
    scope = lint_scope(rs_file)
    if not scope.enabled:
        return scope, []
    try:
        source = read_text(os.fspath(rs_file))
    except (OSError, UnicodeError):
        return scope, []
    example = any(
        os.path.basename(parent) == "examples"
        and os.path.isfile(os.path.join(os.path.dirname(parent), "Cargo.toml"))
        for parent in ancestor_directories(os.path.dirname(os.path.abspath(rs_file)))
    )
    return scope, [
        function
        for function in measure_functions(source, skip_example_tests=example)
        if not function.exempt and function.lines > scope.threshold
    ]
