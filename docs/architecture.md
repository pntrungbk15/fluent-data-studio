# Architecture

Fluent Data Studio has two layers. The **engine** (`fluent_data_studio/engine`) is plain Python on DuckDB: it never
imports Qt and is fully usable from the command line and tests. The **desktop interface**
(`fluent_data_studio/ui`) is built with FluentQt Community and FluentQt Pro and only calls into the engine.

```
fluent_data_studio/
  engine/
    workspace.py      one in-process DuckDB database: datasets and step results are tables in it
    sources/          connectors (files, databases, web) that stage read-only snapshots
    safety.py         read-only checks for user-typed source queries
    schema.py         logical types, column roles and the semantic layer
    profile.py        per-column statistics, frequent values, correlations
    expressions.py    the expression language for filters and formulas
    transforms.py     typed operations compiled into one query, stage by stage
    charts.py         chart specs, honest-chart repairs, prepared series
    plan.py           analysis plans: requirements, steps, assumptions; validation
    execute.py        running steps, metrics, correlations
    findings.py       facts from results, number formatting, grounding checks
    rules.py          the rule-based planner
    llm.py            OpenAI-compatible client (standard library only)
    assistant.py      model planning with repair, narration, the model's view of the data
    analyst.py        one request: plan, validate, run, narrate (with fallback)
    session.py        analysis thread, report, project save and replay
  ui/                 window, Analyze workspace, report, chart panel and editor, dialogs, worker tasks
  demo.py             synthetic dataset generator
  cli.py              headless commands
```

## Data processing

Every source is copied once into a DuckDB table (see [data sources](data-sources.md)). Everything afterwards runs
locally in DuckDB: profiling uses `SUMMARIZE` plus exact distinct counts on smaller tables, transformations compile to
SQL, charts aggregate in SQL, and correlation matrices use `corr()`. The engine fetches rows into Python only for
previews (up to 1,000 rows per step, 20,000 in the data grid) and for chart series.

DuckDB was chosen over pandas or Polars for three reasons. Its binder checks a whole pipeline without running it,
which is what validation needs. Its SQL is a readable, exact record of what happened to the data. And it reads CSV,
JSON and Parquet natively with good type detection and handles millions of rows on a laptop.

## Transformation system

An operation is a JSON object. A pipeline is a list of operations applied to a dataset or to an earlier result:

| Operation | Does |
|---|---|
| `filter` | keep rows where a condition holds (window conditions use `QUALIFY`) |
| `select`, `drop`, `rename` | choose, remove or rename columns |
| `derive` | add or replace a column from a formula, with an optional display format |
| `cast` | change a type (invalid values become missing) |
| `fill_missing`, `drop_missing` | zero, value, mean, median, mode or previous value; or remove rows |
| `aggregate` | group by columns, date periods or expressions; measures with `sum`, `avg`, `min`, `max`, `median`, `count`, `count_distinct`, `stddev`, each with an optional condition (`FILTER (WHERE …)`) |
| `sort`, `limit`, `top_n` | order rows; first N; top or bottom N overall or within each group |
| `join` | add columns from another dataset (left or inner) |
| `outliers` | rows outside an interquartile-range or standard-score threshold, with a score |
| `distinct` | unique rows or combinations |

`compile_pipeline` turns the list into a chain of CTEs (`s0 … sN`). For each stage it checks the operation against
the current columns, renders it, and asks DuckDB to `DESCRIBE` the query so far. The first failure is reported with
the operation's position and a message that names the problem. Each stage also yields a sentence for people
("Calculate total revenue where year(order_date) = 2025 as revenue_2025 by product"). Column roles and display formats
carry through stages, so a summed currency column is still currency after aggregation.

Conditions and formulas use a small expression language (`expressions.py`). Its parts are column names, quoted
literals, arithmetic, comparisons, `and`/`or`/`not`, `in`, `between`, `is null`, `like`, and about forty whitelisted
functions: dates, text, math, `if`, `coalesce`, and windows such as `share`, `rank_desc`, `previous`,
`running_total` and `zscore`. It is parsed into a tree, type-checked against the schema and rendered with quoted
identifiers and escaped literals. Anything outside the grammar is rejected, so neither a person nor a model can put
SQL into a plan.

## Visualization system

A `ChartSpec` is structured state: kind, x field, measures, colour split, aggregation, time period, sort, limit,
filter, titles, legend and stacking. `prepare_chart` repairs the spec before compiling it to operations:

- a pie of averages, of several measures, split by colour or over periods becomes a bar chart;
- a pie with more than eight slices or negative values becomes a bar chart;
- a line or area over an unordered category becomes a bar chart;
- a scatter needs two numeric fields; more than 5,000 points are sampled reproducibly;
- more than 30 categories keep the largest 30; long time axes keep the latest periods;
- a date axis gets a period from its span (day, week, month, quarter, year);
- missing periods in sums and counts are zeros on lines, not gaps.

Each repair adds a note, shown under the chart. The prepared `ChartData` (categories and series, or a matrix) is
renderer-neutral. The interface draws it with FluentQt Pro: `ChartView` for bar, line, area, scatter and histogram,
`PieChart` for pies and donuts, `HeatmapChart` for correlations and two-way breakdowns, `MetricCard`s for key
figures. Editing a chart edits the spec and prepares it again; no model is involved.

## Analysis history, branching and projects

`Session` keeps a tree of `ThreadNode`s: datasets, requests, the steps each request ran, manual pipelines and chart
refinements. Each node stores what produced it (plan step, operations, spec), never only its output. New work attaches
under the selected node. The planner receives the results on the path to it as possible inputs, so a follow-up such
as "top 3 regions" can start from the previous result, while selecting a dataset starts a fresh branch.

A project (`.fdsproject`) is JSON: sources without secrets, semantic edits, the thread and the report. Opening one
re-imports the sources and replays every node in order from its recorded operations; no model is called and the
results are the same. Database passwords are asked for again.

## Reports

`Report` is an ordered list of items (chart, table, metric or text) that refer to thread nodes. The Report page lays
them out with Pro's `DashboardLayout`, which reflows by width and is reordered by dragging; spans and titles are
editable. HTML export embeds charts as images rendered from the widgets, tables as HTML (first 50 rows) and key
figures as text.

## Threads and responsiveness

The interface never waits on DuckDB, a database or a model in the GUI thread. `TaskRunner` runs imports, previews,
analysis requests, chart preparation, project replay and model tests on a small `QThreadPool`. DuckDB and HTTP
release the GIL, so plain threads give real concurrency without a worker process. Each call uses its own DuckDB
cursor. Results return through signals with a request id, and a page drops replies it no longer wants (a newer
preview, a stopped request). Methods that change the thread or the report run only on the GUI thread; workers compute
and the GUI commits.

## Interface

The window is FluentQt Pro's `TopNavigationWindow` with two destinations, Analyze and Report; project commands, the
planner choice and settings (a drawer) sit in its trailing area. Analyze (`ui/workspace.py`) is one workspace:
datasets and the history tree (or the lineage graph) on the left, the selection in the centre with the request box
under it, an inspector on the right (chart encodings, data steps, column meaning) and rows at the bottom. The
reasons are in [interface decisions](ux-decisions.md).

## FluentQt integration

The application is built only from the public APIs of FluentQt Community and Pro and follows their contract: colours
from the palette at paint time, `@pyqtSlot` bound methods for long-lived signals, base-class calls by name inside
slots, and FluentIcons only. Community provides the window, navigation, dialogs, forms, settings, item views, states
and theming. Pro provides the data grid, the chart family, the dashboard layout, the chat input and the node graph. No
component was copied into the application or extended for it; everything needed already existed in the two editions.
