"""Data sources: every connector stages a bounded snapshot of its data into the local workspace.

A source is described by a :class:`SourceSpec` (a kind plus plain options, saved in project files) and, for databases,
a secret that is kept separately and never saved or sent to a language model. A connector does three things:

* ``tables(spec, secret)`` lists what can be imported (database tables and views, workbook sheets);
* ``stage(workspace, spec, secret, table_name)`` copies the data into a local DuckDB table, read-only on the source
  side and capped at ``spec.row_limit`` rows;
* it describes itself through :class:`Connector` so the user interface can build its form.

Adding a source means adding one :class:`Connector`; nothing else in the engine changes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..profile import profile_table
from ..schema import load_semantic_sidecar
from ..workspace import Dataset, Workspace, WorkspaceError, safe_name

__all__ = ["SourceSpec", "Connector", "ConnectorField", "SourceError", "connectors", "connector", "import_source",
           "list_tables", "DEFAULT_ROW_LIMIT", "kind_for_path"]

DEFAULT_ROW_LIMIT = 1_000_000


class SourceError(RuntimeError):
    """A source that cannot be read, with a message meant for the user."""


@dataclass
class SourceSpec:
    """Where a dataset comes from: ``kind`` (``csv``, ``postgres``…), connector options and the row cap.

    Options never hold passwords or tokens: those are passed separately as ``secret`` and kept in memory only.
    """

    kind: str
    options: Dict[str, Any] = field(default_factory=dict)
    row_limit: int = DEFAULT_ROW_LIMIT

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "options": dict(self.options), "row_limit": self.row_limit}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceSpec":
        return cls(str(data["kind"]), dict(data.get("options", {})), int(data.get("row_limit", DEFAULT_ROW_LIMIT)))

    def label(self) -> str:
        options = self.options
        if "path" in options:
            text = Path(str(options["path"])).name
            return f"{text} [{options['sheet']}]" if options.get("sheet") else text
        if "url" in options:
            return str(options["url"])
        if options.get("query"):
            return f"{self.kind}: custom query"
        host = options.get("host") or ""
        database = options.get("database") or ""
        table = options.get("table") or ""
        return f"{self.kind}://{host}/{database}" + (f" {table}" if table else "")


@dataclass
class ConnectorField:
    """One input on a connector's form."""

    key: str
    label: str
    kind: str = "text"            # text, number, password, path, choice, query
    default: Any = ""
    required: bool = True
    choices: List[str] = field(default_factory=list)
    placeholder: str = ""


@dataclass
class Connector:
    """A kind of data source: its form, whether it lists tables, and how it stages data."""

    kind: str
    title: str
    group: str                    # "File", "Database", "Web"
    fields: List[ConnectorField]
    stage: Callable[[Workspace, SourceSpec, str, str], None]
    tables: Optional[Callable[[SourceSpec, str], List[str]]] = None
    extensions: List[str] = field(default_factory=list)
    requires: str = ""            # an optional Python package
    description: str = ""

    def available(self) -> bool:
        if not self.requires:
            return True
        try:
            __import__(self.requires)
        except ImportError:
            return False
        return True


_REGISTRY: Dict[str, Connector] = {}


def register(connector: Connector) -> None:
    _REGISTRY[connector.kind] = connector


def connectors() -> List[Connector]:
    _load()
    return list(_REGISTRY.values())


def connector(kind: str) -> Connector:
    _load()
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise SourceError(f"unknown source kind {kind!r}") from None


def kind_for_path(path: Path) -> Optional[str]:
    suffix = path.suffix.lower()
    for item in connectors():
        if suffix in item.extensions:
            return item.kind
    return None


def list_tables(spec: SourceSpec, secret: str = "") -> List[str]:
    item = connector(spec.kind)
    if item.tables is None:
        return []
    return item.tables(spec, secret)


def import_source(workspace: Workspace, spec: SourceSpec, name: str = "", secret: str = "",
                  semantics: Optional[Dict[str, Any]] = None) -> Dataset:
    """Stage ``spec`` into ``workspace`` as a dataset called ``name`` (derived from the source when empty) and profile it."""
    item = connector(spec.kind)
    if not item.available():
        raise SourceError(f"{item.title} needs the Python package '{item.requires}'; install it to use this source")
    name = name or safe_name(_default_name(spec), list(workspace.datasets))
    try:
        item.stage(workspace, spec, secret, name)
    except (SourceError, WorkspaceError):
        workspace.drop_table(name)
        raise
    except Exception as exc:  # driver errors: report them as source errors, never crash the application
        workspace.drop_table(name)
        raise SourceError(f"{item.title}: {exc}") from exc
    if semantics is None and "path" in spec.options:
        semantics = load_semantic_sidecar(Path(str(spec.options["path"])))
    schema = profile_table(workspace, name, name, semantics)
    note = ""
    if schema.rows >= spec.row_limit:
        note = f"Imported the first {spec.row_limit:,} rows (the source may have more)."
    dataset = Dataset(name, spec.to_dict(), schema, dict(semantics or {}), note)
    workspace.add_dataset(dataset)
    return dataset


def _default_name(spec: SourceSpec) -> str:
    options = spec.options
    if options.get("table"):
        return str(options["table"]).split(".")[-1]
    if options.get("sheet"):
        return str(options["sheet"])
    if "path" in options:
        return Path(str(options["path"])).stem
    if "url" in options:
        tail = str(options["url"]).split("?")[0].rstrip("/").split("/")[-1]
        return tail.rsplit(".", 1)[0] or "web_data"
    return "query"


_loaded = False
_load_lock = threading.Lock()


def _load() -> None:
    """Import the connector modules once (imports may start on several worker threads at the same time)."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if not _loaded:
            from . import databases, files, web  # noqa: F401  (each module registers its connectors)
            _loaded = True
