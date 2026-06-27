# PydanticAI + Theodosia: an audited FSM as one tool among many

A [PydanticAI](https://ai.pydantic.dev) agent whose toolbox is one native tool
plus a Theodosia-mounted Burr state machine. The thesis: mount a workflow you
want audited into your existing agent as MCP, and it becomes just another tool.

The FSM is `examples/deploy_approval.py`, a deployment-approval workflow
(`open_change -> review -> approve -> deploy -> verify`) with an escalation gate
on `deploy`. Theodosia serves it over stdio with `theodosia serve`. PydanticAI's
native MCP client (`pydantic_ai.mcp.MCPToolset` over `StdioTransport`) connects
to it and exposes its `step` tool next to a native `@agent.tool_plain` business
lookup.

## Run

```bash
.venv/bin/python examples/integrations/pydanticai_audited_workflow/agent.py
```

Two deterministic proofs run with no API key:

1. **Discovery** — PydanticAI lists the six Theodosia MCP tools (`step`,
   `reset_session`, `fork_at`, `fork_from_past`, `list_resources`,
   `read_resource`) and the eleven `theodosia://` resources, alongside the
   native `suggest_risk_tier` tool.
2. **Invocation** — PydanticAI calls `step` directly and drives the FSM to a
   verified deploy, gets a structured `validation_failed` refusal when `deploy`
   is unjustified, gets per-field Pydantic errors on malformed typed input
   (`risk="critical"`), and reads `theodosia://graph` through the same client.

## Model loop (optional)

If a local [Ollama](https://ollama.com) is reachable, a real PydanticAI agent
on an OpenAI-compatible endpoint decides to call the FSM on its own. Configure
with environment variables:

```bash
OLLAMA_MODEL=granite4.1:8b-16k \
  .venv/bin/python examples/integrations/pydanticai_audited_workflow/agent.py
```

The default `qwen2.5:1.5b` is large enough to call the native tool but too small
to reliably chain `step` calls. An 8B-class tool-calling model (for example
`granite4.1:8b`) drives the full walk and self-corrects a malformed `step` call
from the returned `valid_next_actions` / `next_action_schemas`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PYDANTICAI_DEMO_HOME` | `/private/tmp/pydanticai-theodosia` | tracker scratch dir (kept out of the repo) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama OpenAI-compatible base |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | model id for the optional loop |

## Notes

- Theodosia's `step` exposes an `action` enum plus an open `inputs` object.
  PydanticAI passes that schema to the model unchanged; the FSM, not PydanticAI,
  validates the per-action input shape and returns `next_action_schemas` so the
  model learns the shape of the next legal call.
- Refusals (`validation_failed`) come back as ordinary structured results, not
  MCP `isError` responses, so they do not trip PydanticAI's own `ModelRetry`
  machinery. The agent sees the refusal as data and recovers from it.
