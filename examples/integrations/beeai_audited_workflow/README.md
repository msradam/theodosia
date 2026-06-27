# BeeAI + Theodosia: mount an audited workflow as MCP

A [BeeAI](https://github.com/i-am-bee/beeai-framework) agent that consumes a
Theodosia-served Burr FSM as MCP tools, alongside a native BeeAI tool. The
thesis: to get a workflow audited, mount it into your existing agent as MCP. No
SDK rewrite, no special client.

BeeAI is IBM's agent framework. This is the relevant target because Theodosia's
lead user, medea, is an IBM z/OS ops agent; a watsonx/BeeAI ops agent would
mount an audited change-approval workflow exactly this way.

## What it shows

The FSM is `examples/deploy_approval.py`: an eight-action deploy-approval state
machine (`open_change -> review -> approve -> deploy -> verify`) with an
escalation gate on `deploy`. Theodosia serves it over stdio with
`theodosia serve deploy_approval:build --app-dir examples`. BeeAI's `MCPTool`
connects and turns the server's tools into native BeeAI tools that sit in the
same toolbox as a hand-written `current_time` tool.

1. **Discovery** — BeeAI lists the Theodosia tools (`step`, `reset_session`,
   `fork_at`, `fork_from_past`, plus FastMCP's bridged `list_resources` /
   `read_resource`) and they appear in the combined agent toolbox next to the
   native tool.
2. **Invocation** — BeeAI calls `step` directly to drive the FSM
   `open_change -> review -> approve -> deploy -> verify`, and the gate refuses
   two unsafe deploy attempts (one blocked by the transition graph, one by the
   escalation input-validator) with structured, recoverable payloads.

A real `RequirementAgent` on a local Ollama model also gets a turn if one is
reachable; that path is best-effort and skipped otherwise.

## Run

```bash
.venv/bin/uv pip install beeai-framework   # 0.1.81, pulls litellm + mcp extra
.venv/bin/python examples/integrations/beeai_audited_workflow/agent.py
```

No API key required. The optional model loop uses a local Ollama at
`http://localhost:11434` (override with `OLLAMA_HOST` / `OLLAMA_MODEL`); it is
skipped if none is reachable. The audit tracker is redirected to a scratch dir
via `THEODOSIA_HOME` so the demo never writes into the repo.

## How BeeAI consumes it

```python
from beeai_framework.tools.mcp import MCPTool
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

transport = stdio_client(StdioServerParameters(
    command=".venv/bin/theodosia",
    args=["serve", "deploy_approval:build", "--app-dir", "examples"],
))
tools = await MCPTool.from_client(transport)   # one MCPTool per server tool
```

`MCPTool.from_client` calls `tools/list` once and wraps each entry. Structured
tool results (`structuredContent`) pass straight through as `output.result`;
Theodosia's gate refusals arrive as the JSON refusal payload, so the agent can
read `valid_next_actions` and recover.
