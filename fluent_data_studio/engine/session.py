"""A working session: sources, the analysis thread, the report and saving them as a project.

The **thread** is a tree of everything that happened: datasets added, requests asked, the steps each request ran,
manual transformations and chart edits. Every node records the structured operation that produced it, never only its
output, so a project file holds no data, only sources and steps, and reopening it replays the steps to rebuild every
result. New work attaches to the *current* node; selecting an earlier node and continuing from it starts a branch, and
the planner sees the results along that branch.

The **report** collects results (charts, tables, metrics) and text into an arrangement the user edits.

Heavy calls (``import_dataset``, ``compute_run``, ``compute_transform``, ``replay``) touch only DuckDB and may run on a
worker thread; methods that change the thread or report (``commit_*``, ``add_*``) are meant for one thread, the UI's.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .analyst import AnalysisRun, Analyst
from .assistant import Narrative
from .charts import ChartData, ChartSpec, prepare_chart
from .execute import StepResult, execute_plan, run_step
from .llm import LlmSettings
from .plan import AnalysisPlan, PlanningContext, PlanStep, ResultRef, parse_plan
from .schema import TableSchema, semantics_of
from .sources import SourceSpec, import_source
from .workspace import Dataset, Workspace

__all__ = ["Session", "ThreadNode", "ReportItem", "Report", "PROJECT_FORMAT", "ProjectError"]

PROJECT_FORMAT = "fluent-data-studio.project"
PROJECT_VERSION = 1


class ProjectError(RuntimeError):
    """A project file that cannot be read."""


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class ThreadNode:
    """One entry of the analysis thread.

    ``kind`` is ``dataset``, ``request``, ``step`` (a step of a request), ``transform`` (a manual pipeline) or
    ``chart`` (a refined chart of an earlier node). ``payload`` holds what is needed to replay it; ``result`` is the
    live outcome and is not saved.
    """

    id: str
    parent: str
    kind: str
    title: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created: str = field(default_factory=_now)
    result: Optional[StepResult] = field(default=None, repr=False)
    chart: Optional[ChartData] = field(default=None, repr=False)

    @property
    def table(self) -> str:
        if self.kind == "dataset":
            return str(self.payload.get("dataset", ""))
        return self.result.table if self.result is not None and self.result.ok else ""

    @property
    def schema(self) -> Optional[TableSchema]:
        return self.result.schema if self.result is not None and self.result.ok else None

    @property
    def error(self) -> str:
        return self.result.error if self.result is not None else str(self.payload.get("error", ""))

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "parent": self.parent, "kind": self.kind, "title": self.title, "payload": self.payload,
                "created": self.created}


@dataclass
class ReportItem:
    """A result or a text block on the report. ``node`` refers to the thread node shown (empty for text)."""

    id: str
    kind: str                     # chart, table, metric, text
    title: str
    node: str = ""
    text: str = ""
    span: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "node": self.node, "text": self.text,
                "span": self.span}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReportItem":
        return cls(str(data["id"]), str(data["kind"]), str(data.get("title", "")), str(data.get("node", "")),
                   str(data.get("text", "")), int(data.get("span", 1)))


@dataclass
class Report:
    title: str = "Report"
    items: List[ReportItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Report":
        return cls(str(data.get("title", "Report")), [ReportItem.from_dict(i) for i in data.get("items", [])])

    def item(self, item_id: str) -> Optional[ReportItem]:
        return next((i for i in self.items if i.id == item_id), None)


@dataclass
class SourceEntry:
    """A dataset's origin as saved in the project (no secrets)."""

    name: str
    spec: SourceSpec


class Session:
    """``Session()`` starts empty; see the module docstring for the threading contract."""

    def __init__(self, settings: Optional[LlmSettings] = None) -> None:
        self.workspace = Workspace()
        self.settings = settings or LlmSettings()
        self.nodes: Dict[str, ThreadNode] = {}
        self.order: List[str] = []
        self.current = ""
        self.report = Report()
        self.sources: Dict[str, SourceEntry] = {}
        self.secrets: Dict[str, str] = {}
        self.path: Optional[Path] = None
        self.modified = False
        self._ids = itertools.count(1)
        self._report_ids = itertools.count(1)
        self._tables = itertools.count(1)

    # ---- thread ---------------------------------------------------------------------------------------------

    def _new_id(self) -> str:
        while True:
            node_id = f"n{next(self._ids)}"
            if node_id not in self.nodes:
                return node_id

    def add_node(self, kind: str, title: str, payload: Dict[str, Any], parent: Optional[str] = None,
                 result: Optional[StepResult] = None) -> ThreadNode:
        node = ThreadNode(self._new_id(), self.current if parent is None else parent, kind, title, payload,
                          result=result)
        self.nodes[node.id] = node
        self.order.append(node.id)
        self.modified = True
        return node

    def children(self, node_id: str) -> List[ThreadNode]:
        return [self.nodes[i] for i in self.order if self.nodes[i].parent == node_id]

    def path_to(self, node_id: str) -> List[ThreadNode]:
        path: List[ThreadNode] = []
        while node_id and node_id in self.nodes:
            node = self.nodes[node_id]
            path.append(node)
            node_id = node.parent
        return list(reversed(path))

    def set_current(self, node_id: str) -> None:
        if node_id in self.nodes or node_id == "":
            self.current = node_id

    def result_nodes(self) -> List[ThreadNode]:
        return [self.nodes[i] for i in self.order if self.nodes[i].table and self.nodes[i].kind != "dataset"]

    def branch_results(self, node_id: Optional[str] = None) -> List[ThreadNode]:
        """Results the next request may build on: nodes on the path to ``node_id`` and the steps of its requests."""
        node_id = self.current if node_id is None else node_id
        seen: List[ThreadNode] = []
        for node in self.path_to(node_id):
            if node.kind in ("step", "transform", "chart") and node.table and node not in seen:
                seen.append(node)
            if node.kind == "request":
                seen.extend(child for child in self.children(node.id) if child.kind == "step" and child.table
                            and child not in seen)
        return seen

    def focus_input(self, node_id: Optional[str] = None) -> str:
        """The dataset or result a request continues from: the selected result, else its dataset."""
        node_id = self.current if node_id is None else node_id
        for node in reversed(self.path_to(node_id)):
            if node.kind in ("step", "transform", "chart") and node.table:
                return node.id
            if node.kind == "dataset":
                return str(node.payload.get("dataset", ""))
        return next(iter(self.workspace.datasets), "")

    def planning_context(self, node_id: Optional[str] = None) -> PlanningContext:
        datasets = {name: dataset.schema for name, dataset in self.workspace.datasets.items()}
        results = [ResultRef(n.id, n.title, n.table, n.schema) for n in self.branch_results(node_id) if n.schema]
        focus = self.focus_input(node_id)
        return PlanningContext(datasets, results, focus if focus in datasets or any(r.id == focus for r in results)
                               else "")

    def node_input(self, node: ThreadNode) -> str:
        return str(node.payload.get("input", ""))

    # ---- datasets -------------------------------------------------------------------------------------------

    def import_dataset(self, spec: SourceSpec, name: str = "", secret: str = "",
                       semantics: Optional[Dict[str, Any]] = None) -> Dataset:
        """Stage a source (worker-thread safe); call :meth:`commit_dataset` with the result on the UI thread."""
        return import_source(self.workspace, spec, name, secret, semantics)

    def commit_dataset(self, dataset: Dataset, secret: str = "") -> ThreadNode:
        spec = SourceSpec.from_dict(dataset.source)
        self.sources[dataset.name] = SourceEntry(dataset.name, spec)
        if secret:
            self.secrets[dataset.name] = secret
        node = self.add_node("dataset", f"Dataset {dataset.name}", {"dataset": dataset.name}, parent="")
        self.current = node.id
        return node

    def dataset_node(self, name: str) -> Optional[ThreadNode]:
        return next((self.nodes[i] for i in self.order if self.nodes[i].kind == "dataset"
                     and self.nodes[i].payload.get("dataset") == name), None)

    def remove_dataset(self, name: str) -> None:
        self.workspace.remove_dataset(name)
        self.sources.pop(name, None)
        self.secrets.pop(name, None)
        self.modified = True

    # ---- requests -------------------------------------------------------------------------------------------

    def analyst(self) -> Analyst:
        return Analyst(self.workspace, self.settings)

    def conversation(self, node_id: Optional[str] = None) -> List[str]:
        return [str(n.payload.get("request", "")) for n in self.path_to(self.current if node_id is None else node_id)
                if n.kind == "request"]

    def next_prefix(self) -> str:
        return f"fds_r{next(self._tables)}"

    def compute_run(self, request: str, context: PlanningContext, prefix: str, use_model: bool = True,
                    conversation: Optional[List[str]] = None, progress=None) -> AnalysisRun:
        return self.analyst().run(request, context, prefix, use_model, conversation, progress)

    def commit_run(self, run: AnalysisRun, parent: Optional[str] = None) -> ThreadNode:
        """Record a finished run: one request node and a step node per result. Steps that fed others stay linked."""
        payload = {"request": run.request, "plan": run.plan.to_dict() if run.plan else None, "notes": run.notes,
                   "narrative": run.narrative.to_dict() if run.narrative else None, "error": run.error,
                   "seconds": round(run.seconds, 2), "attempts": len(run.attempts)}
        request = self.add_node("request", run.request, payload, parent)
        step_nodes: Dict[str, str] = {}
        last = request.id
        for result in run.results:
            step = result.step
            node = self.add_node("step", step.title, {"step": step.to_dict(), "request": request.id,
                                                      "input": step_nodes.get(step.input, step.input)},
                                 parent=request.id, result=result)
            step_nodes[step.id] = node.id
            if result.ok:
                last = node.id
        request.payload["step_nodes"] = step_nodes
        self.current = last
        return request

    def request_steps(self, request_id: str) -> List[ThreadNode]:
        return [n for n in self.children(request_id) if n.kind == "step"]

    # ---- manual transformations and chart edits --------------------------------------------------------------

    def input_table(self, source: str) -> tuple:
        """(table, schema) for a dataset name or a result node id."""
        dataset = self.workspace.dataset(source)
        if dataset is not None:
            return dataset.name, dataset.schema
        node = self.nodes.get(source)
        if node is not None and node.table:
            return node.table, node.schema
        raise KeyError(f"unknown input {source!r}")

    def compute_transform(self, source: str, operations: List[Dict[str, Any]], title: str, output: str = "table",
                          chart: Optional[ChartSpec] = None, table_name: str = "") -> StepResult:
        table, schema = self.input_table(source)
        step = PlanStep("t", title, source, list(operations), output, chart)
        return run_step(self.workspace, step, table, schema, table_name or self.next_prefix())

    def commit_transform(self, source: str, result: StepResult, parent: Optional[str] = None) -> ThreadNode:
        node = self.add_node("transform", result.step.title, {"step": result.step.to_dict(), "input": source},
                             parent if parent is not None else self.current, result)
        self.current = node.id
        return node

    def chart_of(self, node: ThreadNode) -> Optional[ChartData]:
        if node.chart is not None:
            return node.chart
        return node.result.chart if node.result is not None and node.result.ok else None

    def compute_chart(self, node: ThreadNode, spec: ChartSpec) -> ChartData:
        """Re-prepare a chart over a node's result with an edited spec (no model involved)."""
        source = node
        if node.kind == "chart":
            source = self.nodes[str(node.payload.get("source"))]
        if source.kind == "dataset":
            dataset = self.workspace.dataset(str(source.payload.get("dataset")))
            return prepare_chart(self.workspace, dataset.name, spec, dataset.schema)
        return prepare_chart(self.workspace, source.table, spec, source.schema)

    def commit_chart(self, node: ThreadNode, chart: ChartData, title: str = "") -> ThreadNode:
        """Keep an edited chart as a refinement of ``node`` (the original stays in the thread)."""
        source_id = str(node.payload.get("source")) if node.kind == "chart" else node.id
        source = self.nodes[source_id]
        new = self.add_node("chart", title or chart.spec.title or f"Chart of {source.title}",
                            {"source": source_id, "spec": chart.spec.to_dict()}, parent=node.id, result=source.result)
        new.chart = chart
        self.current = new.id
        return new

    def record_chart_edit(self, node: ThreadNode, chart: ChartData) -> ThreadNode:
        """Record a manual chart edit: the first edit of a result adds a refinement, later ones update it in place.

        An edited chart therefore costs one history entry, however many controls the user changes, and the
        generated original stays as it was.
        """
        if node.kind == "chart":
            node.payload["spec"] = chart.spec.to_dict()
            node.chart = chart
            node.title = chart.spec.title or node.title
            self.modified = True
            return node
        return self.commit_chart(node, chart)

    def is_edited(self, node: ThreadNode) -> bool:
        return node.kind == "chart"

    def source_of(self, node: ThreadNode) -> ThreadNode:
        """The generated result behind a refinement (the node itself otherwise)."""
        if node.kind == "chart":
            return self.nodes.get(str(node.payload.get("source")), node)
        return node

    def report_item_for(self, node_id: str) -> Optional[ReportItem]:
        return next((i for i in self.report.items if i.node == node_id), None)

    # ---- semantic edits -------------------------------------------------------------------------------------

    def update_column(self, dataset_name: str, column_name: str, **changes: Any) -> None:
        dataset = self.workspace.dataset(dataset_name)
        column = dataset.schema.column(column_name) if dataset else None
        if column is None:
            return
        for key, value in changes.items():
            setattr(column, key, value)
        column.user_edited = True
        self.modified = True

    # ---- report ---------------------------------------------------------------------------------------------

    def add_to_report(self, node_id: str, kind: str = "", title: str = "", span: int = 0) -> ReportItem:
        node = self.nodes[node_id]
        chart = self.chart_of(node)
        if not kind:
            output = node.result.step.output if node.result is not None else "table"
            kind = "chart" if chart is not None and chart.spec.kind not in ("table", "metric") else (
                "metric" if output == "metric" else "table")
        if not span:
            span = 2
        item = ReportItem(self._report_id(), kind, title or node.title, node=node_id, span=span)
        self.report.items.append(item)
        self.modified = True
        return item

    def add_text(self, title: str, text: str, span: int = 2) -> ReportItem:
        item = ReportItem(self._report_id(), "text", title, text=text, span=span)
        self.report.items.append(item)
        self.modified = True
        return item

    def _report_id(self) -> str:
        while True:
            item_id = f"i{next(self._report_ids)}"
            if self.report.item(item_id) is None:
                return item_id

    def remove_report_item(self, item_id: str) -> None:
        self.report.items = [i for i in self.report.items if i.id != item_id]
        self.modified = True

    def reorder_report(self, ids: List[str]) -> None:
        by_id = {i.id: i for i in self.report.items}
        self.report.items = [by_id[i] for i in ids if i in by_id] + [i for i in self.report.items if i.id not in ids]
        self.modified = True

    # ---- projects -------------------------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        sources = []
        for name, entry in self.sources.items():
            dataset = self.workspace.dataset(name)
            sources.append({"name": name, "spec": entry.spec.to_dict(),
                            "semantics": semantics_of(dataset.schema, only_edited=True) if dataset else {},
                            "needs_secret": name in self.secrets})
        return {"format": PROJECT_FORMAT, "version": PROJECT_VERSION, "saved": _now(), "sources": sources,
                "thread": {"nodes": [self.nodes[i].to_dict() for i in self.order], "current": self.current},
                "report": self.report.to_dict()}

    def save(self, path: Path) -> None:
        path = Path(path)
        text = json.dumps(self.to_dict(), indent=1, default=str)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        self.path = path
        self.modified = False

    @staticmethod
    def read_project(path: Path) -> Dict[str, Any]:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectError(f"cannot read {path}: {exc}") from None
        if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
            raise ProjectError(f"{path} is not a Fluent Data Studio project")
        if int(data.get("version", 0)) > PROJECT_VERSION:
            raise ProjectError("the project was saved by a newer version of Fluent Data Studio")
        return data

    def replay(self, data: Dict[str, Any], secrets: Optional[Dict[str, str]] = None,
               progress: Optional[Callable[[str], None]] = None) -> List[str]:
        """Rebuild sources and every thread node from a project (worker-thread safe); returns problems found.

        Steps are re-run from their recorded operations and charts from their specs; no model is called.
        """
        problems: List[str] = []
        secrets = secrets or {}
        imported: Dict[str, Dataset] = {}
        for source in data.get("sources", []):
            name = str(source["name"])
            if progress:
                progress(f"Loading {name}")
            try:
                imported[name] = import_source(self.workspace, SourceSpec.from_dict(source["spec"]), name,
                                               secrets.get(name, ""), None)
                semantics = source.get("semantics") or {}
                if semantics.get("columns"):
                    from .schema import apply_semantics
                    apply_semantics(imported[name].schema, semantics)
                    for column_name in semantics["columns"]:
                        column = imported[name].schema.column(column_name)
                        if column is not None:
                            column.user_edited = True
                self.sources[name] = SourceEntry(name, SourceSpec.from_dict(source["spec"]))
                if secrets.get(name):
                    self.secrets[name] = secrets[name]
            except Exception as exc:  # a missing file or an unreachable server must not stop the rest
                problems.append(f"{name}: {exc}")
        thread = data.get("thread", {})
        for raw in thread.get("nodes", []):
            node = ThreadNode(str(raw["id"]), str(raw.get("parent", "")), str(raw["kind"]), str(raw.get("title", "")),
                              dict(raw.get("payload", {})), str(raw.get("created", _now())))
            self.nodes[node.id] = node
            self.order.append(node.id)
        for node_id in list(self.order):
            node = self.nodes[node_id]
            try:
                self._replay_node(node, problems)
            except Exception as exc:
                problems.append(f"{node.title}: {exc}")
        numbers = [int(i[1:]) for i in self.order if i[1:].isdigit()]
        self._ids = itertools.count(max(numbers, default=0) + 1)
        self.current = str(thread.get("current", "")) if thread.get("current") in self.nodes else (
            self.order[-1] if self.order else "")
        self.report = Report.from_dict(data.get("report", {}))
        numbers = [int(i.id[1:]) for i in self.report.items if i.id[1:].isdigit()]
        self._report_ids = itertools.count(max(numbers, default=0) + 1)
        self.modified = False
        return problems

    def _replay_node(self, node: ThreadNode, problems: List[str]) -> None:
        if node.kind == "request":
            plan_data = node.payload.get("plan")
            if not plan_data:
                return
            plan = parse_plan(plan_data, str(node.payload.get("request", "")), str(plan_data.get("planner", "")))
            step_nodes: Dict[str, str] = node.payload.get("step_nodes", {})
            context = self._replay_context(node.parent, plan)
            results = execute_plan(self.workspace, plan, context, self.next_prefix())
            for result in results:
                child = self.nodes.get(step_nodes.get(result.step.id, ""))
                if child is not None:
                    child.result = result
                if not result.ok:
                    problems.append(f"{result.step.title}: {result.error}")
            return
        if node.kind == "transform":
            step = node.payload["step"]
            source = str(node.payload.get("input"))
            table, schema = self.input_table(source)
            plan_step = parse_plan({"steps": [step]}).steps[0]
            node.result = run_step(self.workspace, plan_step, table, schema, self.next_prefix())
            if not node.result.ok:
                problems.append(f"{node.title}: {node.result.error}")
            return
        if node.kind == "chart":
            source = self.nodes.get(str(node.payload.get("source")))
            if source is None:
                return
            node.result = source.result
            node.chart = self.compute_chart(source, ChartSpec.from_dict(node.payload.get("spec", {})))

    def _replay_context(self, parent: str, plan: AnalysisPlan) -> PlanningContext:
        datasets = {name: dataset.schema for name, dataset in self.workspace.datasets.items()}
        results = [ResultRef(n.id, n.title, n.table, n.schema) for n in self.result_nodes() if n.schema]
        return PlanningContext(datasets, results)

    def narrative_of(self, request: ThreadNode) -> Optional[Narrative]:
        data = request.payload.get("narrative")
        return Narrative.from_dict(data) if data else None

    def close(self) -> None:
        self.workspace.close()
