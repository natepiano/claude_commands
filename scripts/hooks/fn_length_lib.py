"""Measure Rust function bodies using clippy's too_many_lines rules."""

from __future__ import annotations

import bisect
import os
import re


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


class LintScope:
    enabled: bool
    threshold: int
    __slots__: tuple[str, ...] = ("enabled", "threshold")

    def __init__(self, enabled: bool, threshold: int) -> None:
        self.enabled = enabled
        self.threshold = threshold


class _Token:
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


def _tokens(source: str) -> tuple[list[_Token], dict[int, int]]:
    found: list[_Token] = []
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
        found.append(_Token(opening, start, end))
        position = end
    return found, comments


def _pairs(tokens: list[_Token]) -> dict[int, int]:
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


def _body_opener(tokens: list[_Token], pairs: dict[int, int], start: int) -> int:
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


def _macro_before(source: str, bang: int, comments: dict[int, int]) -> str:
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
    tokens, comments = _tokens(source)
    pairs = _pairs(tokens)
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
        if word == "!" and _macro_before(source, token.start, comments):
            opener = index + 1
            while opener < len(tokens) and tokens[opener].text not in _OPEN:
                if tokens[opener].text in {";", "}"}:
                    break
                opener += 1
            if opener in pairs and tokens[opener].text in _OPEN:
                index = pairs[opener] + 1
                continue
        if word in {"fn", "impl", "trait", "mod"} and index >= signature_end:
            opener = _body_opener(tokens, pairs, index + 1)
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


def _read_toml(source: str) -> dict[str, object]:
    import tomllib
    from typing import cast

    return cast(dict[str, object], tomllib.loads(source))


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as source:
        return source.read()


def _parents(directory: str) -> list[str]:
    parents: list[str] = []
    while True:
        parents.append(directory)
        parent = os.path.dirname(directory)
        if parent == directory:
            return parents
        directory = parent


def _table(value: object) -> dict[str, object]:
    from typing import cast

    if not isinstance(value, dict):
        return {}
    return cast(dict[str, object], value)


def _level(value: object) -> str:
    if isinstance(value, str):
        return value
    level = _table(value).get("level")
    return level if isinstance(level, str) else ""


def lint_scope(rs_file: os.PathLike[str] | str) -> LintScope:
    disabled = LintScope(False, 100)
    # Search every ancestor, including the file's own directory. Missing
    # manifests are ordinary; malformed manifests fail the scope closed.
    try:
        for parent in _parents(os.path.dirname(os.path.abspath(rs_file))):
            manifest = os.path.join(parent, "Cargo.toml")
            if not os.path.isfile(manifest):
                continue
            raw = _read_text(manifest)
            if "package" not in raw:
                continue
            if "lints" not in raw:
                return disabled
            data = _read_toml(raw)
            if "package" in data:
                package_dir = parent
                package = data
                break
        else:
            return disabled
        lints = _table(package.get("lints"))
        if lints.get("workspace") is True:
            package_table = _table(package.get("package"))
            workspace_path = package_table.get("workspace")
            if isinstance(workspace_path, str):
                workspace = _read_toml(
                    _read_text(os.path.realpath(os.path.join(package_dir, workspace_path, "Cargo.toml")))
                )
            else:
                workspace = {}
                for parent in _parents(package_dir):
                    manifest = os.path.join(parent, "Cargo.toml")
                    if os.path.isfile(manifest):
                        candidate = _read_toml(_read_text(manifest))
                        if "workspace" in candidate:
                            workspace = candidate
                            break
            clippy = _table(_table(_table(workspace.get("workspace")).get("lints")).get("clippy"))
        else:
            clippy = _table(lints.get("clippy"))
        level = (
            _level(clippy["too_many_lines"])
            if "too_many_lines" in clippy
            else _level(clippy.get("pedantic"))
        )
        if level not in {"warn", "deny", "forbid"}:
            return disabled
        threshold = 100
        for parent in _parents(package_dir):
            for name in ("clippy.toml", ".clippy.toml"):
                config = os.path.join(parent, name)
                if not os.path.isfile(config):
                    continue
                raw = _read_text(config)
                if "too-many-lines-threshold" not in raw:
                    continue
                value = _read_toml(raw).get("too-many-lines-threshold")
                if value is not None:
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        return disabled
                    threshold = value
                    return LintScope(True, threshold)
        return LintScope(True, threshold)
    except (OSError, ValueError, UnicodeError):
        return disabled


def long_functions(rs_file: os.PathLike[str] | str) -> tuple[LintScope, list[FunctionLength]]:
    scope = lint_scope(rs_file)
    if not scope.enabled:
        return scope, []
    try:
        source = _read_text(os.fspath(rs_file))
    except (OSError, UnicodeError):
        return scope, []
    example = any(
        os.path.basename(parent) == "examples"
        and os.path.isfile(os.path.join(os.path.dirname(parent), "Cargo.toml"))
        for parent in _parents(os.path.dirname(os.path.abspath(rs_file)))
    )
    return scope, [
        function
        for function in measure_functions(source, skip_example_tests=example)
        if not function.exempt and function.lines > scope.threshold
    ]
