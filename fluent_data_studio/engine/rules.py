"""A rule-based planner: common analytical requests without a language model.

It understands requests built from familiar patterns ("revenue by region", "top 5 products by growth", "monthly
sales", "relationship between price and units", "unusual values in cost", "summarize", "what is correlated") and
the constraints people attach to them ("in 2025", "excluding cancelled orders", "for the West region",
"compared with 2024"). Several requests separated by commas, "and" or semicolons become separate requirements that
share the constraints. Column names are matched through the semantic layer (names, synonyms, roles).

It produces the same :class:`~fluent_data_studio.engine.plan.AnalysisPlan` as the model-based planner, so plans are
validated and run the same way. What it cannot interpret is reported as an unanswered requirement, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .charts import ChartSpec
from .plan import AnalysisPlan, PlanningContext, PlanStep, Requirement
from .schema import ColumnInfo, Role, TableSchema, column_terms, words

__all__ = ["RulePlanner"]

_TIME_WORDS = {"daily": "day", "day": "day", "days": "day", "weekly": "week", "week": "week", "weeks": "week",
               "monthly": "month", "month": "month", "months": "month", "quarterly": "quarter", "quarter": "quarter",
               "quarters": "quarter", "yearly": "year", "annual": "year", "annually": "year", "year": "year",
               "years": "year"}
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "twenty": 20}
_COUNT_WORDS = ("how many", "number of", "count of", "count ")
_STOP = {"the", "a", "an", "of", "in", "on", "for", "by", "per", "and", "or", "to", "with", "show", "me", "what",
         "which", "is", "are", "was", "were", "has", "have", "plot", "chart", "graph", "create", "make", "give",
         "find", "compare", "across", "between", "each", "all", "total", "sum", "average", "mean", "over", "time",
         "top", "highest", "lowest", "most", "least", "best", "worst", "largest", "smallest", "this", "dataset",
         "data", "table", "analyze", "analyse", "analysis", "from", "than", "vs", "versus", "against", "its", "their"}


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _norm(text: str) -> str:
    return " ".join(_singular(w) for w in words(text))


@dataclass
class _Constraints:
    filters: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    years: List[int] = field(default_factory=list)


class RulePlanner:
    """``RulePlanner().plan(request, context)`` returns an :class:`AnalysisPlan` (possibly with uncovered requirements)."""

    name = "built-in rules"

    def plan(self, request: str, context: PlanningContext) -> AnalysisPlan:
        dataset, schema = self._choose_input(request, context)
        plan = AnalysisPlan(request, planner=self.name)
        if schema is None:
            plan.summary = "No dataset is loaded."
            return plan
        clauses = self._clauses(request)
        constraints = self._constraints(request, schema)
        plan.assumptions.extend(constraints.notes)
        plan.summary = f"Analyse {dataset}" + (f" ({'; '.join(constraints.notes)})" if constraints.notes else "")
        summaries: List[str] = []
        for clause in clauses:
            if self._constraint_only(clause, schema):
                continue
            requirement = Requirement(f"R{len(plan.requirements) + 1}", clause.strip().rstrip(".").capitalize())
            plan.requirements.append(requirement)
            lowered = clause.lower()
            if re.search(r"\b(dashboard|report)\b", lowered):
                plan.add_to_report = True
            if re.search(r"\b(summari[sz]e|sum up|conclude|explain)\b.*\b(finding|result|insight|analysis|it|them)", lowered):
                summaries.append(requirement.id)  # answered by the written findings of the whole run
                continue
            unknown = self._unmatched_words(clause, schema)
            if unknown:
                plan.assumptions.append(f"{requirement.id}: not understood and ignored: {', '.join(unknown)}")
            for step in self._steps_for(clause, request, dataset, schema, constraints, plan):
                same = next((s for s in plan.steps if s.input == step.input and s.operations == step.operations
                             and s.output == step.output and s.chart == step.chart), None)
                if same is not None:
                    same.requirements.append(requirement.id)
                    continue
                step.id = f"s{len(plan.steps) + 1}"
                step.requirements = [requirement.id]
                plan.steps.append(step)
            if re.search(r"\b(dashboard|report)\b", lowered):
                for step in plan.steps:  # a dashboard collects every result of the request
                    if requirement.id not in step.requirements:
                        step.requirements.append(requirement.id)
        if plan.steps:
            for requirement_id in summaries:
                for step in plan.steps:
                    step.requirements.append(requirement_id)
        if not plan.requirements:
            requirement = Requirement("R1", request.strip().capitalize() or "Summarize the data")
            plan.requirements.append(requirement)
            for step in self._summary_steps(dataset, schema, constraints):
                step.id = f"s{len(plan.steps) + 1}"
                step.requirements = ["R1"]
                plan.steps.append(step)
        return plan

    # ---- input and clauses ----------------------------------------------------------------------------------

    def _choose_input(self, request: str, context: PlanningContext) -> Tuple[str, Optional[TableSchema]]:
        """The input the user is looking at, unless it lacks what the request names and another input has it."""
        candidates: List[Tuple[str, TableSchema]] = list(context.datasets.items())
        candidates += [(ref.id, ref.schema) for ref in context.results]
        if not candidates:
            return "", None
        text = " " + _norm(request) + " "

        def score(schema: TableSchema) -> float:
            return sum(1.0 for c in schema.columns if c.role in (Role.MEASURE, Role.DIMENSION, Role.TIME)
                       for term in column_terms(c) if term and " " + _norm(term) + " " in text)

        focus = next(((n, s) for n, s in candidates if n == context.focus), None)
        best = max(candidates, key=lambda item: score(item[1]))
        if focus is not None and score(focus[1]) >= score(best[1]):
            return focus
        return best if score(best[1]) > 0 else (focus or candidates[0])

    @staticmethod
    def _clauses(request: str) -> List[str]:
        text = re.sub(r"\s+", " ", request.strip())
        parts = re.split(r";|[.?!]\s+|\n|,\s*(?:and\s+|then\s+)?|\s+then\s+|\s+and\s+(?=(?:show|find|create|plot|compare|"
                         r"list|summari[sz]e|identify|analy[sz]e|chart|give|what|which|how|make|build|add|rank|"
                         r"highlight|break|top|bottom|monthly|weekly|daily|quarterly|yearly|annual|average|total|"
                         r"distribution|correlation|unusual|outliers|the\s+(?:top|bottom|monthly|total|average))\b)",
                         text, flags=re.I)
        return [p.strip(" .?!") for p in parts if p and p.strip(" .?!")]

    def _constraint_only(self, clause: str, schema: TableSchema) -> bool:
        lowered = clause.lower()
        if re.match(r"^(excluding|exclude|without|except|ignore|ignoring|only|in|for|during|within|using)\b", lowered):
            return not self._mentions_measure(clause, schema) or lowered.startswith(("exclud", "without", "except",
                                                                                      "ignor", "only"))
        return False

    # ---- constraints ----------------------------------------------------------------------------------------

    def _constraints(self, request: str, schema: TableSchema) -> _Constraints:
        result = _Constraints()
        lowered = request.lower()
        time_column = self._time_column(schema)
        years = sorted({int(y) for y in re.findall(r"\b(19\d\d|20\d\d)\b", request)})
        result.years = years
        comparing = len(years) >= 2 or bool(re.search(r"\b(compare|compared|versus|vs\.?|against|growth|change)\b",
                                                     lowered)) and len(years) >= 2
        if time_column is not None and years and not comparing:
            column = time_column.name
            if len(years) == 1:
                result.filters.append(f"year({column}) = {years[0]}" if time_column.is_temporal else f"{column} = {years[0]}")
                result.notes.append(f"Only {years[0]} ({column})")
        exclusions = re.findall(r"\b(?:exclud\w*|without|except|ignor\w*|not counting|remove|removing)\s+([\w\s-]+?)"
                                r"(?=[,.;]|\band\b|$)", lowered)
        for phrase in exclusions:
            match = self._value_match(phrase, schema)
            if match:
                column, value = match
                result.filters.append(f"{column.name} != '{_quote(value)}'")
                result.notes.append(f"Excluding {column.name} = {value}")
        inclusions = re.findall(r"\b(?:only|just)\s+(?:for\s+|in\s+|the\s+)?([\w\s-]+?)(?=[,.;]|\band\b|$)", lowered)
        inclusions += re.findall(r"\b(?:in|for|from)\s+the\s+([\w\s-]+?)\s+(?:region|channel|category|segment|line|"
                                 r"carrier|mode|campaign|shift)\b", lowered)
        for phrase in inclusions:
            match = self._value_match(phrase, schema)
            if match:
                column, value = match
                condition = f"{column.name} = '{_quote(value)}'"
                if condition not in result.filters:
                    result.filters.append(condition)
                    result.notes.append(f"Only {column.name} = {value}")
        return result

    @staticmethod
    def _value_match(phrase: str, schema: TableSchema) -> Optional[Tuple[ColumnInfo, str]]:
        tokens = {_singular(w) for w in words(phrase)}
        best: Optional[Tuple[ColumnInfo, str]] = None
        for column in schema.columns:
            if column.role not in (Role.DIMENSION,) or not column.top_values or column.distinct > 60:
                continue
            for value in column.top_values:
                value_tokens = {_singular(w) for w in words(value)}
                if value_tokens and value_tokens <= tokens:
                    if best is None or len(value) > len(best[1]):
                        best = (column, value)
        return best

    _KNOWN = set("""what which who how many much is are was were the a an of in on for by per and or to with from at as
        show me give find list plot chart graph create make build compare comparison across between each all every
        total sum average mean median min minimum max maximum count number top bottom highest lowest most least fewest
        best worst largest smallest biggest over time trend trends monthly weekly daily quarterly yearly annual
        annually month months week weeks day days quarter quarters year years distribution histogram spread range
        relationship correlation correlated related vs versus against scatter unusual outlier outliers anomaly
        anomalies abnormal strange suspicious summarize summarise summary overview describe profile dataset data
        table dashboard report key metrics metric figures kpi kpis headline share proportion breakdown mix split
        percentage growth grew growing increase change exclude excluding without except ignore ignoring only just
        then also it its their this that these those analyze analyse analysis do does did has have had there
        values value columns column rows row records record has get see let can could would should please""".split())

    def _unmatched_words(self, clause: str, schema: TableSchema) -> List[str]:
        """Content words of a clause that match no column, value or known request word."""
        text = " " + " ".join(_singular(w) for w in words(clause)) + " "
        for column in schema.columns:
            for term in column_terms(column):
                text = text.replace(" " + _norm(term) + " ", " ")
            for value in column.top_values:
                text = text.replace(" " + _norm(value) + " ", " ")
        known = {_singular(w) for w in self._KNOWN} | {_singular(w) for w in words(schema.name)}
        return [w for w in text.split() if w not in known and not w.isdigit() and w not in _NUMBERS and len(w) > 2
                and not re.fullmatch(r"(19|20)\d\d", w)]

    # ---- columns --------------------------------------------------------------------------------------------

    @staticmethod
    def _time_column(schema: TableSchema) -> Optional[ColumnInfo]:
        times = [c for c in schema.columns if c.role == Role.TIME]
        temporal = [c for c in times if c.is_temporal]
        return (temporal or times or [None])[0]

    @staticmethod
    def _find(clause: str, schema: TableSchema, roles: Sequence[str]) -> List[ColumnInfo]:
        """Columns of the given roles mentioned in ``clause``, in order of appearance (longest phrase wins)."""
        text = " " + _norm(clause) + " "
        found: List[Tuple[int, int, ColumnInfo]] = []
        for column in schema.columns:
            if column.role not in roles:
                continue
            for term in column_terms(column):
                phrase = _norm(term)
                if not phrase:
                    continue
                position = text.find(" " + phrase + " ")
                while position >= 0:
                    found.append((position, -len(phrase), column))
                    position = text.find(" " + phrase + " ", position + 1)
        found.sort(key=lambda item: (item[0], item[1]))
        ordered: List[ColumnInfo] = []
        covered: List[Tuple[int, int]] = []
        for position, negative_length, column in found:
            span = (position, position - negative_length)
            if column in ordered or any(a <= span[0] < b or span[0] <= a < span[1] for a, b in covered):
                continue
            ordered.append(column)
            covered.append(span)
        return ordered

    def _mentions_measure(self, clause: str, schema: TableSchema) -> bool:
        return bool(self._find(clause, schema, (Role.MEASURE,)))

    @staticmethod
    def _default_measure(schema: TableSchema) -> Optional[ColumnInfo]:
        measures = [c for c in schema.columns if c.role == Role.MEASURE]
        for pattern in ("revenue", "sales", "amount", "profit", "spend", "cost", "value", "units", "quantity"):
            for column in measures:
                if pattern in column.name.lower():
                    return column
        return measures[0] if measures else None

    # ---- steps ----------------------------------------------------------------------------------------------

    def _base(self, constraints: _Constraints) -> List[Dict[str, Any]]:
        return [{"op": "filter", "where": f} for f in constraints.filters]

    def _steps_for(self, clause: str, request: str, dataset: str, schema: TableSchema, constraints: _Constraints,
                   plan: AnalysisPlan) -> List[PlanStep]:
        lowered = clause.lower()
        measures = self._find(clause, schema, (Role.MEASURE,))
        dimensions = self._find(clause, schema, (Role.DIMENSION, Role.IDENTIFIER))
        time_column = self._time_column(schema)
        free = " " + " ".join(words(lowered)) + " "
        for column in measures + dimensions:  # "transit days" is a column, not a request for daily figures
            for term in column_terms(column):
                free = free.replace(" " + term + " ", " ").replace(" " + _norm(term) + " ", " ")
        bucket = next((_TIME_WORDS[w] for w in free.split() if w in _TIME_WORDS and w not in ("year", "years")
                       or (w in ("yearly", "annual", "annually"))), "")
        over_time = bool(bucket) or bool(re.search(r"\bover time\b|\btrend|\bper (day|week|month|quarter|year)\b|"
                                                   r"\bby (day|week|month|quarter|year)\b|\btimeline\b", free))
        if not bucket:
            match = re.search(r"\b(?:per|by|each)\s+(day|week|month|quarter|year)\b", free)
            bucket = match.group(1) if match else ""
        top = re.search(r"\b(?:top|best|largest|biggest|highest)\s+(\d+|" + "|".join(_NUMBERS) + r")\b", lowered)
        bottom = re.search(r"\b(?:bottom|worst|lowest|smallest)\s+(\d+|" + "|".join(_NUMBERS) + r")\b", lowered)
        count = None
        if top or bottom:
            raw = (top or bottom).group(1)
            count = int(raw) if raw.isdigit() else _NUMBERS[raw]
        superlative = re.search(r"\b(which|what)\b.*\b(highest|most|largest|biggest|best|lowest|least|smallest|worst)\b",
                                lowered)
        counting = any(w in lowered for w in _COUNT_WORDS) or re.search(r"\b(orders|shipments|transactions|records|"
                                                                         r"rows)\b", lowered) and not measures
        base = self._base(constraints)
        title_suffix = f" in {constraints.years[0]}" if len(constraints.years) == 1 else ""

        if re.search(r"\b(summari[sz]e|summary|overview|describe|profile)\b", lowered) and not dimensions:
            return self._summary_steps(dataset, schema, constraints)
        if re.search(r"\bcorrelat|\brelated\b|\brelationships?\b", lowered) and len(measures) < 2:
            return [PlanStep("", "Correlation between measures" + title_suffix, dataset, base, "correlation",
                             why="Pearson correlation of every pair of numeric measures")]
        if re.search(r"\b(unusual|outliers?|anomal\w*|abnormal|strange|suspicious)\b", lowered):
            targets = measures or [m for m in [self._default_measure(schema)] if m]
            if not targets:
                return []
            return [PlanStep("", f"Unusual {m.name} values", dataset, base + [{"op": "outliers", "column": m.name,
                                                                                 "method": "iqr", "threshold": 3}],
                             "table", why="Rows more than 3 interquartile ranges outside the middle half (far outliers)")
                    for m in targets[:2]]
        if re.search(r"\b(relationship|vs\.?|versus|against|correlation between|scatter)\b", lowered) and len(measures) >= 2:
            x, y = measures[0], measures[1]
            color = dimensions[0].name if dimensions else ""
            return [PlanStep("", f"{y.name.capitalize()} against {x.name}", dataset, base, "chart",
                             ChartSpec("scatter", x.name, [y.name], color=color, aggregate="none"),
                             why="Each point is one row")]
        if re.search(r"\b(distribution|histogram|spread|range of)\b", lowered):
            target = (measures or [self._default_measure(schema)])[0]
            if target is None:
                return []
            return [PlanStep("", f"Distribution of {target.name}", dataset, base, "chart",
                             ChartSpec("histogram", target.name, aggregate="count"))]
        if re.search(r"\b(dashboard|kpis?|key (metrics|figures|numbers)|headline)\b", lowered):
            return self._kpi_steps(dataset, schema, constraints)

        analytic = re.search(r"\b(total|sum|average|mean|median|minimum|maximum|sales|revenue|amount|trend|over time|"
                             r"compare|comparison|breakdown|share|by|per)\b", lowered)
        if not (measures or dimensions or counting or over_time or top or bottom or superlative or analytic):
            return []  # nothing in the clause refers to the data: report it as not understood rather than guess
        measure = measures[0] if measures else (None if counting else self._default_measure(schema))
        growth = bool(re.search(r"\bgrowth|\bgrew|\bgrowing|\bincrease|\bchange\b", lowered))
        years = constraints.years
        if len(years) >= 2 and (growth or re.search(r"\b(compare|compared|versus|vs|against)\b", lowered)) \
                and time_column is not None and measure is not None:
            return [self._period_comparison(dataset, schema, constraints, measure, dimensions, years, count,
                                            bool(bottom), growth)]
        if len(years) >= 2 and growth is False and time_column is not None and measure is not None \
                and re.search(r"\bcompar", request.lower()) and not dimensions and not over_time:
            return [self._period_comparison(dataset, schema, constraints, measure, [], years, None, False, False)]

        agg_word = re.search(r"\b(average|mean|avg|median|minimum|min|maximum|max)\b", lowered)
        agg = None
        if agg_word:
            agg = {"average": "avg", "mean": "avg", "avg": "avg", "median": "median", "minimum": "min", "min": "min",
                   "maximum": "max", "max": "max"}[agg_word.group(1)]
        if measure is not None:
            agg = agg or (measure.aggregation if measure.aggregation in ("sum", "avg", "min", "max", "median") else "sum")
        noun = re.search(r"\b(?:how many|number of|count of|most|fewest|least)\s+(?:\w+\s+)?([a-z]+s)\b", lowered)
        count_alias = noun.group(1) if noun and not self._find(noun.group(1), schema, Role.ALL) else "count"
        value_alias = (measure.name if agg == "sum" else f"{agg}_{measure.name}") if measure else count_alias
        measure_def = ({"column": measure.name, "agg": agg, "as": value_alias} if measure
                       else {"agg": "count", "as": count_alias})

        if over_time and time_column is not None:
            key: Any = {"column": time_column.name, "bucket": bucket or "month", "as": bucket or "month"} \
                if time_column.is_temporal else time_column.name
            group = [key] + ([dimensions[0].name] if dimensions else [])
            x = bucket or "month" if time_column.is_temporal else time_column.name
            ops = base + [{"op": "aggregate", "group_by": group, "measures": [measure_def]}]
            # the step already aggregated by this period, so the chart keeps the period and adds nothing up
            spec = ChartSpec("line", x, [value_alias], color=dimensions[0].name if dimensions else "", sort="x",
                             x_bucket=(bucket or "month") if time_column.is_temporal else "")
            what = (measure.name if agg == "sum" else f"average {measure.name}" if agg == "avg"
                    else f"{agg} {measure.name}") if measure else count_alias
            return [PlanStep("", f"{what.capitalize()} by {x}" + (f" and {dimensions[0].name}" if dimensions else "")
                             + title_suffix, dataset, ops, "chart", spec)]

        if dimensions:
            dimension = dimensions[0]
            ops = base + [{"op": "aggregate", "group_by": [dimension.name], "measures": [measure_def]}]
            if count or superlative:
                n = count or (1 if superlative else 10)
                descending = not bottom and not (superlative and re.search(r"lowest|least|smallest|worst",
                                                                           superlative.group(0)))
                ops.append({"op": "top_n", "column": value_alias, "count": n, "descending": descending})
            share = re.search(r"\b(share|proportion|breakdown|mix|split|percentage)\b", lowered)
            kind = "pie" if share else "bar"
            color = dimensions[1].name if len(dimensions) > 1 else ""
            if color:
                ops[-1 if not (count or superlative) else -2]["group_by"].append(color)
            spec = ChartSpec(kind, dimension.name, [value_alias], color=color,
                             sort="value_asc" if bottom else "value_desc")
            what = ((measure.name if agg == "sum" else f"average {measure.name}" if agg == "avg"
                     else f"{agg} {measure.name}") if measure else count_alias).capitalize()
            title = f"{what} by {dimension.name}" + (f" and {color}" if color else "")
            if count:
                title = f"{'Bottom' if bottom else 'Top'} {count} {_plural(dimension.name)} by {measure.name if measure else 'count'}"
            elif superlative:
                title = f"{dimension.name.capitalize()} with the " + (
                    f"{'lowest' if not descending else 'highest'} {measure.name}" if measure else
                    f"{'fewest' if not descending else 'most'} {count_alias}")
            return [PlanStep("", title + title_suffix, dataset, ops, "chart", spec)]

        if measure is not None or counting:
            return [PlanStep("", ("Total " if agg == "sum" else f"{agg.capitalize() if agg else 'Count'} ")
                             + (measure.name if measure else "rows") + title_suffix, dataset,
                             base + [{"op": "aggregate", "measures": [measure_def]}], "metric")]
        return []

    def _period_comparison(self, dataset: str, schema: TableSchema, constraints: _Constraints, measure: ColumnInfo,
                           dimensions: List[ColumnInfo], years: List[int], count: Optional[int], bottom: bool,
                           growth: bool) -> PlanStep:
        time_column = self._time_column(schema)
        year_expr = f"year({time_column.name})" if time_column.is_temporal else time_column.name
        first, last = years[0], years[-1]
        agg = measure.aggregation if measure.aggregation in ("sum", "avg") else "sum"
        measures = [{"column": measure.name, "agg": agg, "as": f"{measure.name}_{y}", "where": f"{year_expr} = {y}"}
                    for y in (first, last)]
        ops: List[Dict[str, Any]] = self._base(constraints) + [{"op": "filter", "where": f"{year_expr} in ({first}, {last})"}]
        aggregate: Dict[str, Any] = {"op": "aggregate", "group_by": [dimensions[0].name] if dimensions else [],
                                     "measures": measures}
        ops.append(aggregate)
        ops.append({"op": "derive", "name": "growth", "format": "percent",
                    "expression": f"{measure.name}_{last} / {measure.name}_{first} - 1"})
        if dimensions:
            ops.append({"op": "derive", "name": "change", "expression": f"{measure.name}_{last} - {measure.name}_{first}"})
            if count:
                ops.append({"op": "top_n", "column": "growth" if growth else f"{measure.name}_{last}", "count": count,
                            "descending": not bottom})
            spec = ChartSpec("bar", dimensions[0].name, ["growth"] if growth else [f"{measure.name}_{first}",
                                                                                   f"{measure.name}_{last}"],
                             sort="value_asc" if bottom else "value_desc")
            title = (f"{'Top' if not bottom else 'Bottom'} {count} {_plural(dimensions[0].name)} by " if count else
                     f"{dimensions[0].name.capitalize()}: ") + f"{measure.name} {'growth' if growth else ''} {last} vs {first}"
            return PlanStep("", re.sub(r"\s+", " ", title), dataset, ops, "chart", spec,
                            why=f"{agg} of {measure.name} per year, then the relative change")
        return PlanStep("", f"{measure.name.capitalize()} {last} vs {first}", dataset, ops, "metric",
                        why=f"{agg} of {measure.name} for each year and the relative change")

    def _summary_steps(self, dataset: str, schema: TableSchema, constraints: _Constraints) -> List[PlanStep]:
        steps = self._kpi_steps(dataset, schema, constraints)
        measure = self._default_measure(schema)
        time_column = self._time_column(schema)
        dimensions = [c for c in schema.columns if c.role == Role.DIMENSION and 1 < c.distinct <= 30]
        base = self._base(constraints)
        if measure is not None and time_column is not None and time_column.is_temporal:
            steps.append(PlanStep("", f"{measure.name.capitalize()} over time", dataset, base, "chart",
                                  ChartSpec("line", time_column.name, [measure.name], aggregate=measure.aggregation
                                            if measure.aggregation in ("sum", "avg") else "sum")))
        if measure is not None and dimensions:
            dimension = min(dimensions, key=lambda c: c.distinct)
            steps.append(PlanStep("", f"{measure.name.capitalize()} by {dimension.name}", dataset, base, "chart",
                                  ChartSpec("bar", dimension.name, [measure.name], aggregate=measure.aggregation
                                            if measure.aggregation in ("sum", "avg") else "sum", sort="value_desc")))
        return steps

    def _kpi_steps(self, dataset: str, schema: TableSchema, constraints: _Constraints) -> List[PlanStep]:
        measures = [c for c in schema.columns if c.role == Role.MEASURE][:4]
        defs: List[Dict[str, Any]] = [{"agg": "count", "as": "rows"}]
        for column in measures:
            agg = column.aggregation if column.aggregation in ("sum", "avg") else "sum"
            defs.append({"column": column.name, "agg": agg, "as": column.name if agg == "sum" else f"avg_{column.name}"})
        return [PlanStep("", "Key figures", dataset, self._base(constraints) + [{"op": "aggregate", "measures": defs}],
                         "metric", why="Row count and the main measures")]


def _plural(name: str) -> str:
    return name if name.endswith("s") else (name[:-1] + "ies" if name.endswith("y") else name + "s")


def _quote(value: str) -> str:
    return value.replace("'", "''")
