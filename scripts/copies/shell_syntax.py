"""A nonexecuting shell grammar retaining quotes, expansions and command structure.

This is a policy parser, not an evaluator: parameter values and command outputs
remain explicit segments. Unsupported executable syntax is a parse error.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
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
class LiteralWord:
    text: str


@dataclass(frozen=True)
class ExpandedWord:
    text: str


UNKNOWN_VALUE = "__copy_guard_unknown_value__"


@dataclass(frozen=True)
class Word:
    segments: tuple[Literal | Parameter | Execution | ArrayElements, ...]
    line: int

    def literal(self) -> LiteralWord | ExpandedWord:
        if all(isinstance(segment, Literal) for segment in self.segments):
            return LiteralWord("".join(segment.text for segment in self.segments if isinstance(segment, Literal)))
        return ExpandedWord("".join(segment.text if isinstance(segment, Literal) else UNKNOWN_VALUE
                                    for segment in self.segments))

    def keyword(self, text: str) -> bool:
        offset = 0
        for segment in self.segments:
            if not isinstance(segment, Literal) or segment.quoted or not text.startswith(segment.text, offset):
                return False
            offset += len(segment.text)
        return offset == len(text)


class HereDocumentState(Enum):
    PENDING = auto()
    COMPLETE = auto()
    ABANDONED = auto()


@dataclass
class HereDocument:
    delimiter: str
    quoted: bool
    strip_tabs: bool
    body: str = ""
    segments: tuple[Literal | Parameter | Execution, ...] = ()
    state: HereDocumentState = field(default=HereDocumentState.PENDING, init=False)

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


class ShellTokenError(ShellSyntaxError):
    """The lexer could not read the next token."""


def alternatives(programs: tuple[Program, ...]) -> Compound:
    """Keep each possible branch separate from the surrounding sequence."""
    return Compound("alternatives", Program(tuple(Compound("branch", program) for program in programs)))


_OPERATORS = (";;&", ";;", ";&", "&&", "||", "|&", ";", "&", "|", "(", ")", "\n")
_COMMAND_LIST_SEPARATORS = (";", "&", "|", "|&", "\n", "&&", "||")
_REDIRECT = re.compile(r"<<<|<<-|<<|&>>|&>|>>|<>|<&|>&|>\||<|>")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


MAX_SHELL_WORK = 131_072
MAX_SYNTAX_DEPTH = 32


class ShellWorkLimit(ValueError):
    """The shell reader reached a work or nesting limit."""


@dataclass
class ShellBudget:
    remaining: int = MAX_SHELL_WORK
    depth: int = field(default=0, init=False)

    def charge(self, work: int = 1) -> None:
        if work > self.remaining:
            self.remaining = 0
            raise ShellWorkLimit("the shell target scan reached its work limit")
        self.remaining -= work

    @contextmanager
    def nested(self) -> Generator[None]:
        if self.depth >= MAX_SYNTAX_DEPTH:
            raise ShellWorkLimit("the shell target scan reached its nesting limit")
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


Token = Word | Operator | Redirection


class ShellGrammar(Enum):
    COPY_COMMAND_LIST = auto()
    SHOWRUNNER_PIPELINE = auto()


@dataclass
class Lexer:
    source: str
    budget: ShellBudget
    grammar: ShellGrammar
    position: int = 0
    line: int = 1
    pending: deque[HereDocument] = field(default_factory=deque)
    documents_due: bool = False
    document_completed: Callable[[], None] | None = None

    def advance(self, count: int = 1) -> str:
        count = min(count, len(self.source) - self.position)
        self.budget.charge(count)
        text = self.source[self.position:self.position + count]
        self.position += count
        self.line += text.count("\n")
        return text

    def delimited_segments(self, opening: str, closing: str, *, process: bool,
                           word_list: bool = False) -> tuple[tuple[Program, ...], int]:
        """Read the closing delimiter and the expansions inside it."""
        start = self.position
        depth = 1
        quote = ""
        replay_embedded = False
        copy_embedded = self.grammar is ShellGrammar.COPY_COMMAND_LIST and not word_list
        segments: list[Literal | Parameter | Execution] = []
        while self.position < len(self.source):
            char = self.source[self.position]
            if char == "\\" and quote != "'":
                _ = self.advance(2)
                continue
            if (word_list and not quote and char == "#"
                    and (self.position == start or self.source[self.position - 1].isspace())):
                while self.position < len(self.source) and self.source[self.position] != "\n":
                    _ = self.advance()
                continue
            process_start = process and self.source.startswith(("<(", ">("), self.position)
            if copy_embedded and (process_start or quote == "'" and char in "$`"):
                replay_embedded = True
            if char in "\"'":
                if not quote:
                    quote = char
                elif quote == char:
                    quote = ""
            elif quote != "'" and (char in "$`" or process_start and not quote and not copy_embedded):
                segments.append(self.expansion(process=process))
                continue
            elif not quote:
                if char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
                    if depth == 0:
                        end = self.position
                        _ = self.advance()
                        if replay_embedded:
                            # Main scans this bounded text without quote context.
                            self.budget.charge(end - start)
                            segments = list(Lexer(self.source[start:end], self.budget, self.grammar)
                                            .embedded(process=process))
                        return _programs(tuple(segments)), end
            _ = self.advance()
        raise ShellSyntaxError("unterminated expansion")

    def backtick(self) -> str:
        body: list[str] = []
        while self.position < len(self.source):
            char = self.advance()
            if char == '`':
                self.budget.charge(len(body))
                return "".join(body)
            if char == "\\" and self.position < len(self.source) and self.source[self.position] in "`$\\":
                char = self.advance()
            body.append(char)
        raise ShellSyntaxError("unterminated backtick substitution")

    def expansion(self, *, process: bool = True) -> Literal | Parameter | Execution:
        self.budget.charge()
        if self.source.startswith(("$(", "<(", ">("), self.position):
            arithmetic = self.source.startswith("$((", self.position)
            _ = self.advance(2)
            with self.budget.nested():
                if arithmetic:
                    programs, _ = self.delimited_segments("(", ")", process=process)
                    return Parameter("arithmetic", programs)
                # The nested parser consumes its closing parenthesis.
                pending, self.pending = self.pending, deque()
                due, self.documents_due = self.documents_due, False
                try:
                    parser = Parser(self)
                    program = parser.program((")",))
                    parser.consume(")")
                    if self.pending:
                        raise ShellSyntaxError("unterminated heredoc")
                finally:
                    self.pending = pending
                    self.documents_due = due
                return Execution(program)
        if self.source[self.position] == '`':
            _ = self.advance()
            with self.budget.nested():
                return Execution(parse(self.backtick(), grammar=self.grammar, budget=self.budget))
        _ = self.advance()
        if self.source.startswith("{", self.position):
            _ = self.advance()
            start = self.position
            with self.budget.nested():
                programs, end = self.delimited_segments("{", "}", process=process)
            self.budget.charge(end - start)
            return Parameter(self.source[start:end], programs)
        end = self.position + self.budget.remaining
        match = _NAME.match(self.source, self.position, end)
        if match:
            if match.end() == end and end < len(self.source) and _NAME.fullmatch("a" + self.source[end]):
                self.budget.charge(self.budget.remaining + 1)
            return Parameter(self.advance(match.end() - self.position))
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
                self.budget.charge(len(literal))
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
                self.budget.charge(len(literal) + 1)
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
                self.budget.charge()
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
                    self.budget.charge()
                    segments.append(Literal(escaped, True))
                continue
            if char in "\"'" and (not quote or char == quote):
                flush()
                _ = self.advance()
                quote = "" if quote else char
                # Preserve empty quoting, including a quoted heredoc delimiter.
                self.budget.charge()
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
        self.budget.charge()
        return Word(tuple(segments), line)

    def document(self, document: HereDocument) -> None:
        lines: list[str] = []
        while self.position < len(self.source):
            limit = self.position + self.budget.remaining
            end = self.source.find("\n", self.position, limit)
            end = len(self.source) if end < 0 else end + 1
            line = self.advance(end - self.position)
            compared = line.lstrip("\t") if document.strip_tabs else line
            if compared.rstrip("\n") == document.delimiter:
                self.budget.charge(sum(map(len, lines)))
                document.body = "".join(lines)
                document.state = HereDocumentState.COMPLETE
                if not document.quoted:
                    document.segments = Lexer(document.body, self.budget, self.grammar).embedded()
                return
            lines.append(compared)
        raise ShellSyntaxError("unterminated heredoc")

    def next_token(self) -> Token | None:
        while self.documents_due and self.pending:
            self.document(self.pending[0])
            _ = self.pending.popleft()
            if self.document_completed is not None:
                self.document_completed()
        self.documents_due = False
        return self.read()

    def embedded(self, *, process: bool = False) -> tuple[Literal | Parameter | Execution, ...]:
        segments: list[Literal | Parameter | Execution] = []
        start = self.position
        while self.position < len(self.source):
            char = self.source[self.position]
            if char == "\\":
                _ = self.advance(2)
            elif char in "$`" or process and self.source.startswith(("<(", ">("), self.position):
                if start < self.position:
                    self.budget.charge()
                    segments.append(Literal(self.source[start:self.position]))
                segments.append(self.expansion(process=process))
                start = self.position
            else:
                _ = self.advance()
        if start < self.position:
            self.budget.charge()
            segments.append(Literal(self.source[start:self.position]))
        return tuple(segments)

    def array(self) -> ArrayElements:
        words: list[Word] = []
        if self.grammar is ShellGrammar.COPY_COMMAND_LIST:
            with self.budget.nested():
                start = self.position
                _, end = self.delimited_segments("(", ")", process=True, word_list=True)
                self.budget.charge(end - start)
                # The array lexer owns its newlines and heredocs.
                reader = Lexer(self.source[start:end], self.budget, self.grammar)
                while (token := reader.next_token()) is not None:
                    if isinstance(token, Word):
                        words.append(token)
                    elif not isinstance(token, Operator) or token.text != "\n":
                        raise ShellSyntaxError("unsupported array expression")
                self.budget.charge(len(words) + 1)
                return ArrayElements(tuple(words))
        with self.budget.nested():
            while (token := self.next_token()) is not None:
                if isinstance(token, Word):
                    words.append(token)
                elif isinstance(token, Operator) and token.text == ")":
                    self.budget.charge(len(words) + 1)
                    return ArrayElements(tuple(words))
                elif not isinstance(token, Operator) or token.text != "\n":
                    raise ShellSyntaxError("unsupported array expression")
        raise ShellSyntaxError("unterminated array expression")

    def redirect(self, operator: str, digits: int) -> Redirection:
        descriptor_text = self.advance(digits)
        _ = self.advance(len(operator))
        while self.position < len(self.source) and self.source[self.position] in " \t":
            _ = self.advance()
        target = self.word()
        try:
            descriptor = int(descriptor_text) if descriptor_text else (0 if operator.startswith("<") else 1)
        except ValueError as error:
            if self.grammar is ShellGrammar.COPY_COMMAND_LIST:
                raise
            raise ShellSyntaxError("invalid redirection descriptor") from error
        document: HereDocument | NoHereDocument = NoHereDocument()
        if operator in {"<<", "<<-"}:
            delimiter = target.literal()
            if isinstance(delimiter, ExpandedWord):
                raise ShellSyntaxError("dynamic heredoc delimiter")
            self.budget.charge()
            document = HereDocument(delimiter.text, any(isinstance(part, Literal) and part.quoted
                                                       for part in target.segments), operator == "<<-")
            self.pending.append(document)
        return Redirection(descriptor, operator, target, document)

    def read(self) -> Token | None:
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
            self.budget.charge()
            end = self.position
            while end < len(self.source) and self.source[end] in "0123456789":
                self.budget.charge()
                end += 1
            redirect = _REDIRECT.match(self.source, end)
            if redirect and not self.source.startswith(("<(", ">("), self.position):
                return self.redirect(redirect[0], end - self.position)
            operator = next((op for op in _OPERATORS if self.source.startswith(op, self.position)), "")
            if operator:
                token = Operator(operator, self.line)
                _ = self.advance(len(operator))
                if operator == "\n":
                    self.documents_due = True
                return token
            word = self.word()
            if self.source.startswith("(", self.position):
                value = word.literal()
                if isinstance(value, LiteralWord) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]+\])?\+?=", value.text):
                    _ = self.advance()
                    array = self.array()
                    self.budget.charge(len(word.segments) + 1)
                    word = Word((*word.segments, array), word.line)
            return word
        if self.pending:
            raise ShellSyntaxError("unterminated heredoc")
        return None


def expanded_text(document: HereDocument, budget: ShellBudget) -> str:
    """Render the parsed heredoc with unknown values for expansions."""
    parts: list[str] = []
    for segment in document.segments:
        part = segment.text if isinstance(segment, Literal) else UNKNOWN_VALUE
        budget.charge(len(part))
        parts.append(part)
    return "".join(parts)


def _programs(segments: tuple[Literal | Parameter | Execution, ...]) -> tuple[Program, ...]:
    return tuple(program for segment in segments
                 for program in ((segment.program,) if isinstance(segment, Execution)
                                 else segment.programs if isinstance(segment, Parameter) else ()))


@dataclass
class Parser:
    lexer: Lexer
    position: int = 0
    tokens: list[Token] = field(default_factory=list, init=False)

    def peek(self, offset: int = 0) -> Token | None:
        self.lexer.budget.charge()
        while self.position + offset >= len(self.tokens):
            try:
                token = self.lexer.next_token()
            except ShellSyntaxError as error:
                raise ShellTokenError(str(error)) from error
            if token is None:
                return None
            self.tokens.append(token)
        return self.tokens[self.position + offset]

    def at(self, *values: str) -> bool:
        token = self.peek()
        return (isinstance(token, Operator) and token.text in values
                or isinstance(token, Word) and any(token.keyword(value) for value in values))

    def consume(self, value: str) -> None:
        if not self.at(value):
            raise ShellSyntaxError(f"expected {value}")
        self.position += 1

    def program(self, stops: tuple[str, ...] = ()) -> Program:
        self.lexer.budget.charge()
        return Program(tuple(self.commands(stops)))

    def commands(self, stops: tuple[str, ...] = (), *,
                 begin_recovery: Callable[[], None] | None = None) -> Iterator[SimpleCommand | Compound]:
        if self.lexer.grammar is ShellGrammar.COPY_COMMAND_LIST:
            yield from self.command_list(stops, begin_recovery)
        else:
            yield from self.pipelines(stops)

    def command_list(self, stops: tuple[str, ...], begin_recovery: Callable[[], None] | None
                     ) -> Iterator[SimpleCommand | Compound]:
        chain: list[SimpleCommand | Compound] = []
        conditional = False
        start = self.position
        try:
            while True:
                start = self.position
                if self.peek() is None or (stops and self.at(*stops)):
                    break
                if self.at(*_COMMAND_LIST_SEPARATORS):
                    conditional = conditional or self.at("&&", "||")
                    self.position += 1
                    yield from chain
                    chain.clear()
                    continue
                command = self.command()
                if conditional:
                    self.lexer.budget.charge(7)
                    command = alternatives((Program(()), Program((command,))))
                chain.append(command)
                conditional = False
        except ShellSyntaxError as error:
            if begin_recovery is not None:
                begin_recovery()
                yield from chain
                self.position = start if isinstance(error, ShellTokenError) else start + 1
            raise
        yield from chain

    def pipelines(self, stops: tuple[str, ...]) -> Iterator[SimpleCommand | Compound]:
        chain: list[SimpleCommand | Compound] = []
        conditional = False
        while self.peek() is not None and not (stops and self.at(*stops)):
            if self.at("&&", "||"):
                conditional = True
                self.position += 1
                continue
            if self.at(";", "&", "\n"):
                self.position += 1
                separator = self.tokens[self.position - 1]
                if conditional and isinstance(separator, Operator) and separator.text == "\n":
                    continue
                if isinstance(separator, Operator) and separator.text == "&" and chain:
                    self.lexer.budget.charge(len(chain) + 2)
                    yield Compound("background", Program(tuple(chain)))
                else:
                    yield from chain
                chain.clear()
                continue
            command = self.command()
            if conditional:
                self.lexer.budget.charge(7)
                command = alternatives((Program(()), Program((command,))))
            stages = [command]
            while self.at("|", "|&"):
                self.position += 1
                while self.at("\n"):
                    self.position += 1
                if self.peek() is None or (stops and self.at(*stops)):
                    raise ShellSyntaxError("expected a pipeline command")
                stages.append(self.command())
            if len(stages) > 1:
                self.lexer.budget.charge(len(stages) + 2)
                command = Compound("pipeline", Program(tuple(stages)))
            chain.append(command)
            conditional = False
        yield from chain

    def command(self) -> SimpleCommand | Compound:
        with self.lexer.budget.nested():
            self.lexer.budget.charge()
            return self.command_body()

    def command_body(self) -> SimpleCommand | Compound:
        if self.at("[["):
            self.position += 1
            headers: list[Word] = []
            while (token := self.peek()) is not None and not self.at("]]"):
                self.position += 1
                if isinstance(token, Word):
                    headers.append(token)
                elif isinstance(token, Redirection):
                    headers.append(token.target)
            self.consume("]]")
            return Compound("test", Program(()), tuple(headers))
        if self.at("(", "{"):
            subshell = self.at("(")
            self.position += 1
            closing = ")" if subshell else "}"
            body = self.program((closing,))
            self.consume(closing)
            return Compound("subshell" if subshell else "group", body)
        if self.at("if"):
            self.position += 1
            conditions = list(self.program(("then",)).commands)
            self.consume("then")
            branches = [self.branch(conditions, self.program(("elif", "else", "fi")))]
            while self.at("elif"):
                self.position += 1
                conditions.extend(self.program(("then",)).commands)
                self.consume("then")
                branches.append(self.branch(conditions, self.program(("elif", "else", "fi"))))
            if self.at("else"):
                self.position += 1
                branches.append(self.branch(conditions, self.program(("fi",))))
            else:
                branches.append(self.branch(conditions, Program(())))
            self.consume("fi")
            self.lexer.budget.charge(len(branches) + 2)
            return alternatives(tuple(branches))
        if self.at("while", "until", "for", "select"):
            kind = self.peek()
            self.position += 1
            if isinstance(kind, Word) and (kind.keyword("for") or kind.keyword("select")):
                headers = []
                while (token := self.peek()) is not None and not self.at("do"):
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
            while (token := self.peek()) is not None and not self.at("in"):
                self.position += 1
                if isinstance(token, Word):
                    headers.append(token)
            self.consume("in")
            branches = [Program(())]
            while not self.at("esac"):
                while self.at("\n", ";;", ";&", ";;&"):
                    self.position += 1
                if self.at("esac"):
                    break
                while (token := self.peek()) is not None and not self.at(")"):
                    self.position += 1
                    if isinstance(token, Word):
                        headers.append(token)
                self.consume(")")
                branches.append(self.program((";;", ";&", ";;&", "esac")))
            self.consume("esac")
            self.lexer.budget.charge(len(branches) + 3)
            return Compound("case", Program((alternatives(tuple(branches)),)), tuple(headers))
        if self.at("function"):
            self.position += 1
            if not isinstance(self.peek(), Word):
                raise ShellSyntaxError("expected function name")
            self.position += 1
            if self.at("("):
                self.consume("(")
                self.consume(")")
            while self.at("\n"):
                self.position += 1
            return Compound("function", Program((self.command(),)))
        if isinstance(self.peek(), Word):
            left = self.peek(1)
            if isinstance(left, Operator) and left.text == "(" and (
                isinstance(right := self.peek(2), Operator) and right.text == ")"
            ):
                self.position += 3
                while self.at("\n"):
                    self.position += 1
                return Compound("function", Program((self.command(),)))
        if self.at("!", "time", "coproc"):
            coproc = self.at("coproc")
            self.position += 1
            if self.at("-p"):
                self.position += 1
            if coproc and isinstance(self.peek(), Word):
                following = self.peek(1)
                if isinstance(following, Word) and following.keyword("{"):
                    self.position += 1
            return Compound("background" if coproc else "prefix", Program((self.command(),)))
        words: list[Word] = []
        redirects: list[Redirection] = []
        while (token := self.peek()) is not None:
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

    def branch(self, conditions: list[SimpleCommand | Compound], body: Program) -> Program:
        self.lexer.budget.charge(len(conditions) + len(body.commands) + 1)
        return Program((*conditions, *body.commands))


def parse(source: str, *, grammar: ShellGrammar = ShellGrammar.COPY_COMMAND_LIST,
          budget: ShellBudget | None = None) -> Program:
    # The top-level program consumes all tokens or raises.
    return Parser(Lexer(source, budget if budget is not None else ShellBudget(), grammar)).program()


def _command_documents(command: SimpleCommand | Compound) -> Iterator[HereDocument]:
    if isinstance(command, Compound):
        for child in command.body.commands:
            yield from _command_documents(child)
    else:
        for redirect in command.redirects:
            if isinstance(redirect.document, HereDocument) and redirect.document.state is HereDocumentState.PENDING:
                yield redirect.document


@dataclass
class PendingCommand:
    command: SimpleCommand | Compound
    documents: deque[HereDocument]

    def ready(self) -> bool:
        while self.documents and self.documents[0].state is not HereDocumentState.PENDING:
            _ = self.documents.popleft()
        return not self.documents


@dataclass
class CommandDelivery:
    consume: Callable[[SimpleCommand | Compound], None]
    waiting: deque[PendingCommand] = field(default_factory=deque)

    def queue(self, command: SimpleCommand | Compound) -> None:
        self.waiting.append(PendingCommand(command, deque(_command_documents(command))))
        self.deliver_ready()

    def deliver_ready(self) -> None:
        while self.waiting and self.waiting[0].ready():
            self.consume(self.waiting.popleft().command)


def read_commands(source: str, budget: ShellBudget, consume: Callable[[SimpleCommand | Compound], None]) -> None:
    """Deliver complete top-level commands in source order."""
    delivery = CommandDelivery(consume)
    lexer = Lexer(source, budget, ShellGrammar.SHOWRUNNER_PIPELINE, document_completed=delivery.deliver_ready)
    for command in Parser(lexer).commands():
        delivery.queue(command)


class CommandDeliveryPhase(Enum):
    BEFORE_ERROR = auto()
    RECOVERY = auto()


def readable_program(source: str, budget: ShellBudget,
                     consume: Callable[[SimpleCommand | Compound, CommandDeliveryPhase], None]) -> None:
    """Deliver complete commands and recover from the first syntax error."""
    phase = CommandDeliveryPhase.BEFORE_ERROR

    def begin_recovery() -> None:
        nonlocal phase
        phase = CommandDeliveryPhase.RECOVERY

    def deliver(command: SimpleCommand | Compound) -> None:
        consume(command, phase)

    delivery = CommandDelivery(deliver)
    lexer = Lexer(source, budget, ShellGrammar.COPY_COMMAND_LIST, document_completed=delivery.deliver_ready)
    parser = Parser(lexer)
    try:
        for command in parser.commands(begin_recovery=begin_recovery):
            delivery.queue(command)
    except ShellTokenError:
        _recover_lexer(parser.lexer, delivery)
    except ShellSyntaxError:
        pass
    else:
        return
    _read_tail(parser, delivery)


def _recover_lexer(lexer: Lexer, delivery: CommandDelivery) -> None:
    lexer.budget.charge(len(lexer.pending))
    for document in lexer.pending:
        if document.state is HereDocumentState.PENDING:
            document.state = HereDocumentState.ABANDONED
    lexer.pending.clear()
    lexer.documents_due = False
    delivery.deliver_ready()
    while lexer.position < len(lexer.source) and lexer.source[lexer.position] not in ";&|\n":
        _ = lexer.advance()


def _read_tail(parser: Parser, delivery: CommandDelivery) -> None:
    while True:
        start = parser.position
        try:
            if parser.peek() is None:
                return
            if parser.at(*_COMMAND_LIST_SEPARATORS):
                parser.position += 1
                continue
            command = parser.command()
        except ShellTokenError:
            _recover_lexer(parser.lexer, delivery)
            parser.position = start
        except ShellSyntaxError:
            parser.position = start + 1
        else:
            delivery.queue(command)
