---
title: 'Driving other MCP servers'
description: 'The upstream feature: actions calling other MCP servers.'
---

Theodosia is normally the MCP server the agent talks to. With `upstream`, it also
opens MCP *client* sessions to other servers (Kubernetes, Grafana, filesystem,
and so on). A Burr action calls those servers' tools from inside its Python body
via `call_upstream(server, tool, args)`.

## Wiring

```python
from theodosia import call_upstream, mount
from burr.core import action

@action(reads=[], writes=["pods"])
async def survey(state):
    pods = await call_upstream("k8s", "list_pods", {"namespace": "prod"})
    return state.update(pods=pods)

server = mount(
    build_application,
    upstream={"k8s": {"command": "npx", "args": ["-y", "kubernetes-mcp-server"]}},
)
```

Each value in the `upstream` map is anything `fastmcp.Client` accepts as a
transport: a URL string, an mcp-config dict, a transport object, or a bare
`{"command", "args", "env", "cwd"}` stdio spec. The bare stdio spec is mapped to
an explicit `StdioTransport` so the upstream tool names are not namespaced the
way an mcp-config dict would prefix them.

## Why this shape

- **Single surface.** The agent connects to one server (this one) and sees one
  tool (`step`). The upstream servers are never exposed to it. There is no
  separate "query the cluster" surface for a weak model to get absorbed in.
- **Every call is a ledger entry.** The upstream call happens inside an action,
  so it advances state by construction. The graph cannot fall out of sync with
  what actually happened.
- **Any server.** MCP is a standard protocol and `fastmcp.Client` speaks every
  transport (stdio, http, sse). Theodosia does not need to know what the upstream
  server is.
- **No arg-guessing.** The action author writes the call explicitly. There is no
  per-backend name or argument inference.

## Lifecycle

`UpstreamManager` lazily opens and caches one `fastmcp.Client` session per
server, keyed by name, opened on first use and kept open for the manager's
lifetime. Calls are serialized per manager with an `asyncio.Lock`, since a single
Client session is not guaranteed safe under concurrent calls and Burr steps are
serialized per session anyway. `mount()` binds a manager around each `step` via
`bind_upstream` and resets it afterward.

For tests or harness embeddings, `bind_upstream` accepts any object with an async
`call(server, tool, args)` method, so you can bind an already-open session
instead of the built-in manager.

## Per-session isolation

By default one shared client serves every MCP session. That is correct and
cheaper for stateless upstreams (filesystem reads, fetch). A stateful upstream (a
memory server, a per-tenant database) needs its own client per session so two
agents do not collide in one backend.

Put a `{session}` placeholder anywhere in an upstream's config and Theodosia
switches that server to per-session mode: it substitutes the FastMCP session id
into the placeholder and builds a separate client or subprocess per session.

```python
mount(
    build_application,
    upstream={
        "mem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {"MEMORY_FILE_PATH": "/data/{session}.json"},
        },
    },
)
```

Each session gets its own memory file, so its state does not leak into another
session's. Per-session clients are closed when the session is evicted.
`theodosia://upstreams` reports `{"mode": "per_session", "servers": [...]}` for
this configuration and does not ping the servers (pinging would spawn a client).

## Per-call timeout

`call_upstream` and `safe_upstream` take a `timeout` (seconds) that bounds one
call independently of the step-level `action_timeout_seconds`:

```python
rows = await call_upstream("db", "query", {"sql": "..."}, timeout=5)
```

On expiry a structured `UpstreamError(server, tool, body="timeout")` is raised,
so a slow or hung upstream fails fast instead of stalling the whole step. Through
`safe_upstream` the same expiry surfaces as a classified `ERROR` result.

## Classifying results

`safe_upstream` calls an upstream and returns a classified `SourceResult` without
ever raising; `classify_payload` does the classification on a payload you already
have. The `expect` argument tunes what counts as usable:

- `expect="any"` (default): for structured (JSON) upstreams. A bare string is
  treated as unstructured, or as an error if it reads like one.
- `expect="rows"`: for tabular upstreams. Coerces output that arrives as JSON, a
  single dict, or a Python `repr` string (many DB/MCP servers return rows as a
  `repr`, not JSON) into a list of rows. Parsing uses `ast.literal_eval`, so it
  cannot execute upstream code.
- `expect="text"`: for prose-returning upstreams (a fetch server, a filesystem
  `read_file`). A non-empty string is `OK`. Use this so a page that merely
  mentions the word "error" is not misclassified as a failure, which the default
  `any` mode would do.

```python
rows = await safe_upstream("db", "db", "query", {"sql": "..."}, expect="rows")
page = await safe_upstream("web", "fetch", "get", {"url": url}, expect="text")
```

## Serving an FSM that mounts upstream

If your module builds the server itself, for example a `build_server()` that
calls `mount(..., upstream={...})` and returns the FastMCP, point `theodosia
serve` at that function:

```bash
theodosia serve mymodule:build_server
```

`serve` runs an already-mounted FastMCP (or a callable returning one) as-is
rather than trying to re-mount it, so a server that owns its upstream config
serves correctly.

## Example

`examples/upstream_filesystem.py` is a code-audit FSM that drives the official
filesystem MCP server this way: list files, read a candidate, flag findings,
report. The agent only ever calls `step`.

## A note on stdio upstreams + in-memory clients

If you drive your Theodosia server through FastMCP's in-memory `Client(server)`
(common in tests and notebook exploration) and your `upstream` is a stdio
subprocess (`{"command": "...", "args": [...]}`), the subprocess client raises
`Client failed to connect: fileno` because the in-memory transport has no real
file descriptor to hand to the child process. Theodosia rewrites this into a
clear `UpstreamError` directing you to either drive the parent over a real
transport (stdio / http / sse) or substitute `FakeUpstream` for the test. For
production `theodosia serve` runs the upstream stdio path works because the
parent itself runs over a real transport.

## Testing with `FakeUpstream`

`theodosia.testing.FakeUpstream` is an in-process stand-in for upstream MCP
servers. It satisfies the same `async call(server, tool, args)` protocol as
`UpstreamManager`, so a test passes it where a real upstream config would go.
Every call is recorded for later assertion.

```python
from theodosia.testing import FakeUpstream
from theodosia import mount

fake = FakeUpstream({
    "grafana": {
        "list_datasources": [{"name": "prometheus", "type": "prometheus"}],
        "query_metric": lambda args: {"rate": 0.42, "series": args["query"]},
    },
})

server = mount(build_application, name="incident", upstream=fake)
# ... drive the server through an MCP Client; the actions reach `fake` instead
# of touching the network.

assert fake.calls_to("grafana", "query_metric")[0].args["query"] == "rate(http_requests[5m])"
```

Responses can be static values, sync callables, or async callables taking the
args dict. A callable that raises simulates upstream failure, which surfaces
through `safe_upstream` as a classified `ERROR` result.

For trajectory-based tests, `theodosia.testing.RecordingUpstream` wraps a real
upstream and writes every call to a JSONL fixture; `ReplayingUpstream` serves
that fixture back in order, raising `ReplayMismatch` if a drift call arrives.
Useful for "record once against the real server, replay forever" test patterns.