"""Dataset profiling: per-column statistics from DuckDB's SUMMARIZE plus the most frequent values of text columns."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .schema import ColumnInfo, LogicalType, TableSchema, apply_semantics, infer_role, logical_type, quote_ident
from .workspace import Workspace, plain_value

__all__ = ["profile_table", "profile_summary", "correlations"]

_TOP_VALUES = 8


def profile_table(workspace: Workspace, table: str, name: Optional[str] = None,
                  semantics: Optional[Dict[str, Any]] = None) -> TableSchema:
    """Profile ``table`` and infer each column's role; ``semantics`` (a sidecar or user edits) is applied on top."""
    quoted = quote_ident(table)
    summary = workspace.query(f"SUMMARIZE {quoted}", limit=None, count=False)
    index = {c: i for i, c in enumerate(summary.columns)}
    rows = int(workspace.scalar(f"SELECT count(*) FROM {quoted}"))
    columns: List[ColumnInfo] = []
    for record in summary.rows:
        duck_type = str(record[index["column_type"]])
        column = ColumnInfo(str(record[index["column_name"]]), duck_type, logical_type(duck_type))
        null_pct = record[index["null_percentage"]]
        column.nulls = int(round(float(null_pct or 0) * rows / 100.0))
        column.distinct = int(record[index["approx_unique"]] or 0)
        column.minimum = _typed(record[index["min"]], column.type)
        column.maximum = _typed(record[index["max"]], column.type)
        if column.is_numeric and record[index["avg"]] is not None:
            try:
                column.mean = float(record[index["avg"]])
            except (TypeError, ValueError):
                column.mean = None
        columns.append(column)
    # approx_unique can overshoot on small tables; exact counts are cheap there
    if rows <= 200_000 and columns:
        exact = workspace.query("SELECT " + ", ".join(f"count(DISTINCT {quote_ident(c.name)})" for c in columns)
                                + f" FROM {quoted}", limit=None, count=False).rows[0]
        for column, value in zip(columns, exact):
            column.distinct = int(value)
    for column in columns:
        if column.type in (LogicalType.TEXT, LogicalType.BOOLEAN) or (column.type == LogicalType.INTEGER
                                                                      and column.distinct <= 20):
            top = workspace.query(
                f"SELECT {quote_ident(column.name)} AS v, count(*) AS n FROM {quoted} WHERE v IS NOT NULL "
                f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {_TOP_VALUES}", limit=None, count=False)
            column.top_values = [str(plain_value(row[0])) for row in top.rows]
        infer_role(column, rows)
    schema = TableSchema(name or table, columns, rows)
    apply_semantics(schema, semantics or {})
    return schema


def _typed(value: Any, logical: str) -> Any:
    """SUMMARIZE reports min/max as text; numbers come back as numbers."""
    if value is None:
        return None
    if logical in LogicalType.NUMERIC:
        try:
            number = float(value)
            return int(number) if logical == LogicalType.INTEGER and number.is_integer() else number
        except (TypeError, ValueError):
            return value
    return str(value)


def profile_summary(schema: TableSchema) -> List[str]:
    """Plain-language facts about a profiled dataset (shown without a language model and given to it as context)."""
    facts = [f"{schema.rows:,} rows and {len(schema.columns)} columns."]
    measures = [c.name for c in schema.columns if c.role == "measure"]
    dimensions = [c.name for c in schema.columns if c.role == "dimension"]
    times = [c for c in schema.columns if c.role == "time"]
    if measures:
        facts.append("Measures: " + ", ".join(measures) + ".")
    if dimensions:
        facts.append("Dimensions: " + ", ".join(dimensions) + ".")
    for column in times:
        if column.minimum is not None:
            facts.append(f"{column.name} spans {column.minimum} to {column.maximum}.")
    missing = [(c.name, c.nulls) for c in schema.columns if c.nulls]
    if missing:
        worst = sorted(missing, key=lambda item: -item[1])[:4]
        facts.append("Missing values: " + ", ".join(f"{n} ({k / max(schema.rows, 1):.1%})" for n, k in worst) + ".")
    else:
        facts.append("No missing values.")
    constant = [c.name for c in schema.columns if c.distinct == 1 and schema.rows > 1]
    if constant:
        facts.append("Constant columns: " + ", ".join(constant) + ".")
    return facts


def correlations(workspace: Workspace, table: str, columns: List[str]) -> List[List[Optional[float]]]:
    """The Pearson correlation matrix of numeric ``columns`` (``None`` where undefined)."""
    if not columns:
        return []
    pairs = []
    for i, a in enumerate(columns):
        for b in columns[i + 1:]:
            pairs.append(f"corr({quote_ident(a)}, {quote_ident(b)})")
    values = workspace.query(f"SELECT {', '.join(pairs)} FROM {quote_ident(table)}", limit=None,
                             count=False).rows[0] if pairs else []
    matrix: List[List[Optional[float]]] = [[1.0 if i == j else None for j in range(len(columns))]
                                           for i in range(len(columns))]
    k = 0
    for i in range(len(columns)):
        for j in range(i + 1, len(columns)):
            value = values[k]
            k += 1
            value = None if value is None or value != value else round(float(value), 4)
            matrix[i][j] = matrix[j][i] = value
    return matrix
