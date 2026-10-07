"""Analysis plans: the structured form of a request.

A request such as *"compare 2025 revenue with 2024 by region, excluding cancelled orders, and show the top five
products"* becomes an :class:`AnalysisPlan`:

* ``requirements`` lists each thing the request asks for, in the user's terms, so none is silently dropped;
* ``steps`` are the analyses that answer them. Each step reads a dataset or an earlier step's result, applies typed
  operations (:mod:`~fluent_data_studio.engine.transforms`), and shows the result as a table, a chart
  (:class:`~fluent_data_studio.engine.charts.ChartSpec`), key metrics or a correlation matrix of its numeric
  columns. Each step says which requirements it covers;
* ``assumptions`` records interpretations the planner made (which column means "revenue", what "cancelled" matches).

Plans are plain JSON. Language models and the rule-based planner produce the same structure, and
:func:`validate_plan` checks it, compiling every step against the real data, before anything is shown as a result.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .charts import CHART_KINDS, ChartSpec
from .schema import TableSchema
from .transforms import OperationError, compile_pipeline
from .workspace import Workspace

__all__ = ["Requirement", "PlanStep", "AnalysisPlan", "PlanIssue", "PlanError", "parse_plan", "validate_plan",
           "PlanningContext", "ResultRef", "OUTPUTS"]

OUTPUTS = ("table", "chart", "metric", "correlation")


class PlanError(ValueError):
    """A plan that is not valid JSON structure."""


@dataclass
class Requirement:
    id: str
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "text": self.text}


@dataclass
class PlanStep:
    """One analysis: an input, operations, and how to show the result."""

    id: str
    title: str
    input: str
    operations: List[Dict[str, Any]] = field(default_factory=list)
    output: str = "table"
    chart: Optional[ChartSpec] = None
    requirements: List[str] = field(default_factory=list)
    why: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "input": self.input, "operations": list(self.operations),
                "output": self.output, "chart": self.chart.to_dict() if self.chart else None,
                "requirements": list(self.requirements), "why": self.why}


@dataclass
class AnalysisPlan:
    request: str
    summary: str = ""
    requirements: List[Requirement] = field(default_factory=list)
    steps: List[PlanStep] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    planner: str = ""
    add_to_report: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"request": self.request, "summary": self.summary,
                "requirements": [r.to_dict() for r in self.requirements],
                "steps": [s.to_dict() for s in self.steps], "assumptions": list(self.assumptions),
                "planner": self.planner, "add_to_report": self.add_to_report}

    def step(self, step_id: str) -> Optional[PlanStep]:
        return next((s for s in self.steps if s.id == step_id), None)

    def uncovered(self) -> List[Requirement]:
        covered = {r for s in self.steps for r in s.requirements}
        return [r for r in self.requirements if r.id not in covered]


@dataclass
class PlanIssue:
    """A problem found in a plan; ``step`` is empty for plan-level issues."""

    step: str
    message: str

    def __str__(self) -> str:
        return f"{self.step}: {self.message}" if self.step else self.message


@dataclass
class ResultRef:
    """An earlier result the planner may build on (from this plan or from the analysis history)."""

    id: str
    title: str
    table: str
    schema: TableSchema


@dataclass
class PlanningContext:
    """What a planner may use: dataset schemas, earlier results on the current branch and the default input."""

    datasets: Dict[str, TableSchema]
    results: List[ResultRef] = field(default_factory=list)
    focus: str = ""

    def input_schema(self, name: str) -> Optional[TableSchema]:
        if name in self.datasets:
            return self.datasets[name]
        lowered = name.lower()
        for key, schema in self.datasets.items():
            if key.lower() == lowered:
                return schema
        ref = next((r for r in self.results if r.id == name), None)
        return ref.schema if ref else None

    def resolve_input(self, name: str) -> str:
        """Canonical dataset name or result id for ``name``, or ``""``."""
        if name in self.datasets or any(r.id == name for r in self.results):
            return name
        lowered = name.lower()
        for key in self.datasets:
            if key.lower() == lowered:
                return key
        return ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_plan(data: Any, request: str = "", planner: str = "") -> AnalysisPlan:
    """Build a plan from JSON-like data, tolerating small variations; raises :class:`PlanError` on wrong structure."""
    if not isinstance(data, dict):
        raise PlanError("the plan must be a JSON object")
    plan = AnalysisPlan(request or _text(data.get("request")), _text(data.get("summary")), planner=planner)
    raw_requirements = data.get("requirements") or []
    if not isinstance(raw_requirements, list):
        raise PlanError("'requirements' must be a list")
    for index, item in enumerate(raw_requirements):
        if isinstance(item, str):
            labelled = re.match(r"\s*(R\d+)\s*[:.)-]\s*(.+)", item)
            plan.requirements.append(Requirement(labelled.group(1), labelled.group(2).strip()) if labelled
                                     else Requirement(f"R{index + 1}", item))
        elif isinstance(item, dict):
            plan.requirements.append(Requirement(_text(item.get("id")) or f"R{index + 1}",
                                                 _text(item.get("text") or item.get("description"))))
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError("'steps' must be a non-empty list")
    seen = set()
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise PlanError(f"step {index + 1} must be an object")
        step_id = _text(item.get("id")) or f"s{index + 1}"
        if step_id in seen:
            step_id = f"{step_id}_{index + 1}"
        seen.add(step_id)
        operations = item.get("operations") or []
        if not isinstance(operations, list) or not all(isinstance(o, dict) for o in operations):
            raise PlanError(f"step {step_id}: 'operations' must be a list of objects")
        output = _text(item.get("output")).lower() or ("chart" if item.get("chart") else "table")
        if output in CHART_KINDS and output not in ("table", "metric"):
            output = "chart"
        chart = None
        if isinstance(item.get("chart"), dict):
            chart = ChartSpec.from_dict(item["chart"])
        requirements = item.get("requirements") or []
        if isinstance(requirements, str):
            requirements = [requirements]
        plan.steps.append(PlanStep(step_id, _text(item.get("title")) or f"Step {index + 1}", _text(item.get("input")),
                                   list(operations), output if output in OUTPUTS else "table", chart,
                                   [_text(r) for r in requirements], _text(item.get("why"))))
    plan.assumptions = [_text(a) for a in (data.get("assumptions") or []) if _text(a)]
    plan.add_to_report = bool(data.get("add_to_report"))
    return plan


def validate_plan(plan: AnalysisPlan, workspace: Workspace, context: PlanningContext) -> List[PlanIssue]:
    """Structural and data checks for every step; an empty list means the plan can run.

    Steps are compiled against the real tables (DuckDB binds every stage), so wrong columns, wrong types and
    impossible operations are reported here, with messages a planner can act on.
    """
    issues: List[PlanIssue] = []
    token = next(_counter)
    placeholders: List[str] = []
    known_requirements = {r.id for r in plan.requirements}
    available: Dict[str, TableSchema] = {}
    tables: Dict[str, str] = {}
    for name, schema in context.datasets.items():
        available[name] = schema
        tables[name] = name
    for ref in context.results:
        available[ref.id] = ref.schema
        tables[ref.id] = ref.table
    for step in plan.steps:
        name = step.input or context.focus
        canonical = context.resolve_input(name) if name not in available else name
        if not canonical or canonical not in available:
            issues.append(PlanIssue(step.id, f"unknown input {name!r}; use a dataset ({', '.join(context.datasets)}) "
                                             "or the id of an earlier step"))
            continue
        step.input = canonical
        for requirement in step.requirements:
            if known_requirements and requirement not in known_requirements:
                issues.append(PlanIssue(step.id, f"refers to unknown requirement {requirement!r}"))
        try:
            compiled = compile_pipeline(workspace, tables[canonical], step.operations, available[canonical])
        except OperationError as exc:
            issues.append(PlanIssue(step.id, str(exc)))
            continue
        output_schema = compiled.schema
        if step.output == "chart":
            if step.chart is None:
                issues.append(PlanIssue(step.id, "output is 'chart' but the step has no chart spec"))
            else:
                for field_name in [step.chart.x, step.chart.color] + list(step.chart.y):
                    if field_name and output_schema.column(field_name) is None:
                        issues.append(PlanIssue(step.id, f"chart field {field_name!r} is not in the step result; "
                                                         f"result columns: {', '.join(output_schema.names())}"))
        available[step.id] = output_schema
        tables[step.id] = _placeholder(workspace, token, step.id, compiled.sql)
        placeholders.append(tables[step.id])
    for requirement in plan.uncovered():
        issues.append(PlanIssue("", f"requirement {requirement.id} ({requirement.text}) is not covered by any step"))
    for name in reversed(placeholders):
        workspace.execute(f'DROP VIEW IF EXISTS "{name}"')
    return issues


_counter = itertools.count(1)


def _placeholder(workspace: Workspace, token: int, step_id: str, sql: str) -> str:
    """A view standing in for a step result so later steps can be checked without running anything."""
    name = f"fds_check_{token}_" + re.sub(r"\W", "_", step_id)
    workspace.execute(f'CREATE OR REPLACE VIEW "{name}" AS {sql}')
    return name
