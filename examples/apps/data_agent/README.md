# data_agent

An auditable text-to-SQL data-analysis agent built on Theodosia. A Burr finite
state machine, mounted as an MCP server, drives a real SQLite database through
an upstream sqlite MCP server and reads the source CSV through an upstream
filesystem MCP server. The driving agent connects to only this server and sees
only the `step` tool; it never receives SQL-execution tools.

## What it does

The FSM is a six-action analysis workflow:

| action | does | upstream tool |
| --- | --- | --- |
| `connect` | entry; list tables | sqlite `list_tables` |
| `load` | read the CSV, create the table, bulk-insert rows (idempotent) | filesystem `read_file`, sqlite `create_table` + `write_query` |
| `profile` | row count, schema, regions, products, date span | sqlite `describe_table` + `read_query` |
| `query(sql)` | run one read-only SELECT, return rows in `state.last_rows` | sqlite `read_query` |
| `finding(insight, query)` | record an insight that must cite a query you ran | none |
| `report(summary)` | terminal; requires >= 2 findings | none |

The agent loops `query` -> `finding` until it has at least two findings, then
writes the report.

## Guardrails

- `query` gates agent-supplied SQL to a single read-only `SELECT` before it
  reaches the database: `DROP`, `DELETE`, `UPDATE`, `INSERT`, DDL, and
  `;`-chained statements are rejected as a step error and never sent upstream.
  This is defense in depth on top of the sqlite server's own check, and the
  refusal is the FSM's own, recorded in the ledger.
- A SELECT the database rejects (bad column, syntax) surfaces as a step error
  rather than a silently empty result.
- `finding` requires its `query` argument to match a SELECT the agent actually
  ran, so insights cannot be fabricated against queries that never executed.
- `report` requires >= 2 findings and a substantive summary.
- Write and DDL upstream tools are exercised only by the FSM-authored `load`
  action; the agent never authors write SQL.

## Run

Serve it (mounts both upstreams):

```
theodosia serve app:build_server --app-dir examples/apps/data_agent
```

Then connect any MCP client to the `data` server and drive it with `step`. The
agent should read `theodosia://graph` first, then walk `connect` -> `load` ->
`profile` -> `query` -> `finding` -> `report`.

## Configuration

| env var | default | purpose |
| --- | --- | --- |
| `DATA_AGENT_DB` | `data/sales.db` | SQLite file the upstream opens |
| `DATA_AGENT_HOME` | `/tmp/data-agent-tracker` | Burr tracker storage root |

The upstream sqlite server is `uvx mcp-server-sqlite`; the filesystem server is
`npx -y @modelcontextprotocol/server-filesystem`. Both are launched by
`build_server()`.

## Files

- `app.py` — the FSM, the SQL gate, and `build_server()`.
- `data/sales.csv` — 473 rows of sample sales data (date, region, product,
  quantity, amount, order_id) with real regional and product signals.
