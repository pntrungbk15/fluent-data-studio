# AI workflow

## One planner, structured output, validation

A request is planned once, by one planner. There is no chain of agents. The planner's output is an
`AnalysisPlan` in JSON:

```json
{
  "summary": "Compare 2025 revenue with 2024 by region, excluding cancelled orders",
  "requirements": [{"id": "R1", "text": "2025 revenue compared with 2024"}, {"id": "R2", "text": "by region"}],
  "assumptions": ["'revenue' is the net revenue column", "'cancelled' matches status = 'Cancelled'"],
  "add_to_report": false,
  "steps": [{
    "id": "s1", "title": "Revenue by region, 2025 vs 2024", "input": "retail_orders",
    "operations": [
      {"op": "filter", "where": "status != 'Cancelled'"},
      {"op": "aggregate", "group_by": ["region"], "measures": [
        {"column": "revenue", "agg": "sum", "as": "rev_2024", "where": "year(order_date) = 2024"},
        {"column": "revenue", "agg": "sum", "as": "rev_2025", "where": "year(order_date) = 2025"}]},
      {"op": "derive", "name": "growth", "expression": "rev_2025 / rev_2024 - 1", "format": "percent"}],
    "output": "chart",
    "chart": {"kind": "bar", "x": "region", "y": ["growth"]},
    "requirements": ["R1", "R2"]
  }]
}
```

Why this shape:

- **Requirements** make compound requests accountable. Each step lists the requirements it answers. Validation
  rejects a plan that leaves one uncovered, and the interface marks each requirement *Answered*, *Failed* or *Not
  covered*.
- **Operations instead of code.** The model chooses from fifteen typed operations and writes conditions in a small
  expression language. The engine owns SQL generation, quoting and type checks. A plan cannot read files, call
  functions outside the whitelist or modify anything.
- **Assumptions** record interpretations (which column is "sales", what "cancelled" matched) so they can be
  reviewed.
- **Steps that read earlier steps** build multi-stage analyses (aggregate, then rank) without nesting.

`validate_plan` compiles every step against the real tables. Each problem goes back to the model in the same
conversation, for example *"s2: step 1 (aggregate): unknown group column 'regoin'; columns: …"* or *"chart field
'revenue' is not in the step result; result columns: region, rev_2024, rev_2025"*, and the model returns a corrected
plan, at most twice (sooner if the model repeats exactly the same problems). Then the analyst either runs the plan or
hands the request to the rule planner and records why.

## What the model sees

`data_context()` builds the only description of the data that leaves the machine:

- dataset names, row counts and descriptions;
- for each column: name, logical type, role, default aggregation, display format, description, synonyms, missing
  count, range (dates and measures), and up to eight frequent values of categorical columns;
- the titles and columns of earlier results on the current branch; the earlier requests of the branch.

It does not include file paths, URLs, hosts, database or user names, passwords, tokens or rows beyond those frequent
values. The narration call adds the facts and at most twelve rows (or chart points) per step. Tests check that a
dataset whose source has a host and user name produces no request containing them.

## Narration and grounding

Facts are computed by `findings.py` for every successful step:

- **key figures:** each value, formatted;
- **comparisons:** highest and lowest category, and the top category's share when the measure is additive;
- **time series:** first and last value with the change, and the peak;
- **correlations:** the strongest pairs and how many pairs show no linear relationship;
- **outliers:** how many rows were found.

Without a model, these facts are the findings, grouped by requirement. With one, the model writes a short summary and
one finding per requirement from the facts and small excerpts. `unverified_numbers` then compares every figure in the
text with the results, accepting rounding, percentages of rates and column or series totals. Anything else is shown as
*"Check these figures; they do not appear in the results"*.

## The rule planner

`rules.py` handles common requests with no model: measures and dimensions matched through names and synonyms, time
periods, top and bottom N, superlatives, shares, distributions, relationships, correlations, outliers, summaries and
dashboards. It also applies constraints such as "in 2025", "excluding cancelled orders", "only the West region" and
"compared with 2024". It splits compound requests on separators and on "and" followed by a new analysis, and merges
identical steps that answer several requirements. When a clause refers to nothing in the data, it produces no step and
the request is reported as not understood.

It is also the fallback when a model is not configured, unreachable, too slow, or cannot produce a valid plan after
two repairs.

## Providers

`LlmClient` speaks the OpenAI-compatible chat completions API with the standard library: `POST {base}/chat/completions`
with `response_format: {"type": "json_object"}`. If a server rejects that field (HTTP 400), the call is retried
without it, and JSON is then extracted from the reply, including from code fences. `GET {base}/models` lists models
in Settings. Presets fill in the usual addresses for Ollama, LM Studio, llama.cpp server, OpenAI and Gemini's
compatibility endpoint; all fields stay editable. Temperature defaults to 0.1 and the timeout to 180 s (raise it for
local models on a CPU).

What was verified:

- the full path against a scripted OpenAI-compatible server in the test suite: request format, key header, JSON-mode
  fallback, repair with validation messages, fallback to rules, narration grounding, and no private details in
  requests;
- a real local model, `qwen2.5:3b` in Ollama on a laptop CPU (no GPU): simple requests produced valid plans that ran,
  at roughly 1 to 4 minutes per call. On the six-part example request it proposed a nine-step plan, repeated the same
  column mistakes after the validation feedback, and the run fell back to the rule planner, which answered all six
  requirements. Larger models were not available on the test machine.
