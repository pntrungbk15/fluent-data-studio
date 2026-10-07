"""Running plan steps: compile, materialize, prepare the chart or metrics and derive facts."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .charts import ChartData, ChartError, ChartSpec, prepare_chart, recommend_chart
from .findings import chart_facts, step_facts
from .profile import correlations
from .plan import AnalysisPlan, PlanningContext, PlanStep
from .schema import TableSchema
from .transforms import CompiledPipeline, OperationError, compile_pipeline
from .workspace import ResultSet, Workspace, WorkspaceError

__all__ = ["StepResult", "run_step", "execute_plan", "PREVIEW_ROWS"]

PREVIEW_ROWS = 1000


@dataclass
class StepResult:
    """What one step produced; ``error`` is set (and the rest empty) when it failed."""

    step: PlanStep
    table: str = ""
    schema: Optional[TableSchema] = None
    preview: Optional[ResultSet] = None
    pipeline: Optional[CompiledPipeline] = None
    chart: Optional[ChartData] = None
    facts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: str = ""
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def rows(self) -> int:
        return self.preview.total if self.preview else 0


def run_step(workspace: Workspace, step: PlanStep, input_table: str, input_schema: Optional[TableSchema],
             table_name: str) -> StepResult:
    """Run one step over ``input_table`` and store its result as ``table_name``."""
    started = time.perf_counter()
    result = StepResult(step)
    try:
        pipeline = compile_pipeline(workspace, input_table, step.operations, input_schema)
        workspace.materialize(table_name, pipeline.sql)
        schema = pipeline.schema
        schema.name = table_name
        schema.rows = int(workspace.scalar(f'SELECT count(*) FROM "{table_name}"'))
        _fill_ranges(workspace, table_name, schema)
        result.table, result.schema, result.pipeline = table_name, schema, pipeline
        result.preview = workspace.query(f'SELECT * FROM "{table_name}"', limit=PREVIEW_ROWS)
        if step.output == "metric" and result.preview.total > 1:
            step.output = "table"  # key figures are one row; anything longer is shown as the table it is
            result.notes.append(f"Shown as a table: the result has {result.preview.total:,} rows, not one row of "
                                "key figures.")
        if step.output == "correlation":
            result.chart, result.facts = correlation_chart(workspace, table_name, schema, step.title)
            return _done(result, started)
        if step.output == "chart":
            spec = step.chart or recommend_chart(schema)
            if not spec.title:
                spec.title = step.title
            result.chart = prepare_chart(workspace, table_name, spec, schema)
        if result.chart is not None and (result.chart.categories or result.chart.spec.kind == "scatter"):
            result.facts = chart_facts(step.title, result.chart, schema)
        else:
            result.facts = step_facts(step.title, schema, result.preview.columns, result.preview.rows,
                                      result.preview.total, step.output, step.operations)
            if result.chart is not None:
                result.facts += chart_facts(step.title, result.chart, schema)
    except (OperationError, ChartError, WorkspaceError, ValueError) as exc:
        result.error = str(exc)
    return _done(result, started)


def _done(result: StepResult, started: float) -> StepResult:
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result


def correlation_chart(workspace: Workspace, table: str, schema: TableSchema, title: str):
    """A heatmap of Pearson correlations between the numeric measures of ``table`` and the strongest pairs as facts."""
    names = [c.name for c in schema.columns if c.is_numeric and c.role != "identifier"][:12]
    if len(names) < 2:
        raise ValueError("a correlation needs at least two numeric columns")
    matrix = correlations(workspace, table, names)
    spec = ChartSpec("heatmap", title=title or "Correlation")
    chart = ChartData(spec, [], list(names), names, [], [], y_format="", row_labels=list(names), matrix=matrix,
                      total_rows=schema.rows)
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if matrix[i][j] is not None:
                pairs.append((abs(matrix[i][j]), names[i], names[j], matrix[i][j]))
    pairs.sort(reverse=True)
    facts = [f"{title}: {a} and {b} have correlation {r:+.2f} ({_strength(r)})." for _, a, b, r in pairs[:3]]
    weakest = [p for p in pairs if p[0] < 0.1]
    if weakest:
        facts.append(f"{title}: {len(weakest)} pairs show no linear relationship (|r| < 0.1).")
    return chart, facts


def _strength(r: float) -> str:
    size = abs(r)
    word = "very strong" if size >= 0.8 else "strong" if size >= 0.6 else "moderate" if size >= 0.4 else \
        "weak" if size >= 0.2 else "negligible"
    return f"{word}, {'positive' if r > 0 else 'negative'}" if size >= 0.2 else word


def _fill_ranges(workspace: Workspace, table: str, schema: TableSchema) -> None:
    """Min and max of date columns, so charts can choose a sensible period."""
    temporal = [c for c in schema.columns if c.is_temporal]
    if not temporal:
        return
    parts = ", ".join(f'min("{c.name}"), max("{c.name}")' for c in temporal)
    row = workspace.query(f'SELECT {parts} FROM "{table}"', limit=None, count=False).rows[0]
    for i, column in enumerate(temporal):
        column.minimum, column.maximum = row[2 * i], row[2 * i + 1]


def execute_plan(workspace: Workspace, plan: AnalysisPlan, context: PlanningContext, prefix: str,
                 progress: Optional[Callable[[int, StepResult], None]] = None) -> List[StepResult]:
    """Run every step in order; a step whose input failed is reported as skipped. Result tables are ``{prefix}_{id}``."""
    tables: Dict[str, str] = {name: name for name in context.datasets}
    schemas: Dict[str, Optional[TableSchema]] = dict(context.datasets)
    for ref in context.results:
        tables[ref.id] = ref.table
        schemas[ref.id] = ref.schema
    results: List[StepResult] = []
    for index, step in enumerate(plan.steps):
        source = step.input or context.focus
        if source not in tables:
            source = context.resolve_input(source) or source
        if source not in tables:
            result = StepResult(step, error=f"input {source!r} is not available"
                                + (" (an earlier step failed)" if plan.step(source) else ""))
        else:
            result = run_step(workspace, step, tables[source], schemas.get(source), f"{prefix}_{step.id}")
            if result.ok:
                tables[step.id] = result.table
                schemas[step.id] = result.schema
        results.append(result)
        if progress is not None:
            progress(index, result)
    return results


def chart_for(workspace: Workspace, result: StepResult, spec: ChartSpec) -> ChartData:
    """Re-prepare a step's chart after the user edited its spec (no model involved)."""
    return prepare_chart(workspace, result.table, spec, result.schema)


def summarize_results(results: List[StepResult]) -> Dict[str, Any]:
    return {"steps": len(results), "failed": sum(1 for r in results if not r.ok),
            "facts": [f for r in results for f in r.facts]}
