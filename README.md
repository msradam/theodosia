# Theodosia

[![PyPI](https://img.shields.io/pypi/v/theodosia?style=flat-square&color=286983&logo=pypi&logoColor=white)](https://pypi.org/project/theodosia/)
[![tests](https://img.shields.io/github/actions/workflow/status/msradam/theodosia/ci.yml?branch=main&style=flat-square&label=tests)](https://github.com/msradam/theodosia/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-286983?style=flat-square)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-theodosia-286983?style=flat-square&logo=astro&logoColor=white)](https://msradam.github.io/theodosia/)
[![Built on Apache Burr](https://img.shields.io/badge/built%20on-Apache%20Burr-7a3c4a?style=flat-square)](https://github.com/apache/burr)
[![Built on FastMCP](https://img.shields.io/badge/built%20on-FastMCP-7a3c4a?style=flat-square)](https://github.com/jlowin/fastmcp)

Theodosia mounts a [Burr](https://burr.dagworks.io/) `Application` as an MCP server. Every Burr action is reachable through a single `step(action, inputs)` tool; the server checks reachability against the graph before each action runs, refuses out-of-order calls with the legal next moves, and records every attempt.

![A coffee-order FSM driven over MCP, with a refusal and recovery](demos/demo.gif)

## Install

Python 3.11, 3.12, or 3.13 (Burr does not yet support 3.14). On a fresh Python 3.14 install you will see "no version that satisfies the requirement theodosia"; create a 3.11–3.13 venv first.

```bash
uv venv --python 3.13       # or: python3.13 -m venv .venv
uv pip install theodosia    # or: pip install theodosia
```

Optional extras: `theodosia[observability]`, `theodosia[ui]`, `theodosia[claude]`, `theodosia[mellea]`, `theodosia[all]`.

On a slim Docker image (`python:3.13-slim`, Alpine) the install pulls a `psutil` build that needs `gcc` and `python3-dev`. Either use the full `python:3.13` image, or `apt-get install -y gcc python3-dev` before `pip install`.

## Try it without an API key

```bash
theodosia primer
```

Walks the coffee-order FSM through Theodosia's `step` tool in-process and prints the timeline with state diffs and one structured refusal. No LLM, no network, same output every run.

## Quickstart

```python
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from theodosia import mount


@action(reads=[], writes=["item", "stage"])
def take_order(state: State, item: str) -> State:
    return state.update(item=item, stage="ordered")


@action(reads=["stage"], writes=["stage"])
def pay(state: State, amount: float) -> State:
    return state.update(stage="paid")


def build_application():
    is_ordered = Condition.expr("stage == 'ordered'")
    return (
        ApplicationBuilder()
        .with_actions(take_order=take_order, pay=pay)
        .with_transitions(("take_order", "pay", is_ordered))
        .with_state(item=None, stage="empty")
        .with_entrypoint("take_order")
        .build()
    )


if __name__ == "__main__":
    mount(build_application, name="coffee").run()
```

Save as `coffee.py` and run `python coffee.py` to serve over stdio. Pass a factory (a callable returning a built `Application`) so each MCP session gets its own isolated state. Passing an already-built `Application` works too but shares state across sessions.

Exercise the server in-process with FastMCP's `Client`:

```python
import asyncio
from fastmcp import Client
from coffee import build_application

async def main():
    async with Client(mount(build_application, name="coffee")) as client:
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        print(r.structured_content)
        r = await client.call_tool("step", {"action": "take_order", "inputs": {"item": "mocha"}})
        r = await client.call_tool("step", {"action": "pay", "inputs": {"amount": 5.0}})
        print(r.structured_content)

asyncio.run(main())
```

A client that calls `pay` before `take_order` gets a structured refusal it can recover from:

```json
{ "error": "invalid_transition", "valid_next_actions": ["take_order"] }
```

## Primitives

- **`mount(application, *, hooks=[...], middleware=[...], upstream=..., personas=...)`** wraps a Burr `Application` (or factory) as a FastMCP server. Returns the server; call `.run()` or pass to FastMCP's in-memory `Client`. Optional kwargs forward Burr `LifecycleAdapter`, FastMCP `Middleware`, upstream MCP clients, and `PERSONA.md` identity layers. A factory may return an unbuilt `ApplicationBuilder`; Theodosia then stamps `app_id = session_id`, so the Burr tracking dir is bound to the session and never drifts on reset.
- **The four MCP tools** every mounted server exposes: `step(action, inputs)`, `reset_session`, `fork_at(sequence_id)`, `fork_from_past(app_id, sequence_id)`. The action namespace lives in `step`'s argument schema; FSM complexity changes the schema, not the tool count. FastMCP's `ResourcesAsTools` transform adds `list_resources` and `read_resource` for clients that lack native `resources/read`.
- **Structured refusals** from `step`: `invalid_transition`, `unknown_action`, `validation_failed`, `action_timeout`, `action_error`. Every refusal carries `valid_next_actions`. `fork_at` / `fork_from_past` return their own `error` codes on the same wire shape.
- **`theodosia://` resources** for inspection: `graph` (plus `graph/mermaid`, `graph/dot`, and `source/{action}`), `state`, `next`, `history`, `subruns`, `trace`, `children`, `upstreams`, and `session` (tracker coordinates, run progress, and fork/spawn lineage).
- **`upstream`** lets a Burr action body call tools on other MCP servers through `call_upstream(server, tool, args)`. The agent driving Theodosia never sees those servers; it only sees `step`. A `{session}` placeholder in an upstream's config isolates stateful upstreams per session.

Full reference (Persona, Assembly, hooks, middleware, tracker, `drive_claude`) lives in the [docs](https://msradam.github.io/theodosia/).

## Use it in your existing agent framework

A mounted server is a plain MCP server, so any agent framework with an MCP client can mount an audited workflow as one tool among its own, with no Theodosia or Burr knowledge. The gates and the tamper-evident ledger ride inside the `step` tool and hold server-side regardless of what the agent tries. Dogfooded end to end against [Strands](examples/integrations/strands_data_pipeline/), [LangGraph](examples/integrations/langgraph_audited_workflow/), [PydanticAI](examples/integrations/pydanticai_audited_workflow/), [CrewAI](examples/integrations/crewai_audited_workflow/), and [BeeAI](examples/integrations/beeai_audited_workflow/); see [`examples/integrations/`](examples/integrations/) for the recipe and the per-framework notes. Three serve-time env toggles tune the wire shape for an embedding client: `THEODOSIA_QUIET` (no startup banner), `THEODOSIA_SINGLE_BLOCK` (single JSON result block), and `THEODOSIA_STRICT_ERRORS` (route guidance refusals through the framework's tool-error path).

## Scope

Theodosia does not include an agent, a model, or a workflow engine. It mounts an existing Burr `Application` and gates an MCP client's access to it. The rails are only as tight as the graph you author.

## Benchmarks (preliminary)

Theodosia's headline property is structural and holds regardless of any accuracy number: every step an agent takes, and every step it was refused, lands in a typed, replayable, hash-chained ledger. The benchmarks below are supporting evidence.

**ITBench SRE (Claude Haiku 4.5, 35 scenarios).** On IBM Research's [ITBench SRE benchmark](https://github.com/itbench-hub/ITBench), scored by the benchmark's own judge, gating the agent's procedure with Theodosia improved root-cause entity F1 over the raw agent (0.561 vs 0.408, paired p<0.01). Same model, same prompt, same tools, same data; the only change is the gate. Full methodology, the FSM, and the seven validity controls: [theodosia-bench](https://github.com/msradam/theodosia-bench/blob/main/RESULTS.md).

**Small local models (5 open-weight models, M4 Mac Mini, Ollama).** Five models ≤8B were tested against three FSM compliance checks: navigate a multi-stage FSM to terminal, self-correct from a structured refusal using `valid_next_actions`, and receive a correct `isError=True` signal on a raised exception.

| Model | Parameters | FSM navigation | Self-correction | Error signal |
|---|---|---|---|---|
| Granite 4.1 8B | 8.8B | ✓ | ✓ | ✓ |
| Qwen3 8B | 8B | ✓ | ✓ | ✓ |
| Nemotron Nano 4B | 3.5B active | ✓ | ✓ | — |
| Llama 3.1 8B | 8B | ✗ | ✗ | ✗ |
| Phi4-mini | 3.8B | ✗ | ✗ | ✗ |

Nemotron Nano's "—" on error signal: the model declined to pass the deliberately invalid inputs, so the exception was never triggered. Llama 3.1 stalled before the terminal state and ignored tools entirely on the refusal test. Phi4-mini emitted no tool calls across all three tests. Parameter count does not predict protocol compliance; the models trained explicitly on structured tool-calling formats pass, the others do not. The `tests/smoke/test_ollama.py` suite is reproducible against any Ollama instance.

## Command line

```bash
theodosia serve module:app                       # mount as MCP server (stdio, default)
theodosia serve module:app --transport http --port 8000   # serve over HTTP
theodosia render module:app                      # draw the state machine in the terminal
theodosia doctor module:app                      # statically validate the graph; exits nonzero for CI
theodosia sessions show <id>                     # full timeline: per-step state diff + timing
theodosia sessions diff <a> <b>                  # cross-session: action-path divergence + final-state diff
theodosia watch                                  # live-tail a running session
theodosia logs --refusals                        # only the steps that were refused
theodosia status                                 # tracker storage + recent activity snapshot
theodosia verify                                 # recompute the ledger hash chain
theodosia primer                                 # offline first-touch, no API key
theodosia ui                                     # open the Burr UI
```

A downstream package can ship its own command (`my-fsm serve`, `my-fsm doctor`, ...) with `build_cli`.

## Documentation

Full docs at **[msradam.github.io/theodosia](https://msradam.github.io/theodosia/)**.

| Page | Covers |
|---|---|
| [Introduction](https://msradam.github.io/theodosia/introduction/) | What Theodosia is and the primitives you reach for |
| [Build your own agent](https://msradam.github.io/theodosia/tutorial/) | End-to-end: write a Burr graph, serve it, drive it with an MCP client |
| [Authoring a graph](https://msradam.github.io/theodosia/authoring/) | The Burr building blocks: `@action`, `Condition`, `with_transitions` |
| [Architecture](https://msradam.github.io/theodosia/architecture/) | `mount()`, the four-tool surface, the action-selection bridge |
| [Refusals](https://msradam.github.io/theodosia/refusals/) | The five refusal shapes and how the agent recovers |
| [Sessions](https://msradam.github.io/theodosia/sessions/) | Per-session isolation, `fork_at`, `fork_from_past`, partition keys |
| [Security model](https://msradam.github.io/theodosia/security-model/) | The trust boundary, what the ledger does and does not prove |
| [Observability](https://msradam.github.io/theodosia/observability/) | The `theodosia://` resources, CLI, Burr UI, OpenTelemetry |
| [Personas](https://msradam.github.io/theodosia/personas/) | `PERSONA.md` identity layer mounted as MCP prompts |
| [Upstream](https://msradam.github.io/theodosia/upstream/) | Calling other MCP servers from action bodies |
| [Compatibility](https://msradam.github.io/theodosia/compatibility/) | What works through `mount()` (typed state, persistence, hooks, parallelism, telemetry) |
| [CLI](https://msradam.github.io/theodosia/cli/) | `serve` / `doctor` / `render` / `sessions` / `watch` / `logs` / `status` / `report` / `primer`, and `build_cli` |
| [Deployment recipes](https://msradam.github.io/theodosia/deployment/) | Copy-pasteable configs: Claude Code, Cursor, mcphost, fast-agent, HTTP, SSE, Lambda, Kubernetes |
| [ITBench study](https://msradam.github.io/theodosia/itbench/) | Controlled raw-vs-gated study on IBM Research's ITBench SRE, scored by the benchmark's own judge |
| [Case study](https://msradam.github.io/theodosia/case-study/) | Kimi K2.6 on Grafana o11y-bench: free-ranging vs gated |
| [Research foundation](https://msradam.github.io/theodosia/research-foundation/) | The published evidence the design rests on |

## Examples and tests

[`examples/`](examples/) ships self-contained FSMs covering pure-FSM, typed state, hooks, persistence, real shellouts, LLM-in-the-graph, SKILL-to-FSM, upstream, and multi-graph, plus branching (`diagnostic_fsm`), typed-input escalation (`deploy_approval`), and native sub-app spawning (`fanout_research`). [`examples/apps/`](examples/apps/) holds production-shaped agents that drive a real upstream MCP server (code review, incident response, research, data analysis), and [`examples/integrations/`](examples/integrations/) shows a Theodosia FSM mounted as one tool in a Strands agent. Each FSM runs with `uv run python examples/<file>.py`. The test suite runs with `uv run pytest`.

## Related projects

- [theodosia-rehearsal](https://github.com/msradam/theodosia-rehearsal): drop-in demos that exercise the framework end to end.
- [Philip](https://github.com/msradam/philip): lifts declarative artifacts (Ansible YAML, Mermaid `stateDiagram-v2`, Excalidraw) into Burr `Application` instances that `theodosia.mount()` serves directly.

## Acknowledgements

Theodosia is glue between two libraries that do the hard parts: [Apache Burr](https://github.com/apache/burr) provides the state-machine `Application`, the transition graph, and the tracking UI; [FastMCP](https://github.com/jlowin/fastmcp) provides the MCP server, the transforms, and the client behind `upstream`. The SKILL demos under `examples/skills/` are reproduced verbatim from Anthropic and Trail of Bits with attribution.

Theodosia is an independent project, not affiliated with or endorsed by the Apache Software Foundation, DAGWorks, the Apache Burr project, or FastMCP.

## License

Apache 2.0. Theodosia is independent open-source work by Adam Munawar Rahman and does not represent the views of IBM Corporation or any other employer. See [NOTICE.md](NOTICE.md).
