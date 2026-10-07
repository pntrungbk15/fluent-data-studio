# Data sources

## Strategy

Every connector does one job: copy a bounded, read-only snapshot of its data into the local DuckDB workspace. After
that, every source looks the same to profiling, transformations, charts, planners and projects. A connector is a
`Connector` record with a form description (fields such as path, host, table, query, password), an optional table
lister and a `stage` function; adding a source means registering one more. The interface builds its "Add data" form
from the field list.

Staging locally instead of querying remote systems live keeps the analytical engine, the validation and the SQL
dialect identical for every source. It makes results reproducible within a session and protects production databases
from exploratory queries. The cost is that analysis sees a snapshot, capped at the row limit (one million rows by
default); the dataset says so when the cap was reached.

## Implemented

| Kind | How it reads | Safety |
|---|---|---|
| CSV, TSV | DuckDB `read_csv`, types detected from the whole file | local file |
| JSON, JSON Lines | DuckDB `read_json_auto` | local file |
| Parquet | DuckDB `read_parquet` | local file |
| Excel | openpyxl (read-only, values), first non-empty row as header, then DuckDB | local file |
| SQLite | `sqlite3` with a `mode=ro` URI; table, view or a checked query | read-only connection |
| DuckDB file | `ATTACH … (READ_ONLY)` under a unique alias, detached afterwards | read-only attachment |
| PostgreSQL | `psycopg` 3; `COPY (SELECT …) TO STDOUT` as CSV with column types from the result description | read-only transaction, `statement_timeout` 60 s, row cap |
| Web address | `urllib` download (30 s, 200 MB cap), CSV, JSON or Parquet by type or extension; a records path for nested JSON (`data.items`) | http/https only; bearer token sent to that address only |

All of these are covered by tests. The PostgreSQL tests run against a real local server (`pgserver`). The web tests
run against a local HTTP server that requires the token.

## Query safety

Database connectors accept a table or a query typed by the user. Language models never write source queries; they only
plan operations on the staged copy. A query passes `check_read_only_query` only as a single `SELECT`, `WITH` or
`VALUES` statement. It may not contain data-modifying keywords outside string literals and comments (including inside
CTEs and `SELECT … INTO`), file-reading or side-effecting functions (`pg_sleep`, `pg_read_file`, `dblink`,
`read_csv`, …), row locks, or dollar quoting. The connection is also read-only, so a query that slips past the text
check still cannot write. The PostgreSQL test proves this: a `SELECT` that calls a function containing an `INSERT`
fails with *"cannot execute INSERT in a read-only transaction"*.

## Credentials

Passwords and tokens are a separate `secret` argument. They are kept in memory for the session, are never written to
project files (a project records only that a source needs one) and are never part of what the model sees. The model's
API key is stored in the user's data folder with owner-only permissions, or comes from `FDS_LLM_API_KEY`.

## Roadmap and trade-offs

| Source | Plan | Main cost |
|---|---|---|
| MySQL / MariaDB | `pymysql` or `mysqlclient`; `SET SESSION TRANSACTION READ ONLY`, `MAX_EXECUTION_TIME` | driver dependency; no `COPY`, so batched fetch |
| SQL Server | `pyodbc` with ODBC Driver 18; `ApplicationIntent=ReadOnly` | system ODBC driver install |
| Oracle | `python-oracledb` thin mode; read-only transaction | large dialect differences in the typed-query path |
| ClickHouse | `clickhouse-connect`; `readonly=1` setting | low risk; good fit for large tables |
| BigQuery, Snowflake, Databricks | vendor connectors; dry-run cost estimates before staging | credentials flows (OAuth, key files), cost control |
| Amazon S3, Azure Blob | DuckDB `httpfs`/`azure` extensions for CSV, JSON, Parquet | extension download at run time; credential chains |
| MongoDB | `pymongo` with a projection and limit, flattened to JSON lines | schema-less documents need a flattening policy |
| Live queries (no staging) | push plans down as SQL to the source for very large tables | dialect translation and cost control |

These were deferred because each one adds a driver, credentials handling or a dialect to test properly, and the
connector interface does not need to change for any of them.
