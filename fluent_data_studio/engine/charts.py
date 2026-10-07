"""Visualization specs: a chart is structured state, never generated code.

A :class:`ChartSpec` names a chart kind and the fields on its channels (x, y measures, colour split), how values are
aggregated, sorted and limited, and its titles. :func:`prepare_chart` checks the spec against the data, repairs choices
that would mislead (a pie of averages, a line through unordered categories, sixty bars) and records why in ``notes``,
compiles the spec into ordinary :mod:`transforms <fluent_data_studio.engine.transforms>` operations, runs them and
returns :class:`ChartData` series that any renderer can draw. Editing a chart is editing the spec.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .schema import LogicalType, Role, TableSchema, quote_ident
from .transforms import TIME_BUCKETS, CompiledPipeline, compile_pipeline
from .workspace import Workspace, plain_value

__all__ = ["ChartSpec", "ChartData", "ChartSeries", "ChartError", "prepare_chart", "recommend_chart", "CHART_KINDS",
           "AGGREGATIONS"]

CHART_KINDS = ("bar", "line", "area", "scatter", "histogram", "pie", "heatmap", "table", "metric")
AGGREGATIONS = ("sum", "avg", "min", "max", "median", "count", "count_distinct", "none")
MAX_BARS = 30
MAX_PIE_SLICES = 8
MAX_SERIES = 8
MAX_SCATTER_POINTS = 5000
MAX_LINE_POINTS = 2000


class ChartError(ValueError):
    """A chart that cannot be drawn from this data."""


@dataclass
class ChartSpec:
    """What to draw.

    ``x`` is the category, time or numeric field; ``y`` lists measures (empty means a row count); ``color`` splits a
    measure into one series per value; ``aggregate`` combines rows that share x (``"none"`` plots rows as they are);
    ``sort`` is ``"x"``, ``"value_desc"``, ``"value_asc"`` or ``""`` (automatic); ``limit`` keeps the largest
    categories. ``stacked`` stacks bar and area series.
    """

    kind: str = "bar"
    x: str = ""
    y: List[str] = field(default_factory=list)
    color: str = ""
    aggregate: str = "sum"
    x_bucket: str = ""
    sort: str = ""
    limit: int = 0
    filter: str = ""
    title: str = ""
    x_title: str = ""
    y_title: str = ""
    legend: bool = True
    stacked: bool = False
    bins: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChartSpec":
        data = dict(data or {})
        if isinstance(data.get("y"), str):
            data["y"] = [data["y"]] if data["y"] else []
        for alias, key in (("type", "kind"), ("group", "color"), ("series", "color"), ("bucket", "x_bucket"),
                           ("aggregation", "aggregate")):
            if alias in data and key not in data:
                data[key] = data.pop(alias)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        spec = cls(**known)
        spec.kind = str(spec.kind or "bar").lower()
        spec.aggregate = str(spec.aggregate or "sum").lower().replace("mean", "avg")
        spec.y = [str(v) for v in (spec.y or []) if v]
        spec.limit = int(spec.limit or 0)
        spec.bins = int(spec.bins or 0)
        for key in ("x", "color", "x_bucket", "sort", "filter", "title", "x_title", "y_title"):
            value = getattr(spec, key)
            setattr(spec, key, "" if value is None else str(value))
        return spec

    def copy(self, **changes: Any) -> "ChartSpec":
        data = self.to_dict()
        data.update(changes)
        return ChartSpec.from_dict(data)


@dataclass
class ChartSeries:
    name: str
    xs: List[float]
    ys: List[Optional[float]]
    labels: Optional[List[str]] = None


@dataclass
class ChartData:
    """A prepared chart: the final spec, the series to draw and the rows behind them.

    ``categories`` holds the x labels of categorical and bucketed time axes (series x values are then 0, 1, 2…).
    ``notes`` explains every change made to the requested spec. ``matrix`` holds heatmap values (rows × columns).
    """

    spec: ChartSpec
    series: List[ChartSeries]
    categories: Optional[List[str]]
    columns: List[str]
    rows: List[Tuple[Any, ...]]
    notes: List[str] = field(default_factory=list)
    x_format: str = ""
    y_format: str = ""
    pipeline: Optional[CompiledPipeline] = None
    row_labels: List[str] = field(default_factory=list)
    matrix: List[List[Optional[float]]] = field(default_factory=list)
    total_rows: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"spec": self.spec.to_dict(), "notes": list(self.notes), "categories": self.categories,
                "series": [{"name": s.name, "xs": s.xs, "ys": s.ys} for s in self.series],
                "columns": self.columns, "rows": [[plain_value(v) for v in r] for r in self.rows[:200]]}


def _bucket_for_span(minimum: Any, maximum: Any) -> str:
    try:
        lo = _dt.date.fromisoformat(str(minimum)[:10])
        hi = _dt.date.fromisoformat(str(maximum)[:10])
    except ValueError:
        return "month"
    days = (hi - lo).days
    if days > 365 * 6:
        return "year"
    if days > 365 * 2:
        return "quarter"
    if days > 120:
        return "month"
    if days > 30:
        return "week"
    return "day"


def recommend_chart(schema: TableSchema, x: str = "", y: Sequence[str] = (), color: str = "") -> ChartSpec:
    """A sensible default chart for the given fields (or for the table when no field is named)."""
    measures = [c for c in schema.columns if c.role == Role.MEASURE]
    times = [c for c in schema.columns if c.role == Role.TIME and c.is_temporal]
    dimensions = [c for c in schema.columns if c.role == Role.DIMENSION]
    x_column = schema.column(x) if x else None
    y_names = [schema.column(n).name for n in y if schema.column(n)]
    if x_column is None:
        if times:
            x_column = times[0]
        elif dimensions:
            x_column = min(dimensions, key=lambda c: c.distinct or 10_000)
        elif len(measures) >= 2:
            return ChartSpec("scatter", measures[0].name, [measures[1].name], aggregate="none")
        elif measures:
            return ChartSpec("histogram", measures[0].name, [], aggregate="count")
        else:
            return ChartSpec("table")
    if not y_names and measures and x_column.role != Role.MEASURE:
        y_names = [measures[0].name]
    if x_column.is_numeric and x_column.role == Role.MEASURE:
        if y_names and schema.column(y_names[0]).is_numeric and y_names[0] != x_column.name:
            return ChartSpec("scatter", x_column.name, y_names[:1], color=color, aggregate="none")
        return ChartSpec("histogram", x_column.name, [], aggregate="count")
    aggregate = schema.column(y_names[0]).aggregation if y_names else "count"
    if aggregate not in AGGREGATIONS or aggregate == "none":
        aggregate = "sum"
    if x_column.is_temporal or x_column.role == Role.TIME:
        return ChartSpec("line", x_column.name, y_names[:2], color=color, aggregate=aggregate)
    return ChartSpec("bar", x_column.name, y_names[:2], color=color, aggregate=aggregate, sort="value_desc")


def _check_fields(spec: ChartSpec, schema: TableSchema) -> None:
    for name in [spec.x, spec.color] + list(spec.y):
        if name and schema.column(name) is None:
            raise ChartError(f"unknown field {name!r}; fields: {', '.join(schema.names()[:30])}")


def _repair(spec: ChartSpec, schema: TableSchema, notes: List[str], pre_aggregated: bool = False) -> ChartSpec:
    """Fix choices that would mislead or fail, explaining each change."""
    if spec.kind not in CHART_KINDS:
        notes.append(f"Unknown chart type {spec.kind!r}; using a bar chart.")
        spec.kind = "bar"
    if spec.aggregate not in AGGREGATIONS:
        notes.append(f"Unknown aggregation {spec.aggregate!r}; using sum.")
        spec.aggregate = "sum"
    if spec.kind in ("table", "metric"):
        return spec
    x = schema.column(spec.x) if spec.x else None
    if spec.kind == "histogram":
        if x is None or not x.is_numeric:
            raise ChartError("a histogram needs a numeric field on x")
        spec.y, spec.color, spec.aggregate = [], "", "count"
        return spec
    if x is None:
        raise ChartError("choose a field for the x axis")
    spec.x = x.name
    spec.y = [schema.column(n).name for n in spec.y]
    if spec.color:
        spec.color = schema.column(spec.color).name
    if spec.kind == "scatter":
        if not spec.y:
            raise ChartError("a scatter plot needs a numeric field on y")
        y = schema.column(spec.y[0])
        if not (x.is_numeric and y.is_numeric):
            notes.append("A scatter plot needs two numeric fields; showing a bar chart instead.")
            spec.kind = "bar"
        else:
            spec.y = spec.y[:1]
            spec.aggregate = "none"
            return spec
    for name in spec.y:
        column = schema.column(name)
        if not column.is_numeric and spec.aggregate not in ("count", "count_distinct"):
            notes.append(f"{name} is not numeric, so its distinct values are counted.")
            spec.aggregate = "count_distinct"
    if spec.aggregate == "none" and spec.kind in ("bar", "pie", "heatmap"):
        notes.append("Rows that share an x value are added up.")
        spec.aggregate = "sum"
    if spec.kind in ("line", "area"):
        if not (x.is_temporal or x.is_numeric or x.role == Role.TIME):
            notes.append(f"{x.name} has no natural order, so a line would suggest a trend that is not there; "
                         "showing a bar chart instead.")
            spec.kind = "bar"
    if spec.x_bucket and spec.x_bucket not in TIME_BUCKETS:
        notes.append(f"Unknown period {spec.x_bucket!r}; choosing one from the date range.")
        spec.x_bucket = ""
    if x.is_temporal and not spec.x_bucket:
        spec.x_bucket = _bucket_for_span(x.minimum, x.maximum)
        if pre_aggregated and spec.aggregate not in ("sum", "count"):
            # one row per date already: a coarser period would average averages (or take a max of maxima)
            spec.x_bucket = "day"
    if not x.is_temporal:
        spec.x_bucket = ""
    if spec.kind == "pie":
        reason = ""
        if len(spec.y) > 1:
            reason = "a pie shows one measure"
        elif spec.color:
            reason = "a pie cannot also split by colour"
        elif spec.aggregate not in ("sum", "count", "count_distinct"):
            reason = f"{spec.aggregate} values are not parts of a whole"
        elif x.is_temporal or x.role == Role.TIME:
            reason = "periods are better compared on an axis"
        if reason:
            notes.append(f"Showing a bar chart instead of a pie: {reason}.")
            spec.kind = "bar"
    if spec.kind == "heatmap" and (not spec.color or len(spec.y) > 1):
        if not spec.color:
            notes.append("A heatmap needs a second category (colour field); showing a bar chart instead.")
            spec.kind = "bar"
        else:
            spec.y = spec.y[:1]
    return spec


def _y_label(spec: ChartSpec, name: str) -> str:
    return name if spec.aggregate in ("sum", "none") else f"{spec.aggregate.replace('_', ' ')} of {name}"


def prepare_chart(workspace: Workspace, table: str, spec: ChartSpec, schema: Optional[TableSchema] = None
                  ) -> ChartData:
    """Check, repair, compile and run ``spec`` over ``table``; see the module docstring."""
    schema = schema or workspace.table_schema(table)
    spec = ChartSpec.from_dict(spec.to_dict())
    _check_fields(spec, schema)
    notes: List[str] = []
    spec = _repair(spec, schema, notes, _one_row_per_key(workspace, table, spec, schema))
    operations: List[Dict[str, Any]] = []
    if spec.filter:
        operations.append({"op": "filter", "where": spec.filter})
    if spec.kind == "table":
        pipeline = compile_pipeline(workspace, table, operations, schema)
        result = workspace.query(pipeline.sql, limit=500)
        return ChartData(spec, [], None, result.columns, result.rows, notes, pipeline=pipeline, total_rows=result.total)
    if spec.kind == "metric":
        return _metric(workspace, table, spec, schema, operations, notes)
    if spec.kind == "histogram":
        return _histogram(workspace, table, spec, schema, operations, notes)
    if spec.kind == "scatter":
        return _scatter(workspace, table, spec, schema, operations, notes)
    return _categorical(workspace, table, spec, schema, operations, notes)


def _one_row_per_key(workspace: Workspace, table: str, spec: ChartSpec, schema: TableSchema) -> bool:
    """Whether the table already has one row per (x, colour): an aggregated result rather than raw rows."""
    x = schema.column(spec.x) if spec.x else None
    if x is None or not x.is_temporal:
        return False
    keys = [quote_ident(x.name)] + ([quote_ident(schema.column(spec.color).name)] if spec.color and
                                    schema.column(spec.color) else [])
    distinct = workspace.scalar(f"SELECT count(*) FROM (SELECT DISTINCT {', '.join(keys)} FROM {quote_ident(table)})")
    return int(distinct or 0) == int(workspace.scalar(f"SELECT count(*) FROM {quote_ident(table)}") or 0)


def _metric(workspace: Workspace, table: str, spec: ChartSpec, schema: TableSchema, operations: List[Dict[str, Any]],
            notes: List[str]) -> ChartData:
    if spec.y:
        aggregate = spec.aggregate if spec.aggregate != "none" else "sum"
        measures = [{"column": n, "agg": aggregate, "as": _y_label(spec.copy(aggregate=aggregate), n)} for n in spec.y]
    else:
        measures = [{"agg": "count", "as": "rows"}]
    operations = operations + [{"op": "aggregate", "group_by": [], "measures": measures}]
    pipeline = compile_pipeline(workspace, table, operations, schema)
    result = workspace.query(pipeline.sql, limit=1)
    fmt = schema.column(spec.y[0]).format if spec.y else "integer"
    return ChartData(spec, [], None, result.columns, result.rows, notes, y_format=fmt, pipeline=pipeline,
                     total_rows=result.total)


def _histogram(workspace: Workspace, table: str, spec: ChartSpec, schema: TableSchema,
               operations: List[Dict[str, Any]], notes: List[str]) -> ChartData:
    pipeline = compile_pipeline(workspace, table, operations + [{"op": "select", "columns": [spec.x]},
                                                                {"op": "filter", "where": f'"{spec.x}" is not null'}],
                                schema)
    result = workspace.query(pipeline.sql, limit=200_000)
    values = [float(v[0]) for v in result.rows]
    if result.truncated:
        notes.append(f"Binned a sample of {len(values):,} of {result.total:,} values.")
    x = schema.column(spec.x)
    return ChartData(spec, [ChartSeries(spec.x, values, [])], None, result.columns, result.rows[:500], notes,
                     x_format=x.format, pipeline=pipeline, total_rows=result.total)


def _scatter(workspace: Workspace, table: str, spec: ChartSpec, schema: TableSchema, operations: List[Dict[str, Any]],
             notes: List[str]) -> ChartData:
    fields = [spec.x, spec.y[0]] + ([spec.color] if spec.color else [])
    operations = operations + [{"op": "select", "columns": fields},
                               {"op": "filter", "where": f'"{spec.x}" is not null and "{spec.y[0]}" is not null'}]
    pipeline = compile_pipeline(workspace, table, operations, schema)
    total = int(workspace.scalar(f"SELECT count(*) FROM ({pipeline.sql})"))
    sql = pipeline.sql
    if total > MAX_SCATTER_POINTS:
        sql = f"SELECT * FROM ({pipeline.sql}) USING SAMPLE reservoir({MAX_SCATTER_POINTS} ROWS) REPEATABLE (7)"
        notes.append(f"Showing a random sample of {MAX_SCATTER_POINTS:,} of {total:,} points.")
    result = workspace.query(sql, limit=None, count=False)
    groups: Dict[str, Tuple[List[float], List[float]]] = {}
    for row in result.rows:
        key = str(plain_value(row[2])) if spec.color else spec.y[0]
        xs, ys = groups.setdefault(key, ([], []))
        xs.append(float(row[0]))
        ys.append(float(row[1]))
    if len(groups) > MAX_SERIES:
        keep = sorted(groups, key=lambda k: -len(groups[k][0]))[:MAX_SERIES]
        notes.append(f"Coloured the {MAX_SERIES} largest {spec.color} groups; the others are hidden.")
        groups = {k: groups[k] for k in keep}
    series = [ChartSeries(name, xs, ys) for name, (xs, ys) in groups.items()]
    correlation = workspace.scalar(f"SELECT corr({quote_ident(spec.x)}, {quote_ident(spec.y[0])}) FROM ({pipeline.sql})")
    if correlation is not None and correlation == correlation:
        notes.append(f"Pearson correlation: {correlation:.2f}.")
    return ChartData(spec, series, None, result.columns, result.rows[:500], notes,
                     x_format=schema.column(spec.x).format, y_format=schema.column(spec.y[0]).format,
                     pipeline=pipeline, total_rows=total)


def _categorical(workspace: Workspace, table: str, spec: ChartSpec, schema: TableSchema,
                 operations: List[Dict[str, Any]], notes: List[str]) -> ChartData:
    x = schema.column(spec.x)
    temporal = x.is_temporal or x.role == Role.TIME
    key: Any = {"column": spec.x, "bucket": spec.x_bucket, "as": spec.x} if spec.x_bucket else spec.x
    group_by = [key] + ([spec.color] if spec.color else [])
    if spec.y:
        measures = [{"column": n, "agg": spec.aggregate, "as": n} for n in spec.y]
    else:
        measures = [{"agg": "count", "as": "rows"}]
        spec.aggregate = "count"
    if spec.aggregate == "none":
        notes.append("Rows that share an x value are added up.")
        spec.aggregate = "sum"
        measures = [{"column": n, "agg": "sum", "as": n} for n in spec.y]
    operations = operations + [{"op": "aggregate", "group_by": group_by, "measures": measures}]
    pipeline = compile_pipeline(workspace, table, operations, schema)
    result = workspace.query(pipeline.sql, limit=None, count=False)
    value_names = [m["as"] for m in measures]
    x_index = 0
    color_index = 1 if spec.color else None
    first_value = 2 if spec.color else 1
    # totals per category decide sorting and limits
    totals: Dict[Any, float] = {}
    for row in result.rows:
        totals[row[x_index]] = totals.get(row[x_index], 0.0) + sum(float(v or 0) for v in row[first_value:first_value + 1])
    categories = list(totals)
    sort = spec.sort or ("x" if temporal or x.is_numeric else "value_desc")
    if sort == "x":
        categories.sort(key=lambda v: (v is None, v if v is not None else 0))
    elif sort in ("value_desc", "value_asc"):
        categories.sort(key=lambda v: totals[v], reverse=sort == "value_desc")
    limit = spec.limit
    cap = MAX_PIE_SLICES if spec.kind == "pie" else (MAX_LINE_POINTS if temporal and spec.kind != "bar" else MAX_BARS)
    if spec.kind == "heatmap":
        cap = 40
    if not limit and len(categories) > cap:
        limit = cap
        if spec.kind == "pie":
            notes.append(f"{len(categories)} categories are too many slices; showing a bar chart of the top {MAX_BARS}.")
            spec.kind = "bar"
            limit = MAX_BARS
        else:
            notes.append(f"Showing the {cap} largest of {len(categories)} {x.name} values.")
    if limit and len(categories) > limit:
        if sort == "x" and not temporal:
            keep = set(sorted(categories, key=lambda v: -totals[v])[:limit])
            categories = [c for c in categories if c in keep]
        elif sort == "x":
            categories = categories[-limit:]
            if not spec.limit:
                notes[-1] = f"Showing the latest {limit} periods."
        else:
            categories = categories[:limit]
    if spec.kind == "pie" and any(totals[c] < 0 for c in categories):
        notes.append("Negative values cannot be pie slices; showing a bar chart instead.")
        spec.kind = "bar"
    position = {c: i for i, c in enumerate(categories)}
    labels = [_label(c, spec.x_bucket) for c in categories]
    series: List[ChartSeries] = []
    if spec.kind == "heatmap":
        columns_seen: Dict[Any, None] = {}
        for row in result.rows:
            columns_seen.setdefault(row[color_index], None)
        column_keys = sorted(columns_seen, key=lambda v: (v is None, str(v)))[:40]
        matrix = [[None for _ in column_keys] for _ in categories]
        col_pos = {c: i for i, c in enumerate(column_keys)}
        for row in result.rows:
            if row[x_index] in position and row[color_index] in col_pos:
                matrix[position[row[x_index]]][col_pos[row[color_index]]] = _num(row[first_value])
        data = ChartData(spec, [], labels, result.columns, result.rows[:500], notes,
                         y_format=schema.column(spec.y[0]).format if spec.y else "integer", pipeline=pipeline,
                         row_labels=[str(plain_value(c)) for c in column_keys], matrix=matrix,
                         total_rows=len(result.rows))
        return data
    if spec.color:
        by_color: Dict[Any, List[Optional[float]]] = {}
        color_totals: Dict[Any, float] = {}
        for row in result.rows:
            if row[x_index] not in position:
                continue
            values = by_color.setdefault(row[color_index], [None] * len(categories))
            values[position[row[x_index]]] = _num(row[first_value])
            color_totals[row[color_index]] = color_totals.get(row[color_index], 0.0) + float(row[first_value] or 0)
        keys = sorted(by_color, key=lambda k: -color_totals[k])
        if len(keys) > MAX_SERIES:
            notes.append(f"Showing the {MAX_SERIES} largest {spec.color} groups of {len(keys)}.")
            keys = keys[:MAX_SERIES]
        for k in keys:
            series.append(ChartSeries(str(plain_value(k)), list(range(len(categories))), by_color[k]))
    else:
        for offset, name in enumerate(value_names):
            values: List[Optional[float]] = [None] * len(categories)
            for row in result.rows:
                if row[x_index] in position:
                    values[position[row[x_index]]] = _num(row[first_value + offset])
            series.append(ChartSeries(_y_label(spec, name) if name != "rows" else "rows",
                                      list(range(len(categories))), values))
    if spec.kind in ("line", "area"):
        for s in series:  # missing periods are real zeros for counts and sums, gaps otherwise
            if spec.aggregate in ("sum", "count", "count_distinct"):
                s.ys = [0.0 if v is None else v for v in s.ys]
    y_format = schema.column(spec.y[0]).format if spec.y and spec.aggregate not in ("count", "count_distinct") else (
        "integer" if spec.aggregate in ("count", "count_distinct") else "")
    rows = [r for r in result.rows if r[x_index] in position]
    rows.sort(key=lambda r: position[r[x_index]])
    return ChartData(spec, series, labels, result.columns, rows[:500], notes, y_format=y_format, pipeline=pipeline,
                     total_rows=len(result.rows))


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _label(value: Any, bucket: str) -> str:
    if value is None:
        return "(missing)"
    if isinstance(value, (_dt.date, _dt.datetime)):
        if bucket == "month":
            return value.strftime("%b %Y")
        if bucket == "quarter":
            return f"Q{(value.month - 1) // 3 + 1} {value.year}"
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(plain_value(value))


def is_time_like(schema: TableSchema, name: str) -> bool:
    column = schema.column(name)
    return bool(column and (column.type in LogicalType.TEMPORAL or column.role == Role.TIME))
