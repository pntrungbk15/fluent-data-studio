# Engineering decisions and limitations

## Decisions

**Plans of typed operations, not generated code.** Asking a model for pandas or SQL code is flexible, but the code
must then be sandboxed, it is hard to check before running, and it is hard for people to read. Here the model chooses
operations and writes conditions in a small language the engine parses. Every plan is validated against the data
before anything runs, every step reads as plain sentences, and the SQL is generated, quoted and inspectable. The
trade-off is expressiveness: an analysis the fifteen operations cannot express (for example a statistical test or a
forecast) is not available yet.

**One planner and a repair loop instead of several agents.** A single structured planner, a validator that returns
exact problems, and at most two repairs give predictable behaviour and a clear failure mode (fallback to the rule
planner, with a note). Splitting planning, SQL writing, charting and narration across agents would add latency and
places for errors without improving what the user can check.

**A rule planner beside the model.** The application must be useful without a model. It also needs a deterministic
baseline for tests and a fallback for slow or failing models. Both planners produce the same plan structure, so
validation, execution, history and reports do not care which one planned.

**Facts first, prose second.** Numbers in findings come from the engine. The model may phrase them, and figures it
introduces that are not in the results are flagged. This keeps a small or local model from inventing results.

**Local DuckDB snapshot of every source.** One engine and one dialect for all sources, read-only access at the source,
and reproducibility within a session, at the cost of analysing a snapshot capped by a row limit. See
[data sources](data-sources.md).

**A small semantic layer, not an enterprise one.** Roles, default aggregations, formats, descriptions and synonyms per
column, inferred and editable, with an optional sidecar per dataset. That is enough for "sales" to find `revenue`, for
averages of prices instead of sums, and for percentages to display as percentages. Metrics catalogues, hierarchies and
cross-dataset relationships were left out.

**Structured chart specs with honest-chart repairs.** A chart is data, so editing never calls the model and every chart
can be re-prepared on replay. The repair rules encode common mistakes once instead of hoping the model avoids them.

**Threads on the GUI side, not processes.** The heavy work (DuckDB queries, HTTP) releases the GIL, so a small thread
pool keeps the window responsive without the cost of a worker process. Results carry request ids and stale replies
are dropped.

**Projects store steps, not data.** Project files are small, contain no rows or secrets, and rebuild results by
replaying recorded operations. The data must still be reachable when the project is opened.

**Original interface built from FluentQt.** One Analyze workspace (data, history, the focused result, an inspector,
rows) and a Report, under FluentQt Pro's top navigation. The interface was redesigned after a product-level review
against Data Formulator; see [interface decisions](ux-decisions.md). Everything is built from FluentQt Community and
Pro components, and nothing had to be added to the frameworks.

## Deferred

- More connectors (see the roadmap in [data sources](data-sources.md)) and live queries pushed down to sources.
- Statistical operations (tests, regressions, forecasting) as typed operations.
- PDF export and scheduled refresh of reports; sharing reports as files with data.
- Streaming model responses and cancelling an in-flight model call (a stopped request is ignored, not aborted).
- A visual relationship editor for joins.
- Windows and macOS packaging (Nuitka), and checks of hover and focus states on a real Windows display.

## Known limitations

- The rule planner understands common single-table patterns; it does not join datasets or interpret open-ended
  questions.
- Small local models (3B parameters on a CPU) are slow and need repairs for compound requests; quality depends on the
  model chosen.
- The data grid shows the first 20,000 rows of a dataset; analysis always uses the whole staged table.
- The expression language has no regular expressions and no user-defined functions.
- Value filters recorded from the grid are converted only for text and whole-number columns.
