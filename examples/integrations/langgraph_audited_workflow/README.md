# LangGraph + Theodosia: an audited workflow as one tool among many

A LangGraph ReAct agent consumes a Theodosia-mounted Burr FSM as ordinary
LangChain tools, sitting next to a native `calculator`. The point: to get an
audited, gated workflow inside an existing LangGraph agent you do not adopt a new
framework. You mount the workflow you want audited as an MCP server and let the
agent call it.

The FSM is [`examples/deploy_approval.py`](../../deploy_approval.py): a gated
deploy-approval state machine (`open_change -> review -> approve -> deploy ->
verify`) with an escalation gate on `deploy`. Theodosia serves it over stdio;
`langchain_mcp_adapters` turns its `step` tool into a LangChain `StructuredTool`.

## Run

```
.venv/bin/python examples/integrations/langgraph_audited_workflow/agent.py
```

No API key. The optional model loop uses a local Ollama (`qwen2.5:1.5b` at
`http://localhost:11434`) and is skipped if unreachable.

## What it proves

1. **Discovery.** The adapter exposes 6 Theodosia tools (`step`, `reset_session`,
   `fork_at`, `fork_from_past`, `list_resources`, `read_resource`). They drop into
   the agent's tool list next to the native `calculator`. The `step` schema
   advertises the action `enum` and an open-object `inputs`.
2. **Invocation.** Driving `step` directly through the bound tool: a transition
   guard refuses `deploy` from `reviewed` (`invalid_transition` + the valid set);
   the escalation validator refuses `deploy` with an empty reason
   (`validation_failed`); a correct walk reaches a terminal `verified` state. All
   gating is enforced server-side.

## Required: a persistent session for stateful FSMs

`MultiServerMCPClient.get_tools()` opens a fresh MCP session per tool call, which
resets the FSM every step. To drive a stateful workflow, bind tools to one
session:

```python
client = MultiServerMCPClient({"deploy": connection})
async with client.session("deploy") as session:
    tools = await load_mcp_tools(session, server_name="deploy")
    # every tool here shares one session_id, so the FSM advances across calls
```

## Notes for LangGraph users

- Theodosia's gated refusals (`invalid_transition`, `validation_failed`) come back
  with `status="success"`, not as tool errors. The refusal is legible in both the
  `ToolMessage` text content and its `artifact` (the MCP `structuredContent`), but
  LangGraph-level error routing keyed on `ToolMessage.status == "error"` will not
  fire. Only genuine execution failures are flagged `isError`.
- `create_react_agent` consumes tools only, not MCP resources. `theodosia://graph`
  and friends are reachable through the `read_resource` / `list_resources` tools
  Theodosia also registers, so the agent can still discover the topology.
