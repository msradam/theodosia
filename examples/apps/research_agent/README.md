# research_agent

An auditable research agent built on Theodosia. A Burr finite-state machine,
mounted as a single MCP server, fetches real web pages through an upstream
*fetch* MCP server and writes a cited report through an upstream *filesystem*
MCP server. A Haiku agent drives the whole workflow using only the `step` tool;
it is never handed a fetch tool or a filesystem tool.

## What it is

The agent connects to one Theodosia server and sees one tool, `step`. The FSM
actions call the upstream servers from inside their bodies via
`call_upstream`, so every fetch and every disk write advances FSM state and is
recorded by the tracker. What you get is a research transcript you can audit:
which URLs were fetched, which claims cite which fetched source, and the exact
report that was persisted.

FSM shape (entry: `frame`):

```
frame(question)
  -> fetch_source(url)        # loop; retrieve a page via the fetch upstream
  -> extract(claim, source)   # gated: claim must cite a fetched source
  -> write_report(filename)   # gated: persist via the filesystem upstream
  -> synthesize(report)       # terminal; gated
```

Two gates make the output trustworthy:

- `extract(claim, source)` rejects any claim whose `source` is not a source the
  FSM actually fetched with status OK. You cannot cite a page you never read,
  or one whose fetch failed.
- `write_report` and `synthesize` require at least 2 cited claims drawn from at
  least 2 distinct fetched sources, plus substantive markdown. `synthesize`
  additionally requires the report text to mention each cited source id, so the
  citations survive into the final artifact.

## Upstreams

Started by `mount(upstream=...)` in `build_server()`:

- `fetch` -> `uvx mcp-server-fetch` (the official Python fetch server). Its
  `fetch` tool returns a URL as markdown. Requires `uvx` on PATH and network
  access.
- `files` -> `npx -y @modelcontextprotocol/server-filesystem <out_dir> <sources_dir>`.
  Its `write_file` tool persists the report; its `read_file` tool serves the
  offline fallback. Requires `npx` on PATH.

### Offline fallback

If a `fetch_source` URL has no scheme (a bare filename), the FSM reads it from
`sources/` through the filesystem upstream instead of the network. The seed
files `sources/web_overview.md` and `sources/http_overview.md` let the full
workflow run hermetically when the fetch server is unavailable. The driver
prompt instructs the agent to retry with the matching filename if a live fetch
returns status `error`.

## Run it

Through the CLI seam (pointed at `build_server`, which returns the mounted
FastMCP with its `upstream=` config; `serve` runs it as-is):

```
.venv/bin/theodosia serve app:build_server --app-dir examples/apps/research_agent
```

or run the module directly:

```
.venv/bin/python examples/apps/research_agent/app.py
```

Point any MCP client at that stdio command and allow only
`mcp__research-agent__step` (plus the always-on `reset_session` /
resource-read tools). The driver used to validate this app is a small Claude
Agent SDK script that runs Haiku on the Claude Code login (no API key),
fetches two live Wikipedia pages, extracts two cited claims, and synthesizes a
report that passes the citation gate.

## Configuration

- `RESEARCH_OUT_DIR`: directory the report is written into. Defaults to a fresh
  temp directory per process. The path is resolved with `os.path.realpath`
  because the filesystem MCP server rejects paths that do not match its allowed
  root after symlink resolution (on macOS, `/var` resolves to `/private/var`).
- Tracker: project `research-agent`, stored under a unique temp directory per
  process (`with_tracker(theodosia.tracker("research-agent", storage_dir=...))`).

## License

Same as the parent Theodosia project.
