"""Findings computed from results: the facts every summary is grounded in.

Facts are derived here, deterministically, from step results (largest and smallest categories, shares, first-to-last
change and peaks of time series, metric values, outlier counts). Without a language model they are the findings; with
one, the model only phrases them, and :func:`unverified_numbers` flags any figure in its text that does not come
from the results.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, List, Optional, Sequence, Set

from .schema import LogicalType, TableSchema

__all__ = ["format_value", "step_facts", "unverified_numbers", "numbers_in"]


def format_value(value: Any, fmt: str = "") -> str:
    """Human formatting: thousands separators, two decimals for fractions, percentages for rates."""
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number):
            return "missing"
        if fmt == "percent":
            return f"{number * 100:.1f}%"
        if number.is_integer() and abs(number) < 1e15:
            return f"{int(number):,}"
        if abs(number) >= 100:
            return f"{number:,.0f}"
        return f"{number:,.2f}"
    return str(value)


def _numeric_columns(schema: TableSchema, exclude: Sequence[str] = ()) -> List[str]:
    return [c.name for c in schema.columns if c.is_numeric and c.name not in exclude and c.role != "identifier"]


def _fmt(schema: TableSchema, name: str) -> str:
    column = schema.column(name)
    return column.format if column else ""


def step_facts(title: str, schema: TableSchema, columns: List[str], rows: List[tuple], total: int,
               output: str = "table", operations: Iterable[dict] = ()) -> List[str]:
    """Plain facts about one table result (at most a handful, most informative first).

    Long row-level results only get their size; aggregated results get the extremes of the measure the step ranked
    by (or its first measures), shares of the total for additive measures, and change over time for time axes.
    """
    if total == 0:
        return [f"{title}: no rows match."]
    ops = [o for o in operations if isinstance(o, dict)]
    kinds = [o.get("op") for o in ops]
    facts: List[str] = []
    if "outliers" in kinds:
        facts.append(f"{title}: {total:,} unusual rows found.")
    index = {c: i for i, c in enumerate(columns)}
    if output == "metric" or total == 1:
        parts = [f"{c} = {format_value(rows[0][index[c]], _fmt(schema, c))}" for c in columns]
        return facts + [f"{title}: " + ", ".join(parts[:8]) + "."]
    aggregated = "aggregate" in kinds or total <= 60
    if not aggregated or total > len(rows):
        return facts or [f"{title}: {total:,} rows."]
    labels = [c.name for c in schema.columns if not c.is_numeric or c.role in ("time", "dimension")]
    measures = [m for m in _numeric_columns(schema) if not labels or m != labels[0]]
    if not labels or not measures:
        return facts or [f"{title}: {total:,} rows."]
    focus = _ranked_column(ops)
    if focus and focus in measures:
        measures = [focus] + [m for m in measures if m != focus]
    label = labels[0]
    for measure in measures[:2]:
        values = [(row[index[label]], row[index[measure]]) for row in rows if row[index[measure]] is not None]
        facts.extend(_series_facts(title, measure, values, _fmt(schema, measure), _is_time(schema, label),
                                   additive=_additive(schema, measure)))
    return facts[:5]


def chart_facts(title: str, chart, schema: Optional[TableSchema] = None) -> List[str]:
    """Facts about a prepared chart's series (categories or periods against measures)."""
    spec = chart.spec
    if spec.kind in ("table", "metric", "histogram", "heatmap") or not chart.categories:
        if spec.kind == "scatter":
            return [f"{title}: {n}" for n in chart.notes if n.startswith("Pearson")]
        return []
    temporal = bool(spec.x_bucket) or spec.sort == "x"
    facts: List[str] = []
    for series in chart.series[:3]:
        values = [(chart.categories[int(x)], y) for x, y in zip(series.xs, series.ys) if y is not None]
        measure = spec.y[0] if spec.y else "rows"
        name = f"{measure} ({series.name})" if len(chart.series) > 1 and spec.color else series.name
        source = schema.column(measure) if schema is not None and spec.y else None
        additive = spec.aggregate in ("sum", "count", "count_distinct") and not (
            source is not None and (source.aggregation in ("avg", "median", "min", "max") or source.format == "percent"))
        facts.extend(_series_facts(title, name, values, chart.y_format, temporal, additive=additive))
    return facts[:4]


def _series_facts(title: str, measure: str, values: List[tuple], fmt: str, temporal: bool, additive: bool) -> List[str]:
    if not values:
        return []
    if temporal:
        first, last = values[0], values[-1]
        peak = max(values, key=lambda item: float(item[1]))
        change = _change(float(first[1]), float(last[1]))
        return [f"{title}: {measure} went from {format_value(first[1], fmt)} ({_label(first[0])}) to "
                f"{format_value(last[1], fmt)} ({_label(last[0])}){change}; peak {format_value(peak[1], fmt)} "
                f"in {_label(peak[0])}."]
    ranked = sorted(values, key=lambda item: -float(item[1]))
    top, bottom = ranked[0], ranked[-1]
    text = f"{title}: highest {measure} is {_label(top[0])} ({format_value(top[1], fmt)})"
    if len(ranked) > 1:
        text += f", lowest is {_label(bottom[0])} ({format_value(bottom[1], fmt)})"
    total_value = sum(float(v) for _, v in values)
    if additive and fmt != "percent" and len(ranked) > 1 and total_value > 0 and all(float(v) >= 0 for _, v in values):
        text += f"; {_label(top[0])} is {float(top[1]) / total_value:.1%} of the total shown"
    return [text + "."]


def _ranked_column(ops: List[dict]) -> str:
    for op in reversed(ops):
        if op.get("op") == "top_n":
            return str(op.get("column") or op.get("by") or "")
        if op.get("op") == "sort":
            by = op.get("by") or []
            first = by[0] if by else None
            if isinstance(first, dict):
                return str(first.get("column") or "")
            if isinstance(first, str):
                return first.lstrip("-")
    return ""


def _additive(schema: TableSchema, name: str) -> bool:
    column = schema.column(name)
    return bool(column and column.aggregation in ("sum", "count") and column.format != "percent")


def _change(first: float, last: float) -> str:
    if first == 0:
        return ""
    return f" ({(last - first) / abs(first):+.1%})"


def _label(value: Any) -> str:
    if value is None:
        return "(missing)"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2} 00:00:00", text) else text


def _is_time(schema: TableSchema, name: str) -> bool:
    column = schema.column(name)
    return bool(column and (column.type in LogicalType.TEMPORAL or column.role == "time"))


_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*(%|k|K|m|M|bn|B)?")


def numbers_in(text: str) -> List[float]:
    """Numeric values mentioned in ``text``, with %, k, M and bn expanded."""
    found: List[float] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(0).strip()
        suffix = match.group(1) or ""
        digits = raw[:len(raw) - len(suffix)].strip().replace(",", "") if suffix else raw.replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        scale = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "bn": 1e9, "B": 1e9}.get(suffix, 1.0)
        found.append(value * scale if suffix != "%" else value)
    return found


def _known(values: Iterable[Any]) -> Set[float]:
    known: Set[float] = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            known.add(float(value))
            known.add(round(float(value) * 100, 1))   # rates written as percentages
    return known


def unverified_numbers(text: str, facts: Sequence[str], tables: Iterable[Sequence[tuple]]) -> List[str]:
    """Figures in ``text`` that match neither a fact nor a result value (rounded as people round).

    Totals of each column and series count as known (summaries often quote them). Years, small counts (below 10)
    and list numbering are ignored.
    """
    tables = [list(rows) for rows in tables]
    known = _known(v for rows in tables for row in rows for v in row)
    for rows in tables:
        for row in rows:  # series are passed as rows of values
            numbers = [float(v) for v in row if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if len(numbers) > 1:
                known.add(sum(numbers))
        width = max((len(row) for row in rows), default=0)
        for index in range(width):
            column = [row[index] for row in rows if index < len(row) and isinstance(row[index], (int, float))
                      and not isinstance(row[index], bool)]
            if len(column) > 1:
                known.add(float(sum(column)))
    for fact in facts:
        known.update(numbers_in(fact))
    suspicious: List[str] = []
    for value in numbers_in(text):
        if abs(value) < 10 or (1900 <= value <= 2100 and float(value).is_integer()):
            continue
        if not any(_close(value, k) for k in known):
            suspicious.append(format_value(value))
    return suspicious


def _close(value: float, known: float) -> bool:
    if known == value:
        return True
    if known == 0:
        return abs(value) < 0.5
    ratio = abs(value - known) / abs(known)
    if ratio <= 0.006:   # 1,234,567 written as 1.23M or 1,235,000
        return True
    return abs(value - known) <= 0.051 * max(1.0, 10 ** math.floor(math.log10(abs(known)) - 2)) \
        if abs(known) >= 1 else ratio <= 0.01


def best_fact(facts: Sequence[str]) -> Optional[str]:
    return facts[0] if facts else None
