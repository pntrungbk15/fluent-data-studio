"""A small, safe expression language for filters and calculated columns.

Plans written by people or by a language model never contain SQL. Conditions and formulas use this language
instead, which is parsed into a tree, checked against the table's columns and only then rendered to DuckDB SQL with
quoted identifiers and escaped literals. Anything outside the grammar is rejected with a message that names the
problem, so a model can correct it.

Examples::

    status != 'Cancelled' and year(order_date) = 2025
    revenue - cost
    if(units > 0, revenue / units, null)
    region in ('North', 'South') and not is_null(discount)
    share(revenue)                      -- a window: each row's share of the column total

Columns are written as plain names, or quoted with double quotes or backticks when they contain spaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .schema import LogicalType, TableSchema, quote_ident

__all__ = ["ExpressionError", "Expression", "parse_expression", "FUNCTIONS", "function_reference"]


class ExpressionError(ValueError):
    """An expression that cannot be parsed or does not fit the table."""


# ---- tokens ------------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<number>\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?
  | (?P<string>'(?:[^']|'')*')
  | (?P<dquoted>"(?:[^"]|"")+")
  | (?P<bquoted>`[^`]+`)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|!=|<>|==|\|\||[-+*/%(),=<>])
""", re.X)

_KEYWORDS = {"and", "or", "not", "in", "between", "is", "null", "true", "false", "like"}


@dataclass
class _Token:
    kind: str  # number string name ident op keyword end
    text: str
    pos: int


def _tokenize(text: str) -> List[_Token]:
    tokens: List[_Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise ExpressionError(f"unexpected character {text[pos]!r} at position {pos + 1}")
        kind = match.lastgroup
        value = match.group(0)
        if kind == "space":
            pass
        elif kind == "number" or (kind is None and match.group("number")):
            tokens.append(_Token("number", value, pos))
        elif kind == "string":
            tokens.append(_Token("string", value[1:-1].replace("''", "'"), pos))
        elif kind == "dquoted":
            tokens.append(_Token("ident", value[1:-1].replace('""', '"'), pos))
        elif kind == "bquoted":
            tokens.append(_Token("ident", value[1:-1], pos))
        elif kind == "name":
            lowered = value.lower()
            tokens.append(_Token("keyword", lowered, pos) if lowered in _KEYWORDS else _Token("name", value, pos))
        else:
            tokens.append(_Token("op", "=" if value == "==" else ("!=" if value == "<>" else value), pos))
        pos = match.end()
    tokens.append(_Token("end", "", len(text)))
    return tokens


# ---- tree --------------------------------------------------------------------------------------------------------

@dataclass
class _Node:
    kind: str                       # literal column call unary binary compare in between isnull logic not
    value: object = None
    children: List["_Node"] = field(default_factory=list)


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokenize(text)
        self.i = 0

    def peek(self, offset: int = 0) -> _Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def take(self) -> _Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def accept(self, kind: str, text: Optional[str] = None) -> Optional[_Token]:
        token = self.peek()
        if token.kind == kind and (text is None or token.text == text):
            return self.take()
        return None

    def expect(self, kind: str, text: str) -> _Token:
        token = self.accept(kind, text)
        if token is None:
            got = self.peek()
            raise ExpressionError(f"expected {text!r} at position {got.pos + 1}, found {got.text or 'the end'!r}")
        return token

    def parse(self) -> _Node:
        if self.peek().kind == "end":
            raise ExpressionError("the expression is empty")
        node = self.parse_or()
        if self.peek().kind != "end":
            token = self.peek()
            raise ExpressionError(f"unexpected {token.text!r} at position {token.pos + 1}")
        return node

    def parse_or(self) -> _Node:
        node = self.parse_and()
        while self.accept("keyword", "or"):
            node = _Node("logic", "OR", [node, self.parse_and()])
        return node

    def parse_and(self) -> _Node:
        node = self.parse_not()
        while self.accept("keyword", "and"):
            node = _Node("logic", "AND", [node, self.parse_not()])
        return node

    def parse_not(self) -> _Node:
        if self.accept("keyword", "not"):
            return _Node("not", None, [self.parse_not()])
        return self.parse_compare()

    def parse_compare(self) -> _Node:
        left = self.parse_add()
        token = self.peek()
        if token.kind == "op" and token.text in ("=", "!=", "<", "<=", ">", ">="):
            self.take()
            return _Node("compare", token.text, [left, self.parse_add()])
        negated = False
        if token.kind == "keyword" and token.text == "is":
            self.take()
            negated = bool(self.accept("keyword", "not"))
            self.expect("keyword", "null")
            return _Node("isnull", negated, [left])
        if token.kind == "keyword" and token.text == "not" and self.peek(1).kind == "keyword" \
                and self.peek(1).text in ("in", "between", "like"):
            self.take()
            negated = True
            token = self.peek()
        if self.accept("keyword", "in"):
            self.expect("op", "(")
            items = [self.parse_add()]
            while self.accept("op", ","):
                items.append(self.parse_add())
            self.expect("op", ")")
            return _Node("in", negated, [left] + items)
        if self.accept("keyword", "between"):
            low = self.parse_add()
            self.expect("keyword", "and")
            high = self.parse_add()
            return _Node("between", negated, [left, low, high])
        if self.accept("keyword", "like"):
            return _Node("like", negated, [left, self.parse_add()])
        if negated:
            raise ExpressionError(f"expected 'in', 'between' or 'like' after 'not' at position {token.pos + 1}")
        return left

    def parse_add(self) -> _Node:
        node = self.parse_mul()
        while self.peek().kind == "op" and self.peek().text in ("+", "-", "||"):
            op = self.take().text
            node = _Node("binary", op, [node, self.parse_mul()])
        return node

    def parse_mul(self) -> _Node:
        node = self.parse_unary()
        while self.peek().kind == "op" and self.peek().text in ("*", "/", "%"):
            op = self.take().text
            node = _Node("binary", op, [node, self.parse_unary()])
        return node

    def parse_unary(self) -> _Node:
        if self.accept("op", "-"):
            return _Node("unary", "-", [self.parse_unary()])
        if self.accept("op", "+"):
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> _Node:
        token = self.take()
        if token.kind == "number":
            text = token.text
            return _Node("literal", float(text) if any(c in text for c in ".eE") else int(text))
        if token.kind == "string":
            return _Node("literal", token.text)
        if token.kind == "keyword" and token.text in ("true", "false"):
            return _Node("literal", token.text == "true")
        if token.kind == "keyword" and token.text == "null":
            return _Node("literal", None)
        if token.kind == "op" and token.text == "(":
            node = self.parse_or()
            self.expect("op", ")")
            return node
        if token.kind == "ident":
            return _Node("column", token.text)
        if token.kind == "name":
            if self.accept("op", "("):
                args: List[_Node] = []
                if not self.accept("op", ")"):
                    args.append(self.parse_or())
                    while self.accept("op", ","):
                        args.append(self.parse_or())
                    self.expect("op", ")")
                return _Node("call", token.text.lower(), args)
            return _Node("column", token.text)
        if token.kind == "end":
            raise ExpressionError("the expression ends too early")
        raise ExpressionError(f"unexpected {token.text!r} at position {token.pos + 1}")


# ---- functions ---------------------------------------------------------------------------------------------------

ANY, NUM, TEXT, BOOL, DATE = "any", "number", "text", "boolean", "date"
_TIME_UNITS = ("year", "quarter", "month", "week", "day", "hour")


@dataclass
class _Function:
    args: Tuple[str, ...]           # argument kinds; a trailing "*" repeats the last one
    returns: str
    render: Callable[[List[str], List[_Node]], str]
    doc: str
    window: bool = False
    min_args: Optional[int] = None


def _unit(nodes: List[_Node], index: int, name: str) -> str:
    node = nodes[index]
    if node.kind != "literal" or str(node.value).lower() not in _TIME_UNITS:
        raise ExpressionError(f"{name}() needs a time unit as text: one of {', '.join(_TIME_UNITS)}")
    return str(node.value).lower()


def _simple(sql: str) -> Callable[[List[str], List[_Node]], str]:
    return lambda a, _n: f"{sql}({', '.join(a)})"


FUNCTIONS: Dict[str, _Function] = {
    "year": _Function((DATE,), NUM, lambda a, _n: f"year({a[0]})", "calendar year of a date"),
    "quarter": _Function((DATE,), NUM, lambda a, _n: f"quarter({a[0]})", "quarter 1-4"),
    "month": _Function((DATE,), NUM, lambda a, _n: f"month({a[0]})", "month 1-12"),
    "week": _Function((DATE,), NUM, lambda a, _n: f"week({a[0]})", "ISO week number"),
    "day": _Function((DATE,), NUM, lambda a, _n: f"day({a[0]})", "day of the month"),
    "weekday": _Function((DATE,), TEXT, lambda a, _n: f"dayname({a[0]})", "weekday name"),
    "month_name": _Function((DATE,), TEXT, lambda a, _n: f"monthname({a[0]})", "month name"),
    "bucket": _Function((DATE, TEXT), DATE, lambda a, n: f"CAST(date_trunc('{_unit(n, 1, 'bucket')}', {a[0]}) AS DATE)",
                        "start of the period: bucket(order_date, 'month')"),
    "date": _Function((TEXT,), DATE, lambda a, _n: f"TRY_CAST({a[0]} AS DATE)", "a date from text: date('2025-01-31')"),
    "today": _Function((), DATE, lambda a, _n: "current_date", "today's date"),
    "days_between": _Function((DATE, DATE), NUM, lambda a, _n: f"date_diff('day', {a[0]}, {a[1]})",
                              "whole days from the first date to the second"),
    "abs": _Function((NUM,), NUM, _simple("abs"), "absolute value"),
    "round": _Function((NUM, NUM), NUM, _simple("round"), "round(x) or round(x, digits)", min_args=1),
    "floor": _Function((NUM,), NUM, _simple("floor"), "round down"),
    "ceil": _Function((NUM,), NUM, _simple("ceil"), "round up"),
    "sqrt": _Function((NUM,), NUM, lambda a, _n: f"sqrt(greatest({a[0]}, 0))", "square root"),
    "ln": _Function((NUM,), NUM, lambda a, _n: f"ln(nullif(greatest({a[0]}, 0), 0))", "natural logarithm"),
    "log10": _Function((NUM,), NUM, lambda a, _n: f"log10(nullif(greatest({a[0]}, 0), 0))", "base-10 logarithm"),
    "power": _Function((NUM, NUM), NUM, _simple("power"), "power(x, y)"),
    "least": _Function((ANY, ANY, "*"), ANY, _simple("least"), "smallest argument", min_args=2),
    "greatest": _Function((ANY, ANY, "*"), ANY, _simple("greatest"), "largest argument", min_args=2),
    "coalesce": _Function((ANY, ANY, "*"), ANY, _simple("coalesce"), "first non-missing argument", min_args=2),
    "nullif": _Function((ANY, ANY), ANY, _simple("nullif"), "null when both are equal"),
    "is_null": _Function((ANY,), BOOL, lambda a, _n: f"({a[0]} IS NULL)", "true when missing"),
    "if": _Function((BOOL, ANY, ANY), ANY, lambda a, _n: f"(CASE WHEN {a[0]} THEN {a[1]} ELSE {a[2]} END)",
                    "if(condition, then, else)"),
    "lower": _Function((TEXT,), TEXT, _simple("lower"), "lower case"),
    "upper": _Function((TEXT,), TEXT, _simple("upper"), "upper case"),
    "trim": _Function((TEXT,), TEXT, _simple("trim"), "strip spaces"),
    "length": _Function((TEXT,), NUM, _simple("length"), "number of characters"),
    "concat": _Function((ANY, ANY, "*"), TEXT, _simple("concat"), "join texts", min_args=2),
    "contains": _Function((TEXT, TEXT), BOOL, lambda a, _n: f"contains(lower(CAST({a[0]} AS VARCHAR)), lower({a[1]}))",
                          "case-insensitive substring test"),
    "starts_with": _Function((TEXT, TEXT), BOOL, lambda a, _n: f"starts_with(CAST({a[0]} AS VARCHAR), {a[1]})",
                             "prefix test"),
    "ends_with": _Function((TEXT, TEXT), BOOL, lambda a, _n: f"ends_with(CAST({a[0]} AS VARCHAR), {a[1]})",
                           "suffix test"),
    "replace": _Function((TEXT, TEXT, TEXT), TEXT, _simple("replace"), "replace(text, old, new)"),
    "to_number": _Function((ANY,), NUM, lambda a, _n: f"TRY_CAST({a[0]} AS DOUBLE)", "text to number (null if invalid)"),
    "to_text": _Function((ANY,), TEXT, lambda a, _n: f"CAST({a[0]} AS VARCHAR)", "value as text"),
    "to_date": _Function((ANY,), DATE, lambda a, _n: f"TRY_CAST({a[0]} AS DATE)", "value as a date (null if invalid)"),
    "safe_div": _Function((NUM, NUM), NUM, lambda a, _n: f"({a[0]} / NULLIF({a[1]}, 0))", "division, null on zero"),
    # windows: evaluated over the whole table (after earlier steps)
    "share": _Function((NUM,), NUM, lambda a, _n: f"({a[0]} / NULLIF(SUM({a[0]}) OVER (), 0))",
                       "row value divided by the column total", window=True),
    "rank_desc": _Function((NUM,), NUM, lambda a, _n: f"RANK() OVER (ORDER BY {a[0]} DESC NULLS LAST)",
                           "1 for the largest value", window=True),
    "previous": _Function((ANY, ANY), ANY, lambda a, _n: f"LAG({a[0]}) OVER (ORDER BY {a[1]})",
                          "previous(value, order_column): the value in the previous row", window=True),
    "running_total": _Function((NUM, ANY), NUM,
                               lambda a, _n: f"SUM({a[0]}) OVER (ORDER BY {a[1]} ROWS UNBOUNDED PRECEDING)",
                               "running_total(value, order_column)", window=True),
    "zscore": _Function((NUM,), NUM, lambda a, _n: f"(({a[0]} - AVG({a[0]}) OVER ()) / NULLIF(STDDEV_SAMP({a[0]}) OVER (), 0))",
                        "standard score against the column", window=True),
}


def function_reference() -> str:
    """One line per function, for documentation and for the planner prompt."""
    return "\n".join(f"{name}: {fn.doc}" for name, fn in sorted(FUNCTIONS.items()))


# ---- rendering ---------------------------------------------------------------------------------------------------

def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _kind_of_type(logical: str) -> str:
    if logical in LogicalType.NUMERIC:
        return NUM
    if logical in LogicalType.TEMPORAL:
        return DATE
    if logical == LogicalType.BOOLEAN:
        return BOOL
    if logical == LogicalType.TEXT:
        return TEXT
    return ANY


def _compatible(expected: str, actual: str) -> bool:
    return expected == ANY or actual == ANY or expected == actual or (expected == TEXT and actual == DATE)


@dataclass
class Expression:
    """A parsed, validated expression.

    ``sql`` is the DuckDB rendering, ``columns`` the referenced columns (with their canonical names), ``kind`` the
    result kind (number, text, boolean, date or any) and ``window`` whether it needs the whole table.
    """

    text: str
    sql: str
    columns: List[str]
    kind: str
    window: bool


class _Renderer:
    def __init__(self, schema: Optional[TableSchema]) -> None:
        self.schema = schema
        self.columns: List[str] = []
        self.window = False

    def render(self, node: _Node) -> Tuple[str, str]:
        kind = node.kind
        if kind == "literal":
            value = node.value
            if isinstance(value, bool):
                return _literal(value), BOOL
            if isinstance(value, (int, float)):
                return _literal(value), NUM
            return _literal(value), (ANY if value is None else TEXT)
        if kind == "column":
            name = str(node.value)
            if self.schema is not None:
                column = self.schema.column(name)
                if column is None:
                    raise ExpressionError(_unknown_column(name, self.schema.names()))
                name = column.name
                result_kind = _kind_of_type(column.type)
            else:
                result_kind = ANY
            if name not in self.columns:
                self.columns.append(name)
            return quote_ident(name), result_kind
        if kind == "call":
            return self.render_call(node)
        if kind == "unary":
            sql, k = self.render(node.children[0])
            self.require(k, NUM, "'-'")
            return f"(-{sql})", NUM
        if kind == "binary":
            (a, ka), (b, kb) = self.render(node.children[0]), self.render(node.children[1])
            op = str(node.value)
            if op == "||":
                return f"(CAST({a} AS VARCHAR) || CAST({b} AS VARCHAR))", TEXT
            if op == "-" and ka == DATE and kb == DATE:
                return f"date_diff('day', {b}, {a})", NUM
            self.require(ka, NUM, f"'{op}'")
            self.require(kb, NUM, f"'{op}'")
            if op == "/":
                return f"({a} / NULLIF({b}, 0))", NUM
            return f"({a} {op} {b})", NUM
        if kind == "compare":
            (a, ka), (b, kb) = self.render(node.children[0]), self.render(node.children[1])
            if ANY not in (ka, kb) and not _compatible(ka, kb) and not _compatible(kb, ka):
                raise ExpressionError(f"cannot compare {ka} with {kb}")
            op = "<>" if node.value == "!=" else str(node.value)
            return f"({a} {op} {b})", BOOL
        if kind == "isnull":
            sql, _ = self.render(node.children[0])
            return f"({sql} IS {'NOT ' if node.value else ''}NULL)", BOOL
        if kind == "in":
            parts = [self.render(child)[0] for child in node.children]
            return f"({parts[0]} {'NOT ' if node.value else ''}IN ({', '.join(parts[1:])}))", BOOL
        if kind == "between":
            a, b, c = (self.render(child)[0] for child in node.children)
            return f"({a} {'NOT ' if node.value else ''}BETWEEN {b} AND {c})", BOOL
        if kind == "like":
            (a, _), (b, _) = self.render(node.children[0]), self.render(node.children[1])
            return f"(CAST({a} AS VARCHAR) {'NOT ' if node.value else ''}ILIKE {b})", BOOL
        if kind == "logic":
            (a, ka), (b, kb) = self.render(node.children[0]), self.render(node.children[1])
            self.require(ka, BOOL, str(node.value).lower())
            self.require(kb, BOOL, str(node.value).lower())
            return f"({a} {node.value} {b})", BOOL
        if kind == "not":
            sql, k = self.render(node.children[0])
            self.require(k, BOOL, "not")
            return f"(NOT {sql})", BOOL
        raise ExpressionError(f"unsupported expression part {kind}")

    def render_call(self, node: _Node) -> Tuple[str, str]:
        name = str(node.value)
        fn = FUNCTIONS.get(name)
        if fn is None:
            raise ExpressionError(f"unknown function {name}(); available: {', '.join(sorted(FUNCTIONS))}")
        args = node.children
        repeat = bool(fn.args) and fn.args[-1] == "*"
        fixed = fn.args[:-1] if repeat else fn.args
        minimum = fn.min_args if fn.min_args is not None else len(fixed)
        if len(args) < minimum or (not repeat and len(args) > len(fixed)):
            expected = f"at least {minimum}" if repeat else (f"{minimum} to {len(fixed)}" if minimum != len(fixed)
                                                             else str(len(fixed)))
            raise ExpressionError(f"{name}() takes {expected} argument(s), got {len(args)}")
        rendered: List[str] = []
        for index, arg in enumerate(args):
            sql, kind = self.render(arg)
            expected = fixed[min(index, len(fixed) - 1)] if fixed else ANY
            if not _compatible(expected, kind) and not (expected == DATE and kind == TEXT and arg.kind == "literal"):
                raise ExpressionError(f"{name}() expects a {expected} as argument {index + 1}, got a {kind}")
            rendered.append(sql)
        if fn.window:
            self.window = True
        return fn.render(rendered, args), fn.returns

    @staticmethod
    def require(actual: str, expected: str, where: str) -> None:
        if actual != ANY and actual != expected:
            raise ExpressionError(f"{where} needs a {expected}, got a {actual}")


def _unknown_column(name: str, names: Sequence[str]) -> str:
    lowered = name.lower()
    close = [n for n in names if lowered in n.lower() or n.lower() in lowered][:3]
    hint = f"; did you mean {', '.join(close)}?" if close else f"; columns: {', '.join(names[:30])}"
    return f"unknown column {name!r}{hint}"


def parse_expression(text: Union[str, int, float], schema: Optional[TableSchema] = None) -> Expression:
    """Parse ``text`` and check it against ``schema`` (columns and argument kinds); raises :class:`ExpressionError`."""
    source = str(text).strip()
    tree = _Parser(source).parse()
    renderer = _Renderer(schema)
    sql, kind = renderer.render(tree)
    return Expression(source, sql, renderer.columns, kind, renderer.window)


def literal_sql(value: object) -> str:
    """A safe SQL literal (used by operations that take plain values, like filling missing values)."""
    return _literal(value)
