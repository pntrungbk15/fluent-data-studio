"""Column types, semantic roles and the per-dataset semantic layer.

DuckDB types are reduced to a few logical types the planner and the chart rules reason about. On top of them each
column gets a *role* (dimension, measure, time, identifier), a default aggregation, a display format, an optional
description and synonyms. Roles are inferred from types, names and statistics; a dataset may ship a sidecar file
(``<name>.semantic.json``) and the user can correct any of it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "LogicalType", "Role", "ColumnInfo", "TableSchema", "logical_type", "quote_ident",
    "MEASURE_AGGREGATIONS", "load_semantic_sidecar",
]


class LogicalType:
    INTEGER = "integer"
    NUMBER = "number"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIME = "time"
    OTHER = "other"

    NUMERIC = (INTEGER, NUMBER)
    TEMPORAL = (DATE, TIMESTAMP)


class Role:
    DIMENSION = "dimension"
    MEASURE = "measure"
    TIME = "time"
    IDENTIFIER = "identifier"
    ALL = (DIMENSION, MEASURE, TIME, IDENTIFIER)


MEASURE_AGGREGATIONS = ("sum", "avg", "min", "max", "median", "count", "count_distinct", "stddev")

_INTEGER_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                  "UBIGINT", "UHUGEINT", "INT")
_NUMBER_TYPES = ("FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC")


def logical_type(duck_type: str) -> str:
    """The logical type of a DuckDB type name (``DECIMAL(18,2)`` is a number, ``TIMESTAMP WITH TIME ZONE`` a timestamp)."""
    name = duck_type.upper().strip()
    base = name.split("(")[0].strip()
    if base in _INTEGER_TYPES:
        return LogicalType.INTEGER
    if base in _NUMBER_TYPES:
        return LogicalType.NUMBER
    if base in ("VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR", "UUID", "ENUM"):
        return LogicalType.TEXT
    if base in ("BOOLEAN", "BOOL"):
        return LogicalType.BOOLEAN
    if base == "DATE":
        return LogicalType.DATE
    if base.startswith("TIMESTAMP") or base == "DATETIME":
        return LogicalType.TIMESTAMP
    if base.startswith("TIME"):
        return LogicalType.TIME
    return LogicalType.OTHER


def quote_ident(name: str) -> str:
    """A SQL identifier, always double-quoted, so column names never become SQL syntax."""
    return '"' + str(name).replace('"', '""') + '"'


_CURRENCY = re.compile(r"(revenue|sales|price|cost|amount|profit|spend|budget|income|expense|value|fee|margin_usd|gmv)",
                       re.I)
_RATE = re.compile(r"(rate|ratio|pct|percent|share|score|margin|conversion|ctr|utili[sz]ation|yield)", re.I)
_AVERAGE = re.compile(r"(price|rate|ratio|pct|percent|score|age|temperature|duration|days|minutes|hours|lead_time|"
                      r"rating|margin|discount|weight|speed|latency)", re.I)
_IDENTIFIER = re.compile(r"(^id$|_id$|^id_|_key$|_code$|^code$|_no$|_number$|^sku$|uuid|^zip|postal|phone)", re.I)
_YEAR = re.compile(r"^(year|yr|fiscal_year)$", re.I)


@dataclass
class ColumnInfo:
    """One column of a dataset, with what the planner needs to reason about its meaning."""

    name: str
    duck_type: str
    type: str
    role: str = Role.DIMENSION
    aggregation: str = "sum"
    format: str = ""               # "", "currency", "percent", "integer"
    description: str = ""
    synonyms: List[str] = field(default_factory=list)
    nulls: int = 0
    distinct: int = 0
    minimum: Any = None
    maximum: Any = None
    mean: Optional[float] = None
    top_values: List[str] = field(default_factory=list)
    user_edited: bool = False

    @property
    def is_numeric(self) -> bool:
        return self.type in LogicalType.NUMERIC

    @property
    def is_temporal(self) -> bool:
        return self.type in LogicalType.TEMPORAL

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for key in ("minimum", "maximum"):
            value = data[key]
            if value is not None and not isinstance(value, (int, float, str, bool)):
                data[key] = str(value)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ColumnInfo":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class TableSchema:
    """The columns of a table plus its row count and an optional description."""

    name: str
    columns: List[ColumnInfo]
    rows: int = 0
    description: str = ""

    def column(self, name: str) -> Optional[ColumnInfo]:
        """The column called ``name``, matched case-insensitively."""
        lowered = name.lower()
        for column in self.columns:
            if column.name == name:
                return column
        for column in self.columns:
            if column.name.lower() == lowered:
                return column
        return None

    def names(self) -> List[str]:
        return [c.name for c in self.columns]

    def by_role(self, *roles: str) -> List[ColumnInfo]:
        return [c for c in self.columns if c.role in roles]

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "rows": self.rows, "description": self.description,
                "columns": [c.to_dict() for c in self.columns]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TableSchema":
        return cls(data["name"], [ColumnInfo.from_dict(c) for c in data.get("columns", [])], int(data.get("rows", 0)),
                   data.get("description", ""))


def infer_role(column: ColumnInfo, rows: int) -> None:
    """Fill ``role``, ``aggregation`` and ``format`` from the column's type, name and statistics (unless edited)."""
    if column.user_edited:
        return
    name = column.name
    if column.is_temporal:
        column.role = Role.TIME
        column.aggregation = "count"
        return
    if column.type == LogicalType.INTEGER and _YEAR.match(name):
        column.role = Role.TIME
        column.aggregation = "count"
        return
    unique_ratio = column.distinct / rows if rows else 0.0
    if _IDENTIFIER.search(name) or (column.type in (LogicalType.TEXT, LogicalType.INTEGER) and rows >= 50
                                    and unique_ratio > 0.95 and not _CURRENCY.search(name)):
        column.role = Role.IDENTIFIER
        column.aggregation = "count_distinct"
        return
    if column.is_numeric:
        low_cardinality_int = column.type == LogicalType.INTEGER and column.distinct <= 12 and not _CURRENCY.search(name)
        if low_cardinality_int and re.search(r"(month|quarter|week|day|level|tier|rank|grade|class|floor)", name, re.I):
            column.role = Role.DIMENSION
            column.aggregation = "count"
            return
        column.role = Role.MEASURE
        column.aggregation = "avg" if _AVERAGE.search(name) else "sum"
        if _RATE.search(name) and column.maximum is not None and _as_float(column.maximum) <= 1.0:
            column.format = "percent"
        elif _CURRENCY.search(name):
            column.format = "currency"
        elif column.type == LogicalType.INTEGER:
            column.format = "integer"
        return
    column.role = Role.DIMENSION
    column.aggregation = "count"


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def load_semantic_sidecar(path: Path) -> Dict[str, Any]:
    """The semantic sidecar next to a data file (``sales.csv`` → ``sales.semantic.json``), or ``{}``.

    Format: ``{"description": str, "columns": {name: {"description", "role", "aggregation", "format", "synonyms"}}}``.
    """
    sidecar = path.with_name(path.stem + ".semantic.json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_semantics(schema: TableSchema, semantics: Dict[str, Any]) -> None:
    """Overlay a sidecar (or saved user edits) on an inferred schema; unknown columns are ignored."""
    if not semantics:
        return
    if semantics.get("description"):
        schema.description = str(semantics["description"])
    for name, meta in (semantics.get("columns") or {}).items():
        column = schema.column(name)
        if column is None or not isinstance(meta, dict):
            continue
        if meta.get("role") in Role.ALL:
            column.role = meta["role"]
        if meta.get("aggregation") in MEASURE_AGGREGATIONS:
            column.aggregation = meta["aggregation"]
        for key in ("format", "description"):
            if isinstance(meta.get(key), str):
                setattr(column, key, meta[key])
        if isinstance(meta.get("synonyms"), list):
            column.synonyms = [str(s) for s in meta["synonyms"]]


def semantics_of(schema: TableSchema, only_edited: bool = False) -> Dict[str, Any]:
    """The semantic overlay of a schema, the inverse of :func:`apply_semantics` (used to save user edits)."""
    columns: Dict[str, Any] = {}
    for column in schema.columns:
        if only_edited and not column.user_edited:
            continue
        columns[column.name] = {"role": column.role, "aggregation": column.aggregation, "format": column.format,
                                "description": column.description, "synonyms": list(column.synonyms)}
    return {"description": schema.description, "columns": columns}


def words(text: str) -> List[str]:
    """Lower-case word tokens of a column name or a request (``netRevenue_usd`` → ``net revenue usd``)."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [w for w in re.split(r"[^0-9a-zA-Z]+", spaced.lower()) if w]


def column_terms(column: ColumnInfo) -> Iterable[str]:
    """Phrases that refer to a column: its name in words plus its synonyms."""
    yield " ".join(words(column.name))
    for synonym in column.synonyms:
        yield " ".join(words(synonym))
