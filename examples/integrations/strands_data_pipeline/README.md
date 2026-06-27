# A Theodosia FSM as one tool in a Strands agent

Thesis: **an agent workflow is an MCP.** A Burr state machine mounted by
Theodosia is a plain stdio MCP server. Any MCP-capable framework can mount it.
This example wires a [Strands](https://github.com/strands-agents/sdk-python)
agent with several tools where exactly one of them is a Theodosia-mounted
data-pipeline FSM. Strands' own MCP client turns the FSM's `step` tool (and its
companions) into native Strands tools that sit in the same toolbox as a
calculator and a clock.

## What is mounted

The FSM is `examples/apps/data_agent/app.py`: an auditable text-to-SQL pipeline
(`connect -> load -> profile -> query -> finding -> report`). It drives a real
SQLite database and a CSV through two upstream MCP servers internally, but to a
client it exposes a single `step` tool plus session controls. The agent never
gets raw SQL tools; every `query` is gated to a read-only `SELECT` inside the
FSM.

## The wiring

```
Strands Agent
  tools = [
    calculator,        # native strands_tools
    current_time,      # native strands_tools
    *MCPClient(...).list_tools_sync()   # <- the Theodosia FSM, as MCP tools
  ]
                         |
                         v  stdio
  python examples/apps/data_agent/app.py   (Theodosia -> Burr FSM -> SQLite/CSV)
```

`agent.py` builds an `mcp.StdioServerParameters` that launches the FSM module,
hands it to `strands.tools.mcp.MCPClient`, and calls `list_tools_sync()`. The
returned `MCPAgentTool` objects go straight into `Agent(tools=[...])` next to the
native tools.

The FSM is launched as `python app.py` (not `theodosia serve`) because this FSM
configures two upstream MCP servers in its `build_server()`, and the `theodosia
serve` CLI does not carry per-app upstream maps. For an FSM without upstreams,
`theodosia serve module:factory --app-dir ...` works as the stdio command just
the same.

## Run it

```bash
.venv/bin/python examples/integrations/strands_data_pipeline/agent.py
```

This needs no API key. It prints two proofs:

1. **Discovery** — the combined Strands toolbox listing the Theodosia tools
   (`step`, `reset_session`, `fork_at`, `fork_from_past`, `list_resources`,
   `read_resource`) alongside `calculator` and `current_time`.
2. **Invocation** — Strands calls the Theodosia `step` tool directly and drives
   the pipeline end to end, getting real SQLite rows back through the gated
   `query` action (and showing the gate refuse a `DROP TABLE`).

## Model providers (optional model-driven loop)

`run_model_loop()` lets a real Strands `Agent` decide to call the FSM on its own.
It is skipped unless a local model is reachable, so the demo never blocks on a
provider. Options:

- **Ollama (local, no API key)** — used by this demo. `ollama serve`, then
  `ollama pull qwen2.5:1.5b`, and `uv pip install ollama`. Strands'
  `OllamaModel` then runs the loop. Override with `OLLAMA_MODEL` / `OLLAMA_HOST`.
- **Amazon Bedrock** — Strands' default model provider, needs AWS credentials.
- **Anthropic API** — `strands.models.anthropic.AnthropicModel` with
  `ANTHROPIC_API_KEY`.
- **Any custom provider** — implement Strands' `Model` interface, e.g. to bridge
  the Claude Agent SDK login.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `STRANDS_DEMO_HOME` | `/private/tmp/strands-theodosia-demo` | tracker + DB scratch dir |
| `DEMO_PYTHON` | repo `.venv/bin/python` | interpreter that launches the FSM |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint for the model loop |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | tool-calling model for the model loop |

## What this demonstrates

To Strands, the Theodosia FSM is not special. It is discovered, schema-typed,
and invoked through the same MCP path as any other tool, and it can be one
capability among many in a larger agent. The workflow's guarantees (legal
transitions, the read-only SQL gate, the audit ledger) ride along inside the
tool. That is the thesis: an agent workflow is an MCP.
