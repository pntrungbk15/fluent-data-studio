# Interface decision record

This record explains the layout and interaction model of Fluent Data Studio 0.1: what was wrong with the first
interface, what was learned from Data Formulator and from FluentQt Pro's top navigation, and what was decided.

## 1. Problems in the first interface

The first build mapped each part of the engine to a page in a FluentWindow side navigation: Data, Analysis, Report,
Workflow and Settings, plus "Open project" and "Save project" as navigation entries. Walking through the scenarios
(import, understand, ask a compound question, edit a chart, refine, build a report, go back, save) showed these
problems:

1. **Data and analysis were separate places.** Asking a question meant leaving the rows and the column profile.
   Looking at the data meant leaving the analysis. The Analysis page compensated with an "Ask about" combo box that
   duplicated the dataset list.
2. **The analysis was one long document.** A request showed its requirements card, its findings, and every step at
   full size: operations, a 340 px chart, facts and four buttons each. A six-part request was several screens tall,
   and there was no notion of *the* result being worked on.
3. **Chart editing was detached.** "Edit chart" opened a 14-field panel on the right that squeezed the document and
   edited a card that could be scrolled out of view. Changes had to be kept with a separate button, and nothing
   showed afterwards that a chart had been edited.
4. **Generated and manual work were different systems.** AI steps appeared in Analysis. Manual preparation lived in a
   third tab of the Data page and, when applied, threw the user over to the Analysis page. A generated step's
   operations could be read but not changed.
5. **The same history appeared twice.** The history tree on the Analysis page and the node graph on the Workflow page
   showed the same thread in two places, and the graph offered no action beyond opening a node.
6. **The report was a separate application.** Adding a result showed a toast and nothing else. The Analysis page did
   not show what was already on the report, and a report card could not lead back to the analysis that produced it.
7. **Navigation cost space and meant little.** The expanded side pane took about 300 px of a 1600 px window, more than
   the history tree. It exposed implementation areas (Workflow, Settings) and project commands as if they were
   places.
8. **The request bar was crowded and ambiguous.** "Continuing from: …", "Ask about [combo]", a model switch and a model
   label sat side by side; it was unclear which of them decided what the next request would read.

## 2. What Data Formulator does well, and where it creates friction

Studied from its current README, changelog, the Data Formulator 2 paper and the structure of its interface (no code
was used):

- **It is one workspace.** Apart from a landing page there are no pages: a thread pane of cards (prompt, table, chart
  thumbnails) on the left, a contextual canvas for the focused chart, table or report on the right, and the chat box at
  the bottom of the thread. Source tables are pinned at the head of the threads, so the data context sits next to the
  analysis.
- **The thread is the history and the navigation.** You go back by clicking an earlier card and branch by asking from
  it. Nothing is undone or replaced.
- **Encodings are the manual control.** Moving an existing field re-encodes the chart; asking for a new field derives
  new data. The two meet in the same shelf.
- **Its friction:** the encoding shelf hides behind an "edit chart" popover, and the paper reports users expecting
  language alone to set channels. There are four separate prompt boxes. The layout jumps as the canvas opens and
  closes. The derivation code sits in a dialog. The same table appears in several places, told apart by subtle
  highlighting.

## 3. Top navigation

FluentQt Pro's `TopNavigationWindow` places a 48 px tab bar under the title bar. It handles overflow into a "More"
menu, has a trailing area for widgets, and is one keyboard tab stop. Against the side pane, it gives back about 300 px
of width to a workspace that needs width (data, history, chart, inspector) for 48 px of height.

It is the right primitive only if the destinations are few and real. After the audit there are two: **Analyze** (all
data work and analysis) and **Report** (composing kept results, which deserves the full window). Everything else is
not a place:

- datasets, history, lineage, the chart editor, data steps and column meaning are panels or modes of Analyze;
- project commands (new, open, save, export) are a **Project** menu in the trailing area;
- the planner choice is a **model** menu showing what will plan the next request ("Built-in rules", "Model:
  qwen2.5:3b");
- settings open in a **drawer** over the current work instead of replacing it.

## 4. Decisions

1. **One Analyze workspace with stable regions:**
   - left: datasets above the history tree (switchable to the lineage graph);
   - centre: the selection, with the request box under it;
   - right: the inspector;
   - bottom: rows.

   Selecting a dataset shows its profile and rows. Selecting a result shows its chart, provenance and facts, with its
   encodings beside it and its rows below. Selecting a request shows what was understood and every result as a tile.
2. **The selection is the context.** The request box always says what the next request reads ("Next request reads:
   Top 5 products by revenue growth"); closing that chip switches to the whole dataset. Both planners now follow this
   context unless the request names columns only another input has. This was a real bug found in this review: the
   rule planner picked the `products` table for "top 5 products by revenue" while the screen showed `retail_orders`.
3. **Requirements are shown where they are useful.** A request's overview lists each requirement with its status
   (Answered, Failed, Not covered) and links to the results that answer it; each result tile and each result header
   carry their requirement ids. Assumptions and notes are folded and open by themselves when something was not
   understood. There is no permanent requirements panel.
4. **Charts are edited beside the chart, essentials first.** The chart editor is in the inspector:
   - always visible: type as an icon strip, then x, measures, split and how rows combine;
   - under "More options": period, order, limit, filter, titles, legend and stacking.

   Edits apply at once. The first edit of a generated chart records one refinement in the history, and later edits
   update it, so the history does not fill with every click. The result is labelled *Generated*, *Edited* or
   *Prepared*, "Back to the generated chart" returns to the original, and repaired choices are explained under the
   chart.
5. **Generated and manual data work are the same thing.** The inspector's *Data steps* tab lists the operations
   behind any result, generated or not, and lets you add, remove and reorder them. The rows panel previews the edited
   steps, and "Run as new result" branches from the result. On a dataset, the same tab prepares data. There is no
   separate Prepare page.
6. **One history, two views.** The tree and the lineage graph are the same thread; the graph is a mode of the history
   panel, not a page.
7. **The report is connected both ways.** "Add to report" becomes "On the report" and can be undone. A notice offers
   to open the report. Each report card links back to the result that produced it, and offers "Use the edited chart"
   when the result was refined after being added.
8. **The request overview uses the whole window.** It hides the inspector and the rows panel, which have nothing to
   show there, and the panels come back at the sizes the user left. This is a deliberate mode change on a different
   kind of selection, not a jump on every click.

## 5. What was kept, what changed

- **Kept:** the engine and its APIs, except two additions (`record_chart_edit` to coalesce edits, and provenance
  helpers). Also kept: the Pro chart panel and data grid, the dashboard grid of the report with drag reordering and
  HTML export, the connection dialog, the operation forms, the settings content and the thread tree.
- **Changed:** side navigation became top navigation with two destinations. The Data, Analysis and Workflow pages
  merged into the workspace. The long request document became an overview of tiles plus a focused result view. The
  chart editor panel became a progressive inspector tab. Prepare became editable data steps on any result. Project
  commands, the model choice and settings moved out of navigation.

## 6. Data Formulator ideas deliberately not followed

- **Encodings in a popover:** the user study shows the cost. Here they are always beside the focused result.
- **Typing a new field name to derive data:** convenient, but it hides a transformation inside an encoding. Here
  derivations are explicit data steps that can be read and edited.
- **Several prompt boxes:** there is one request box, and its context chip says what it reads.
- **A canvas that opens and closes with focus:** the workspace regions are stable. Only the request overview, a
  different kind of selection, changes the layout.
- **Report as a canvas mode inside the thread:** composing a report needs the whole window and a grid, so Report is
  its own destination, linked both ways to the analysis.
- **AI-written reports:** reports here are composed from results and computed findings by the user. A model can phrase
  the findings, but it does not write the report.
