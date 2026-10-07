"""Web connector: CSV, JSON or Parquet from an HTTP(S) URL, including JSON REST endpoints with nested records."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..schema import quote_ident
from ..workspace import Workspace
from . import Connector, ConnectorField, SourceError, SourceSpec, register
from .files import stage_csv

_MAX_BYTES = 200 * 1024 * 1024
_TIMEOUT = 30


def _download(url: str, token: str) -> tuple:
    if not url.lower().startswith(("http://", "https://")):
        raise SourceError("only http:// and https:// addresses are supported")
    request = urllib.request.Request(url, headers={"User-Agent": "FluentDataStudio/0.1", "Accept": "*/*"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            kind = response.headers.get("Content-Type", "")
            data = response.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"the server answered {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise SourceError(f"cannot download: {getattr(exc, 'reason', exc)}") from None
    if len(data) > _MAX_BYTES:
        raise SourceError("the response is larger than 200 MB")
    return data, kind.lower()


def _records_at(document: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(document, dict) and part in document:
            document = document[part]
        elif isinstance(document, list) and part.isdigit() and int(part) < len(document):
            document = document[int(part)]
        else:
            raise SourceError(f"the response has no field {part!r} on the records path")
    return document


def _stage_url(workspace: Workspace, spec: SourceSpec, secret: str, name: str) -> None:
    url = str(spec.options.get("url") or "").strip()
    data, content_type = _download(url, secret)
    fmt = str(spec.options.get("format") or "auto")
    path_part = url.split("?")[0].lower()
    if fmt == "auto":
        if "json" in content_type or path_part.endswith((".json", ".jsonl", ".ndjson")):
            fmt = "json"
        elif "parquet" in content_type or path_part.endswith(".parquet"):
            fmt = "parquet"
        else:
            fmt = "csv"
    handle, staging = tempfile.mkstemp(suffix="." + fmt, prefix="fds_")
    try:
        with os.fdopen(handle, "wb") as out:
            if fmt == "json":
                try:
                    document = json.loads(data.decode("utf-8"))
                except ValueError:
                    document = None  # JSON Lines
                if document is not None:
                    records = _records_at(document, str(spec.options.get("records_path") or ""))
                    if isinstance(records, dict):
                        records = [records]
                    if not isinstance(records, list):
                        raise SourceError("the records path does not point at a list of records")
                    data = "\n".join(json.dumps(r) for r in records).encode("utf-8")
            out.write(data)
        if fmt == "csv":
            stage_csv(workspace, name, Path(staging), spec.row_limit)
        else:
            reader = "read_json_auto" if fmt == "json" else "read_parquet"
            workspace.execute(f"CREATE OR REPLACE TABLE {quote_ident(name)} AS SELECT * FROM {reader}(?) "
                              f"LIMIT {int(spec.row_limit)}", [staging])
    finally:
        try:
            os.unlink(staging)
        except OSError:
            pass


register(Connector("url", "Web address (CSV, JSON, REST API)", "Web",
                   [ConnectorField("url", "URL", placeholder="https://..."),
                    ConnectorField("format", "Format", "choice", "auto", False, ["auto", "csv", "json", "parquet"]),
                    ConnectorField("records_path", "Records path (JSON)", "text", "", False, placeholder="data.items"),
                    ConnectorField("password", "Bearer token", "password", "", False)],
                   _stage_url,
                   description="Downloads once (up to 200 MB); the token is sent only to this address and never saved."))
