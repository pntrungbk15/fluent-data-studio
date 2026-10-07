"""The analysis workspace: one in-process DuckDB database holding every dataset and every step result.

Sources are staged into local tables once (see :mod:`fluent_data_studio.engine.sources`); every transformation, chart
and finding afterwards runs locally against those tables, so analysis never touches the original database again and
results are reproducible from the recorded steps.

DuckDB releases the GIL while it executes, so the desktop application runs queries on a worker thread; each call here
uses its own cursor, which DuckDB makes safe across threads.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb

from .schema import ColumnInfo, TableSchema, logical_type, quote_ident

__all__ = ["Workspace", "ResultSet", "Dataset", "WorkspaceError", "safe_name", "plain_value"]


class WorkspaceError(RuntimeError):
    """A query or staging error, with DuckDB's message."""


def safe_name(text: str, taken: Sequence[str] = ()) -> str:
    """A short snake_case dataset name derived from ``text`` that is not in ``taken``."""
    base = re.sub(r"[^0-9a-zA-Z]+", "_", text.strip()).strip("_").lower() or "data"
    if base[0].isdigit():
        base = "t_" + base
    base = base[:40]
    name, n = base, 2
    lowered = {t.lower() for t in taken}
    while name.lower() in lowered:
        name = f"{base}_{n}"
        n += 1
    return name


def plain_value(value: Any) -> Any:
    """A JSON-friendly version of a DuckDB value (decimals as floats, dates as ISO text)."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (list, dict, tuple)):
        return str(value)
    return value


@dataclass
class ResultSet:
    """Rows of a query with their column names and DuckDB types; ``total`` counts all rows, not only those fetched."""

    columns: List[str]
    types: List[str]
    rows: List[Tuple[Any, ...]]
    total: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)

    def column_values(self, name: str) -> List[Any]:
        index = self.columns.index(name)
        return [row[index] for row in self.rows]

    def to_records(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        rows = self.rows if limit is None else self.rows[:limit]
        return [{c: plain_value(v) for c, v in zip(self.columns, row)} for row in rows]


@dataclass
class Dataset:
    """A staged dataset: its name (also its table), where it came from and its profiled schema."""

    name: str
    source: Dict[str, Any]
    schema: TableSchema
    semantics: Dict[str, Any] = field(default_factory=dict)
    note: str = ""


class Workspace:
    """``Workspace()`` opens an in-memory DuckDB database; datasets and results are tables inside it."""

    def __init__(self, threads: int = 0) -> None:
        self._db = duckdb.connect(":memory:")
        if threads:
            self._db.execute(f"SET threads = {int(threads)}")
        self._lock = threading.Lock()
        self.datasets: Dict[str, Dataset] = {}

    # ---- low level ------------------------------------------------------------------------------------------

    def cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            return self._db.cursor()

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        cursor = self.cursor()
        try:
            cursor.execute(sql, params or [])
        except duckdb.Error as exc:
            raise WorkspaceError(_clean(exc)) from None
        finally:
            cursor.close()

    def describe(self, sql: str) -> List[Tuple[str, str]]:
        """Column names and types of a query without running it (DuckDB binds it and reports errors)."""
        cursor = self.cursor()
        try:
            rows = cursor.execute(f"DESCRIBE {sql}").fetchall()
        except duckdb.Error as exc:
            raise WorkspaceError(_clean(exc)) from None
        finally:
            cursor.close()
        return [(row[0], str(row[1])) for row in rows]

    def query(self, sql: str, limit: Optional[int] = 1000, count: bool = True) -> ResultSet:
        """Run ``sql`` and fetch up to ``limit`` rows (``None`` fetches everything)."""
        cursor = self.cursor()
        try:
            relation = cursor.sql(sql)
            columns = list(relation.columns)
            types = [str(t) for t in relation.types]
            rows = relation.limit(limit).fetchall() if limit is not None else relation.fetchall()
            total = len(rows)
            if count and limit is not None and len(rows) >= limit:
                total = int(cursor.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0])
        except duckdb.Error as exc:
            raise WorkspaceError(_clean(exc)) from None
        finally:
            cursor.close()
        return ResultSet(columns, types, rows, total)

    def scalar(self, sql: str, params: Optional[Sequence[Any]] = None) -> Any:
        cursor = self.cursor()
        try:
            row = cursor.execute(sql, params or []).fetchone()
        except duckdb.Error as exc:
            raise WorkspaceError(_clean(exc)) from None
        finally:
            cursor.close()
        return row[0] if row else None

    def table_exists(self, table: str) -> bool:
        return bool(self.scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]))

    def drop_table(self, table: str) -> None:
        self.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")

    def materialize(self, table: str, sql: str) -> int:
        """Store the result of ``sql`` as ``table`` (replacing it) and return its row count."""
        self.execute(f"CREATE OR REPLACE TABLE {quote_ident(table)} AS {sql}")
        return int(self.scalar(f"SELECT count(*) FROM {quote_ident(table)}"))

    def table_schema(self, table: str, name: Optional[str] = None) -> TableSchema:
        """The plain schema of a table (types only, no statistics)."""
        columns = [ColumnInfo(n, t, logical_type(t)) for n, t in self.describe(f"SELECT * FROM {quote_ident(table)}")]
        rows = int(self.scalar(f"SELECT count(*) FROM {quote_ident(table)}"))
        return TableSchema(name or table, columns, rows)

    # ---- datasets -------------------------------------------------------------------------------------------

    def dataset(self, name: str) -> Optional[Dataset]:
        found = self.datasets.get(name)
        if found is None:
            lowered = name.lower()
            found = next((d for key, d in self.datasets.items() if key.lower() == lowered), None)
        return found

    def add_dataset(self, dataset: Dataset) -> None:
        self.datasets[dataset.name] = dataset

    def remove_dataset(self, name: str) -> None:
        if self.datasets.pop(name, None) is not None:
            self.drop_table(name)

    def close(self) -> None:
        self._db.close()


def _clean(exc: Exception) -> str:
    """DuckDB's message without its multi-line context block."""
    text = str(exc).strip()
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return type(exc).__name__
    first = lines[0]
    for line in lines[1:3]:
        if line.startswith(("Candidate", "Did you mean", "LINE")):
            first += " " + line.strip()
    return first
