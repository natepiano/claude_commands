"""Find likely multiply-add expressions in Rust edits."""

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


class SourceLineSelection(Enum):
    ALL = "all source lines"


class MulAddFinding:
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
_LITERALS = re.compile(
    r'(?:br|cr|r)(?P<hashes>\#*)"[\s\S]*?"(?P=hashes)|'
    + r'(?:b|c)?"(?:\\[\s\S]|[^"\\])*"|(?:b)?\'(?:\\(?:u\{[0-9a-fA-F_]+\}|.)|[^\'\\])\''
)
_FLOAT = re.compile(r"\d[\d_]*\.\d[\d_]*(?:[eE][+-]?\d+)?(?:_?f(?:32|64))?\Z|\d[\d_]*[eE][+-]?\d+(?:_?f(?:32|64))?\Z|\d[\d_]*_?f(?:32|64)\Z|\d[\d_]*\.\Z")
_INTEGER = re.compile(r"(?:0[xbo][\da-fA-F_]+|\d[\d_]*)(?:_?[iu](?:8|16|32|64|128|size))?\Z")
_LEX = re.compile(
    r"\d[\d_]*\.\d[\d_]*(?:[eE][+-]?\d+)?(?:_?f(?:32|64))?|"
    + r"\d[\d_]*[eE][+-]?\d+(?:_?f(?:32|64))?|\d[\d_]*_?f(?:32|64)|\d[\d_]*\.(?![.\w])|"
    + r"0[xbo][\da-fA-F_]+\w*|\d[\d_]*\w*|[A-Za-z_]\w*|"
    + r"\.\.=|\.\.|<<=|>>=|\+=|-=|\*=|/=|%=|==|!=|<=|>=|&&|\|\||->|=>|::|[^\s]"
)
_FLOAT_EVIDENCE = re.compile(r"\bas\s+f(?:32|64)\b|\bf(?:32|64)::|\.to_f(?:32|64)\s*\(|\.(?:sin|cos|tan|sqrt|powi|powf|to_radians|to_degrees|sin_cos|atan2|exp|ln|hypot|recip|fract|signum|lerp)\s*\(|\b(?:PI|TAU|FRAC_PI_\d|EPSILON)\b")
_INTEGER_EVIDENCE = re.compile(r"\bas\s+(?:usize|isize|[iu](?:8|16|32|64|128))\b|\.(?:len|count)\s*\(\s*\)")
VECTOR = re.compile(r"\b(?:D?Vec[234]A?|Quat|Mat[234]|Dir[23]|Affine[23]|Isometry[23]d?|Transform|GlobalTransform|LinearRgba|Srgba|Color)\b|\.(?:normalize(?:_or_zero|_or)?|translation|rotation|truncate|extend|xy|xz|yz|xyz|as_vec[234]|as_dvec[234]|cross|perp|forward|right|up|back|left|down|local_[xyz]|half_size|half_extents|center|centre|size|extents|position|origin|direction|normal|tangent|min|max|offset|scale)\b(?!\s*[.(]\s*[xyzw\d]\b)(?!\.(?:x|y|z|w|width|height|depth|length|0|1)\b)")
_PARAMETER = re.compile(r"(?:^|[,(])\s*(?:mut\s+)?([A-Za-z_]\w*)\s*:\s*([^,)=]+)")
_LET_BINDING = re.compile(r"\blet\s+(?:mut\s+)?([A-Za-z_]\w*)\b")
_ANNOTATION = re.compile(r"\blet\s+(?:mut\s+)?([A-Za-z_]\w*)\s*:\s*([^=;]+)")
_VECTOR_BINDING = re.compile(r"\blet\s+(?:mut\s+)?([A-Za-z_]\w*)\s*=\s*([^;]+);")
_VECTOR_VALUE = re.compile(r"\b(?:D?Vec[234]::(?:new|splat|from|ZERO|ONE|X|Y|Z)|\.as_[diu]?vec[234]\s*\(|\.transform_(?:point|vector)\s*\()")
_FILE_VALUE = re.compile(r"\b(?:const|static(?:\s+mut)?)\s+([A-Za-z_]\w*)\s*:\s*([^=;]+)=")
_KEY_BOUND = frozenset(("return", "let", "if", "else", "match", "while", "in", "for", "loop", "break", "mut", "move", "where", "fn", "const", "static"))
_OP_BOUND = frozenset((",", ";", "=", "+=", "-=", "*=", "/=", "%=", "==", "!=", "<", ">", "<=", ">=", "&&", "||", "=>", "->", "..", "..=", "|", ":", "#", "!"))
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = frozenset(_OPEN.values())
_SCALAR_RESULT = re.compile(r"^\s*\(\s*\)\s*\.\s*(?:x|y|z|w|width|height|depth|length|0|1)\b")


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
        if token.text != "!" or macro_before(source, token.start, comments) != "macro_rules":
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


class ArithmeticTokens:
    """Operands and matched groups in one visible function body."""

    __slots__: tuple[str, ...] = ("lexemes", "pairs")
    lexemes: list[RustToken]
    pairs: dict[int, int]

    def __init__(self, visible: str, start: int, end: int) -> None:
        self.lexemes = [RustToken(match.group(), start + match.start(), start + match.end())
                        for match in _LEX.finditer(visible[start:end])]
        self.pairs = {}
        stack: list[int] = []
        for index, token in enumerate(self.lexemes):
            generic = token.text == "<" and (
                index > 0 and self.lexemes[index - 1].text == "::"
                or bool(stack) and self.lexemes[stack[-1]].text == "<"
            )
            if token.text in _OPEN or generic:
                stack.append(index)
            elif stack and _OPEN.get(self.lexemes[stack[-1]].text, ">") == token.text:
                opening = stack.pop()
                self.pairs[index] = opening
                self.pairs[opening] = index

    def binary(self, index: int) -> bool:
        if index <= 0:
            return False
        previous = self.lexemes[index - 1].text
        return (bool(re.fullmatch(r"[A-Za-z_]\w*", previous) or _FLOAT.fullmatch(previous)
                     or _INTEGER.fullmatch(previous))
                and previous not in _KEY_BOUND and previous != "as") or previous in {")", "]", "?"} or (
                    previous == ">" and index - 1 in self.pairs
                )

    def boundary(self, index: int, direction: int, *, additive: bool = False) -> int:
        while 0 <= index < len(self.lexemes):
            text = self.lexemes[index].text
            if direction < 0 and (text in _CLOSE or text == ">") and index in self.pairs:
                index = self.pairs[index] - 1
                continue
            if direction > 0 and (text in _OPEN or text == "<") and index in self.pairs:
                index = self.pairs[index] + 1
                continue
            if text in _OPEN or text in _CLOSE or text in _OP_BOUND or text in _KEY_BOUND:
                return index
            if text == "&" and self.binary(index):
                return index
            if text in {"+", "-"} and self.binary(index) and not additive:
                return index
            index += direction
        return index

    def product_root(self, start: int, end: int) -> int:
        root = -1
        index = start
        while index < end:
            text = self.lexemes[index].text
            if (text in _OPEN or text == "<") and index in self.pairs:
                index = self.pairs[index] + 1
                continue
            if text in {"*", "/", "%"} and self.binary(index):
                root = index
            index += 1
        return root

    def disproving_terms(
        self, start: int, end: int, names: BareNameEvidence, at: int,
    ) -> tuple[bool, bool]:
        """Integer literals and excluded bare names outside calls and indices."""
        integer = vector = False
        index = start
        while index < end:
            text = self.lexemes[index].text
            previous = self.lexemes[index - 1].text if index > start else ""
            following = self.lexemes[index + 1].text if index + 1 < end else ""
            if text == "[" or text == "<" and index in self.pairs or (
                text == "(" and index > start and self.binary(index)
            ):
                index = self.pairs.get(index, index) + 1
                continue
            continuation = previous in {".", "::", "as"} or following in {".", "::", "(", "[", "?"}
            unary = previous in {"-", "&", "*"} and not self.binary(index - 1)
            if not continuation:
                integer |= bool(_INTEGER.fullmatch(text))
                vector |= not unary and names.disproves(text, at)
            index += 1
        return integer, vector


class NameArithmeticEvidence(Enum):
    UNKNOWN = "unannotated binding"
    FLOAT = "f32 or f64 annotation"
    NONFLOAT = "another annotated type"
    VECTOR = "vector constructor binding"


class BareNameEvidence:
    """The latest binding before a sum, within its own function."""

    __slots__: tuple[str, ...] = ("fallback", "bindings")
    fallback: dict[str, NameArithmeticEvidence]
    bindings: dict[str, list[tuple[int, NameArithmeticEvidence]]]

    def __init__(
        self, visible: str, fn_start: int, start: int, end: int,
        file_nonfloats: set[str], nested: list[tuple[int, int]],
    ) -> None:
        self.fallback = dict.fromkeys(file_nonfloats, NameArithmeticEvidence.NONFLOAT)
        for match in _PARAMETER.finditer(visible[fn_start:start]):
            self.fallback[match.group(1)] = (
                NameArithmeticEvidence.FLOAT if match.group(2).strip() in {"f32", "f64"}
                else NameArithmeticEvidence.NONFLOAT
            )
        self.bindings = {}
        body = visible[start:end]
        nested_starts = [left for left, _ in nested]
        for match in _LET_BINDING.finditer(body):
            position = start + match.start()
            enclosing = bisect_right(nested_starts, position) - 1
            if enclosing >= 0 and position < nested[enclosing][1]:
                continue
            state = NameArithmeticEvidence.UNKNOWN
            if annotation := _ANNOTATION.match(body, match.start()):
                state = (NameArithmeticEvidence.FLOAT
                         if annotation.group(2).strip() in {"f32", "f64"}
                         else NameArithmeticEvidence.NONFLOAT)
            elif binding := _VECTOR_BINDING.match(body, match.start()):
                if _VECTOR_VALUE.search(binding.group(2)):
                    state = NameArithmeticEvidence.VECTOR
            self.bindings.setdefault(match.group(1), []).append((position, state))

    def disproves(self, name: str, at: int) -> bool:
        bindings = self.bindings.get(name, [])
        index = bisect_right(bindings, at, key=lambda binding: binding[0]) - 1
        state = (bindings[index][1] if index >= 0
                 else self.fallback.get(name, NameArithmeticEvidence.UNKNOWN))
        return state in {NameArithmeticEvidence.NONFLOAT, NameArithmeticEvidence.VECTOR}


def _compact(text: str) -> str:
    if not any(marker in text for marker in ('"', "'", "//", "/*")):
        return re.sub(r"\s+", " ", text).strip()
    _, comments = tokenize_rust(text)
    spans = [(start, end, False) for end, start in comments.items()]
    spans.extend((match.start(), match.end(), True) for match in _LITERALS.finditer(text))
    pieces: list[str] = []
    outside: list[str] = []
    cursor = 0
    for start, end, literal in sorted(spans):
        if start < cursor:
            continue
        outside.append(text[cursor:start])
        if literal:
            pieces.append(re.sub(r"\s+", " ", "".join(outside)))
            pieces.append(text[start:end])
            outside.clear()
        else:
            outside.append(" ")
        cursor = end
    outside.append(text[cursor:])
    pieces.append(re.sub(r"\s+", " ", "".join(outside)))
    return "".join(pieces).strip()


def _vector_evidence(text: str) -> bool:
    return any(not (match.group().startswith(".") and _SCALAR_RESULT.match(text[match.end():]))
               for match in VECTOR.finditer(text))


def _receiver(text: str) -> str:
    text = _compact(text)
    tokens = ArithmeticTokens(text, 0, len(text))
    if tokens.lexemes and tokens.lexemes[0].text == "(" and tokens.pairs.get(0) == len(tokens.lexemes) - 1:
        return text
    if text.startswith(("-", "&", "*")):
        return f"({text})"
    cursor = 0
    while cursor < len(tokens.lexemes):
        word = tokens.lexemes[cursor].text
        if (word in _OPEN or word == "<") and cursor in tokens.pairs:
            cursor = tokens.pairs[cursor] + 1
            continue
        if word == "as" or word in {"+", "-", "*", "/", "%", "&"} and tokens.binary(cursor):
            return f"({text})"
        cursor += 1
    return text


def _negated(text: str) -> str:
    text = _compact(text)
    tokens = ArithmeticTokens(text, 0, len(text))
    cursor = 0
    while cursor < len(tokens.lexemes):
        word = tokens.lexemes[cursor].text
        if word in {"[", "?"}:
            return f"-({text})"
        if (word in _OPEN or word == "<") and cursor in tokens.pairs:
            cursor = tokens.pairs[cursor] + 1
            continue
        if word == "as" or word in {"+", "-", "*", "/", "%", "&", "[", "?"}:
            return f"-({text})"
        cursor += 1
    return f"-{text}"


def _sqrt_receiver(tokens: ArithmeticTokens, start: int, end: int) -> bool:
    cursor = end
    while cursor < len(tokens.lexemes) and tokens.lexemes[cursor].text == ")":
        if tokens.pairs.get(cursor, start) >= start:
            return False
        cursor += 1
    return (cursor > end and cursor + 1 < len(tokens.lexemes)
            and tokens.lexemes[cursor].text == "." and tokens.lexemes[cursor + 1].text == "sqrt")


def _find_in_body(
    source: str, visible: str, fn_start: int, start: int, end: int,
    file_nonfloats: set[str], nested: list[tuple[int, int]], const_ranges: list[tuple[int, int]],
    newlines: list[int], selected: frozenset[int] | SourceLineSelection,
) -> list[MulAddFinding]:
    body = visible[start:end]
    if "*" not in body or "+" not in body and "-" not in body:
        return []
    names = BareNameEvidence(visible, fn_start, start, end, file_nonfloats, nested)
    tokens = ArithmeticTokens(visible, start, end)
    lexemes = tokens.lexemes
    nested_starts = [left for left, _ in nested]
    const_starts = [left for left, _ in const_ranges]
    seen_roots: set[int] = set()
    seen_sums: set[int] = set()
    findings: list[MulAddFinding] = []
    for index, token in enumerate(lexemes):
        if token.text != "*" or not tokens.binary(index):
            continue
        nested_index = bisect_right(nested_starts, token.start) - 1
        if nested_index >= 0 and token.start < nested[nested_index][1]:
            continue
        const_index = bisect_right(const_starts, token.start) - 1
        if const_index >= 0 and token.start < const_ranges[const_index][1]:
            continue
        left = tokens.boundary(index - 1, -1)
        right = tokens.boundary(index + 1, 1)
        root = tokens.product_root(left + 1, right)
        if root < 0 or lexemes[root].text != "*" or root in seen_roots:
            continue
        seen_roots.add(root)
        factor_start, factor_end = left + 1, right
        while (left >= 0 and right < len(lexemes) and lexemes[left].text == "("
               and tokens.pairs.get(left) == right and not tokens.binary(left)):
            left -= 1
            right += 1
        before = lexemes[left].text if left >= 0 else ""
        after = lexemes[right].text if right < len(lexemes) else ""
        if factor_start != left + 1 and (
            before in {"*", "/", "%", "&"}
            or before == "-" and not tokens.binary(left)
            or after in {"*", "/", "%"}
        ):
            continue
        product_last = (before in {"+=", "-="} and after not in {"+", "-"}
                        or before in {"+", "-"} and tokens.binary(left))
        if product_last:
            operator = left
            term_end = left
            term_start = (tokens.boundary(left - 1, -1, additive=before in {"+", "-"}) + 1)
            expr_start, expr_end = term_start, right
        elif after in {"+", "-"} and tokens.binary(right):
            operator = right
            term_start = right + 1
            term_end = tokens.boundary(right + 1, 1)
            expr_start, expr_end = left + 1, term_end
        else:
            continue
        if operator in seen_sums or term_start >= term_end or root <= factor_start or root + 1 >= factor_end:
            continue
        seen_sums.add(operator)
        evidence_start = tokens.boundary(left - 1, -1, additive=True) + 1 if before in {"+", "-"} else left + 1
        evidence_end = tokens.boundary(right + 1, 1, additive=True) if after in {"+", "-"} else right
        if product_last:
            evidence_start = term_start
        evidence = visible[lexemes[evidence_start].start:lexemes[evidence_end - 1].end]
        float_evidence = bool(_FLOAT_EVIDENCE.search(evidence)) or any(
            _FLOAT.fullmatch(lexeme.text) for lexeme in lexemes[evidence_start:evidence_end]
        )
        integer, vector_name = tokens.disproving_terms(
            evidence_start, evidence_end, names, lexemes[expr_start].start,
        )
        index_open = evidence_start - 1
        integer |= (index_open >= 0 and lexemes[index_open].text == "["
                    and tokens.pairs.get(index_open) == evidence_end and tokens.binary(index_open))
        if _vector_evidence(evidence) or vector_name or not float_evidence and (integer or _INTEGER_EVIDENCE.search(evidence)):
            continue
        if _sqrt_receiver(tokens, expr_start, expr_end):
            continue
        a = source[lexemes[factor_start].start:lexemes[root - 1].end]
        b = source[lexemes[root + 1].start:lexemes[factor_end - 1].end]
        c = source[lexemes[term_start].start:lexemes[term_end - 1].end]
        if _FLOAT.fullmatch(a) and not re.search(r"_?f(?:32|64)$", a):
            if _FLOAT.fullmatch(b) and not re.search(r"_?f(?:32|64)$", b):
                continue
            a, b = b, a
        word = lexemes[operator].text
        addend = _negated(c) if not product_last and word == "-" else c
        factor = _negated(b) if product_last and word in {"-", "-="} else b
        rewrite = f"{_receiver(a)}.mul_add({_compact(factor)}, {_compact(addend)})"
        if word in {"+=", "-="}:
            rewrite = f"{_compact(c)} = {rewrite}"
        raw_expr = source[lexemes[expr_start].start:lexemes[expr_end - 1].end]
        line = bisect_right(newlines, lexemes[expr_start].start) + 1
        last_line = bisect_right(newlines, lexemes[expr_end - 1].end - 1) + 1
        if isinstance(selected, frozenset) and not any(number in selected for number in range(line, last_line + 1)):
            continue
        findings.append(MulAddFinding(line, a, b, c, _compact(raw_expr), _compact(rewrite)))
    return findings


def mul_add_findings(
    rs_file: os.PathLike[str] | str, written: frozenset[int] | None = None,
) -> tuple[SuboptimalFlopsLintScope, list[MulAddFinding]]:
    """Return likely multiply-adds; written=None includes every source line."""
    scope = suboptimal_flops_scope(rs_file)
    if not scope.enabled:
        return scope, []
    source = read_text(os.fspath(rs_file))
    if "*" not in source or "+" not in source and "-" not in source:
        return scope, []
    tokens, comments = tokenize_rust(source)
    pairs = pair_delimiters(tokens)
    visible = _visible(source, comments, tokens, pairs)
    bodies = _functions(source, visible, tokens, pairs, comments)
    opening_at = {token.start: index for index, token in enumerate(tokens) if token.text == "{"}
    const_ranges = sorted((match.start(), tokens[pairs[opening_at[match.end() - 1]]].end)
                          for match in re.finditer(r"\bconst\s*\{", visible)
                          if match.end() - 1 in opening_at and opening_at[match.end() - 1] in pairs)
    declarations = [(match.start(), match.group(1), match.group(2).strip())
                    for match in _FILE_VALUE.finditer(visible)]
    token_starts = [token.start for token in tokens]
    for match in _FILE_VALUE.finditer(visible):
        index = bisect_right(token_starts, match.end() - 1)
        while index < len(tokens):
            if tokens[index].text == ";":
                const_ranges.append((match.start(), tokens[index].end))
                break
            index = pairs[index] + 1 if index in pairs else index + 1
    merged_ranges: list[tuple[int, int]] = []
    for left, right in sorted(const_ranges):
        if merged_ranges and left <= merged_ranges[-1][1]:
            merged_ranges[-1] = (merged_ranges[-1][0], max(right, merged_ranges[-1][1]))
        else:
            merged_ranges.append((left, right))
    function_ranges: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.text == "fn":
            opener = body_opener(tokens, pairs, index + 1)
            if opener in pairs:
                function_ranges.append((tokens[opener].end, tokens[pairs[opener]].start))
    file_nonfloats = {name for position, name, annotation in declarations
                     if annotation not in {"f32", "f64"}
                     and not any(start <= position < end for start, end in function_ranges)}
    newlines = [match.start() for match in re.finditer("\n", source)]
    function_starts = [start for start, _ in function_ranges]
    selected = SourceLineSelection.ALL if written is None else written
    results: list[MulAddFinding] = []
    for fn_start, start, end in bodies:
        nested: list[tuple[int, int]] = []
        for nested_start, nested_end in function_ranges[bisect_right(function_starts, start):]:
            if nested_start >= end:
                break
            if start < nested_start and nested_end < end:
                if not nested or nested_start >= nested[-1][1]:
                    nested.append((nested_start, nested_end))
        excluded = file_nonfloats | {name for position, name, annotation in declarations
                                    if start <= position < end and annotation not in {"f32", "f64"}
                                    and not any(left <= position < right for left, right in nested)}
        results.extend(_find_in_body(source, visible, fn_start, start, end,
                                     excluded, nested, merged_ranges, newlines, selected))
    results.sort(key=lambda finding: finding.line)
    return scope, results
