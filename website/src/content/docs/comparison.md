---
title: 'Why mount a graph'
description: 'The same LangGraph workflow rendered three ways (LangGraph, Burr orchestrator, Theodosia over MCP), and when mounting a graph for an agent to drive is worth it.'
---

Why mount a graph over MCP instead of just running Burr with an LLM at each node?
And why Burr instead of LangGraph? The
[`examples/comparison/`](https://github.com/msradam/theodosia/tree/main/examples/comparison)
suite answers both by taking real LangGraph examples, the status quo, and
rendering each one three ways: the LangGraph original, a plain Burr orchestrator,
and the same graph mounted with Theodosia. Everything runs key-free (the model
calls are deterministic stubs), so the only variable is the architecture.

## Callee versus caller

In an orchestrator (standard LangGraph, standard Burr) a script or runtime owns
the loop and calls the LLM at each node. The LLM is a callee: invoked, returns a
value, no agency over control flow, no view of the state machine. Any guardrails
live in the graph that party runs.

Theodosia inverts that. The graph becomes an MCP server; the agent is the caller.
It asks "what are my legal next moves?" ([`theodosia://next`](tools.md)), attempts
a step, and the server permits or refuses it. Enforcement lives server-side,
independent of which client connects, and every attempt, including refused ones,
is recorded.

Picture the workflow as a building. In the orchestrator model there is no building
the agent can see: the runtime walks the rooms for it. In Theodosia the agent
walks the rooms itself, and at each room it can see which doors are open before it
moves. If it tries a locked door, the door stays shut and tells it which doors are
open. A log by the entrance records every door it opened and every locked one it
rattled.

## The MCP thesis

MCP normally exposes a flat list of tools. Theodosia uses it to expose a machine
instead: a state machine with legal transitions, typed inputs, and a ledger,
behind the single `step` tool. So any agent loop that speaks MCP (LangGraph,
Strands, PydanticAI, CrewAI, BeeAI, Claude Code) can drive an audited, gated
workflow with no framework-specific code. Mounting a workflow is orthogonal to the
agent that drives it. The
[SQL example](https://github.com/msradam/theodosia/tree/main/examples/comparison/08_sql_agent.py)
makes this concrete: a LangGraph MCP client drives the same mounted machine and
gets the read-only guarantee and the ledger for free.

## Why Burr over LangGraph

Lines of code are not the measure. For a simple chain the two frameworks are
within a line or two of each other. The differences that matter are elsewhere:

- **Dependencies.** A LangGraph graph pulls five packages before your code runs
  (`langgraph`, `langgraph-checkpoint`, `langgraph-prebuilt`, `langgraph-sdk`,
  `langchain-core`). Burr's graph is one package (`apache-burr`).
- **Expressiveness.** Reasonable people can disagree, but the Burr rendering reads
  as data: a declarative `Condition.expr(...)` on state rather than a router
  function plus an edge map, and per-action `reads`/`writes` that make data flow
  legible and [`doctor`](cli.md)-checkable.
- **The `app` framing is what makes mounting possible.** A Burr `Application` is a
  first-class object that exposes its graph, its state, and its legal transitions
  for inspection. That is exactly the surface `mount()` needs, so mounting is one
  line. A compiled LangGraph is built to be invoked, not handed to a server that
  drives it a step at a time and reports the legal next moves.

## What mounting adds

The same Burr graph, mounted, gains all of this with no change to the graph:

| Capability | LangGraph / Burr orchestrator | Theodosia over MCP |
|---|---|---|
| Agent queries its legal next-states before acting | No; edges are internal | **Yes** (`theodosia://next`) |
| Drivable by any MCP client | No; a Python call into the graph | **Yes**; any framework with an MCP client |
| Slot contract | Post-hoc; you validate output in code | **Server-validated** before the action runs |
| Observability of what ran | Yes (checkpointer, LangSmith / Burr tracker) | Yes (`theodosia://history`, tracker) |
| Records *refused* attempts as tamper-evident evidence | Not by default; first-party | **Yes**, server-side, hash-chained |
| Guard binds a caller you do not host | No; your edges run only if the caller runs your graph | **Yes**; the gate is behind `step` |
| Latency | Lowest (in-process) | Higher; one MCP round-trip per step |

## Worked examples

Each is a real LangGraph example rendered three ways. The frameworks are real
(real LangGraph, real Burr, real Theodosia); the model content is stubbed so they
run key-free; each file's docstring states its structural fidelity to the source.

| Example | Sourced from | What it shows |
|---|---|---|
| [Prompt chaining](https://github.com/msradam/theodosia/tree/main/examples/comparison/01_langgraph.py) | LangGraph prompt-chaining | The callee-vs-caller distinction and the ceremony comparison, verbatim |
| [Routing](https://github.com/msradam/theodosia/tree/main/examples/comparison/06_routing.py) | LangGraph routing | The first branch, so the agent genuinely chooses among legal moves |
| [Stored-program machine](https://github.com/msradam/theodosia/tree/main/examples/comparison/07_rewoo.py) | LangGraph ReWOO | The agent as a tape head: it submits a program and drives it one instruction at a time |
| [SQL agent](https://github.com/msradam/theodosia/tree/main/examples/comparison/08_sql_agent.py) | LangGraph SQL agent | `DROP TABLE` refused server-side, and any MCP framework driving the same machine |
| [Two agents, one machine](https://github.com/msradam/theodosia/tree/main/examples/comparison/09_multi_agent.py) | LangGraph multi-agent | Two independent clients drive one shared machine; one ledger records both |
| [Roles and phases](https://github.com/msradam/theodosia/tree/main/examples/comparison/11_roles.py) | LangGraph hierarchical teams | Different agents enter at different phases of one machine that keeps the state |
| [Self-correction](https://github.com/msradam/theodosia/tree/main/examples/comparison/10_code_assistant.py) | LangGraph code assistant | A bad draft is refused with the exact error; the agent resubmits, no client retry logic |
| [Sensitive-tool gate](https://github.com/msradam/theodosia/tree/main/examples/comparison/05_airline_hitl.py) | LangGraph customer-support | LangGraph's own `interrupt_before` gate is client-side and bypassable; the mounted one is not |

## When to use which

For a single trusted caller running a fixed pipeline, mounting is honestly
overkill: a script would do, and it only adds a round-trip. The examples say so.
The case for mounting a graph for an agent to drive rests on three things:

1. **The driver is not the party that hosts the guards.** When a second client, a
   different framework, or an untrusted agent drives the same capability, an
   in-graph guard does not travel with it.
2. **The agent brings judgment the graph cannot bake in.** When there is a real
   choice among legal moves, letting the agent see the open doors and pick is the
   point.
3. **The record has to be trustworthy to someone other than you.** A first-party
   log of what your process did is a different artifact from a server-side,
   tamper-evident record of what a caller attempted and was refused.

If none of those hold, the orchestrator is the right tool and Theodosia is
overhead. If any of them holds, mounting the graph and letting the agent drive it
is a legitimate choice, not a novelty. For the security boundary these guarantees
sit on, see the [security model](security-model.md).
