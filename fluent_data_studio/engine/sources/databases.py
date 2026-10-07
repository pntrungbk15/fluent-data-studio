"""Database connectors: SQLite and DuckDB files, and PostgreSQL servers.

Every connection is read-only at the source (``mode=ro`` SQLite URIs, DuckDB ``READ_ONLY`` attachments, PostgreSQL
read-only transactions with a statement timeout). A dataset is either one table or view, or a custom query that
passes :func:`~fluent_data_studio.engine.safety.check_read_only_query`. Rows are capped by the source's row limit.
"""

from __future__ import annotations

import csv
import itertools
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from ..safety import check_read_only_query
from ..schema import quote_ident
from ..workspace import Workspace
from . import Connector, ConnectorField, SourceError, SourceSpec, register
from .files import stage_csv

_STATEMENT_TIMEOUT_MS = 60_000


def _selection(spec: SourceSpec, quote) -> str:
    """The SELECT to run at the source: a checked custom query, or one table."""
    query = str(spec.options.get("query") or "").strip()
    if query:
        return f"SELECT * FROM ({check_read_only_query(query)}) AS fds_query"
    table = str(spec.options.get("table") or "").strip()
    if not table:
        raise SourceError("choose a table or enter a query")
    return "SELECT * FROM " + ".".join(quote(part) for part in table.split("."))


# ---- SQLite ------------------------------------------------------------------------------------------------------

def _sqlite_connect(spec: SourceSpec) -> sqlite3.Connection:
    path = Path(str(spec.options.get("path", ""))).expanduser()
    if not path.is_file():
        raise SourceError(f"file not found: {path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)


def _sqlite_tables(spec: SourceSpec, _secret: str = "") -> List[str]:
    with _sqlite_connect(spec) as con:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                           "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _stage_sqlite(workspace: Workspace, spec: SourceSpec, _secret: str, name: str) -> None:
    select = _selection(spec, quote_ident)
    con = _sqlite_connect(spec)
    try:
        cursor = con.execute(f"{select} LIMIT {int(spec.row_limit)}")
        header = [d[0] for d in cursor.description]
        # SQLite stores dates as text and types loosely; DuckDB detects types from all values (codes with leading
        # zeros stay text)
        _write_and_stage(workspace, name, header, cursor, spec.row_limit, {})
    finally:
        con.close()


def _write_and_stage(workspace: Workspace, name: str, header: List[str], rows, limit: int,
                     types: Dict[str, str]) -> None:
    handle, staging = tempfile.mkstemp(suffix=".csv", prefix="fds_")
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
        stage_csv(workspace, name, Path(staging), limit, ",", types)
    finally:
        try:
            os.unlink(staging)
        except OSError:
            pass


# ---- DuckDB files ------------------------------------------------------------------------------------------------

def _duckdb_path(spec: SourceSpec) -> Path:
    path = Path(str(spec.options.get("path", ""))).expanduser()
    if not path.is_file():
        raise SourceError(f"file not found: {path}")
    return path


def _duckdb_tables(spec: SourceSpec, _secret: str = "") -> List[str]:
    import duckdb
    con = duckdb.connect(str(_duckdb_path(spec)), read_only=True)
    try:
        rows = con.execute("SELECT table_schema, table_name FROM information_schema.tables "
                           "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') ORDER BY 1, 2").fetchall()
    finally:
        con.close()
    return [name if schema == "main" else f"{schema}.{name}" for schema, name in rows]


_attachments = itertools.count(1)


def _stage_duckdb(workspace: Workspace, spec: SourceSpec, _secret: str, name: str) -> None:
    path = _duckdb_path(spec)
    select = _selection(spec, quote_ident)
    cursor = workspace.cursor()
    alias = f"fds_source_{next(_attachments)}"
    literal = "'" + str(path).replace("'", "''") + "'"  # ATTACH takes no parameters
    try:
        cursor.execute(f"ATTACH {literal} AS {alias} (READ_ONLY)")
        try:
            cursor.execute(f"USE {alias}")
            cursor.execute(f"CREATE OR REPLACE TABLE memory.main.{quote_ident(name)} AS {select} "
                           f"LIMIT {int(spec.row_limit)}")
        finally:
            cursor.execute("USE memory")
            cursor.execute(f"DETACH {alias}")
    except Exception as exc:
        raise SourceError(str(exc).splitlines()[0]) from None
    finally:
        cursor.close()


# ---- PostgreSQL --------------------------------------------------------------------------------------------------

def _pg_connect(spec: SourceSpec, secret: str):
    try:
        import psycopg
    except ImportError:
        raise SourceError("PostgreSQL needs the Python package 'psycopg' (pip install \"psycopg[binary]\")") from None
    options = spec.options
    try:
        con = psycopg.connect(host=str(options.get("host") or "localhost"), port=int(options.get("port") or 5432),
                              dbname=str(options.get("database") or "postgres"), user=str(options.get("user") or ""),
                              password=secret or None, sslmode=str(options.get("sslmode") or "prefer"),
                              connect_timeout=10, application_name="Fluent Data Studio")
    except psycopg.Error as exc:
        raise SourceError(f"cannot connect: {str(exc).strip().splitlines()[0]}") from None
    con.read_only = True
    with con.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
    con.commit()
    return con


def _pg_quote(name: str) -> str:
    return quote_ident(name)


def _pg_tables(spec: SourceSpec, secret: str = "") -> List[str]:
    con = _pg_connect(spec, secret)
    try:
        with con.cursor() as cursor:
            cursor.execute("SELECT table_schema, table_name FROM information_schema.tables "
                           "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2")
            rows = cursor.fetchall()
    finally:
        con.close()
    return [f"{schema}.{name}" for schema, name in rows]


_PG_TYPES: Dict[int, str] = {16: "BOOLEAN", 20: "BIGINT", 21: "BIGINT", 23: "BIGINT", 700: "DOUBLE", 701: "DOUBLE",
                             1700: "DOUBLE", 1082: "DATE", 1114: "TIMESTAMP", 1184: "TIMESTAMP", 25: "VARCHAR",
                             1043: "VARCHAR", 1042: "VARCHAR", 2950: "VARCHAR"}


def _stage_postgres(workspace: Workspace, spec: SourceSpec, secret: str, name: str) -> None:
    import psycopg
    select = _selection(spec, _pg_quote)
    con = _pg_connect(spec, secret)
    handle, staging = tempfile.mkstemp(suffix=".csv", prefix="fds_")
    try:
        with con.cursor() as cursor:
            cursor.execute(f"SELECT * FROM ({select}) AS fds_probe LIMIT 0")
            columns: List[Tuple[str, int]] = [(d.name, d.type_code) for d in cursor.description]
            types = {n: _PG_TYPES.get(code, "VARCHAR") for n, code in columns}
            with os.fdopen(handle, "wb") as out:
                with cursor.copy(f"COPY ({select} LIMIT {int(spec.row_limit)}) TO STDOUT WITH (FORMAT csv, HEADER true)") as copy:
                    for chunk in copy:
                        out.write(chunk)
        con.rollback()
        stage_csv(workspace, name, Path(staging), spec.row_limit, ",", types)
    except psycopg.Error as exc:
        raise SourceError(str(exc).strip().splitlines()[0]) from None
    finally:
        con.close()
        try:
            os.unlink(staging)
        except OSError:
            pass


_TABLE = ConnectorField("table", "Table or view", "choice", "", False)
_QUERY = ConnectorField("query", "Or a read-only query", "query", "", False, placeholder="SELECT ... FROM ...")

register(Connector("sqlite", "SQLite database", "Database",
                   [ConnectorField("path", "Database file", "path"), _TABLE, _QUERY],
                   _stage_sqlite, tables=_sqlite_tables, extensions=[".sqlite", ".sqlite3", ".db"],
                   description="Opened read-only."))
register(Connector("duckdb", "DuckDB database", "Database",
                   [ConnectorField("path", "Database file", "path"), _TABLE, _QUERY],
                   _stage_duckdb, tables=_duckdb_tables, extensions=[".duckdb", ".ddb"],
                   description="Attached read-only."))
register(Connector("postgres", "PostgreSQL", "Database",
                   [ConnectorField("host", "Host", default="localhost"),
                    ConnectorField("port", "Port", "number", 5432),
                    ConnectorField("database", "Database", default="postgres"),
                    ConnectorField("user", "User"),
                    ConnectorField("password", "Password", "password", "", False),
                    ConnectorField("sslmode", "SSL", "choice", "prefer", False,
                                   ["prefer", "require", "disable", "verify-full"]),
                    _TABLE, _QUERY],
                   _stage_postgres, tables=_pg_tables, requires="psycopg",
                   description="Read-only transactions with a 60 s statement timeout; the password is never saved."))
