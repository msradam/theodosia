# CrewAI + Theodosia: mount an audited FSM as one tool among many

This integration mounts a Theodosia FSM into a CrewAI agent over MCP. The
deploy-approval state machine in [`examples/deploy_approval.py`](../../deploy_approval.py)
is a Burr workflow that Theodosia serves as a plain stdio MCP server. CrewAI's
`MCPServerAdapter` connects to it and turns its `step` tool (plus
`reset_session`, `fork_at`, `list_resources`, `read_resource`, ...) into CrewAI
`BaseTool` instances, which sit in the same toolbox as an ordinary native
CrewAI tool. To the agent, the audited workflow is one capability among many.

The thesis: to get an audited, gated workflow inside the agent you already
have, mount it as MCP. You do not rewrite the agent; you add a server.

## What it proves

1. **Discovery** -- `MCPServerAdapter` turns Theodosia's tools into CrewAI tools,
   listed alongside the native `record_note` tool.
2. **Invocation** -- CrewAI calls the adapted `step` tool and drives the FSM. Two
   guards hold when driven through CrewAI:
   - `deploy` from the `reviewed` stage is refused as an `invalid_transition`
     (FSM topology guard).
   - `deploy` after `approve` but with an empty `reason` is refused by the
     escalation gate as a `validation_failed`.
   - `deploy` with a justification then succeeds, and `verify` reaches a
     terminal stage. Every transition is appended to Theodosia's hash-chained
     ledger, so the run is auditable end to end.

## Run

```bash
.venv/bin/python examples/integrations/crewai_audited_workflow/agent.py
```

The deterministic proofs run with no API key. The optional model loop
(`run_model_loop`) lets a real CrewAI `Agent`/`Crew` decide to call the FSM via
a local Ollama; it is skipped when no local model is reachable. Set
`CREWAI_DEMO_MODEL_LOOP=0` to skip it explicitly.

CrewAI talks to models through LiteLLM. For Ollama the model string is
`ollama/<model>` and the base URL comes from `OLLAMA_API_BASE`
(default `http://localhost:11434`, model `qwen2.5:1.5b`).

## The recipe

```python
from crewai_tools import MCPServerAdapter
from mcp import StdioServerParameters

params = StdioServerParameters(
    command=".venv/bin/theodosia",
    args=["serve", "deploy_approval:build", "--app-dir", "examples"],
    cwd=".",
    env={"THEODOSIA_HOME": "/tmp/ledger", ...},  # keep the ledger out of the repo
)

server = MCPServerAdapter(params)          # NOTE: starts the server eagerly
try:
    tools = list(server.tools)             # CrewAI BaseTool instances
    agent = Agent(role=..., tools=[native_tool, *tools], llm=llm)
    Crew(agents=[agent], tasks=[task]).kickoff()
finally:
    server.stop()                          # or use `with MCPServerAdapter(...) as tools:`
```

## CrewAI-specific papercuts

These are real friction points observed against `crewai 1.15.1` /
`crewai-tools[mcp]`, not Theodosia bugs.

- **Multi-block results are stringified, not parsed.** Theodosia returns two
  content blocks per `step` (a human `Step N: ...` line and the JSON result).
  CrewAI's `CrewAIToolAdapter._run` returns `content[0].text` only for a
  single-block result; for multiple blocks it returns `str([...])`, the Python
  `repr` of a list of strings. The agent (and any caller) gets a stringified
  list, not JSON. `agent.py` recovers the structure with `ast.literal_eval`
  then `json.loads`. This is the single biggest ergonomic gap.
- **`structuredContent` is dropped.** The adapter never reads MCP
  `structuredContent`; it only forwards text blocks. Theodosia mirrors its
  structured result into a JSON text block, so nothing is lost, but a CrewAI
  consumer must parse text rather than receive typed fields.
- **Eager lifecycle.** `MCPServerAdapter.__init__` starts the subprocess
  immediately (it calls `start()`), so the server is live before you enter any
  `with` block. `__enter__` just returns the already-started tools. Always pair
  construction with `stop()` (or use it as a context manager) or you leak the
  child process.
- **Refusals arrive as ordinary text, not exceptions.** A gated/invalid `step`
  comes back as a normal tool result whose JSON carries `error`,
  `valid_next_actions`, and `next_action_schemas`. CrewAI does not raise; the
  model has to read the refusal and recover. Theodosia's `next_hint` and
  `next_action_schemas` are exactly the recovery scaffolding it needs, but the
  small model often needs nudging to use them.
- **Tool naming.** Names pass through `sanitize_tool_name`. Theodosia's names
  (`step`, `reset_session`, ...) are already snake_case, so they survive intact
  and collide with nothing.
- **Resources are not exposed as tools.** CrewAI's MCP adapter surfaces only
  MCP *tools*. Theodosia's resources (`theodosia://graph`, `theodosia://next`,
  `theodosia://state`, the ledger views) are reachable only because Theodosia
  also exposes them through the `list_resources` / `read_resource` *tools*. A
  CrewAI agent reads the graph by calling `read_resource`, not via any native
  MCP-resource affordance.
- **LiteLLM / Ollama tool-calling.** Routing CrewAI to Ollama is just
  `LLM(model="ollama/<model>", base_url=...)`. With this path CrewAI 1.15 runs a
  `call_llm_native_tools` flow, and against `qwen2.5:1.5b` that flow was fragile:
  several turns came back as `Invalid response from LLM call - None or empty`,
  and the model's final answer was the tool call emitted as *text*
  (`{"name": "open_change", "arguments": {"change": {"service_name": ...,
  "risk": 0, ...}}}`) with a hallucinated argument shape (`service_name` for
  `service`, `risk: 0` for the `"low"`/`"high"` literal) rather than an actual
  tool invocation. The model never landed a valid `step`. The deterministic
  path (calling the adapted tool directly) is the reliable proof that the FSM is
  driven and gated correctly through CrewAI; the model loop demonstrates the
  wiring and exposes how much tool-calling competence a small local model
  lacks, not that the FSM is the bottleneck.

## Theodosia v0.8 enhancement ideas (from this integration)

- **Single-block result option.** A serve flag (or default) to emit one JSON
  text block instead of a human line plus a JSON block would make Theodosia
  survive CrewAI's "first block only / else `str(list)`" adapter unscathed. The
  human summary could move into the JSON (`summary` field) so no client has to
  `ast.literal_eval` a list repr.
- **A graph/state preamble for tool-only clients.** Since CrewAI never sees MCP
  resources, an optional flag to fold the current `valid_next_actions` +
  `next_action_schemas` into the `step` tool description (progressive
  disclosure already does some of this) would let resource-blind frameworks
  plan without a `read_resource` round-trip.
- **A tiny framework adapter / cookbook entry.** A documented `theodosia.mcp`
  helper that returns ready StdioServerParameters and a result-parsing shim
  (the `ast.literal_eval` + `json.loads` dance) would save every CrewAI user
  rediscovering the multi-block papercut.

## Verdict

For CrewAI users the thesis holds with one caveat. Mounting a Theodosia FSM as
MCP works: discovery is clean, the tools sit alongside native ones, and the
FSM's guards (topology + escalation gate) are fully enforced when the workflow
is driven through CrewAI. The caveat is on the result channel: CrewAI's MCP
adapter mangles Theodosia's two-block result into a stringified list and drops
`structuredContent`, so a consumer must parse text. That is a CrewAI adapter
limitation, not a Theodosia one, and a one-line single-block serve option on
Theodosia's side would remove it. Gating and auditability come for free; you
just mount the workflow you want audited into the agent you already have.
