"""Typed data operations and the pipeline that compiles them to one DuckDB query.

An operation is a small JSON object such as ``{"op": "filter", "where": "status != 'Cancelled'"}``. A pipeline is a
list of them applied to a dataset or to an earlier result. Compilation checks every operation against the columns it
receives (DuckDB binds each stage too, so type errors are caught before anything runs), renders it as one SQL stage
and describes it in plain words, so people can read what happened to their data and the same list always produces
the same result.

Operations: filter, select, drop, rename, derive, cast, fill_missing, drop_missing, aggregate, sort, limit, top_n,
join, outliers, distinct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .expressions import ExpressionError, literal_sql, parse_expression
from .schema import MEASURE_AGGREGATIONS, ColumnInfo, LogicalType, TableSchema, logical_type, quote_ident
from .workspace import Workspace, WorkspaceError

__all__ = ["OperationError", "Stage", "CompiledPipeline", "compile_pipeline", "describe_operation", "OPERATIONS",
           "TIME_BUCKETS", "CAST_TYPES", "normalize_operation"]

TIME_BUCKETS = ("year", "quarter", "month", "week", "day")
CAST_TYPES = {"integer": "BIGINT", "number": "DOUBLE", "text": "VARCHAR", "date": "DATE", "timestamp": "TIMESTAMP",
              "boolean": "BOOLEAN"}
_AGG_SQL = {"sum": "SUM({x})", "avg": "AVG({x})", "min": "MIN({x})", "max": "MAX({x})", "median": "MEDIAN({x})",
            "count": "COUNT({x})", "count_distinct": "COUNT(DISTINCT {x})", "stddev": "STDDEV_SAMP({x})"}
_AGG_ALIASES = {"mean": "avg", "average": "avg", "total": "sum", "distinct": "count_distinct", "nunique": "count_distinct",
                "std": "stddev", "count_unique": "count_distinct"}
_AGG_WORDS = {"sum": "total", "avg": "average", "min": "minimum", "max": "maximum", "median": "median",
              "count": "count of", "count_distinct": "distinct count of", "stddev": "standard deviation of"}


class OperationError(ValueError):
    """An operation that does not fit its input; ``index`` is its position in the pipeline."""

    def __init__(self, index: int, op: str, message: str) -> None:
        super().__init__(f"step {index + 1} ({op}): {message}")
        self.index = index
        self.op = op
        self.message = message


@dataclass
class Stage:
    """One compiled operation: what it does in words, its SQL and the columns it produces."""

    op: Dict[str, Any]
    description: str
    sql: str
    columns: List[Tuple[str, str]]


@dataclass
class CompiledPipeline:
    """The pipeline as a single query (``sql``), stage by stage, with the output schema."""

    source: str
    stages: List[Stage] = field(default_factory=list)
    sql: str = ""
    schema: Optional[TableSchema] = None

    def descriptions(self) -> List[str]:
        return [stage.description for stage in self.stages]


# ---- helpers -----------------------------------------------------------------------------------------------------

def _column(schema: TableSchema, name: Any, what: str = "column") -> ColumnInfo:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what} must be a column name")
    column = schema.column(name)
    if column is None:
        raise ValueError(f"unknown {what} {name!r}; columns: {', '.join(schema.names()[:30])}")
    return column


def _columns(schema: TableSchema, names: Any, what: str = "columns") -> List[ColumnInfo]:
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)) or not names:
        raise ValueError(f"{what} must be a non-empty list of column names")
    return [_column(schema, n) for n in names]


def _alias(name: str) -> str:
    text = str(name).strip()
    if not text:
        raise ValueError("a result column needs a name")
    if len(text) > 64:
        raise ValueError("column names are limited to 64 characters")
    return text


def _schema_from(columns: Sequence[Tuple[str, str]], name: str = "result") -> TableSchema:
    return TableSchema(name, [ColumnInfo(n, t, logical_type(t)) for n, t in columns])


def normalize_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Accept common spellings from people and models (``type`` for ``op``, ``mean`` for ``avg``…)."""
    if not isinstance(op, dict):
        raise ValueError("an operation must be an object")
    data = dict(op)
    kind = str(data.pop("op", data.pop("type", data.pop("operation", "")))).strip().lower()
    data = {"op": kind, **data}
    if kind == "filter" and "where" not in data:
        for key in ("expression", "condition", "predicate"):
            if key in data:
                data["where"] = data.pop(key)
    if kind == "aggregate":
        if isinstance(data.get("group_by"), str):
            data["group_by"] = [data["group_by"]]
        measures = data.get("measures") or data.get("metrics") or []
        data.pop("metrics", None)
        fixed = []
        for m in measures if isinstance(measures, list) else [measures]:
            if isinstance(m, dict):
                m = dict(m)
                agg = str(m.get("agg", m.pop("aggregation", m.pop("function", "sum")))).lower()
                m["agg"] = _AGG_ALIASES.get(agg, agg)
                if "alias" in m and "as" not in m:
                    m["as"] = m.pop("alias")
                if "name" in m and "as" not in m:
                    m["as"] = m.pop("name")
                if "filter" in m and "where" not in m:
                    m["where"] = m.pop("filter")
            fixed.append(m)
        data["measures"] = fixed
    if kind == "sort":
        by = data.get("by", data.pop("columns", None))
        if isinstance(by, (str, dict)):
            by = [by]
        items = []
        for item in by or []:
            if isinstance(item, str):
                descending = item.startswith("-")
                items.append({"column": item.lstrip("-"), "descending": descending or bool(data.get("descending"))})
            elif isinstance(item, dict):
                order = str(item.get("order", "")).lower()
                items.append({"column": item.get("column"), "descending": bool(item.get("descending")) or order == "desc"})
        data["by"] = items
        data.pop("descending", None)
    if kind == "limit" and "count" not in data:
        data["count"] = data.pop("n", data.pop("rows", 100))
    if kind == "top_n":
        if "count" not in data:
            data["count"] = data.pop("n", 10)
        if "column" not in data:
            data["column"] = data.pop("by", None)
    return data


# ---- operations --------------------------------------------------------------------------------------------------

Compiled = Tuple[str, str]  # (select SQL over {src}, description)


def _filter(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    expression = parse_expression(op.get("where", ""), schema)
    if expression.kind not in ("boolean", "any"):
        raise ValueError("the filter condition must be true or false for each row")
    clause = "QUALIFY" if expression.window else "WHERE"
    return f"SELECT * FROM {src} {clause} {expression.sql}", f"Keep rows where {expression.text}"


def _select(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    columns = _columns(schema, op.get("columns"))
    return (f"SELECT {', '.join(quote_ident(c.name) for c in columns)} FROM {src}",
            "Keep columns " + ", ".join(c.name for c in columns))


def _drop(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    columns = _columns(schema, op.get("columns"))
    if len(columns) >= len(schema.columns):
        raise ValueError("cannot remove every column")
    return (f"SELECT * EXCLUDE ({', '.join(quote_ident(c.name) for c in columns)}) FROM {src}",
            "Remove columns " + ", ".join(c.name for c in columns))


def _rename(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    mapping = op.get("mapping") or op.get("columns")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("mapping must be an object of old name to new name")
    pairs = [(_column(schema, old).name, _alias(new)) for old, new in mapping.items()]
    renamed = {old: new for old, new in pairs}
    final = [renamed.get(c.name, c.name) for c in schema.columns]
    if len({n.lower() for n in final}) != len(final):
        raise ValueError("the new names clash with existing columns")
    parts = [f"{quote_ident(c.name)} AS {quote_ident(renamed[c.name])}" if c.name in renamed else quote_ident(c.name)
             for c in schema.columns]
    return f"SELECT {', '.join(parts)} FROM {src}", "Rename " + ", ".join(f"{a} to {b}" for a, b in pairs)


def _derive(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    name = _alias(op.get("name") or op.get("as") or "")
    expression = parse_expression(op.get("expression", ""), schema)
    if schema.column(name) is not None:
        return (f"SELECT * REPLACE ({expression.sql} AS {quote_ident(schema.column(name).name)}) FROM {src}",
                f"Recalculate {name} as {expression.text}")
    return f"SELECT *, {expression.sql} AS {quote_ident(name)} FROM {src}", f"Add {name} = {expression.text}"


def _cast(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    column = _column(schema, op.get("column"))
    target = str(op.get("to", "")).lower()
    if target not in CAST_TYPES:
        raise ValueError(f"'to' must be one of {', '.join(CAST_TYPES)}")
    sql = f"TRY_CAST({quote_ident(column.name)} AS {CAST_TYPES[target]})"
    return (f"SELECT * REPLACE ({sql} AS {quote_ident(column.name)}) FROM {src}",
            f"Convert {column.name} to {target} (invalid values become missing)")


def _fill_missing(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    column = _column(schema, op.get("column"))
    method = str(op.get("method", "value" if "value" in op else "zero")).lower()
    name = quote_ident(column.name)
    if method in ("mean", "median"):
        if not column.is_numeric:
            raise ValueError(f"{method} filling needs a numeric column")
        fill = f"{'AVG' if method == 'mean' else 'MEDIAN'}({name}) OVER ()"
        text = f"the column {method}"
    elif method == "mode":
        fill = f"(SELECT mode({name}) FROM {src})"
        text = "the most frequent value"
    elif method == "zero":
        if not column.is_numeric:
            raise ValueError("zero filling needs a numeric column; use a value instead")
        fill, text = "0", "0"
    elif method == "value":
        value = op.get("value")
        fill, text = literal_sql(value), repr(value)
    elif method in ("previous", "forward"):
        order = _column(schema, op.get("order_by"), "order_by column")
        fill = f"LAST_VALUE({name} IGNORE NULLS) OVER (ORDER BY {quote_ident(order.name)} ROWS UNBOUNDED PRECEDING)"
        text = f"the previous value by {order.name}"
    else:
        raise ValueError("method must be zero, value, mean, median, mode or previous")
    return (f"SELECT * REPLACE (coalesce({name}, {fill}) AS {name}) FROM {src}",
            f"Fill missing {column.name} with {text}")


def _drop_missing(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    columns = _columns(schema, op.get("columns")) if op.get("columns") else schema.columns
    condition = " AND ".join(f"{quote_ident(c.name)} IS NOT NULL" for c in columns)
    which = ", ".join(c.name for c in columns) if op.get("columns") else "any column"
    return f"SELECT * FROM {src} WHERE {condition}", f"Remove rows with missing {which}"


def _group_key(item: Any, schema: TableSchema) -> Tuple[str, str, str]:
    """(sql, alias, words) of one aggregate group key: a column, a column with a time bucket, or an expression."""
    if isinstance(item, str):
        column = schema.column(item)
        if column is not None:
            return quote_ident(column.name), column.name, column.name
        try:
            expression = parse_expression(item, schema)
        except ExpressionError as exc:
            raise ValueError(f"unknown group column {item!r} ({exc})") from None
        return _expression_key(expression, "", item)
    if not isinstance(item, dict):
        raise ValueError("group_by items are column names, {column, bucket} or {expression, as}")
    if item.get("expression") and not item.get("column"):
        return _expression_key(parse_expression(item["expression"], schema), str(item.get("as") or ""), item["expression"])
    column = _column(schema, item.get("column"), "group column")
    bucket = str(item.get("bucket") or "").lower()
    if not bucket:
        alias = _alias(item.get("as") or column.name)
        return quote_ident(column.name), alias, column.name
    if bucket not in TIME_BUCKETS:
        raise ValueError(f"bucket must be one of {', '.join(TIME_BUCKETS)}")
    if not column.is_temporal:
        raise ValueError(f"{column.name} is not a date, so it cannot be grouped by {bucket}")
    alias = _alias(item.get("as") or bucket)
    if bucket == "year":
        sql = f"year({quote_ident(column.name)})"
    else:
        sql = f"CAST(date_trunc('{bucket}', {quote_ident(column.name)}) AS DATE)"
    return sql, alias, f"{bucket} of {column.name}"


def _expression_key(expression, alias: str, text: str) -> Tuple[str, str, str]:
    if expression.window:
        raise ValueError("cannot group by a window function")
    if not alias:
        alias = re.sub(r"\W+", "_", text).strip("_").lower()[:40] or "group"
        match = re.fullmatch(r"(year|quarter|month|week|day|weekday|month_name)\(\s*[\w\"`]+\s*\)", text.strip(), re.I)
        if match:
            alias = match.group(1).lower()
    return expression.sql, _alias(alias), text


def _aggregate(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    keys = [_group_key(item, schema) for item in (op.get("group_by") or [])]
    measures = op.get("measures") or []
    if not measures:
        raise ValueError("aggregate needs at least one measure")
    parts: List[str] = [f"{sql} AS {quote_ident(alias)}" for sql, alias, _ in keys]
    aliases = [alias.lower() for _, alias, _ in keys]
    words: List[str] = []
    for measure in measures:
        if not isinstance(measure, dict):
            raise ValueError("measures are objects such as {\"column\": \"revenue\", \"agg\": \"sum\"}")
        agg = _AGG_ALIASES.get(str(measure.get("agg", "sum")).lower(), str(measure.get("agg", "sum")).lower())
        if agg not in _AGG_SQL:
            raise ValueError(f"unknown aggregation {agg!r}; use one of {', '.join(MEASURE_AGGREGATIONS)}")
        if measure.get("expression"):
            expression = parse_expression(measure["expression"], schema)
            if expression.window:
                raise ValueError("window functions cannot be aggregated")
            value_sql, value_text = expression.sql, f"({expression.text})"
            default_alias = None
        elif measure.get("column") in (None, "", "*"):
            if agg != "count":
                raise ValueError(f"{agg} needs a column")
            value_sql, value_text, default_alias = "*", "rows", "rows"
        else:
            column = _column(schema, measure.get("column"), "measure column")
            if agg in ("sum", "avg", "median", "stddev") and not column.is_numeric:
                raise ValueError(f"{agg} needs a numeric column, but {column.name} is {column.type}")
            value_sql, value_text = quote_ident(column.name), column.name
            default_alias = column.name if agg == "sum" else f"{agg}_{column.name}"
        alias = measure.get("as") or default_alias
        if not alias:
            raise ValueError("a measure computed from an expression needs a name ('as')")
        alias = _alias(alias)
        if alias.lower() in aliases:
            raise ValueError(f"two result columns are called {alias!r}; give each measure its own 'as'")
        aliases.append(alias.lower())
        sql = _AGG_SQL[agg].format(x=value_sql)
        text = f"{_AGG_WORDS[agg]} {value_text}"
        if measure.get("where"):
            condition = parse_expression(measure["where"], schema)
            sql += f" FILTER (WHERE {condition.sql})"
            text += f" where {condition.text}"
        parts.append(f"{sql} AS {quote_ident(alias)}")
        words.append(f"{text} as {alias}" if alias != value_text else text)
    group = f" GROUP BY {', '.join(str(i + 1) for i in range(len(keys)))}" if keys else ""
    order = f" ORDER BY {', '.join(str(i + 1) for i in range(len(keys)))}" if keys else ""
    by = " by " + ", ".join(w for _, _, w in keys) if keys else ""
    return f"SELECT {', '.join(parts)} FROM {src}{group}{order}", "Calculate " + "; ".join(words) + by


def _sort(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    items = op.get("by") or []
    if not items:
        raise ValueError("sort needs at least one column in 'by'")
    parts, words = [], []
    for item in items:
        column = _column(schema, item.get("column") if isinstance(item, dict) else item, "sort column")
        descending = bool(item.get("descending")) if isinstance(item, dict) else False
        parts.append(f"{quote_ident(column.name)} {'DESC' if descending else 'ASC'} NULLS LAST")
        words.append(f"{column.name} {'descending' if descending else 'ascending'}")
    return f"SELECT * FROM {src} ORDER BY {', '.join(parts)}", "Sort by " + ", ".join(words)


def _count(op: Dict[str, Any], default: int = 10) -> int:
    try:
        value = int(op.get("count", default))
    except (TypeError, ValueError):
        raise ValueError("count must be a whole number") from None
    if value < 1 or value > 1_000_000:
        raise ValueError("count must be between 1 and 1,000,000")
    return value


def _limit(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    count = _count(op, 100)
    return f"SELECT * FROM {src} LIMIT {count}", f"Keep the first {count:,} rows"


def _top_n(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    column = _column(schema, op.get("column"), "ranking column")
    count = _count(op)
    descending = op.get("descending", True) is not False
    direction = "DESC" if descending else "ASC"
    within = op.get("within")
    if within:
        group = _column(schema, within, "group column")
        sql = (f"SELECT * FROM {src} QUALIFY ROW_NUMBER() OVER (PARTITION BY {quote_ident(group.name)} "
               f"ORDER BY {quote_ident(column.name)} {direction} NULLS LAST) <= {count} "
               f"ORDER BY {quote_ident(group.name)}, {quote_ident(column.name)} {direction} NULLS LAST")
        return sql, f"Keep the {'top' if descending else 'bottom'} {count} rows by {column.name} within each {group.name}"
    return (f"SELECT * FROM {src} ORDER BY {quote_ident(column.name)} {direction} NULLS LAST LIMIT {count}",
            f"Keep the {'top' if descending else 'bottom'} {count} rows by {column.name}")


def _join(op: Dict[str, Any], schema: TableSchema, src: str, ws: Workspace) -> Compiled:
    other = ws.dataset(str(op.get("dataset") or ""))
    if other is None:
        raise ValueError(f"unknown dataset {op.get('dataset')!r}; datasets: {', '.join(ws.datasets)}")
    how = str(op.get("how", "left")).lower()
    if how not in ("left", "inner"):
        raise ValueError("how must be left or inner")
    on = op.get("on")
    if isinstance(on, str):
        on = [[on, on]]
    pairs = []
    for item in on or []:
        left, right = (item, item) if isinstance(item, str) else (item[0], item[1])
        pairs.append((_column(schema, left, "join column").name, _column(other.schema, right, "join column").name))
    if not pairs:
        raise ValueError("join needs 'on': the matching columns")
    right_names = [c.name for c in other.schema.columns]
    wanted = op.get("columns")
    if wanted:
        right_names = [_column(other.schema, n).name for n in ([wanted] if isinstance(wanted, str) else wanted)]
    right_keys = {r for _, r in pairs}
    left_names = {c.lower() for c in schema.names()}
    extra = []
    for name in right_names:
        if name in right_keys and not wanted:
            continue
        alias = name if name.lower() not in left_names else f"{other.name}_{name}"
        extra.append(f"r.{quote_ident(name)} AS {quote_ident(alias)}")
    if not extra:
        raise ValueError("the join adds no columns")
    condition = " AND ".join(f"l.{quote_ident(a)} = r.{quote_ident(b)}" for a, b in pairs)
    sql = (f"SELECT l.*, {', '.join(extra)} FROM {src} AS l {how.upper()} JOIN {quote_ident(other.name)} AS r "
           f"ON {condition}")
    return sql, f"Add {other.name} columns ({how} join on {', '.join(a for a, _ in pairs)})"


def _outliers(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    column = _column(schema, op.get("column"))
    if not column.is_numeric:
        raise ValueError("outlier detection needs a numeric column")
    method = str(op.get("method", "iqr")).lower()
    name = quote_ident(column.name)
    if method == "iqr":
        k = float(op.get("threshold", 1.5))
        stats = (f"quantile_cont({name}, 0.25) OVER () AS fds_q1, quantile_cont({name}, 0.75) OVER () AS fds_q3")
        sql = (f"SELECT * EXCLUDE (fds_q1, fds_q3), CASE WHEN {name} < fds_q1 THEN fds_q1 - {name} ELSE {name} - fds_q3 END "
               f"/ NULLIF(fds_q3 - fds_q1, 0) AS outlier_score FROM (SELECT *, {stats} FROM {src}) "
               f"WHERE {name} < fds_q1 - {k} * (fds_q3 - fds_q1) OR {name} > fds_q3 + {k} * (fds_q3 - fds_q1) "
               f"ORDER BY outlier_score DESC")
        return sql, f"Keep unusual {column.name} values (outside {k:g} × the interquartile range)"
    if method == "zscore":
        k = float(op.get("threshold", 3.0))
        sql = (f"SELECT * FROM (SELECT *, ({name} - AVG({name}) OVER ()) / NULLIF(STDDEV_SAMP({name}) OVER (), 0) "
               f"AS outlier_score FROM {src}) WHERE abs(outlier_score) > {k} ORDER BY abs(outlier_score) DESC")
        return sql, f"Keep unusual {column.name} values (more than {k:g} standard deviations from the mean)"
    raise ValueError("method must be iqr or zscore")


def _distinct(op: Dict[str, Any], schema: TableSchema, src: str, _ws: Workspace) -> Compiled:
    if op.get("columns"):
        columns = _columns(schema, op.get("columns"))
        names = ", ".join(quote_ident(c.name) for c in columns)
        return f"SELECT DISTINCT {names} FROM {src}", "Unique combinations of " + ", ".join(c.name for c in columns)
    return f"SELECT DISTINCT * FROM {src}", "Remove duplicate rows"


OPERATIONS: Dict[str, Callable[[Dict[str, Any], TableSchema, str, Workspace], Compiled]] = {
    "filter": _filter, "select": _select, "drop": _drop, "rename": _rename, "derive": _derive, "cast": _cast,
    "fill_missing": _fill_missing, "drop_missing": _drop_missing, "aggregate": _aggregate, "sort": _sort,
    "limit": _limit, "top_n": _top_n, "join": _join, "outliers": _outliers, "distinct": _distinct,
}


def compile_pipeline(workspace: Workspace, source_table: str, operations: Sequence[Dict[str, Any]],
                     source_schema: Optional[TableSchema] = None) -> CompiledPipeline:
    """Check and compile ``operations`` over ``source_table``; raises :class:`OperationError` at the first bad one.

    ``source_schema`` (the profiled dataset schema) lets the first stage see column roles; later stages use the
    types DuckDB reports.
    """
    try:
        columns = workspace.describe(f"SELECT * FROM {quote_ident(source_table)}")
    except WorkspaceError as exc:
        raise OperationError(-1, "source", str(exc)) from None
    schema = source_schema or _schema_from(columns, source_table)
    ctes: List[str] = [f"s0 AS (SELECT * FROM {quote_ident(source_table)})"]
    compiled = CompiledPipeline(source_table)
    for index, raw in enumerate(operations):
        try:
            op = normalize_operation(raw)
        except ValueError as exc:
            raise OperationError(index, "?", str(exc)) from None
        kind = op["op"]
        handler = OPERATIONS.get(kind)
        if handler is None:
            raise OperationError(index, kind or "?", f"unknown operation; use one of {', '.join(OPERATIONS)}")
        try:
            sql, description = handler(op, schema, f"s{index}", workspace)
        except (ValueError, ExpressionError) as exc:
            raise OperationError(index, kind, str(exc)) from None
        ctes.append(f"s{index + 1} AS ({sql})")
        query = "WITH " + ", ".join(ctes) + f" SELECT * FROM s{index + 1}"
        try:
            columns = workspace.describe(query)
        except WorkspaceError as exc:
            raise OperationError(index, kind, str(exc)) from None
        compiled.stages.append(Stage(op, description, sql, columns))
        schema = _carry_formats(op, schema, _carry_roles(_schema_from(columns), schema))
    compiled.sql = "WITH " + ", ".join(ctes) + f" SELECT * FROM s{len(ctes) - 1}"
    compiled.schema = schema
    return compiled


def _carry_roles(new: TableSchema, old: TableSchema) -> TableSchema:
    """Keep semantic roles of columns that pass through unchanged, so later steps still know what they mean."""
    for column in new.columns:
        previous = old.column(column.name)
        if previous is not None and previous.type == column.type:
            column.role, column.aggregation, column.format = previous.role, previous.aggregation, previous.format
            column.description, column.synonyms = previous.description, list(previous.synonyms)
        elif column.type in LogicalType.TEMPORAL:
            column.role = "time"
        elif column.is_numeric:
            column.role = "measure"
    return new


_FORMATS = ("", "currency", "percent", "integer")
_KEEPS_FORMAT = ("sum", "avg", "min", "max", "median", "stddev")


def _carry_formats(op: Dict[str, Any], old: TableSchema, new: TableSchema) -> TableSchema:
    """Display formats of new columns: given explicitly (``format``) or inherited from the aggregated column."""
    if op["op"] == "derive" and op.get("format") in _FORMATS:
        column = new.column(str(op.get("name") or op.get("as") or ""))
        if column is not None:
            column.format = op["format"]
    if op["op"] == "aggregate":
        for measure in op.get("measures") or []:
            if not isinstance(measure, dict):
                continue
            agg = _AGG_ALIASES.get(str(measure.get("agg", "sum")).lower(), str(measure.get("agg", "sum")).lower())
            source = old.column(str(measure.get("column") or "")) if measure.get("column") else None
            alias = measure.get("as") or (source.name if source and agg == "sum" else
                                          f"{agg}_{source.name}" if source else "rows")
            column = new.column(str(alias))
            if column is None:
                continue
            column.role = "measure"
            if measure.get("format") in _FORMATS:
                column.format = measure["format"]
            elif agg in ("count", "count_distinct"):
                column.format = "integer"
            elif source is not None and agg in _KEEPS_FORMAT:
                column.format = source.format
                column.aggregation = source.aggregation
    return new


def describe_operation(op: Dict[str, Any]) -> str:
    """A short label for an operation without compiling it (for lists and history)."""
    data = normalize_operation(op)
    kind = data["op"]
    if kind == "filter":
        return f"Filter: {data.get('where', '')}"
    if kind == "derive":
        return f"Add {data.get('name') or data.get('as')} = {data.get('expression', '')}"
    if kind == "aggregate":
        keys = [k if isinstance(k, str) else f"{k.get('bucket', '')} {k.get('column', '')}".strip()
                for k in data.get("group_by") or []]
        return "Aggregate" + (f" by {', '.join(keys)}" if keys else "")
    if kind == "sort":
        return "Sort by " + ", ".join(f"{i.get('column')}{' desc' if i.get('descending') else ''}"
                                      for i in data.get("by") or [])
    if kind == "top_n":
        return f"Top {data.get('count')} by {data.get('column')}"
    if kind in ("select", "drop", "distinct", "drop_missing"):
        cols = data.get("columns") or []
        return f"{kind.replace('_', ' ').capitalize()}: {', '.join(cols) if isinstance(cols, list) else cols}"
    if kind == "rename":
        return "Rename " + ", ".join(f"{a} to {b}" for a, b in (data.get("mapping") or {}).items())
    if kind == "join":
        return f"Join {data.get('dataset')}"
    if kind == "outliers":
        return f"Outliers in {data.get('column')}"
    if kind == "cast":
        return f"Convert {data.get('column')} to {data.get('to')}"
    if kind == "fill_missing":
        return f"Fill missing {data.get('column')}"
    if kind == "limit":
        return f"First {data.get('count')} rows"
    return kind
