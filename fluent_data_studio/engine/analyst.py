"""One analysis request from text to findings: plan, validate, run, narrate.

The model-based planner is used when a model is configured; otherwise, or when the model cannot produce a valid plan,
the rule-based planner takes over and the run says so. Either way the same validation and execution apply.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .assistant import LlmPlanner, Narrative, PlanAttempt, narrate
from .execute import StepResult, execute_plan
from .llm import LlmClient, LlmSettings
from .plan import AnalysisPlan, PlanningContext, validate_plan
from .rules import RulePlanner
from .workspace import Workspace

__all__ = ["Analyst", "AnalysisRun"]


@dataclass
class AnalysisRun:
    """Everything one request produced, in the order it happened."""

    request: str
    plan: Optional[AnalysisPlan] = None
    results: List[StepResult] = field(default_factory=list)
    narrative: Optional[Narrative] = None
    attempts: List[PlanAttempt] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and any(r.ok for r in self.results)


class Analyst:
    """``Analyst(workspace, settings).run(request, context, prefix)``; safe to call from a worker thread."""

    def __init__(self, workspace: Workspace, settings: Optional[LlmSettings] = None) -> None:
        self.workspace = workspace
        self.settings = settings or LlmSettings()

    @property
    def uses_model(self) -> bool:
        return self.settings.configured

    def plan(self, request: str, context: PlanningContext, use_model: bool = True,
             conversation: Optional[List[str]] = None, run: Optional[AnalysisRun] = None) -> AnalysisRun:
        run = run or AnalysisRun(request)
        if use_model and self.uses_model:
            outcome = LlmPlanner(LlmClient(self.settings)).plan(request, context, self.workspace, conversation)
            run.attempts = outcome.attempts
            if outcome.plan is not None:
                run.plan = outcome.plan
                if outcome.repaired:
                    run.notes.append(f"The plan was corrected {outcome.repaired} time(s) after validation.")
                return run
            run.notes.append(f"The model could not produce a valid plan ({outcome.error}); "
                             "the built-in rules planned this request instead.")
        run.plan = RulePlanner().plan(request, context)
        issues = validate_plan(run.plan, self.workspace, context)
        for issue in issues:
            run.notes.append(str(issue))
        if not run.plan.steps:
            run.error = ("The built-in rules could not interpret this request. Name a measure and how to break it "
                         "down (for example \"revenue by region\"), or configure a language model in Settings.")
        return run

    def run(self, request: str, context: PlanningContext, prefix: str, use_model: bool = True,
            conversation: Optional[List[str]] = None,
            progress: Optional[Callable[[str, object], None]] = None) -> AnalysisRun:
        started = time.perf_counter()
        run = AnalysisRun(request)
        try:
            self.plan(request, context, use_model, conversation, run)
            if progress:
                progress("planned", run)
            if run.plan is not None and run.plan.steps:
                run.results = execute_plan(self.workspace, run.plan, context, prefix,
                                           (lambda i, r: progress("step", r)) if progress else None)
                client = LlmClient(self.settings) if use_model and self.uses_model and run.plan.planner != \
                    RulePlanner.name else None
                run.narrative = narrate(client, run.plan, run.results)
        finally:
            run.seconds = time.perf_counter() - started
        return run
