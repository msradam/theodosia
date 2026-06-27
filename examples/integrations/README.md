# Use Theodosia with your existing agent framework

A Theodosia-mounted Burr FSM is a plain MCP server. Any agent framework with an
MCP client can mount it as one tool among its own tools, with no Theodosia or
Burr knowledge. The workflow's guarantees, legal transitions, evidence gates,
and a tamper-evident ledger, ride inside the `step` tool and hold server-side
regardless of what the agent tries. The agent author gets an audited workflow
without writing any guard logic.

Each directory below mounts the gated `deploy_approval` FSM
(`examples/deploy_approval.py`) over stdio and drives it from the framework,
next to a native tool. Every one was dogfooded end to end.

| Framework | Version | MCP entry point | Notes |
|---|---|---|---|
| [Strands](strands_data_pipeline/) | 1.45 | `strands.tools.mcp.MCPClient` | Drove the FSM with a local 1.5B model. |
| [LangGraph](langgraph_audited_workflow/) | 1.2.6 | `langchain_mcp_adapters` + `create_react_agent` | Use `client.session(...)`, not `get_tools()` (see below). |
| [PydanticAI](pydanticai_audited_workflow/) | 2.0.0 | `MCPServerStdio` toolset | One line: drop the toolset into `Agent(toolsets=[...])`. |
| [CrewAI](crewai_audited_workflow/) | 1.15.1 | `crewai_tools.MCPServerAdapter` | Set `THEODOSIA_SINGLE_BLOCK=1` (see below). |
| [BeeAI](beeai_audited_workflow/) | 0.1.81 | `MCPTool.from_client` | IBM framework; swap in `WatsonxChatModel` for watsonx. |

## The recipe (every framework)

1. Serve the FSM over stdio: `theodosia serve <module>:<factory> --app-dir <dir>`
   (or run a module whose `build_server()` calls `mount(...)`).
2. Point the framework's MCP client at that stdio command.
3. The framework's MCP adapter turns `step` (and `reset_session`, `fork_at`,
   `fork_from_past`, `list_resources`, `read_resource`) into native tools.
4. Build the agent with `tools = [*theodosia_mcp_tools, *your_native_tools]`.

## Serve-time env toggles for embedding

These tune the wire shape for a framework's MCP client. All default off and
preserve behaviour; set them before `theodosia serve`.

- **`THEODOSIA_QUIET=1`** suppresses FastMCP's startup banner, which otherwise
  prints to stderr and clutters the agent console.
- **`THEODOSIA_SINGLE_BLOCK=1`** emits the step result as a single JSON content
  block instead of a human headline block plus a JSON block. Use it with
  adapters that mangle a multi-block result or read only `content[0]` (CrewAI's
  adapter stringifies the block list).
- **`THEODOSIA_STRICT_ERRORS=1`** marks guidance refusals
  (`invalid_transition` / `validation_failed` / `unknown_action`) as MCP errors,
  so a framework's tool-error routing (LangGraph error edges, CrewAI/BeeAI tool
  retry) fires on them. The structured payload, including `valid_next_actions`,
  is unchanged. Off by default, because a refusal is recoverable and most agents
  self-correct better when it arrives as data, not an exception.

## Two things every MCP-client framework hits

1. **One MCP session is one FSM run.** Theodosia keys FSM state by the MCP
   session id, so all `step` calls must share one session. A client that opens a
   fresh session per tool call (e.g. LangGraph's `MultiServerMCPClient.get_tools()`)
   resets the machine to its entrypoint every call. Use the framework's
   persistent-session API (`async with client.session(...)`), as the LangGraph
   example does.
2. **Resources reach the agent through the tool channel.** Agent loops consume
   tools, not MCP resources, so `theodosia://graph` and friends are surfaced as
   the `read_resource` / `list_resources` tools (FastMCP's `ResourcesAsTools`).
   The `step` tool's description also carries the action surface and the recovery
   contract, so a tool-only agent can plan without any resource read.
