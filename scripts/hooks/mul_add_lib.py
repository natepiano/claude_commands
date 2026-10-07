"""Find provable scalar float multiply-add expressions in Rust edits."""

from __future__ import annotations

import os
import re
from bisect import bisect_right
from enum import Enum

from fn_length_lib import (
    ClippyLintLevel, ConfiguredClippyLint, RustToken, UnavailableClippyLint, body_opener,
    macro_before, pair_delimiters, read_clippy_lint, read_text,
    target_may_be_no_std, tokenize_rust,
)


class ScopeState(Enum):
    """EXEMPT passes no_std targets and files whose crate root is uncertain.

    Uncertain roots include missing lib.rs/main.rs, build.rs, and paths outside
    src/, tests/, examples/, or benches/.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    EXEMPT = "exempt"
    NO_PACKAGE = "no_package"


class SuboptimalFlopsLintScope:
    __slots__: tuple[str, ...] = ("_state",)
    _state: ScopeState

    def __init__(self, state: ScopeState) -> None:
        self._state = state

    @property
    def state(self) -> ScopeState:
        return self._state

    @property
    def enabled(self) -> bool:
        return self.state is ScopeState.ENABLED


class FloatMulAddFinding:
    __slots__: tuple[str, ...] = ("_line", "_a", "_b", "_c", "_expr", "_rewrite")
    _line: int
    _a: str
    _b: str
    _c: str
    _expr: str
    _rewrite: str

    def __init__(self, line: int, a: str, b: str, c: str, expr: str, rewrite: str) -> None:
        self._line, self._a, self._b = line, a, b
        self._c, self._expr, self._rewrite = c, expr, rewrite

    @property
    def line(self) -> int:
        return self._line

    @property
    def a(self) -> str:
        return self._a

    @property
    def b(self) -> str:
        return self._b

    @property
    def c(self) -> str:
        return self._c

    @property
    def expr(self) -> str:
        return self._expr

    @property
    def rewrite(self) -> str:
        return self._rewrite


_EXEMPT = re.compile(r"clippy::(?:suboptimal_flops|nursery)\b")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_LITERALS = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])' ''', re.VERBOSE)
_FLOAT = re.compile(r"(?:\d+\.\d*(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+|f(?:32|64)))(?:_?f(?:32|64))?\Z")
_LEX = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?(?:_?f(?:32|64))?|[A-Za-z_]\w*|\+=|-=|=>|&&|\|\||\.\.|::|[^\s]")
_ANNOTATION = re.compile(r"\b([A-Za-z_]\w*)\s*:\s*(&?\s*(?:mut\s+)?(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)")
_SCALAR_TYPE = re.compile(r"\b([A-Za-z_]\w*)\s*:\s*f(?:32|64)\b")
_NAMED_FIELDS = re.compile(r"\b(?:struct|enum)\s+[A-Za-z_]\w*[^;{]*\{([^}]*)\}")
_VECTOR_BINDING = re.compile(r"\blet\s+(?:mut\s+)?([A-Za-z_]\w*)\s*=\s*([^;]+);")
_VECTOR_VALUE = re.compile(r"\b(?:D?Vec[234]::(?:new|splat|from|ZERO|ONE|X|Y|Z)|\.as_[diu]?vec[234]\s*\(|\.transform_(?:point|vector)\s*\()")
_REFERENCE_VALUE = re.compile(r"^\s*&|\.(?:iter|get|first|last)\s*\(")
_PATTERN_BINDING = re.compile(r"\b(?:for|if\s+let|while\s+let)\s+(?:mut\s+)?([A-Za-z_]\w*)\s+(?:in|=)|\|([^|]+)\||\b([A-Za-z_]\w*)\s*=>")
_DESTRUCTURED_PATTERN = re.compile(r"\b(?:if\s+let|while\s+let)\s+\w+\s*\(\s*([A-Za-z_]\w*)\s*\)\s*=|\b\w+\s*\(\s*([A-Za-z_]\w*)\s*\)\s*=>")
_LET_ELSE = re.compile(r"\blet\s+(?:\w+\s*\(\s*)?([A-Za-z_]\w*)\s*\)?\s*=\s*[^;]+?\belse\s*\{")
_COMPOUND_PATTERN = re.compile(r"\bfor\s+(.+?)\s+in\b|\b(?:if|while)\s+let\s+(.+?)\s*=|\blet\s+(.+?)\s*=\s*[^;]+?\belse\s*\{")
_FILE_VALUE = re.compile(r"\b(?:const|static)\s+([A-Za-z_]\w*)\s*:\s*([^=;]+)=")
_BOUNDARY = frozenset(("(", "[", "{", ",", ";", "=", "+=", "-=", "=>", "<", ">", "&&", "||", "..", "return", "let"))


def _has_let_else(source: str, start: int) -> bool:
    """Find an else on this let statement, after an initializer not ending in }."""
    brackets: list[str] = []
    previous = ""
    for match in _LEX.finditer(source, start):
        token = match.group()
        if not brackets and token == ";":
            return False
        if not brackets and token == "else" and previous != "}":
            return True
        if token in {"(", "[", "{"}:
            brackets.append(token)
        elif token in {")", "]", "}"} and brackets:
            _ = brackets.pop()
        previous = token
    return False


def _attribute_exempts(attribute: str) -> bool:
    compact = re.sub(r"\s+", "", _STRING.sub('""', attribute))
    return bool(_EXEMPT.search(compact) and re.search(r"(?:allow|expect)\(", compact))


def suboptimal_flops_scope(rs_file: os.PathLike[str] | str) -> SuboptimalFlopsLintScope:
    """Resolve this target's effective nursery or suboptimal_flops level."""
    configured = read_clippy_lint(rs_file, "suboptimal_flops", "nursery")
    if not isinstance(configured, ConfiguredClippyLint):
        return SuboptimalFlopsLintScope(
            ScopeState.NO_PACKAGE if configured is UnavailableClippyLint.NO_PACKAGE else ScopeState.DISABLED
        )
    if configured.level not in {ClippyLintLevel.WARN, ClippyLintLevel.DENY, ClippyLintLevel.FORBID}:
        return SuboptimalFlopsLintScope(ScopeState.DISABLED)
    if target_may_be_no_std(rs_file, configured.package_dir):
        return SuboptimalFlopsLintScope(ScopeState.EXEMPT)
    return SuboptimalFlopsLintScope(ScopeState.ENABLED)


def _visible(source: str, comments: dict[int, int], tokens: list[RustToken], pairs: dict[int, int]) -> str:
    spans = [(start, end) for end, start in comments.items()]
    spans.extend((match.start(), match.end()) for match in _LITERALS.finditer(source))
    for index, token in enumerate(tokens):
        if token.text != "!" or not macro_before(source, token.start, comments):
            continue
        opener = index + 1
        while opener < len(tokens) and tokens[opener].text not in {"{", "(", "["}:
            if tokens[opener].text in {";", "}"}:
                break
            opener += 1
        if opener in pairs:
            spans.append((token.start, tokens[pairs[opener]].end))
    if not spans:
        return source
    chars = list(source)
    end_of_previous = 0
    for start, end in sorted(spans):
        if start < end_of_previous:
            continue
        chars[start:end] = ["\n" if char == "\n" else " " for char in source[start:end]]
        end_of_previous = end
    return "".join(chars)


def _functions(
    source: str, visible: str, tokens: list[RustToken], pairs: dict[int, int], comments: dict[int, int]
) -> list[tuple[int, int, int]]:
    if (
        sum(token.text in {"{", "(", "[", "#[", "#!["} for token in tokens) != len(pairs)
        or sum(token.text in {"}", ")", "]"} for token in tokens) != len(pairs)
    ):
        raise ValueError("unbalanced Rust delimiters")
    inherited = [False]
    pending = False
    block_exempt: dict[int, bool] = {}
    bodies: list[tuple[int, int, int]] = []
    signature_end = -1
    index = 0
    while index < len(tokens):
        token = tokens[index]
        word = token.text
        if word in {"#[", "#!["}:
            close = pairs.get(index)
            if close is None:
                raise ValueError("unbalanced Rust attribute")
            exempt = _attribute_exempts(source[token.start:tokens[close].end])
            if word == "#![":
                inherited[-1] |= exempt
            else:
                pending |= exempt
            index = close + 1
            continue
        if word == "!" and macro_before(source, token.start, comments):
            opener = index + 1
            while opener < len(tokens) and tokens[opener].text not in {"{", "(", "["}:
                if tokens[opener].text in {";", "}"}:
                    break
                opener += 1
            if opener in pairs:
                index = pairs[opener] + 1
                continue
        if word in {"fn", "impl", "trait", "mod"} and index >= signature_end:
            opener = body_opener(tokens, pairs, index + 1)
            if word == "fn" and opener in pairs and not (inherited[-1] or pending):
                previous = max(visible.rfind(";", 0, token.start), visible.rfind("{", 0, token.start), visible.rfind("}", 0, token.start))
                prefix = visible[previous + 1:token.start]
                if not re.search(r"\bconst\s*$", prefix):
                    bodies.append((token.start, tokens[opener].end, tokens[pairs[opener]].start))
            if opener >= 0:
                block_exempt[opener] = pending
                signature_end = opener
            pending = False
        if word == "{":
            inherited.append(inherited[-1] or block_exempt.get(index, False))
            pending = False
        elif word == "}":
            if len(inherited) > 1:
                _ = inherited.pop()
            pending = False
        elif word == ";":
            pending = False
        index += 1
    return bodies


def _atom(lexemes: list[RustToken], index: int, direction: int) -> tuple[int, int]:
    if not 0 <= index < len(lexemes):
        return -1, -1
    token = lexemes[index].text
    if direction < 0 and token == ")":
        depth = 1
        for cursor in range(index - 1, -1, -1):
            depth += (lexemes[cursor].text == ")") - (lexemes[cursor].text == "(")
            if depth == 0:
                return cursor, index + 1
    if direction > 0 and token == "(":
        depth = 1
        for cursor in range(index + 1, len(lexemes)):
            depth += (lexemes[cursor].text == "(") - (lexemes[cursor].text == ")")
            if depth == 0:
                return index, cursor + 1
    if re.fullmatch(r"[A-Za-z_]\w*|\d[\w.]*", token) or _FLOAT.fullmatch(token):
        start, end = index, index + 1
        if direction < 0:
            while start >= 2 and lexemes[start - 1].text == "." and re.fullmatch(r"[A-Za-z_]\w*", lexemes[start - 2].text):
                start -= 2
        else:
            while end + 1 < len(lexemes) and lexemes[end].text == "." and re.fullmatch(r"[A-Za-z_]\w*", lexemes[end + 1].text):
                end += 2
        return start, end
    return -1, -1


def _float_atom(text: str, scalars: set[str], excluded: set[str]) -> bool:
    stripped = text.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    if match := re.fullmatch(r"([A-Za-z_]\w*)\s*([+-])\s*([A-Za-z_]\w*)", stripped):
        return match.group(1) in scalars and match.group(3) in scalars
    if stripped in excluded:
        return False
    return bool(_FLOAT.fullmatch(stripped) or stripped in scalars or re.search(r"\bas\s+f(?:32|64)\b\s*$", stripped))


def _literal_compatible(text: str, excluded: set[str]) -> bool:
    if _FLOAT.fullmatch(text):
        return True
    if re.fullmatch(r"[A-Za-z_]\w*", text):
        return text not in excluded
    if text.startswith("(") and text.endswith(")"):
        return not any(match.group() in excluded for match in re.finditer(r"[A-Za-z_]\w*", text))
    return False


def _sqrt_receiver(lexemes: list[RustToken], expr_start: int, expr_end: int) -> bool:
    cursor = expr_end
    while cursor < len(lexemes) and lexemes[cursor].text == ")":
        opening, _ = _atom(lexemes, cursor, -1)
        if opening < 0 or opening >= expr_start:
            return False
        cursor += 1
    return (
        cursor > expr_end
        and cursor + 1 < len(lexemes)
        and lexemes[cursor].text == "."
        and lexemes[cursor + 1].text == "sqrt"
    )


def _find_in_body(
    source: str, visible: str, fn_start: int, start: int, end: int,
    scalar_fields: set[str], nonfloat_fields: set[str], file_floats: set[str], file_nonfloats: set[str],
    nested: list[tuple[int, int]],
    const_ranges: list[tuple[int, int]],
) -> list[FloatMulAddFinding]:
    body = visible[start:end]
    if "*" not in body or "+" not in body and "-" not in body:
        return []
    typed_source = visible[fn_start:end]
    annotations = {match.group(1): match.group(2).strip() for match in _ANNOTATION.finditer(typed_source)}
    scalars = {name for name, annotation in annotations.items() if annotation in {"f32", "f64"}}
    local_bindings = {match.group(1) for match in _VECTOR_BINDING.finditer(typed_source)}
    scalars |= file_floats - annotations.keys() - local_bindings
    excluded = (file_nonfloats - annotations.keys()) | {
        name for name, annotation in annotations.items() if annotation not in {"f32", "f64"}
    }
    for match in _PATTERN_BINDING.finditer(typed_source):
        names = ([match.group(1), match.group(3)] if match.group(2) is None
                 else [name.group() for name in re.finditer(r"[A-Za-z_]\w*", match.group(2))])
        excluded.update(name for name in names if name and name not in annotations)
    excluded.update(name for match in _DESTRUCTURED_PATTERN.finditer(typed_source)
                    if (name := match.group(1) or match.group(2)) not in annotations)
    excluded.update(match.group(1) for match in _LET_ELSE.finditer(typed_source)
                    if match.group(1) not in annotations and _has_let_else(typed_source, match.start()))
    for match in _COMPOUND_PATTERN.finditer(typed_source):
        pattern = match.group(1) or match.group(2) or match.group(3)
        if match.group(3) and not _has_let_else(typed_source, match.start()):
            continue
        excluded.update(name.group() for name in re.finditer(r"[A-Za-z_]\w*", pattern)
                        if name.group() not in annotations)
    excluded.update(
        match.group(1) for match in _VECTOR_BINDING.finditer(typed_source)
        if _VECTOR_VALUE.search(match.group(2)) or _REFERENCE_VALUE.search(match.group(2))
    )
    def field_is_scalar(expression: str) -> bool:
        if "." not in expression:
            return True
        fields = re.findall(r"(?<!\d)\.([A-Za-z_]\w*|\d+)\b", expression)
        return not fields or fields[-1] in scalar_fields

    lexemes = [RustToken(match.group(), start + match.start(), start + match.end()) for match in _LEX.finditer(body)]
    statement_boundaries = [index for index, lexeme in enumerate(lexemes) if lexeme.text in {";", "{", "}"}]
    nested_starts = [range_start for range_start, _ in nested]
    const_starts = [range_start for range_start, _ in const_ranges]
    findings: list[FloatMulAddFinding] = []
    newlines: list[int] | None = None
    for index, token in enumerate(lexemes):
        if token.text != "*":
            continue
        nested_index = bisect_right(nested_starts, token.start) - 1
        if nested_index >= 0 and token.start < nested[nested_index][1]:
            continue
        const_index = bisect_right(const_starts, token.start) - 1
        if const_index >= 0 and token.start < const_ranges[const_index][1]:
            continue
        a_start, a_end = _atom(lexemes, index - 1, -1)
        b_start, b_end = _atom(lexemes, index + 1, 1)
        if a_start < 0 or b_start < 0:
            continue
        if (a_start > 0 and lexemes[a_start - 1].text in {".", "::"}) or (
            b_end < len(lexemes) and lexemes[b_end].text in {".", "::", "(", "["}
        ):
            continue
        a = source[lexemes[a_start].start:lexemes[a_end - 1].end]
        b = source[lexemes[b_start].start:lexemes[b_end - 1].end]
        if a in excluded or b in excluded:
            continue
        if not field_is_scalar(a) or not field_is_scalar(b):
            continue
        literal_proof = bool(_FLOAT.fullmatch(a) or _FLOAT.fullmatch(b))
        if literal_proof:
            if not ((_literal_compatible(a, excluded) or "." in a and field_is_scalar(a))
                    and (_literal_compatible(b, excluded) or "." in b and field_is_scalar(b))):
                continue
        elif not ((_float_atom(a, scalars, excluded) or "." in a and "(" not in a and field_is_scalar(a)
                   and a.rsplit(".", 1)[-1] not in nonfloat_fields)
                  and (_float_atom(b, scalars, excluded) or "." in b and "(" not in b and field_is_scalar(b)
                       and b.rsplit(".", 1)[-1] not in nonfloat_fields)):
            continue
        if a_start > 0 and lexemes[a_start - 1].text in {"*", "/", "%", "as"}:
            continue
        if b_end < len(lexemes) and lexemes[b_end].text in {"*", "/", "%", "as"}:
            continue
        form = ""
        c_start = c_end = expr_start = expr_end = -1
        previous = lexemes[a_start - 1].text if a_start else "="
        unary_minus = (previous == "-" and
                       (a_start == 1 or a_start >= 2 and lexemes[a_start - 2].text in _BOUNDARY))
        if previous in {"+", "-", "+=", "-="} and a_start >= 2 and not unary_minus:
            c_start, c_end = _atom(lexemes, a_start - 2, -1)
            if c_start >= 0:
                while c_start >= 2 and lexemes[c_start - 1].text in {"+", "-"}:
                    earlier, _ = _atom(lexemes, c_start - 2, -1)
                    if earlier < 0:
                        break
                    c_start = earlier
                expr_start, expr_end = c_start, b_end
                form = "product_last_" + previous
        elif b_end < len(lexemes) and lexemes[b_end].text in {"+", "-"}:
            c_start, c_end = _atom(lexemes, b_end + 1, 1)
            expr_start, expr_end = a_start - int(unary_minus), c_end
            form = "product_first_" + lexemes[b_end].text
        if c_start < 0 or c_end < 0 or expr_start < 0:
            continue
        if c_start > 0 and lexemes[c_start - 1].text in {".", "::"}:
            continue
        if c_end < len(lexemes) and lexemes[c_end].text in {".", "::", "(", "["}:
            continue
        if form.startswith("product_first") and c_end < len(lexemes) and lexemes[c_end].text in {"+", "-"}:
            continue
        c = source[lexemes[c_start].start:lexemes[c_end - 1].end]
        if "." not in c and any(name.group() in excluded for name in re.finditer(r"[A-Za-z_]\w*", c)):
            continue
        if not field_is_scalar(c):
            continue
        if c == "()":
            continue
        if form.startswith("product_last") and c_start > 0 and lexemes[c_start - 1].text in {"*", "/", "%"}:
            continue
        if form.startswith("product_first") and c_end < len(lexemes) and lexemes[c_end].text in {"*", "/", "%"}:
            continue
        boundary = bisect_right(statement_boundaries, expr_start - 1) - 1
        statement_start = statement_boundaries[boundary] if boundary >= 0 else -1
        if any(lexeme.text in {"const", "static"} for lexeme in lexemes[statement_start + 1:expr_start]):
            continue
        if form.startswith("product_first") and a_start > 0 and previous not in _BOUNDARY and not unary_minus:
            continue
        if form.startswith("product_last") and c_start > 0 and lexemes[c_start - 1].text not in _BOUNDARY:
            continue
        if form.startswith("product_last") and any(lexeme.text in {"*", "/", "%"} for lexeme in lexemes[c_start:c_end]):
            continue
        if _FLOAT.fullmatch(a) and not re.search(r"_?f(?:32|64)$", a):
            if _FLOAT.fullmatch(b) and not re.search(r"_?f(?:32|64)$", b):
                continue
            a, b = b, a
        if unary_minus:
            a = f"-{a}"
        if _sqrt_receiver(lexemes, expr_start, expr_end):
            continue
        if form in {"product_last_-", "product_last_-="}:
            receiver, addend = f"(-{a})", c
        elif form == "product_first_-":
            receiver, addend = a, f"-{c}"
        else:
            receiver, addend = a, c
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*|\d[\w.]*", receiver):
            receiver = f"({receiver})" if not receiver.startswith("(") else receiver
        call = f"{receiver}.mul_add({b}, {addend})"
        if form == "product_last_-=":
            call = f"{c} = {call}"
        elif form == "product_last_+=":
            call = f"{c} = {call}"
        expr = source[lexemes[expr_start].start:lexemes[expr_end - 1].end]
        if newlines is None:
            newlines = [position for position, char in enumerate(source) if char == "\n"]
        line = bisect_right(newlines, lexemes[expr_start].start) + 1
        findings.append(FloatMulAddFinding(line, a, b, c, expr, call))
    return findings


def float_mul_add_findings(rs_file: os.PathLike[str] | str) -> tuple[SuboptimalFlopsLintScope, list[FloatMulAddFinding]]:
    """Return only expressions whose scalar float types can be proved locally."""
    scope = suboptimal_flops_scope(rs_file)
    if not scope.enabled:
        return scope, []
    source = read_text(os.fspath(rs_file))
    if not re.search(r"\*[^;{}]*[+-]|[+-][^;{}]*\*", source):
        return scope, []
    tokens, comments = tokenize_rust(source)
    pairs = pair_delimiters(tokens)
    visible = _visible(source, comments, tokens, pairs)
    bodies = _functions(source, visible, tokens, pairs, comments)
    opening_at = {token.start: index for index, token in enumerate(tokens) if token.text == "{"}
    const_ranges = sorted((match.start(), tokens[pairs[opening_at[match.end() - 1]]].end)
                          for match in re.finditer(r"\bconst\s*\{", visible)
                          if match.end() - 1 in opening_at and opening_at[match.end() - 1] in pairs)
    scalar_fields = {field.group(1) for declaration in _NAMED_FIELDS.finditer(visible)
                     for field in _SCALAR_TYPE.finditer(declaration.group(1))}
    nonfloat_fields = {field.group(1) for declaration in _NAMED_FIELDS.finditer(visible)
                       for field in _ANNOTATION.finditer(declaration.group(1))
                       if field.group(2).strip() not in {"f32", "f64"}}
    declarations = [(match.start(), match.group(1), match.group(2).strip())
                    for match in _FILE_VALUE.finditer(visible)]
    file_values: dict[str, str] = {}
    for position, name, annotation in declarations:
        if not any(start <= position < end for _, start, end in bodies):
            if name not in file_values or annotation not in {"f32", "f64"}:
                file_values[name] = annotation
    results: list[FloatMulAddFinding] = []
    for body_index, (fn_start, start, end) in enumerate(bodies):
        visible_values = file_values.copy()
        visible_values.update((name, annotation) for position, name, annotation in declarations
                              if start <= position < end)
        file_floats = {name for name, annotation in visible_values.items() if annotation in {"f32", "f64"}}
        file_nonfloats = visible_values.keys() - file_floats
        nested: list[tuple[int, int]] = []
        for _, nested_start, nested_end in bodies[body_index + 1:]:
            if nested_start >= end:
                break
            if nested_end < end:
                nested.append((nested_start, nested_end))
        results.extend(_find_in_body(source, visible, fn_start, start, end,
                                     scalar_fields, nonfloat_fields, file_floats, file_nonfloats,
                                     nested, const_ranges))
    return scope, results
