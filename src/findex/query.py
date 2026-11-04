"""Мова запитів: рекурсивний спуск -> дерево Term / Phrase / And / Or / Not.

Граматика (від найслабшого зв'язування до найсильнішого):

    query   := or_expr EOF
    or_expr := and_expr ("OR" and_expr)*
    and_expr:= unary (["AND"] unary)*          -- AND можна не писати
    unary   := "NOT" unary | primary
    primary := "(" or_expr ")" | "\"фраза\"" | слово

Кожен рівень граматики — окрема функція, що викликає функцію рівня нижче; тому
AND (нижчий рівень) зв'язує сильніше за OR, і `a OR b c` = `Or(a, And(b, c))` — так
само, як `1 + 2 * 3` = `1 + (2 * 3)`. Оператори — ВЕЛИКИМИ літерами; `or` малими — це
просто слово. Слова проходять той самий `tokenize`, що й корпус: «Generators» ->
`Term("generators")`; «e-mail» розпадається на два токени -> `Phrase(("e", "mail"))`.

Вузли — frozen dataclass: `==` порівнює дерева (на цьому побудовані тести парсера),
а `&`, `|`, `~` будують дерева в коді: `Term("a") & ~Term("b")`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from findex.index import Index
from findex.merge import ENGINES, Engine, EngineName, intersect
from findex.tokenize import tokenize


class QueryError(ValueError):
    """Синтаксична помилка в запиті або запит, який індекс не може виконати."""


class Node(ABC):
    """Вузол дерева запиту."""

    @abstractmethod
    def evaluate(self, index: Index, engine: EngineName = "merge") -> list[int]:
        """Відсортовані doc_id документів, що задовольняють вузол."""

    @abstractmethod
    def positive_terms(self) -> Iterator[str]:
        """Терми, що «працюють на» документ (усе, крім того, що під NOT) — для ранжування."""

    def __and__(self, other: Node) -> And:
        return And(self, other)

    def __or__(self, other: Node) -> Or:
        return Or(self, other)

    def __invert__(self) -> Not:
        return Not(self)


@dataclass(frozen=True, slots=True)
class Term(Node):
    term: str

    def evaluate(self, index: Index, engine: EngineName = "merge") -> list[int]:
        return list(index.doc_ids(self.term))

    def positive_terms(self) -> Iterator[str]:
        yield self.term

    def __str__(self) -> str:
        return self.term


@dataclass(frozen=True, slots=True)
class Phrase(Node):
    """Терми мають іти поспіль. Потрібен індекс, побудований з --positions."""

    terms: tuple[str, ...]

    def evaluate(self, index: Index, engine: EngineName = "merge") -> list[int]:
        if not index.with_positions:
            raise QueryError(f'фраза "{self}" потребує позицій: перебудуйте індекс з --positions')
        # 1) кандидати — документи, де є ВСІ терми (AND)
        candidates: Sequence[int] = index.doc_ids(self.terms[0])
        for term in self.terms[1:]:
            candidates = intersect(candidates, index.doc_ids(term))
        if not candidates:
            return []
        wanted = set(candidates)
        # 2) позиції кожного терма в кандидатах
        positions: list[dict[int, frozenset[int]]] = []
        for term in self.terms:
            positions.append(
                {
                    p.doc_id: frozenset(p.positions)
                    for p in index.iter_postings(term)
                    if p.doc_id in wanted
                }
            )
        # 3) є старт s, такий що terms[i] стоїть на позиції s + i
        first, rest = positions[0], positions[1:]
        offsets = range(1, len(self.terms))
        return [
            doc
            for doc in candidates
            if any(all(s + i in rest[i - 1][doc] for i in offsets) for s in first[doc])
        ]

    def positive_terms(self) -> Iterator[str]:
        yield from self.terms

    def __str__(self) -> str:
        return '"' + " ".join(self.terms) + '"'


@dataclass(frozen=True, slots=True)
class And(Node):
    left: Node
    right: Node

    def evaluate(self, index: Index, engine: EngineName = "merge") -> list[int]:
        eng = ENGINES[engine]
        # `x AND NOT y` — різниця, без обчислення доповнення до всіх документів
        if isinstance(self.right, Not) and not isinstance(self.left, Not):
            return eng.not_(
                self.left.evaluate(index, engine), self.right.child.evaluate(index, engine)
            )
        if isinstance(self.left, Not) and not isinstance(self.right, Not):
            return eng.not_(
                self.right.evaluate(index, engine), self.left.child.evaluate(index, engine)
            )
        return eng.and_(self.left.evaluate(index, engine), self.right.evaluate(index, engine))

    def positive_terms(self) -> Iterator[str]:
        yield from self.left.positive_terms()
        yield from self.right.positive_terms()

    def __str__(self) -> str:
        return f"({self.left} {self.right})"


@dataclass(frozen=True, slots=True)
class Or(Node):
    left: Node
    right: Node

    def evaluate(self, index: Index, engine: EngineName = "merge") -> list[int]:
        eng = ENGINES[engine]
        return eng.or_(self.left.evaluate(index, engine), self.right.evaluate(index, engine))

    def positive_terms(self) -> Iterator[str]:
        yield from self.left.positive_terms()
        yield from self.right.positive_terms()

    def __str__(self) -> str:
        return f"({self.left} OR {self.right})"


@dataclass(frozen=True, slots=True)
class Not(Node):
    child: Node

    def evaluate(self, index: Index, engine: EngineName = "merge") -> list[int]:
        eng: Engine = ENGINES[engine]
        return eng.not_(range(index.num_docs), self.child.evaluate(index, engine))

    def positive_terms(self) -> Iterator[str]:
        return iter(())  # те, що під NOT, не підвищує оцінку

    def __str__(self) -> str:
        return f"NOT {self.child}"


# --- лексер ------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lp>\() | (?P<rp>\)) |
        "(?P<phrase>[^"]*)" |      # "фраза в лапках"
        (?P<quote>") |             # непарна лапка -> помилка
        (?P<word>[^\s()"]+)        # будь-що інше до пробілу, дужки чи лапки
    )""",
    re.VERBOSE,
)
_OPERATORS = frozenset({"AND", "OR", "NOT"})


@dataclass(frozen=True, slots=True)
class Tok:
    kind: str  # "(", ")", "AND", "OR", "NOT", "PHRASE", "WORD", "EOF"
    text: str
    pos: int


def lex(query: str) -> list[Tok]:
    tokens: list[Tok] = []
    pos = 0
    while pos < len(query):
        m = _TOKEN.match(query, pos)
        if m is None or m.end() == pos:  # лише пробіли в кінці
            break
        pos = m.end()
        start = m.start(m.lastgroup) if m.lastgroup else m.start()
        if m.group("lp"):
            tokens.append(Tok("(", "(", start))
        elif m.group("rp"):
            tokens.append(Tok(")", ")", start))
        elif m.group("quote"):
            raise QueryError(f"незакрита лапка на позиції {start}")
        elif m.group("phrase") is not None:
            tokens.append(Tok("PHRASE", m.group("phrase"), start))
        else:
            word = m.group("word")
            tokens.append(Tok(word if word in _OPERATORS else "WORD", word, start))
    tokens.append(Tok("EOF", "", len(query)))
    return tokens


# --- парсер (рекурсивний спуск) ------------------------------------------------------------


def _words_to_node(text: str) -> Node | None:
    terms = tuple(tokenize(text))
    if not terms:
        return None  # самі розділові знаки — нічого шукати
    return Term(terms[0]) if len(terms) == 1 else Phrase(terms)


class _Parser:
    def __init__(self, tokens: list[Tok]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self) -> Tok:
        return self.tokens[self.i]

    def take(self) -> Tok:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self) -> Node:
        if self.peek().kind == "EOF":
            raise QueryError("порожній запит")
        node = self.or_expr()
        tok = self.peek()
        if tok.kind != "EOF":
            raise QueryError(f"зайве {tok.text!r} на позиції {tok.pos}")
        return node

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.peek().kind == "OR":
            self.take()
            node = Or(node, self.and_expr())
        return node

    def and_expr(self) -> Node:
        node = self.unary()
        while self.peek().kind in ("AND", "NOT", "(", "PHRASE", "WORD"):
            if self.peek().kind == "AND":
                self.take()
            node = And(node, self.unary())
        return node

    def unary(self) -> Node:
        if self.peek().kind == "NOT":
            self.take()
            return Not(self.unary())
        return self.primary()

    def primary(self) -> Node:
        tok = self.take()
        if tok.kind == "(":
            node = self.or_expr()
            close = self.take()
            if close.kind != ")":
                raise QueryError(f"очікувалась «)» на позиції {close.pos}")
            return node
        if tok.kind in ("WORD", "PHRASE"):
            node = _words_to_node(tok.text)
            if node is None:
                raise QueryError(f"{tok.text!r} не містить слів (позиція {tok.pos})")
            return node
        if tok.kind == "EOF":
            raise QueryError("запит обірвався: очікувалось слово або «(»")
        raise QueryError(
            f"неочікуване {tok.text!r} на позиції {tok.pos}: очікувалось слово або «(»"
        )


def parse(query: str) -> Node:
    """Розбирає рядок запиту в дерево. Кидає QueryError на синтаксичну помилку."""
    # слова без жодного токена (напр. «—») мовчки відкидаємо, як і tokenize для корпусу
    tokens = [t for t in lex(query) if t.kind != "WORD" or any(True for _ in tokenize(t.text))]
    return _Parser(tokens).parse()
