"""A nonexecuting shell grammar retaining quotes, expansions and command structure.

This is a policy parser, not an evaluator: parameter values and command outputs
remain explicit segments. Unsupported executable syntax is a parse error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
import re


class ShellSyntaxError(ValueError):
    """The input cannot be represented by the supported shell grammar."""


@dataclass(frozen=True)
class Literal:
    text: str
    quoted: bool = False


@dataclass(frozen=True)
class Parameter:
    name: str
    programs: tuple[Program, ...] = ()


@dataclass(frozen=True)
class Execution:
    program: Program


@dataclass(frozen=True)
class ArrayElements:
    words: tuple[Word, ...]


@dataclass(frozen=True)
class Word:
    segments: tuple[Literal | Parameter | Execution | ArrayElements, ...]
    line: int

    def literal(self) -> str | None:
        if all(isinstance(segment, Literal) for segment in self.segments):
            return "".join(segment.text for segment in self.segments if isinstance(segment, Literal))
        return None

    def keyword(self, text: str) -> bool:
        return self.literal() == text and all(isinstance(segment, Literal) and not segment.quoted
                                             for segment in self.segments)


@dataclass
class HereDocument:
    delimiter: str
    quoted: bool
    strip_tabs: bool
    body: str = ""
    segments: tuple[Literal | Parameter | Execution, ...] = ()

    @property
    def expansions(self) -> tuple[Program, ...]:
        return _programs(self.segments)

    @property
    def generated(self) -> bool:
        return any(isinstance(segment, (Parameter, Execution)) for segment in self.segments)


@dataclass(frozen=True)
class NoHereDocument:
    pass


@dataclass(frozen=True)
class Redirection:
    descriptor: int
    operator: str
    target: Word
    document: HereDocument | NoHereDocument


@dataclass(frozen=True)
class Operator:
    text: str
    line: int


@dataclass(frozen=True)
class SimpleCommand:
    words: tuple[Word, ...]
    redirects: tuple[Redirection, ...]


@dataclass(frozen=True)
class Compound:
    kind: str
    body: Program
    headers: tuple[Word, ...] = ()


@dataclass(frozen=True)
class Program:
    commands: tuple[SimpleCommand | Compound, ...]


_OPERATORS = (";;&", ";;", ";&", "&&", "||", "|&", ";", "&", "|", "(", ")", "\n")
_REDIRECT = re.compile(r"([0-9]*)(<<<|<<-|<<|&>>|&>|>>|<>|<&|>&|>\||<|>)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class DelimiterContext(Enum):
    PROGRAM = auto()
    WORD_LIST = auto()
    PARAMETER = auto()
    ARITHMETIC = auto()


@dataclass
class Lexer:
    source: str
    position: int = 0
    line: int = 1
    pending: list[HereDocument] = field(default_factory=list)

    def advance(self, count: int = 1) -> str:
        text = self.source[self.position:self.position + count]
        self.position += count
        self.line += text.count("\n")
        return text

    def balanced(self, opening: str, closing: str, *, context: DelimiterContext) -> str:
        """Read a balanced expansion without losing its inner quote context."""
        start = self.position
        depth = 1
        quote = ""
        documents: list[HereDocument] = []
        while self.position < len(self.source):
            char = self.source[self.position]
            if char == "\\" and quote != "'":
                _ = self.advance(2)
                continue
            if quote == "'":
                if char == "'":
                    quote = ""
                _ = self.advance()
                continue
            if not quote and context in {DelimiterContext.PROGRAM, DelimiterContext.WORD_LIST} and char == "#" and (self.position == start or self.source[self.position - 1].isspace()):
                while self.position < len(self.source) and self.source[self.position] != "\n":
                    _ = self.advance()
                continue
            if not quote and context is DelimiterContext.PROGRAM and self.source.startswith("<<<", self.position):
                _ = self.advance(3)
                continue
            if not quote and context is DelimiterContext.PROGRAM and opening == "(" and self.source.startswith("<<", self.position) and not self.source.startswith("<<<", self.position):
                tabs = self.source.startswith("<<-", self.position)
                _ = self.advance(3 if tabs else 2)
                while self.position < len(self.source) and self.source[self.position] in " \t":
                    _ = self.advance()
                delimiter = self.word()
                literal = delimiter.literal()
                if literal is None:
                    raise ShellSyntaxError("dynamic heredoc delimiter")
                documents.append(HereDocument(literal, any(isinstance(part, Literal) and part.quoted for part in delimiter.segments), tabs))
                continue
            if char == "\n" and documents:
                _ = self.advance()
                reader = Lexer(self.source, self.position, self.line, documents)
                reader.documents()
                self.position, self.line = reader.position, reader.line
                documents = []
                continue
            if self.source.startswith("${", self.position):
                _ = self.advance(2)
                _ = self.balanced("{", "}", context=DelimiterContext.PARAMETER)
                continue
            if self.source.startswith(("$(", "<(", ">("), self.position) and (
                    self.source.startswith("$(", self.position) or not quote and context in {
                        DelimiterContext.PROGRAM, DelimiterContext.WORD_LIST}):
                _ = self.expansion()
                continue
            if char == '`':
                _ = self.advance()
                _ = self.backtick()
                continue
            if char in "\"'":
                if not quote:
                    quote = char
                elif quote == char:
                    quote = ""
                _ = self.advance()
                continue
            if not quote:
                if char == opening:
                    depth += 1
                elif char == closing:
                    if depth == 1 and context is DelimiterContext.PROGRAM and opening == "(":
                        # A case-pattern ')' belongs to the case production,
                        # not to this substitution. Only a complete program
                        # can precede its closing delimiter.
                        try:
                            _ = parse(self.source[start:self.position])
                        except ShellSyntaxError:
                            _ = self.advance()
                            continue
                    depth -= 1
                    if depth == 0:
                        end = self.position
                        _ = self.advance()
                        return self.source[start:end]
            _ = self.advance()
        raise ShellSyntaxError("unterminated expansion")

    def backtick(self) -> str:
        body: list[str] = []
        while self.position < len(self.source):
            char = self.advance()
            if char == '`':
                return "".join(body)
            if char == "\\" and self.position < len(self.source) and self.source[self.position] in "`$\\":
                char = self.advance()
            body.append(char)
        raise ShellSyntaxError("unterminated backtick substitution")

    def expansion(self, *, process: bool = True) -> Literal | Parameter | Execution:
        if self.source.startswith(("$(", "<(", ">("), self.position):
            arithmetic = self.source.startswith("$((", self.position)
            _ = self.advance(2)
            body = self.balanced("(", ")", context=DelimiterContext.ARITHMETIC if arithmetic else DelimiterContext.PROGRAM)
            if arithmetic:
                return Parameter("arithmetic", expansions(body, process=process))
            return Execution(parse(body))
        if self.source[self.position] == '`':
            _ = self.advance()
            return Execution(parse(self.backtick()))
        _ = self.advance()  # $
        if self.source.startswith("{", self.position):
            _ = self.advance()
            body = self.balanced("{", "}", context=DelimiterContext.PARAMETER)
            return Parameter(body, expansions(body, process=process))
        match = _NAME.match(self.source, self.position)
        if match:
            _ = self.advance(len(match[0]))
            return Parameter(match[0])
        if self.position < len(self.source) and self.source[self.position] in "0123456789?@*#!-$":
            return Parameter(self.advance())
        return Literal("$")

    def ansi_quote(self) -> str:
        """Decode Bash ANSI-C quotes as literal text, never executable syntax."""
        _ = self.advance(2)
        literal: list[str] = []
        escapes = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                   "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
        while self.position < len(self.source):
            char = self.advance()
            if char == "'":
                return "".join(literal)
            if char != "\\":
                literal.append(char)
                continue
            if self.position == len(self.source):
                break
            char = self.advance()
            if char in escapes:
                literal.append(escapes[char])
            elif char == "\n":
                continue
            elif char in "01234567xuU":
                width = {"x": 2, "u": 4, "U": 8}.get(char, 2)
                digits = "" if char in "xuU" else char
                alphabet = "0123456789abcdefABCDEF" if char in "xuU" else "01234567"
                while width and self.position < len(self.source) and self.source[self.position] in alphabet:
                    digits += self.advance()
                    width -= 1
                if not digits:
                    raise ShellSyntaxError("invalid ANSI-C escape")
                try:
                    literal.append(chr(int(digits, 16 if char in "xuU" else 8)))
                except ValueError as error:
                    raise ShellSyntaxError("invalid ANSI-C code point") from error
            else:
                raise ShellSyntaxError("unsupported ANSI-C escape")
        raise ShellSyntaxError("unterminated ANSI-C quote")

    def word(self) -> Word:
        segments: list[Literal | Parameter | Execution | ArrayElements] = []
        line = self.line
        quote = ""
        literal: list[str] = []

        def flush() -> None:
            if literal:
                segments.append(Literal("".join(literal), bool(quote)))
                literal.clear()

        while self.position < len(self.source):
            char = self.source[self.position]
            if quote == "'":
                if char == "'":
                    flush()
                    quote = ""
                    _ = self.advance()
                else:
                    literal.append(self.advance())
                continue
            if not quote and self.source.startswith("$'", self.position):
                flush()
                segments.append(Literal(self.ansi_quote(), True))
                continue
            if not quote and self.source.startswith('$"', self.position):
                _ = self.advance()
                continue
            if char == "\\":
                flush()
                _ = self.advance()
                if self.position == len(self.source):
                    raise ShellSyntaxError("trailing escape")
                escaped = self.advance()
                if escaped != "\n":
                    if quote == '"' and escaped not in '$`"\\':
                        escaped = "\\" + escaped
                    segments.append(Literal(escaped, True))
                continue
            if char in "\"'" and (not quote or char == quote):
                flush()
                _ = self.advance()
                quote = "" if quote else char
                # Preserve empty quoting, including a quoted heredoc delimiter.
                segments.append(Literal("", True))
                continue
            if char in "$`" or (not quote and self.source.startswith(("<(", ">("), self.position)):
                flush()
                segments.append(self.expansion())
                continue
            if not quote and (char.isspace() or char in ";&|()<>"):
                break
            literal.append(self.advance())
        if quote:
            raise ShellSyntaxError("unterminated quote")
        flush()
        if not segments:
            raise ShellSyntaxError("expected word")
        return Word(tuple(segments), line)

    def documents(self) -> None:
        for document in self.pending:
            lines: list[str] = []
            while self.position < len(self.source):
                end = self.source.find("\n", self.position)
                end = len(self.source) if end < 0 else end + 1
                line = self.advance(end - self.position)
                compared = line.lstrip("\t") if document.strip_tabs else line
                if compared.rstrip("\n") == document.delimiter:
                    document.body = "".join(lines)
                    document.segments = () if document.quoted else embedded_segments(document.body, process=False)
                    break
                lines.append(compared)
            else:
                raise ShellSyntaxError("unterminated heredoc")
        self.pending.clear()

    def tokens(self) -> list[Word | Operator | Redirection]:
        result: list[Word | Operator | Redirection] = []
        while self.position < len(self.source):
            char = self.source[self.position]
            if char in " \t\r":
                _ = self.advance()
                continue
            if self.source.startswith("\\\n", self.position):
                _ = self.advance(2)
                continue
            if char == "#":
                while self.position < len(self.source) and self.source[self.position] != "\n":
                    _ = self.advance()
                continue
            redirect = _REDIRECT.match(self.source, self.position)
            if redirect and not self.source.startswith(("<(", ">("), self.position):
                _ = self.advance(len(redirect[0]))
                while self.position < len(self.source) and self.source[self.position] in " \t":
                    _ = self.advance()
                target = self.word()
                operator = redirect[2]
                descriptor = int(redirect[1]) if redirect[1] else (0 if operator.startswith("<") else 1)
                document: HereDocument | NoHereDocument = NoHereDocument()
                if operator in {"<<", "<<-"}:
                    delimiter = target.literal()
                    if delimiter is None:
                        raise ShellSyntaxError("dynamic heredoc delimiter")
                    document = HereDocument(delimiter, any(isinstance(part, Literal) and part.quoted
                                                          for part in target.segments), operator == "<<-")
                    self.pending.append(document)
                result.append(Redirection(descriptor, operator, target, document))
                continue
            operator = next((op for op in _OPERATORS if self.source.startswith(op, self.position)), "")
            if operator:
                result.append(Operator(operator, self.line))
                _ = self.advance(len(operator))
                if operator == "\n" and self.pending:
                    self.documents()
                continue
            word = self.word()
            if word.literal() is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]+\])?\+?=", word.literal() or "") and self.source.startswith("(", self.position):
                _ = self.advance()
                inner = Lexer(self.balanced("(", ")", context=DelimiterContext.WORD_LIST)).tokens()
                if any(not isinstance(item, Word) and not (isinstance(item, Operator) and item.text == "\n") for item in inner):
                    raise ShellSyntaxError("unsupported array expression")
                word = Word((*word.segments, ArrayElements(tuple(item for item in inner if isinstance(item, Word)))), word.line)
            result.append(word)
        if self.pending:
            self.documents()
        return result


def embedded_segments(text: str, *, process: bool) -> tuple[Literal | Parameter | Execution, ...]:
    """Expansion segments in a parameter/arithmetic or heredoc context."""
    lexer = Lexer(text)
    segments: list[Literal | Parameter | Execution] = []
    while lexer.position < len(text):
        if text[lexer.position] == "\\":
            _ = lexer.advance(2)
        elif text[lexer.position] in "$`" or (process and text.startswith(("<(", ">("), lexer.position)):
            segments.append(lexer.expansion(process=process))
        else:
            _ = lexer.advance()
    return tuple(segments)


def _programs(segments: tuple[Literal | Parameter | Execution, ...]) -> tuple[Program, ...]:
    return tuple(program for segment in segments
                 for program in ((segment.program,) if isinstance(segment, Execution)
                                 else segment.programs if isinstance(segment, Parameter) else ()))


def expansions(text: str, *, process: bool = True) -> tuple[Program, ...]:
    return _programs(embedded_segments(text, process=process))


@dataclass
class Parser:
    tokens: list[Word | Operator | Redirection]
    position: int = 0

    def at(self, *values: str) -> bool:
        if self.position == len(self.tokens):
            return False
        token = self.tokens[self.position]
        return (isinstance(token, Operator) and token.text in values
                or isinstance(token, Word) and any(token.keyword(value) for value in values))

    def consume(self, value: str) -> None:
        if not self.at(value):
            raise ShellSyntaxError(f"expected {value}")
        self.position += 1

    def program(self, stops: tuple[str, ...] = ()) -> Program:
        commands: list[SimpleCommand | Compound] = []
        while self.position < len(self.tokens) and not self.at(*stops):
            if self.at(";", "&", "&&", "||", "|", "|&", "\n"):
                self.position += 1
                continue
            commands.append(self.command())
        return Program(tuple(commands))

    def command(self) -> SimpleCommand | Compound:
        if self.at("(", "{"):
            subshell = self.at("(")
            self.position += 1
            closing = ")" if subshell else "}"
            body = self.program((closing,))
            self.consume(closing)
            return Compound("subshell" if subshell else "group", body)
        if self.at("if"):
            self.position += 1
            commands = list(self.program(("then",)).commands)
            self.consume("then")
            commands.extend(self.program(("elif", "else", "fi")).commands)
            while self.at("elif"):
                self.position += 1
                commands.extend(self.program(("then",)).commands)
                self.consume("then")
                commands.extend(self.program(("elif", "else", "fi")).commands)
            if self.at("else"):
                self.position += 1
                commands.extend(self.program(("fi",)).commands)
            self.consume("fi")
            return Compound("if", Program(tuple(commands)))
        if self.at("while", "until", "for", "select"):
            kind = self.tokens[self.position]
            self.position += 1
            if isinstance(kind, Word) and kind.literal() in {"for", "select"}:
                headers: list[Word] = []
                while self.position < len(self.tokens) and not self.at("do"):
                    token = self.tokens[self.position]
                    self.position += 1
                    if isinstance(token, Word):
                        headers.append(token)
                self.consume("do")
                body = self.program(("done",))
                self.consume("done")
                return Compound("for", body, tuple(headers))
            commands = list(self.program(("do",)).commands)
            self.consume("do")
            commands.extend(self.program(("done",)).commands)
            self.consume("done")
            return Compound("loop", Program(tuple(commands)))
        if self.at("case"):
            self.position += 1
            headers = []
            while self.position < len(self.tokens) and not self.at("in"):
                token = self.tokens[self.position]
                self.position += 1
                if isinstance(token, Word):
                    headers.append(token)
            self.consume("in")
            commands: list[SimpleCommand | Compound] = []
            while not self.at("esac"):
                while self.at("\n", ";;", ";&", ";;&"):
                    self.position += 1
                if self.at("esac"):
                    break
                while self.position < len(self.tokens) and not self.at(")"):
                    token = self.tokens[self.position]
                    self.position += 1
                    if isinstance(token, Word):
                        headers.append(token)
                self.consume(")")
                commands.extend(self.program((";;", ";&", ";;&", "esac")).commands)
            self.consume("esac")
            return Compound("case", Program(tuple(commands)), tuple(headers))
        if self.at("function"):
            self.position += 1
            if self.position == len(self.tokens) or not isinstance(self.tokens[self.position], Word):
                raise ShellSyntaxError("expected function name")
            self.position += 1
            if self.at("("):
                self.consume("(")
                self.consume(")")
            while self.at("\n"):
                self.position += 1
            return Compound("function", Program((self.command(),)))
        if self.position + 2 < len(self.tokens) and isinstance(self.tokens[self.position], Word):
            left, right = self.tokens[self.position + 1:self.position + 3]
            if isinstance(left, Operator) and left.text == "(" and isinstance(right, Operator) and right.text == ")":
                self.position += 3
                while self.at("\n"):
                    self.position += 1
                return Compound("function", Program((self.command(),)))
        if self.at("!", "time", "coproc"):
            coproc = self.at("coproc")
            self.position += 1
            if self.at("-p"):
                self.position += 1
            if coproc and self.position + 1 < len(self.tokens) and isinstance(self.tokens[self.position], Word):
                following = self.tokens[self.position + 1]
                if isinstance(following, Word) and following.keyword("{"):
                    self.position += 1
            return Compound("prefix", Program((self.command(),)))
        words: list[Word] = []
        redirects: list[Redirection] = []
        while self.position < len(self.tokens):
            token = self.tokens[self.position]
            if isinstance(token, Operator):
                break
            if isinstance(token, Word) and token.keyword("}"):
                break
            self.position += 1
            if isinstance(token, Redirection):
                redirects.append(token)
            else:
                words.append(token)
        if not words and not redirects:
            raise ShellSyntaxError("unexpected shell operator")
        return SimpleCommand(tuple(words), tuple(redirects))


def parse(source: str) -> Program:
    parser = Parser(Lexer(source).tokens())
    program = parser.program()
    if parser.position != len(parser.tokens):
        raise ShellSyntaxError("unexpected shell suffix")
    return program
