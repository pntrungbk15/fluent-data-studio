"""The language-model side of analysis: planning requests into validated plans and phrasing computed findings.

The model is used twice per request and never runs code:

1. **Planning.** It receives the request and a description of the data (schemas, roles, sample values; never
   connection details or credentials) and returns an :class:`~fluent_data_studio.engine.plan.AnalysisPlan` as JSON.
   The plan is validated against the real tables; if anything is wrong, the exact problems go back to the model,
   which returns a corrected plan (at most ``MAX_REPAIRS`` times).
2. **Narration.** After the steps ran, it receives the requirements and the facts computed from the results and
   writes the summary. Figures it writes that do not appear in the results are flagged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .execute import StepResult
from .expressions import function_reference
from .findings import format_value, unverified_numbers
from .llm import LlmClient, LlmError, extract_json
from .plan import AnalysisPlan, PlanError, PlanningContext, parse_plan, validate_plan
from .schema import TableSchema
from .workspace import Workspace, plain_value

__all__ = ["LlmPlanner", "PlanAttempt", "PlanOutcome", "Narrative", "narrate", "facts_narrative", "data_context",
           "MAX_REPAIRS"]

MAX_REPAIRS = 2

PLANNER_PROMPT = """You plan data analyses for Fluent Data Studio. You never write code or SQL. You answer with one JSON
object describing an analysis plan; the application validates it against the data and runs it.

Plan format:
{
  "summary": "one sentence restating what the user wants",
  "requirements": [{"id": "R1", "text": "one thing the request asks for"}, ...],
  "assumptions": ["each interpretation you made, e.g. which column means 'sales'"],
  "add_to_report": true only when the user asks for a dashboard or report,
  "steps": [{
     "id": "s1", "title": "short title shown to the user", "why": "one sentence",
     "input": "<dataset name or the id of an earlier step or result>",
     "operations": [ ...operations, applied in order... ],
     "output": "table" | "chart" | "metric" | "correlation",
     "chart": null or {"kind": "bar|line|area|scatter|histogram|pie|heatmap", "x": "field", "y": ["measure"],
               "color": "optional field splitting series", "aggregate": "sum|avg|min|max|median|count|count_distinct|none",
               "x_bucket": "year|quarter|month|week|day (dates only)", "sort": "x|value_desc|value_asc",
               "limit": 0, "title": "...", "stacked": false},
     "requirements": ["R1"]
  }]
}

Operations (JSON objects):
- {"op": "filter", "where": "<condition>"}
- {"op": "derive", "name": "new_column", "expression": "<formula>", "format": "percent|currency|integer|"}
- {"op": "aggregate", "group_by": ["column" or {"column": "date_col", "bucket": "month", "as": "month"}],
   "measures": [{"column": "c", "agg": "sum|avg|min|max|median|count|count_distinct|stddev", "as": "name",
                 "where": "<optional condition for this measure only>"}]}   (count rows: {"agg": "count", "as": "orders"})
- {"op": "sort", "by": [{"column": "c", "descending": true}]}
- {"op": "top_n", "column": "c", "count": 5, "descending": true, "within": "optional group column"}
- {"op": "select", "columns": [...]}, {"op": "drop", "columns": [...]}, {"op": "rename", "mapping": {"old": "new"}}
- {"op": "cast", "column": "c", "to": "integer|number|text|date|timestamp|boolean"}
- {"op": "fill_missing", "column": "c", "method": "zero|value|mean|median|mode", "value": 0}
- {"op": "drop_missing", "columns": [...]}, {"op": "distinct", "columns": [...]}, {"op": "limit", "count": 100}
- {"op": "join", "dataset": "other_dataset", "on": [["left_col", "right_col"]], "how": "left|inner", "columns": [...]}
- {"op": "outliers", "column": "numeric_col", "method": "iqr|zscore", "threshold": 1.5}

Conditions and formulas use this language: column names as written (double quotes if they contain spaces),
'single-quoted text', numbers, + - * / %, = != < <= > >=, and, or, not, in ('a', 'b'), between x and y,
is null, is not null, like 'pat%'. Functions:
FUNCTIONS

Rules:
1. Split the request into requirements. Keep the user's constraints (years, exclusions, segments): apply every
   filter the request states to every step it concerns. Every requirement must be covered by at least one step.
2. Use only the datasets, columns and values listed below; match category values exactly as listed.
3. A step reads one input. Use "current_input" (what the user is looking at) unless the request needs columns it
   does not have. To build on an earlier step, use its id as input.
4. Use "metric" output for key figures (one row of values), "chart" for comparisons and trends, "table" for lists,
   "correlation" for a correlation matrix of the numeric columns of the step result.
   A chart's fields must be columns of the step's result (after its operations). When the step already aggregates,
   chart it with "aggregate": "sum" (or "none" for scatter).
5. Choose honest charts: lines only for ordered x (time); pies only for parts of a whole with few categories;
   scatter for two numeric measures.
6. Growth or comparison between periods: aggregate with one measure per period using "where", then derive the
   difference or ratio (e.g. "rev_2025 / rev_2024 - 1" with "format": "percent").
7. Use 1 to 6 steps. Do not add steps nobody asked for. A requirement to summarize or explain the findings is
   answered by the written summary: list it on every step. Answer with the JSON object only.
""".replace("FUNCTIONS", function_reference())

NARRATOR_PROMPT = """You write the findings of a data analysis for Fluent Data Studio. You receive the user's request,
its requirements and facts computed from the results. Write only what the facts and tables support; use their numbers
as given (you may round them) and never invent figures. If a requirement could not be answered, say so plainly.

Answer with one JSON object:
{"summary": "2 to 4 sentences answering the request",
 "findings": [{"requirement": "R1", "text": "one or two sentences"}],
 "caveats": ["limits of the data or analysis worth knowing"]}
"""


def _column_line(column, examples: bool = True) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"name": column.name, "type": column.type, "role": column.role}
    if column.role == "measure":
        entry["default_aggregation"] = column.aggregation
    if column.format:
        entry["format"] = column.format
    if column.description:
        entry["description"] = column.description
    if column.synonyms:
        entry["synonyms"] = column.synonyms
    if column.nulls:
        entry["missing"] = column.nulls
    if examples and column.top_values and column.role in ("dimension",) and column.type in ("text", "boolean", "integer"):
        entry["values" if column.distinct <= len(column.top_values) else "frequent_values"] = column.top_values
    if column.minimum is not None and (column.is_temporal or column.role in ("time", "measure")):
        entry["range"] = [plain_value(column.minimum), plain_value(column.maximum)]
    return entry


def data_context(context: PlanningContext) -> Dict[str, Any]:
    """What the planner model sees: dataset and result schemas only (no paths, hosts, users or secrets)."""
    datasets = []
    for name, schema in context.datasets.items():
        datasets.append({"name": name, "rows": schema.rows, "description": schema.description,
                         "columns": [_column_line(c) for c in schema.columns]})
    results = [{"id": ref.id, "title": ref.title, "rows": ref.schema.rows,
                "columns": [_column_line(c, examples=False) for c in ref.schema.columns]}
               for ref in context.results[-6:]]
    document: Dict[str, Any] = {"datasets": datasets}
    if results:
        document["earlier_results"] = results
    if context.focus:
        document["current_input"] = context.focus
    return document


@dataclass
class PlanAttempt:
    """One round with the model: its raw reply and the problems found in it."""

    reply: str
    issues: List[str]


@dataclass
class PlanOutcome:
    plan: Optional[AnalysisPlan]
    attempts: List[PlanAttempt] = field(default_factory=list)
    error: str = ""

    @property
    def repaired(self) -> int:
        return max(0, len(self.attempts) - 1)


class LlmPlanner:
    """Turns a request into a validated plan with a model behind an OpenAI-compatible endpoint."""

    name = "language model"

    def __init__(self, client: LlmClient) -> None:
        self.client = client

    def plan(self, request: str, context: PlanningContext, workspace: Workspace,
             conversation: Optional[List[str]] = None) -> PlanOutcome:
        user = {"request": request, "data": data_context(context)}
        if conversation:
            user["earlier_requests"] = conversation[-4:]
        messages = [{"role": "system", "content": PLANNER_PROMPT},
                    {"role": "user", "content": json.dumps(user, default=str)}]
        outcome = PlanOutcome(None)
        for _round in range(MAX_REPAIRS + 1):
            try:
                reply = self.client.chat(messages)
            except LlmError as exc:
                outcome.error = str(exc)
                return outcome
            issues: List[str]
            plan: Optional[AnalysisPlan] = None
            try:
                plan = parse_plan(extract_json(reply), request, f"{self.name} ({self.client.settings.model})")
                issues = [str(i) for i in validate_plan(plan, workspace, context)]
            except (ValueError, PlanError) as exc:
                issues = [f"the reply is not a valid plan: {exc}"]
            outcome.attempts.append(PlanAttempt(reply, issues))
            if plan is not None and not issues:
                outcome.plan = plan
                return outcome
            if plan is not None and _only_coverage(issues):
                outcome.plan = plan  # keep a runnable plan even if the repair below fails
            if len(outcome.attempts) > 1 and issues == outcome.attempts[-2].issues:
                break  # the model repeated the same mistakes; another round would cost minutes for nothing
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": "The application checked this plan against the data and found "
                             "these problems:\n- " + "\n- ".join(issues) + "\nReturn the complete corrected plan as "
                             "one JSON object."})
        if outcome.plan is None:
            outcome.error = "the model's plan still had problems after corrections: " + "; ".join(
                outcome.attempts[-1].issues[:3])
        return outcome


def _only_coverage(issues: List[str]) -> bool:
    return all("is not covered by any step" in issue for issue in issues)


@dataclass
class Narrative:
    """The written findings of a run; ``unverified`` lists figures that do not come from the results."""

    summary: str
    findings: List[Dict[str, str]] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)
    source: str = "facts"

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary, "findings": self.findings, "caveats": self.caveats,
                "unverified": self.unverified, "source": self.source}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Narrative":
        return cls(str(data.get("summary", "")), list(data.get("findings", [])), list(data.get("caveats", [])),
                   list(data.get("unverified", [])), str(data.get("source", "facts")))


def facts_narrative(plan: AnalysisPlan, results: List[StepResult]) -> Narrative:
    """Findings without a model: the computed facts, grouped by requirement.

    A requirement every step answers (such as "summarize the findings" or "build a dashboard") points to the results
    instead of repeating all their facts.
    """
    ok_results = [r for r in results if r.ok]
    findings: List[Dict[str, str]] = []
    for requirement in plan.requirements or []:
        covering = [r for r in results if requirement.id in r.step.requirements]
        texts = [f for r in covering if r.ok for f in r.facts[:2]]
        failed = [r for r in covering if not r.ok]
        if len(covering) > 1 and len(covering) == len(results) and len(plan.requirements) > 1:
            findings.append({"requirement": requirement.id, "text": "Answered by all results: "
                             + ", ".join(r.step.title for r in ok_results) + "."})
        elif texts:
            findings.append({"requirement": requirement.id, "text": " ".join(texts)})
        elif failed:
            findings.append({"requirement": requirement.id, "text": f"Not answered: {failed[0].error}"})
        else:
            findings.append({"requirement": requirement.id, "text": "Not covered by the analysis."})
    if not plan.requirements:
        for r in ok_results:
            if r.facts:
                findings.append({"requirement": "", "text": " ".join(r.facts[:2])})
    seen: List[str] = []
    for r in ok_results:
        if r.facts and r.facts[0] not in seen:
            seen.append(r.facts[0])
    summary = " ".join(seen[:3]) if seen else f"Ran {len(ok_results)} of {len(results)} steps."
    return Narrative(summary, findings, [f"{r.step.title}: {r.error}" for r in results if not r.ok], source="facts")


def _table_excerpt(result: StepResult, limit: int = 12) -> Dict[str, Any]:
    if result.chart is not None and result.chart.categories:
        chart = result.chart
        return {"chart": chart.spec.kind, "x": chart.categories[:limit],
                "series": {s.name: [None if v is None else round(v, 4) for v in s.ys[:limit]] for s in chart.series[:4]},
                "notes": chart.notes}
    preview = result.preview
    if preview is None:
        return {}
    return {"columns": preview.columns, "rows": [[plain_value(v) for v in row] for row in preview.rows[:limit]],
            "total_rows": preview.total}


def narrate(client: Optional[LlmClient], plan: AnalysisPlan, results: List[StepResult]) -> Narrative:
    """Findings phrased by the model when one is configured, otherwise :func:`facts_narrative`."""
    fallback = facts_narrative(plan, results)
    if client is None or not client.settings.configured:
        return fallback
    facts = [f for r in results for f in r.facts]
    payload = {"request": plan.request, "requirements": [r.to_dict() for r in plan.requirements],
               "steps": [{"id": r.step.id, "title": r.step.title, "requirements": r.step.requirements,
                          "status": "ok" if r.ok else f"failed: {r.error}", "facts": r.facts,
                          "result": _table_excerpt(r) if r.ok else {}} for r in results],
               "assumptions": plan.assumptions}
    try:
        reply = client.chat([{"role": "system", "content": NARRATOR_PROMPT},
                             {"role": "user", "content": json.dumps(payload, default=str)}])
        data = extract_json(reply)
    except (LlmError, ValueError) as exc:
        fallback.caveats.append(f"The model could not write the summary ({exc}); showing the computed facts.")
        return fallback
    summary = str(data.get("summary") or "").strip() or fallback.summary
    findings = [{"requirement": str(f.get("requirement", "")), "text": str(f.get("text", "")).strip()}
                for f in data.get("findings") or [] if isinstance(f, dict) and f.get("text")]
    caveats = [str(c) for c in data.get("caveats") or [] if str(c).strip()]
    tables = []
    for r in results:
        if r.preview is not None:
            tables.append(r.preview.rows[:200])
        if r.chart is not None:
            tables.append([tuple(v for v in s.ys if v is not None) for s in r.chart.series])
    text = summary + " " + " ".join(f["text"] for f in findings)
    unverified = unverified_numbers(text, facts, tables)
    return Narrative(summary, findings or fallback.findings, caveats, unverified, source="model")


def describe_schema(schema: TableSchema) -> str:
    return ", ".join(f"{c.name} ({c.role})" for c in schema.columns)


def describe_value(value: Any, fmt: str = "") -> str:
    return format_value(value, fmt)
