"""Read-only checks for SQL that a user (never the model) types against an external database.

The first line of defence is the connection itself (read-only SQLite URIs, ``SET TRANSACTION READ ONLY`` with a
statement timeout on PostgreSQL); this module is the second: it accepts a single ``SELECT`` or ``WITH`` statement and
rejects anything that writes, changes schema, changes session state or calls known side-effecting functions.
"""

from __future__ import annotations

import re

__all__ = ["UnsafeQueryError", "check_read_only_query"]


class UnsafeQueryError(ValueError):
    """A query that is not a single read-only statement."""


_FORBIDDEN = re.compile(
    # the leading-word check already requires SELECT/WITH; this catches data-modifying CTEs and SELECT ... INTO
    r"\b(insert|update|delete|merge|upsert|create|alter|drop|truncate|grant|revoke|copy|attach|detach|install|load|"
    r"pragma|vacuum|lock|call|exec|execute|set|begin|commit|rollback|into)\b",
    re.I)
_FORBIDDEN_FUNCTIONS = re.compile(
    r"\b(pg_sleep|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_terminate_backend|pg_cancel_backend|lo_import|lo_export|"
    r"dblink\w*|set_config|nextval|setval|read_csv\w*|read_json\w*|read_parquet|read_text|read_blob|glob|"
    r"sqlite_scan|postgres_scan|load_extension|writefile|readfile)\s*\(",
    re.I)


def _strip_literals_and_comments(sql: str) -> str:
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'" or c == '"':
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(" ' ' " if c == "'" else " ident ")
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise UnsafeQueryError("unterminated comment")
            i = j + 2
        elif sql.startswith("$$", i) or (c == "$" and re.match(r"\$\w*\$", sql[i:])):
            raise UnsafeQueryError("dollar-quoted strings are not allowed")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_read_only_query(sql: str) -> str:
    """Return the statement without a trailing semicolon, or raise :class:`UnsafeQueryError`."""
    text = sql.strip()
    if not text:
        raise UnsafeQueryError("the query is empty")
    bare = _strip_literals_and_comments(text).strip()
    while bare.endswith(";"):
        bare = bare[:-1].rstrip()
    if ";" in bare:
        raise UnsafeQueryError("only one statement is allowed")
    first = re.match(r"\s*\(*\s*(\w+)", bare)
    if first is None or first.group(1).lower() not in ("select", "with", "values", "table"):
        raise UnsafeQueryError("only SELECT queries are allowed")
    word = _FORBIDDEN.search(bare)
    if word:
        raise UnsafeQueryError(f"'{word.group(1).upper()}' is not allowed in a read-only query")
    call = _FORBIDDEN_FUNCTIONS.search(bare)
    if call:
        raise UnsafeQueryError(f"the function {call.group(1)}() is not allowed")
    if re.search(r"\bfor\s+(update|share|no\s+key|key)\b", bare, re.I):
        raise UnsafeQueryError("row locks are not allowed")
    result = text.rstrip()
    while result.endswith(";"):
        result = result[:-1].rstrip()
    return result
