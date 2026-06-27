"""An auditable text-to-SQL data-analysis agent: a Burr FSM mounted via
Theodosia that drives a REAL SQLite database through an upstream sqlite MCP
server, and reads the source CSV through an upstream filesystem MCP server.

The agent connects to ONLY this Theodosia server and sees ONLY the ``step``
tool. It never receives SQL-execution tools. Every query runs inside an
action body via ``call_upstream(...)``; the action gates agent-supplied SQL
to read-only ``SELECT`` before it reaches the database (defense in depth, on
top of the upstream server's own check), records it, and returns the rows in
the step result so the agent can reason over real data. Write/DDL tools on
the upstream are exercised only by the FSM-controlled ``load`` action with
SQL the agent never authors.

Workflow:

    connect  -> list tables (sqlite upstream)
    load     -> read CSV (filesystem upstream), create table + bulk INSERT
                (sqlite upstream); idempotent
    profile  -> row count + schema + per-dimension cardinality (read_query)
    query    -> run an agent-supplied SELECT (GATED read-only); rows returned
    finding  -> record an insight that must cite a query the agent ran
    report   -> terminal; requires >= 2 findings

Run standalone (mounts both upstreams):

    theodosia serve app:build_server --app-dir examples/apps/data_agent

The builder seam: :func:`build` returns an UNBUILT ``ApplicationBuilder`` so
Theodosia stamps ``app_id = session_id`` per session and the tracker dir
tracks the session key.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from burr.core import ApplicationBuilder, Condition, State, action

import theodosia
from theodosia import ServingMode, call_upstream, classify_payload, mount

_HERE = Path(__file__).parent
_DATA_DIR = _HERE / "data"
_CSV_PATH = _DATA_DIR / "sales.csv"
_TABLE = "sales"
_MIN_FINDINGS = 2
_TRACKER_PROJECT = "data-agent"

_COLUMNS = [
    ("order_date", "TEXT"),
    ("region", "TEXT"),
    ("product", "TEXT"),
    ("quantity", "INTEGER"),
    ("amount", "REAL"),
    ("order_id", "INTEGER"),
]


def _db_path() -> str:
    return os.environ.get("DATA_AGENT_DB", str(_DATA_DIR / "sales.db"))


def _tracker_storage() -> str:
    return os.environ.get("DATA_AGENT_HOME", "/tmp/data-agent-tracker")


_SELECT_ONLY = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|"
    r"DETACH|PRAGMA|VACUUM|REINDEX|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _gate_select(sql: str) -> str:
    """Return a vetted single read-only SELECT or raise ``ValueError``.

    Defense in depth: the upstream sqlite server also refuses non-SELECT in
    ``read_query``, but the FSM refuses first so a rejected query never leaves
    the process and the refusal is the action's own, auditable in the ledger.
    """
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("query must not be empty")
    if ";" in cleaned:
        raise ValueError("only a single statement is allowed (no ';' chaining)")
    if not _SELECT_ONLY.match(cleaned):
        raise ValueError("only read-only SELECT queries are allowed")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("query contains a forbidden write/DDL keyword; SELECT only")
    return cleaned


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip()).lower()


def _rows_from_upstream(payload: Any) -> list[dict[str, Any]]:
    """Coerce an upstream ``read_query`` payload into a list of row dicts.

    The mcp-server-sqlite server serializes rows as a Python ``repr`` string,
    not JSON. Theodosia's ``classify_payload(..., expect="rows")`` handles both
    shapes (JSON or repr) safely, so the action body does not parse text.
    """
    result = classify_payload("rows", payload, expect="rows")
    return result.data if result.usable else []


@action(reads=[], writes=["phase", "tables", "log"])
async def connect(state: State) -> State:
    """Entry. Open the database and list its tables via the sqlite upstream."""
    tables = await call_upstream("sqlite", "list_tables", {})
    names = _rows_from_upstream(tables)
    return state.update(
        phase="connected",
        tables=names,
        log=[f"connected; tables={names}"],
    )


@action(reads=["log"], writes=["phase", "loaded_rows", "log"])
async def load(state: State) -> State:
    """Ensure the ``sales`` table exists and is populated.

    Reads the source CSV through the filesystem upstream, creates the table
    via the sqlite upstream, and bulk-inserts every row with one
    ``write_query``. Idempotent: if the table already holds rows, it is left
    untouched. This is the only action that uses write/DDL upstream tools, and
    the SQL is FSM-authored, never agent-supplied.
    """
    existing = _rows_from_upstream(await call_upstream("sqlite", "list_tables", {}))
    has_table = any((r.get("name") if isinstance(r, dict) else r) == _TABLE for r in existing)
    if has_table:
        count = _rows_from_upstream(
            await call_upstream(
                "sqlite", "read_query", {"query": f"SELECT COUNT(*) AS n FROM {_TABLE}"}
            )
        )
        n = count[0]["n"] if count else 0
        if n:
            return state.update(
                phase="loaded",
                loaded_rows=n,
                log=[*state["log"], f"load skipped; {_TABLE} already has {n} rows"],
            )

    raw = await call_upstream("filesystem", "read_file", {"path": str(_CSV_PATH)})
    if isinstance(raw, dict) and "content" in raw:
        raw = raw["content"]
    text = raw if isinstance(raw, str) else str(raw)
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("source CSV produced no rows")

    cols_ddl = ", ".join(f"{name} {sqltype}" for name, sqltype in _COLUMNS)
    await call_upstream(
        "sqlite",
        "create_table",
        {"query": f"CREATE TABLE IF NOT EXISTS {_TABLE} ({cols_ddl})"},
    )

    col_names = [c[0] for c in _COLUMNS]
    values = []
    for r in rows:
        vals = []
        for name, sqltype in _COLUMNS:
            v = r[name]
            if sqltype in ("INTEGER", "REAL"):
                vals.append(str(v))
            else:
                vals.append("'" + v.replace("'", "''") + "'")
        values.append("(" + ", ".join(vals) + ")")
    insert = f"INSERT INTO {_TABLE} ({', '.join(col_names)}) VALUES " + ", ".join(values)
    await call_upstream("sqlite", "write_query", {"query": insert})

    return state.update(
        phase="loaded",
        loaded_rows=len(rows),
        log=[*state["log"], f"loaded {len(rows)} rows into {_TABLE}"],
    )


@action(reads=["log"], writes=["phase", "profile", "log"])
async def profile(state: State) -> State:
    """Profile the table: row count, schema, and per-dimension cardinality."""
    schema = _rows_from_upstream(
        await call_upstream("sqlite", "describe_table", {"table_name": _TABLE})
    )
    count = _rows_from_upstream(
        await call_upstream(
            "sqlite", "read_query", {"query": f"SELECT COUNT(*) AS n FROM {_TABLE}"}
        )
    )
    regions = _rows_from_upstream(
        await call_upstream(
            "sqlite",
            "read_query",
            {"query": f"SELECT DISTINCT region FROM {_TABLE} ORDER BY region"},
        )
    )
    products = _rows_from_upstream(
        await call_upstream(
            "sqlite",
            "read_query",
            {"query": f"SELECT DISTINCT product FROM {_TABLE} ORDER BY product"},
        )
    )
    span_sql = f"SELECT MIN(order_date) AS first_day, MAX(order_date) AS last_day FROM {_TABLE}"
    span = _rows_from_upstream(await call_upstream("sqlite", "read_query", {"query": span_sql}))
    prof = {
        "row_count": count[0]["n"] if count else 0,
        "schema": schema,
        "regions": [r["region"] for r in regions],
        "products": [p["product"] for p in products],
        "date_span": span[0] if span else {},
    }
    return state.update(
        phase="profiled",
        profile=prof,
        log=[*state["log"], f"profiled {_TABLE}: {prof['row_count']} rows"],
    )


@action(reads=["queries", "log"], writes=["queries", "last_rows", "log"])
async def query(state: State, sql: str) -> State:
    """Run a read-only SELECT against the database and return the rows.

    The SQL is gated to a single ``SELECT`` before it reaches the upstream.
    Rejected SQL raises (surfaced to you as the step error) and never touches
    the database. A SELECT the database itself rejects (bad column, syntax)
    also raises rather than returning silently empty. On success the rows are
    in ``last_rows`` of the step result.

    Args:
        sql: a single read-only SELECT statement.
    """
    vetted = _gate_select(sql)
    payload = await call_upstream("sqlite", "read_query", {"query": vetted})
    if isinstance(payload, str) and not payload.strip().startswith("["):
        raise ValueError(f"database rejected the query: {payload.strip()}")
    rows = _rows_from_upstream(payload)
    record = {"sql": vetted, "rowcount": len(rows), "rows": rows[:50]}
    return state.update(
        queries=[*state["queries"], record],
        last_rows=rows[:50],
        log=[*state["log"], f"query ok ({len(rows)} rows): {vetted[:80]}"],
    )


@action(reads=["queries", "findings", "log"], writes=["findings", "log"])
async def finding(state: State, insight: str, query: str) -> State:
    """Record an insight that must cite a query you actually ran.

    Args:
        insight: the conclusion drawn from real query results.
        query: the SELECT (as run) the insight is derived from; must match a
            query you ran via ``step(action="query")``.
    """
    if not insight.strip():
        raise ValueError("insight must not be empty")
    ran = {_normalize(q["sql"]) for q in state["queries"]}
    if not ran:
        raise ValueError("run at least one query before recording a finding")
    if _normalize(query) not in ran:
        raise ValueError(
            "finding must cite a query you ran; "
            f"{query!r} is not among the executed queries. Run it via query() first."
        )
    return state.update(
        findings=[*state["findings"], {"insight": insight.strip(), "query": _normalize(query)}],
        log=[*state["log"], f"finding recorded ({len(state['findings']) + 1})"],
    )


@action(reads=["findings", "queries", "log"], writes=["phase", "report", "log"])
async def report(state: State, summary: str) -> State:
    """Terminal. Compile the analysis report. Requires >= 2 findings."""
    if len(state["findings"]) < _MIN_FINDINGS:
        raise ValueError(
            f"report requires >= {_MIN_FINDINGS} findings; you have "
            f"{len(state['findings'])}. Run more queries and record findings."
        )
    if len(summary.strip()) < 80:
        raise ValueError("summary must be a substantive report (>= 80 chars)")
    return state.update(
        phase="done",
        report=summary.strip(),
        log=[*state["log"], f"report written ({len(state['findings'])} findings)"],
    )


_OPEN = Condition.expr("phase != 'done'")


def build() -> ApplicationBuilder:
    """Return an UNBUILT ``ApplicationBuilder`` (the Theodosia builder seam).

    Theodosia stamps ``app_id = session_id`` and builds it per session, so the
    tracker writes land under ``<storage>/data-agent/<session_id>/``.
    """
    return (
        ApplicationBuilder()
        .with_actions(
            connect=connect,
            load=load,
            profile=profile,
            query=query,
            finding=finding,
            report=report,
        )
        .with_transitions(
            ("connect", "load", _OPEN),
            ("load", "profile", _OPEN),
            ("profile", "query", _OPEN),
            ("query", "query", _OPEN),
            ("query", "finding", _OPEN),
            ("finding", "query", _OPEN),
            ("finding", "finding", _OPEN),
            ("finding", "report", _OPEN),
            ("query", "report", _OPEN),
        )
        .with_tracker(theodosia.tracker(_TRACKER_PROJECT, storage_dir=_tracker_storage()))
        .with_state(
            phase="new",
            tables=[],
            loaded_rows=0,
            profile=None,
            queries=[],
            last_rows=None,
            findings=[],
            report=None,
            log=[],
        )
        .with_entrypoint("connect")
    )


def _ensure_db() -> None:
    """Create the SQLite file if missing so the upstream can open it.

    The mcp-server-sqlite server creates the file on connect; this just
    guarantees the parent dir exists for a custom ``DATA_AGENT_DB`` path.
    """
    path = Path(_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        sqlite3.connect(path).close()


def build_server():
    _ensure_db()
    db = _db_path()
    return mount(
        build,
        mode=ServingMode.STEP,
        name="data-agent",
        upstream={
            "sqlite": {
                "command": "uvx",
                "args": ["mcp-server-sqlite", "--db-path", db],
            },
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", str(_DATA_DIR)],
            },
        },
        instructions=(
            "An auditable text-to-SQL data-analysis FSM over a real SQLite "
            "database (you are NOT given SQL tools directly; every query runs "
            "through this server). Walk: connect() lists tables; load() ensures "
            "the 'sales' table is populated from the shipped CSV; profile() "
            "returns row count, schema, regions, products, and date span; "
            "query(sql) runs ONE read-only SELECT and returns rows in "
            "state.last_rows (non-SELECT is rejected); finding(insight, query) "
            "records an insight that must cite a query you ran; report(summary) "
            "finishes (needs >= 2 findings). Read state.profile and "
            "state.last_rows after each step."
        ),
    )


if __name__ == "__main__":
    build_server().run()
