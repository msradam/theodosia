"""A sixth comparison: LangGraph's SQL agent, gated, and the MCP thesis.

Ported from LangGraph's SQL-agent tutorial:
    https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/sql/sql-agent.md

That agent lists tables, reads schema, generates SQL, and runs it. The only guard
against a destructive statement is a sentence in the system prompt: "DO NOT make
any DML statements (INSERT, UPDATE, DELETE, DROP...)." That is prose, not
enforcement. This is the Theodosia thesis in one line: move the read-only
guarantee from the prompt into a server-side transition guard that refuses DML
before it reaches the database, and records every query attempted.

Fidelity: `langgraph_sql` keeps the source node names (list_tables, get_schema,
generate_query, run_query) but drops the tutorial's `call_get_schema`,
`check_query`, and the `should_continue` retry loop; `generate_query` is stubbed.
Burr <-> LangGraph: list_tables <-> list_tables, describe <-> get_schema,
run <-> run_query; the tutorial's `check_query` node becomes the server-side DML
gate on `run`.

Renderings:
  langgraph_sql / burr_sql   run the model's SQL guarded only by the prompt: a
                             `DROP TABLE` executes and the table is gone.
  theodosia_sql              the machine is mounted; `run` refuses non-SELECT
                             with `validation_failed` before the DB is touched,
                             and the refused DROP is on the ledger.

And the point of MCP (`any_framework_drives`): the mounted machine is a plain MCP
server, so it is not Claude-specific. A LangGraph agent drives the SAME server
through `langchain-mcp-adapters` and gets the read-only guarantee and the ledger
for free, with no Theodosia or Burr code. Instead of a flat list of tools, MCP
here exposes a *machine*; any agent loop that speaks MCP can drive it.

Run:  ../../.venv/bin/python 08_sql_agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, TypedDict

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client
from langgraph.graph import END, START, StateGraph

sys.path.insert(0, os.path.dirname(__file__))
from theodosia import ValidationFailed, mount

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

_DB: sqlite3.Connection | None = None


def reset_db() -> None:
    global _DB
    _DB = sqlite3.connect(":memory:")
    _DB.executescript(
        "CREATE TABLE artists(id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE albums(id INTEGER PRIMARY KEY, title TEXT, artist_id INTEGER);"
        "INSERT INTO artists VALUES (1,'Miles Davis'),(2,'Nina Simone');"
        "INSERT INTO albums VALUES (1,'Kind of Blue',1),(2,'Wild Is the Wind',2);"
    )


def db() -> sqlite3.Connection:
    if _DB is None:
        reset_db()
    assert _DB is not None
    return _DB


def execute_sql(query: str) -> list:
    cur = db().execute(query)
    rows = [list(r) for r in cur.fetchall()] if cur.description else []
    db().commit()
    return rows


def tables() -> list[str]:
    return [r[0] for r in db().execute("SELECT name FROM sqlite_master WHERE type='table'")]


_DML = ("DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "REPLACE", "TRUNCATE")


def is_readonly(query: str) -> bool:
    u = query.strip().upper()
    return u.startswith("SELECT") and not any(re.search(rf"\b{k}\b", u) for k in _DML)


# ── the Burr SQL machine (shared by burr_sql and theodosia) ───────────
@action(reads=[], writes=["stage", "tables"])
def list_tables(state: State) -> State:
    return state.update(stage="listed", tables=tables())


@action(reads=["tables"], writes=["stage", "schema"])
def describe(state: State, table: str) -> State:
    cur = db().execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,))
    return state.update(stage="described", schema=str([list(r) for r in cur.fetchall()]))


@action(reads=["stage"], writes=["stage", "rows", "last_query"])
def run(state: State, query: str) -> State:
    return state.update(stage="ran", rows=execute_sql(query), last_query=query)


def _run_gate(state: dict, inputs: dict) -> dict | None:
    query = str(inputs.get("query") or "")
    if not is_readonly(query):
        raise ValidationFailed(
            "refused: only read-only SELECT statements are permitted",
            details={"query": query, "reason": "DML/DDL is not allowed on this connection"},
        )
    return None


run._theodosia_validator = _run_gate  # type: ignore[attr-defined]


def build_sql_app() -> ApplicationBuilder:
    return (
        ApplicationBuilder()
        .with_actions(list_tables=list_tables, describe=describe, run=run)
        .with_transitions(
            ("list_tables", "describe", Condition.expr("stage == 'listed'")),
            ("describe", "run", Condition.expr("stage == 'described'")),
            ("run", "run", Condition.expr("stage == 'ran'")),
        )
        .with_state(stage="new", tables=[], schema="", rows=[], last_query="")
        .with_entrypoint("list_tables")
    )


# ── langgraph_sql: the tutorial's shape; run_query executes ungated ───
def langgraph_sql(query: str) -> dict[str, Any]:
    reset_db()

    class S(TypedDict, total=False):
        tables: list
        schema: str
        query: str
        rows: list

    def n_list(s: S) -> dict:
        return {"tables": tables()}

    def n_schema(s: S) -> dict:
        return {"schema": str(execute_sql("SELECT sql FROM sqlite_master"))}

    def n_generate(s: S) -> dict:
        return {"query": query}  # stub for the LLM's generate_query

    def n_run(s: S) -> dict:
        return {
            "rows": execute_sql(s["query"])
        }  # run_query: runs model SQL, guarded only by a prompt

    g = StateGraph(S)
    g.add_node("list_tables", n_list)
    g.add_node("get_schema", n_schema)
    g.add_node("generate_query", n_generate)
    g.add_node("run_query", n_run)
    g.add_edge(START, "list_tables")
    g.add_edge("list_tables", "get_schema")
    g.add_edge("get_schema", "generate_query")
    g.add_edge("generate_query", "run_query")
    g.add_edge("run_query", END)
    out = g.compile().invoke({})
    return {"rows": out.get("rows", []), "tables_after": tables()}


# ── burr_sql: orchestrator drives; run executes ungated ───────────────
def burr_sql(query: str) -> dict[str, Any]:
    reset_db()
    app = build_sql_app().build()
    app.step(inputs={})  # list_tables
    app.step(inputs={"table": "albums"})  # describe
    app.step(inputs={"query": query})  # run (raw Burr ignores the validator)
    return {"rows": app.state["rows"], "tables_after": tables()}


# ── theodosia_sql: mounted; the server refuses DML and records it ─────
async def _step(c: Client, act: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": act}
    if inputs is not None:
        args["inputs"] = inputs
    r = await c.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _res(c: Client, uri: str) -> Any:
    return json.loads((await c.read_resource(uri))[0].text)


async def _theodosia_sql(select: str, drop: str) -> dict[str, Any]:
    reset_db()
    server = mount(build_sql_app, name="sql")
    async with Client(server) as c:
        await _step(c, "list_tables")
        await _step(c, "describe", {"table": "albums"})
        ok = await _step(c, "run", {"query": select})
        bad = await _step(c, "run", {"query": drop})  # a destructive statement
        history = await _res(c, "theodosia://history")
    return {
        "select_rows": ok["state"]["rows"],
        "drop_refused": bad.get("error"),
        "drop_reason": bad.get("reason"),
        "tables_after": tables(),
        "refused_in_ledger": len([h for h in history if h.get("refused")]),
    }


def theodosia_sql(select: str, drop: str) -> dict[str, Any]:
    return asyncio.run(_theodosia_sql(select, drop))


# ── the MCP thesis: a LangGraph agent drives the SAME mounted machine ─
def _sql_connection() -> dict[str, Any]:
    env = dict(os.environ)
    env["THEODOSIA_QUIET"] = "1"
    return {
        "command": str(REPO / ".venv" / "bin" / "theodosia"),
        "args": ["serve", "08_sql_agent:build_sql_app", "--app-dir", str(HERE)],
        "transport": "stdio",
        "cwd": str(REPO),
        "env": env,
    }


async def _any_framework_drives(select: str, drop: str) -> dict[str, Any] | None:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError:
        return None

    def payload(msg: Any) -> dict:
        art = getattr(msg, "artifact", None)
        return art.get("structured_content", {}) if isinstance(art, dict) else {}

    client = MultiServerMCPClient({"sql": _sql_connection()})
    async with client.session("sql") as session:  # one persistent session = one FSM run
        tools = await load_mcp_tools(session, server_name="sql")
        names = sorted(t.name for t in tools)
        step = next(t for t in tools if t.name == "step")

        async def drive(action: str, inputs: dict | None = None) -> dict:
            args: dict[str, Any] = {"action": action}
            if inputs is not None:
                args["inputs"] = inputs
            call = {"name": "step", "args": args, "id": f"{action}-1", "type": "tool_call"}
            return payload(await step.ainvoke(call))

        await drive("list_tables")
        await drive("describe", {"table": "albums"})
        ok = await drive("run", {"query": select})
        bad = await drive("run", {"query": drop})
    return {
        "tool_names": names,
        "select_rows": ok.get("state", {}).get("rows"),
        "drop_refused": bad.get("error"),
    }


def any_framework_drives(select: str, drop: str) -> dict[str, Any] | None:
    return asyncio.run(_any_framework_drives(select, drop))


def main() -> None:
    select = "SELECT name FROM artists ORDER BY id"
    drop = "DROP TABLE albums"
    print(f"read-only query: {select!r}\ndestructive query the model might emit: {drop!r}\n")

    lg = langgraph_sql(select)
    burr = burr_sql(select)
    th = theodosia_sql(select, drop)
    assert lg["rows"] == burr["rows"] == th["select_rows"]
    print(f"  all three run the SELECT and agree: {th['select_rows']}\n")

    print("  now the model emits DROP TABLE albums:")
    langgraph_sql(drop)
    lg_tables = tables()
    burr_sql(drop)
    burr_tables = tables()
    print(f"    langgraph_sql ran it  -> tables now {lg_tables}")
    print(f"    burr_sql ran it       -> tables now {burr_tables}")
    print(f"    theodosia refused it  -> {th['drop_refused']} ({th['drop_reason']})")
    print(
        f"      tables intact: {th['tables_after']}   refusals on ledger: {th['refused_in_ledger']}"
    )
    assert "albums" not in lg_tables and "albums" not in burr_tables
    assert th["drop_refused"] == "validation_failed" and "albums" in th["tables_after"]

    print("\n  the MCP thesis: a LangGraph agent drives the SAME mounted machine")
    other = any_framework_drives(select, drop)
    if other is not None:
        print(f"    langchain-mcp-adapters sees the machine as tool(s): {other['tool_names']}")
        print(f"    its SELECT returned: {other['select_rows']}")
        print(f"    its DROP was refused server-side too: {other['drop_refused']}")
        print("    -> a LangGraph agent got the same guarantee + ledger for free, no Burr code.")


if __name__ == "__main__":
    main()
