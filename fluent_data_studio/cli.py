"""Headless commands: inspect data sources and run analysis requests from a terminal (no Qt needed).

Examples::

    python -m fluent_data_studio profile fluent_data_studio/datasets/retail_orders.csv
    python -m fluent_data_studio ask fluent_data_studio/datasets/retail_orders.csv "revenue by region in 2025"
    python -m fluent_data_studio ask data.csv "monthly sales" --model qwen2.5:3b --endpoint http://localhost:11434/v1
    python -m fluent_data_studio sources
    python -m fluent_data_studio generate-demo
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

__all__ = ["main"]


def _load(workspace, paths: List[str]):
    from .engine.sources import SourceSpec, import_source, kind_for_path
    datasets = []
    for raw in paths:
        path = Path(raw)
        kind = kind_for_path(path)
        if kind is None:
            raise SystemExit(f"unsupported file type: {path.suffix}")
        options = {"path": str(path)}
        if kind in ("sqlite", "duckdb"):
            from .engine.sources import list_tables
            for table in list_tables(SourceSpec(kind, options)):
                datasets.append(import_source(workspace, SourceSpec(kind, {**options, "table": table})))
            continue
        datasets.append(import_source(workspace, SourceSpec(kind, options)))
    return datasets


def _profile(args) -> int:
    from .engine.profile import profile_summary
    from .engine.workspace import Workspace
    workspace = Workspace()
    for dataset in _load(workspace, args.files):
        print(f"\n{dataset.name}")
        for fact in profile_summary(dataset.schema):
            print(f"  {fact}")
        for column in dataset.schema.columns:
            extra = f" [{column.format}]" if column.format else ""
            print(f"  {column.name:<22} {column.type:<9} {column.role:<10} {column.aggregation:<14}{extra}")
    return 0


def _ask(args) -> int:
    from .engine.analyst import Analyst
    from .engine.findings import format_value
    from .engine.llm import LlmSettings
    from .engine.plan import PlanningContext
    from .engine.workspace import Workspace
    workspace = Workspace()
    datasets = _load(workspace, args.files)
    settings = LlmSettings(base_url=args.endpoint or os.environ.get("FDS_LLM_ENDPOINT", ""),
                           model=args.model or os.environ.get("FDS_LLM_MODEL", ""),
                           api_key=os.environ.get("FDS_LLM_API_KEY", ""), temperature=args.temperature,
                           timeout=args.timeout)
    context = PlanningContext({d.name: d.schema for d in datasets}, focus=datasets[0].name if datasets else "")
    run = Analyst(workspace, settings).run(args.request, context, "r1", use_model=settings.configured)
    if args.json:
        print(json.dumps({"plan": run.plan.to_dict() if run.plan else None, "notes": run.notes, "error": run.error,
                          "attempts": [{"reply": a.reply, "issues": a.issues} for a in run.attempts],
                          "results": [{"step": r.step.id, "error": r.error, "rows": r.rows, "facts": r.facts,
                                       "sql": r.pipeline.sql if r.pipeline else "",
                                       "chart": r.chart.to_dict() if r.chart else None} for r in run.results],
                          "narrative": run.narrative.to_dict() if run.narrative else None}, indent=2, default=str))
        return 0 if run.ok else 1
    plan = run.plan
    print(f"Planner: {plan.planner if plan else '-'}   ({run.seconds:.1f} s)")
    if run.error:
        print(f"\n{run.error}")
    for note in run.notes:
        print(f"Note: {note}")
    if plan is None:
        return 1
    print("\nRequirements")
    for requirement in plan.requirements:
        print(f"  {requirement.id}  {requirement.text}")
    for assumption in plan.assumptions:
        print(f"  Assumption: {assumption}")
    for result in run.results:
        step = result.step
        print(f"\n{step.id}  {step.title}   [{step.output}, covers {', '.join(step.requirements) or '-'}]")
        if result.pipeline:
            for description in result.pipeline.descriptions():
                print(f"    {description}")
        if not result.ok:
            print(f"    Failed: {result.error}")
            continue
        if result.chart is not None:
            chart = result.chart
            print(f"    Chart: {chart.spec.kind} of {', '.join(chart.spec.y) or 'rows'} by {chart.spec.x or '-'}")
            for note in chart.notes:
                print(f"    Chart note: {note}")
        preview = result.preview
        if preview is not None and step.output in ("table", "metric"):
            print("    " + " | ".join(preview.columns))
            for row in preview.rows[:args.rows]:
                print("    " + " | ".join(format_value(v) for v in row))
        for fact in result.facts:
            print(f"    Fact: {fact}")
    narrative = run.narrative
    if narrative is not None:
        print(f"\nFindings ({'written by the model' if narrative.source == 'model' else 'computed'})")
        print(f"  {narrative.summary}")
        for finding in narrative.findings:
            print(f"  {finding.get('requirement', '')}: {finding['text']}")
        for caveat in narrative.caveats:
            print(f"  Caveat: {caveat}")
        if narrative.unverified:
            print(f"  Figures not found in the results: {', '.join(narrative.unverified)}")
    return 0 if run.ok else 1


def _sources(_args) -> int:
    from .engine.sources import connectors
    for item in connectors():
        state = "available" if item.available() else f"needs '{item.requires}'"
        print(f"{item.kind:<9} {item.title:<38} {item.group:<9} {state}")
    return 0


def _generate(_args) -> int:
    from .demo import generate_all
    for path in generate_all():
        print(path)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="fluent-data-studio", description="Fluent Data Studio (headless commands)")
    sub = parser.add_subparsers(dest="command", required=True)
    profile = sub.add_parser("profile", help="profile data files")
    profile.add_argument("files", nargs="+")
    profile.set_defaults(run=_profile)
    ask = sub.add_parser("ask", help="plan and run an analysis request")
    ask.add_argument("files", nargs="+", help="data files (CSV, JSON, Parquet, Excel, SQLite, DuckDB)")
    ask.add_argument("request")
    ask.add_argument("--endpoint", help="OpenAI-compatible base URL (or FDS_LLM_ENDPOINT); key in FDS_LLM_API_KEY")
    ask.add_argument("--model", help="model name (or FDS_LLM_MODEL)")
    ask.add_argument("--temperature", type=float, default=0.1)
    ask.add_argument("--timeout", type=float, default=180.0, help="seconds per model call (CPU models need more)")
    ask.add_argument("--rows", type=int, default=8, help="table rows to print")
    ask.add_argument("--json", action="store_true", help="print the plan and results as JSON")
    ask.set_defaults(run=_ask)
    sub.add_parser("sources", help="list data source connectors").set_defaults(run=_sources)
    sub.add_parser("generate-demo", help="regenerate the synthetic demo datasets").set_defaults(run=_generate)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
