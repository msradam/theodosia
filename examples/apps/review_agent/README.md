# review_agent

An auditable AI code reviewer. A Burr finite state machine, mounted as an MCP
server by Theodosia, that drives a real upstream filesystem MCP server to read
source files and record security findings behind hard gates.

The reviewing agent connects to this server and sees ONLY the `step` tool. It is
never given filesystem tools. Every file read happens inside an action body via
`call_upstream("filesystem", ...)`, so each read advances FSM state and is
hash-chained into a tamper-evident ledger. The review gates are enforced in
Python, not in the prompt:

- `flag(path, issue, severity)` rejects any file the agent has not read first.
- `summarize(report)` rejects until at least two issues are flagged and the
  report is substantive.

The audit trail is the product: you can replay and cryptographically verify
exactly which files were read and what was flagged.

## What it is

| Action | Gate | Effect |
| --- | --- | --- |
| `open_review()` | entrypoint | surveys the target tree via the filesystem server |
| `read_file(path)` | loop | returns a file's content and records the read |
| `flag(path, issue, severity)` | must have read `path`; severity enum | records a finding |
| `summarize(report)` | terminal; needs >= 2 flags and a >=120-char report | compiles the report |

Severity is one of `critical | high | medium | low | info`.

The FSM uses the builder seam: `build()` returns an UNBUILT `ApplicationBuilder`
with `.with_tracker(theodosia.tracker("review-agent"))`. Theodosia stamps
`app_id = session_id` and builds it per session.

## Serve

```bash
python examples/apps/review_agent/app.py
```

This launches a stdio MCP server named `review-agent` and, as a child process,
the upstream filesystem server (`npx @modelcontextprotocol/server-filesystem`)
scoped to the review target.

Configuration via environment:

- `REVIEW_TARGET` — directory to review (default: the shipped `vuln_demo`).
- `REVIEW_TRACKER_DIR` — tracker storage root (default: a temp dir, so
  concurrent runs do not collide and nothing is written into the repo).

> Note: serve with `python app.py`, not `theodosia serve ...:build_server`. The
> stock `theodosia serve` CLI mounts the target with its own branding upstream
> and does not pass through the `upstream=` configured in `mount()`, so the
> filesystem server would not be wired. Running `build_server().run()` directly
> (the `__main__` path) is the supported way to serve an FSM that owns an
> upstream.

## Drive

Point any MCP client at the stdio server with only `mcp__review__step` allowed.
A minimal Claude Agent SDK driver (Haiku, login auth, no API key):

```python
from claude_agent_sdk import ClaudeAgentOptions, query

options = ClaudeAgentOptions(
    model="claude-haiku-4-5",
    mcp_servers={
        "review": {
            "type": "stdio",
            "command": "/path/to/.venv/bin/python",
            "args": ["/path/to/examples/apps/review_agent/app.py"],
        }
    },
    allowed_tools=["mcp__review__step"],
    permission_mode="bypassPermissions",
    strict_mcp_config=True,   # do not inherit other MCP servers
    setting_sources=[],       # do not inherit global Claude Code config
    max_turns=30,
)

async for msg in query(prompt="Review this codebase for security issues.", options=options):
    ...
```

Give it "review this codebase for security issues." It surveys the tree, reads
files through the FSM, flags issues, and writes a report, using only `step`.

## Verify the audit

Each session's steps and refusals are hash-chained into `ledger.jsonl` next to
the tracker log. Recompute and verify the chain:

```bash
theodosia verify <app_id> -p review-agent --home "$REVIEW_TRACKER_DIR" --json
```

A broken chain (any after-the-fact edit, reorder, or deletion) exits nonzero and
names the offending line.
