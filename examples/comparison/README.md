# The same LangGraph workflow, three ways

Why mount a graph over MCP instead of just running Burr with an LLM at each node?
And why Burr instead of LangGraph? Each comparison here starts from a real
LangGraph example, the status quo, and renders it in Burr (orchestrator style) and
over Theodosia (mounted as an MCP server), so the only variable is the
architecture.

The one-line thesis, which the SQL example makes concrete: MCP normally exposes a
**flat list of tools**. Theodosia uses it to expose a **machine** instead, a state
machine with legal transitions, typed slots, and a ledger, behind the single
`step` tool. So *any* agent loop that speaks MCP (LangGraph, Strands, PydanticAI,
CrewAI, BeeAI, Claude Code) can drive an audited, gated workflow with no
framework-specific code. Mounting a workflow is orthogonal to the agent that
drives it.

Eight comparisons, all sourced from LangGraph:

1. **Primary three-way** ([01](01_langgraph.py) / [02](02_burr_orchestrator.py) /
   [03](03_theodosia_mcp.py)): LangGraph's own **prompt-chaining** workflow,
   verbatim, rendered three ways. This carries the callee-vs-caller distinction,
   the Burr-vs-LangGraph simplicity question, and what mounting adds.
2. **Routing three-way** ([06](06_routing.py)): LangGraph's **routing** workflow.
   The first one that branches, so "query your legal moves and pick one" stops
   being decoration: the routing decision moves from the graph to the runtime to
   the agent.
3. **Stored-program machine** ([07](07_rewoo.py)): LangGraph's **ReWOO** tutorial,
   which is already a program (a plan with a program counter and a register file).
   Mounted, the agent is the tape head: it submits a program and drives it one
   instruction at a time while the server enforces the machine.
4. **Consequential actions + the MCP thesis** ([08](08_sql_agent.py)): LangGraph's
   **SQL agent**, where the only guard on `DROP TABLE` is a prompt. Mounted, the
   server refuses it, and a LangGraph MCP client drives the same machine and gets
   the same guarantee for free.
5. **Two agents, one machine** ([09](09_multi_agent.py)): LangGraph's
   **multi-agent** collaboration. Mounted as a shared (built) app, two independent
   clients drive one machine; the server enforces the handoff, gates the code, and
   one ledger records both.
6. **Roles and phases** ([11](11_roles.py)): LangGraph's **hierarchical agent
   teams**. A researcher, a writer, and a reviewer each drive their own session
   and enter at their own phase of one shared machine, which keeps the state and
   runs the review loop.
7. **Self-correction loop** ([10](10_code_assistant.py)): LangGraph's
   **code-assistant**. Mounted, a bad draft is refused with the exact error and
   the agent resubmits until it passes, no retry logic in the client.
8. **Sourced scenario** ([05](05_airline_hitl.py)): LangGraph's **customer-support
   tutorial** sensitive-tool gate, next to Theodosia's. This is where mounting is
   straight-up better, plus a live slot-filling probe.

## Fidelity

So an audit knows what it is reading:

- **Frameworks are real.** Every `langgraph_*` rendering uses real LangGraph
  (`StateGraph`, `ToolNode`, `interrupt_before`, `langchain-mcp-adapters`); every
  Burr rendering uses real Burr; the mounts use real Theodosia. Nothing is a mock
  framework.
- **LLM calls are stubbed** deterministically, so the renderings agree and run
  key-free (live model runs are opt-in for `03`/`05`/`06`/`07`). Only the model
  *content* is stubbed, never the graph.
- **Node ↔ action mapping.** Each Burr action carries the LangGraph node's name
  and its verbatim docstring, with the transition-to-edge correspondence in a
  comment. Structural fidelity varies and each file's docstring states it exactly:
  - **Verbatim structure:** `01` (prompt chaining), `06` (routing), `05` (the
    safe/sensitive `interrupt_before` gate).
  - **Faithful data model + one added opcode:** `07` (ReWOO's plan/PC/registers,
    plus a gated `PUBLISH`).
  - **Simplified / distilled** (source nodes kept, extra nodes and retry loops
    dropped, noted per file): `08` (SQL), `09` (multi-agent), `10` (code
    assistant), `11` (roles, a distillation of hierarchical-agent-teams, not a
    structural port).

## Primary three-way: LangGraph prompt chaining

The workflow is [LangGraph's canonical "Prompt chaining" example](https://docs.langchain.com/oss/python/langgraph/workflows-agents),
reproduced verbatim in [`01_langgraph.py`](01_langgraph.py): generate a joke, gate
on whether it has a punchline, and if not, improve then polish it.

```
START -> generate_joke -> check_punchline --Pass--> END
                                |
                               Fail
                                v
                          improve_joke -> polish_joke -> END
```

The three `llm.invoke` calls are replaced by a deterministic stub so every
rendering runs key-free and produces the identical joke. All three agree:

```
A cat walked into a bar and ordered a byte to eat. Turns out it was a robot all along!
```

Run them side by side:

```bash
../../.venv/bin/python run_all.py
```

For the joke chain, mounting is honestly overkill (one trusted caller). For the
case where it is straight-up better, skip to
[the sourced scenario](#sourced-scenario-langgraphs-own-gate-plus-slot-filling).

## The distinction: callee vs caller

In an orchestrator (standard LangGraph, standard Burr) a **script or runtime owns
the loop and calls the LLM** at each node. The LLM is a callee: invoked, returns a
value, no agency over control flow, no view of the state machine. Any guardrails
live in the graph that party runs.

Theodosia inverts that. The graph becomes an MCP **server**; the agent is the
**caller**. It asks "what are my legal next moves?", attempts a step, and the
server permits or refuses it. Enforcement lives server-side, independent of which
client connects, and every attempt (including refused ones) is recorded.

### Walking through rooms

Picture the workflow as a building. In the orchestrator model there is no building
the agent can see: the runtime walks the rooms for it and the LLM only answers
questions shouted from inside each room. In Theodosia the agent walks the rooms
itself, and **at each room it can see which doors are open** before it moves. If it
tries a locked door, the door stays shut and tells it which doors *are* open. A log
by the entrance records every door it opened and every locked one it rattled.

## Why Burr over LangGraph (simplicity)

Compare [`01_langgraph.py`](01_langgraph.py) with the Burr graph in
[`_shared.py`](_shared.py). Same three steps, same punchline gate.

- **The gate.** LangGraph expresses the gate as a separate function
  (`check_punchline` returning `"Pass"`/`"Fail"`) plus a routing map in
  `add_conditional_edges("generate_joke", gate, {"Fail": ..., "Pass": END})`. Burr
  expresses it as a declarative transition condition,
  `Condition.expr("verdict == 'Fail'")`, read straight off state.
- **State.** LangGraph declares a `TypedDict` and each node returns a partial dict
  merged into shared state, so data flow is implicit. Burr declares each action's
  `reads`/`writes`, so data flow is explicit and `theodosia doctor` can statically
  confirm every action's reads are covered.
- **Lines of code are not the measure.** For this chain they are identical (23 and
  23), so LOC settles nothing. The differences that matter are elsewhere:
  - **Dependencies.** A LangGraph graph pulls five packages before your code runs
    (`langgraph`, `langgraph-checkpoint`, `langgraph-prebuilt`, `langgraph-sdk`,
    `langchain-core`). Burr's graph is one package (`apache-burr`). Theodosia adds
    `fastmcp`, `pydantic`, `typer`. Fewer moving parts to pin, break, and upgrade.
  - **Expressiveness.** Reasonable people can disagree here, but the Burr rendering
    reads as data: a declarative `Condition.expr(...)` on state rather than a router
    function plus an edge map, and per-action `reads`/`writes` that make data flow
    legible and `doctor`-checkable. Read both files and judge for yourself.
  - **The `app` framing is what makes mounting possible.** This is the load-bearing
    one. A Burr `Application` is a first-class object that exposes its graph, its
    state, and its legal transitions for inspection. That is exactly the surface
    `theodosia.mount(app)` needs, so mounting is one line. A LangGraph compiled
    graph is built to be *invoked*, not handed to a server that drives it a step at
    a time and reports the legal next moves. Burr being mountable at all is a
    property of that `Application` abstraction, not an afterthought.

  LangGraph also ships `create_react_agent(model, tools)` as a one-liner, but it
  hides the graph, so it is not a fair subject for a ceremony count.

## What Theodosia adds (functionality)

[`03_theodosia_mcp.py`](03_theodosia_mcp.py) mounts the *identical* Burr graph from
`_shared.py`. The only new code in the graph path is one `mount()` call.
Everything below is observed output from `run_theodosia("cat")`:

- **Legal next-states, queryable.** Before acting, the caller reads
  `theodosia://next` -> `["generate_joke"]`. After `generate_joke` (Fail path), it
  reads `["improve_joke"]`. The branch is visible to the caller.
- **Server-side refusal of an out-of-order step.** `polish_joke` first is refused
  with `{"error": "invalid_transition", "valid_next_actions": ["generate_joke"]}`.
- **Server-validated slot.** `generate_joke` takes a `topic` slot; an empty topic
  is refused `{"error": "validation_failed", "reason": "a non-empty topic is
  required"}` before the action runs. Raw Burr (02) ignores that validator.
- **A ledger of every attempt, including refusals.** `theodosia://history` returns
  **5 entries, 2 of them refused** (the out-of-order `polish_joke` and the empty
  `topic`). With Burr's tracker on, the same trail is a durable, hash-chained
  `ledger.jsonl` that `theodosia verify` recomputes.
- **Drivable by any MCP client, including a real agent.** With `COMPARISON_LIVE=1`,
  `03` serves the chain over stdio and hands it to a real Claude agent through the
  Claude Agent SDK on your authenticated Claude Code session, no API key.

## Capability comparison

| Capability | LangGraph (01) | Burr orchestrator (02) | Theodosia over MCP (03) |
|---|---|---|---|
| Agent queries its legal next-states before acting | No; edges are internal to the compiled graph | No; same | **Yes, native** (`theodosia://next`) |
| Drivable by any MCP client | No; a Python call into the graph | No; a Python call into the app | **Yes**; Claude Agent SDK / Claude Code / Cursor / mcphost / any |
| Slot contract | Post-hoc; you validate node output in Python | Post-hoc; the script validates what it passes | **Server-validated** before the action runs (`validation_failed`) |
| Observability of what the workflow ran | Yes (checkpointer, `get_state_history`, LangSmith) | Yes (Burr tracker + UI) | Yes (`theodosia://history`, tracker, UI) |
| Records *refused* attempts as tamper-evident evidence | Not by default; a routed-away edge is not an "attempt denied" record, and any log is first-party | Same | **Yes**, server-side by construction; hash-chained `ledger.jsonl` |
| Guard binds a caller you do not host | No; your edges run only if the caller runs your graph | No; same | **Yes**; the gate is behind the `step` tool |
| Latency | Lowest (in-process) | Lowest (in-process) | Higher; one MCP round-trip per step. **Orchestrators win** |
| Closed pipeline, one trusted caller | Great fit | Great fit, simplest | Overkill; you do not need a trust boundary |
| Ceremony for the same chain (code lines) | 23 | 23 | 23 + 1 `mount()` (+5 for the validated-slot gate) |

## Honest framing

The orchestrators are not missing features by accident, and Theodosia is not
"better always."

- A single-owner orchestrator enforces plenty in code. A LangGraph conditional
  edge is Python, not a prompt convention, and LangGraph is thoroughly auditable
  (checkpointer, `get_state_history`, LangSmith). So the line is not "convention vs
  enforced." It is **who hosts the guard relative to who drives the workflow.** An
  orchestrator's guard runs only because the same party runs the graph. When the
  driver is a client you do not host, that guard does not travel with the
  capability, and a first-party log of what your process did is a different artifact
  from a server-side, tamper-evident record of what a caller attempted and was
  refused.
- **Orchestrators win on latency and simplicity.** No MCP round-trip, no server, no
  client. If you own the loop and control every caller, the orchestrator is the
  right tool and Theodosia is overhead.
- The LOC is a wash and the reasoning step is a deterministic stub, so no rendering
  "wins" on model quality; that is deliberate, to isolate architecture.

## Routing three-way: where the agent actually chooses

The prompt chain is a corridor: at each step there is one open door, so "query
your legal next moves" is real but never *used* for a decision. [LangGraph's
routing workflow](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
is the first one that branches, and [`06_routing.py`](06_routing.py) renders it
three ways. The workflow is the same (classify a request, send it to the story,
joke, or poem writer); the only thing that moves is **where the routing decision
is made**.

- **LangGraph (01-style).** A router node classifies and a conditional edge
  follows it. The graph decides.
- **Burr orchestrator.** A `route` action writes a decision; conditioned
  transitions follow it. The runtime decides.
- **Theodosia.** From `load`, all three writers are legal. The caller reads
  `theodosia://next` -> `["story", "joke", "poem"]` and picks one; the server
  bounds the choice to that set (a branch taken before `load` is refused
  `invalid_transition`). The agent decides.

Observed: all three route `"Write me a joke about cats"` to the joke writer and
produce the identical output. Driven live, a real Claude agent stepped
`['load', 'joke']`: it read its legal moves and made the routing call itself. This
is the caller model's payoff. In the two orchestrators the branch is chosen for
the agent; only when the graph is mounted does the agent see the open doors and
choose, with the choice bounded and recorded server-side. That is the concrete
answer to "why have an *agent* drive it": the agent brings the judgment, the
server brings the rails.

## Stored-program machine: the agent as the tape head

The sharpest version of "an agent driving a graph" is a machine the agent
literally programs. [LangGraph's ReWOO tutorial](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/rewoo/rewoo.ipynb)
already is one, and [`07_rewoo.py`](07_rewoo.py) makes it explicit: a planner
emits a straight-line program of typed steps `#En = TOOL[input]` (parsed by
ReWOO's exact regex); a program counter walks it; a register file `results[#En]`
holds evidence; each step substitutes reads of prior registers and writes one new
register. **The plan is the tape, `results` is the registers, the program counter
is the head.**

Mounted, the agent submits the program and drives `execute` one instruction at a
time. Observed from `theodosia_rewoo`, the head advancing and the registers
filling:

```
pc=1  registers={'#E1': 'result(otter behavior)'}
pc=2  registers={'#E1': 'result(otter behavior)', '#E2': 'reasoned(result(otter behavior))'}
pc=3  registers={'#E1': ..., '#E2': ..., '#E3': 'published(reasoned(result(otter behavior)))'}
```

`solve` before the program is planned is refused `invalid_transition`. Driven
live, a real Claude agent stepped `['plan', 'execute', 'execute', 'execute',
'solve']` through the `step` tool: it drove the tape head itself.

Faithful ReWOO is read-only and single-author, so mounting it alone would be
merely illustrative. So the port adds **one consequential opcode, `PUBLISH`**, and
treats the program as untrusted. Now mounting earns its keep. Give all three
renderings a malicious program whose last instruction is `#E2 = PUBLISH[all
customer emails]` (publishing data no register ever computed):

- the **orchestrator** runs it; `PUBLISHED` leaks `['all customer emails']`.
- **Theodosia** validates the program against the instruction set, and at the
  `PUBLISH` step the server refuses `validation_failed` ("its argument must
  resolve to a computed register"), so nothing leaks and the attempt is on the
  ledger.

The machine's rules, the program counter, the instruction-set check, and the gate
on the one dangerous opcode all live server-side, in front of an untrusted
program the agent drives.

## Consequential actions, and any framework can drive it

[LangGraph's SQL-agent tutorial](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/sql/sql-agent.md)
lists tables, reads schema, generates SQL, and runs it. Its only guard against a
destructive statement is a sentence in the system prompt: "DO NOT make any DML
statements (INSERT, UPDATE, DELETE, DROP...)." That is prose, not enforcement.
[`08_sql_agent.py`](08_sql_agent.py) moves it into a server-side gate.

When the model emits `DROP TABLE albums` (a confused or injected model will):

- `langgraph_sql` and `burr_sql` run it. The table is gone.
- **`theodosia_sql`** refuses `run` with `validation_failed` ("only read-only
  SELECT statements are permitted") before the DB is touched. The table is intact
  and the refused DROP is on the ledger. That ledger is the compliance artifact:
  proof the agent never mutated the database.

### The MCP thesis, concretely

The mounted machine is a plain MCP server, so nothing about it is Claude-specific.
`any_framework_drives` in the same file points **LangGraph's own MCP client**
(`langchain-mcp-adapters`) at the mounted SQL machine over stdio. Observed:

```
langchain-mcp-adapters sees the machine as tool(s): ['fork_at', 'list_resources',
  'read_resource', 'reset_session', 'step']
its SELECT returned: [['Miles Davis'], ['Nina Simone']]
its DROP was refused server-side too: validation_failed
```

A LangGraph agent got the read-only guarantee and the ledger for free, with no
Burr or Theodosia code in it: it just called an MCP tool. This is the thesis. MCP
usually hands an agent a flat list of tools; Theodosia hands it a `step` tool that
is really a machine, and the machine's rules hold for whoever connects, Strands or
PydanticAI or CrewAI or BeeAI alike. The repo dogfoods exactly this against five
frameworks in [`examples/integrations/`](../integrations/).

## Two agents, one machine, one ledger

[LangGraph's multi-agent collaboration](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/multi_agent/multi-agent-collaboration.ipynb)
runs a researcher and a chart generator on one shared state; the charter runs code
through `python_repl_tool` (arbitrary exec, flagged unsafe). [`09_multi_agent.py`](09_multi_agent.py)
mounts a *built* application (not a factory), which shares one FSM across MCP
sessions. So two independent clients drive the same machine. Observed:

```
charter (a separate session) sees the researcher's shared data, next: ['chart']
charter's malicious spec (__import__('os').system('rm -rf /')) -> validation_failed
code that actually executed: ['bar chart of tool use']    # the malicious spec never ran
one ledger, both agents: ['research', 'chart', 'chart', 'finalize']  (1 refused)
```

The researcher and the charter share no code and never talk directly. The server
enforces the handoff (you cannot chart before research produced data), gates the
charter's spec, and records both agents' attempts in one hash-chained ledger. This
is the foreign-driver case at full strength, and it is only possible because the
machine, not either agent, holds the rules.

## Roles and phases: different agents enter at different points

`09` had both agents in the same phase. A machine can also have **distinct phases,
with different agents entering at each while it keeps the state**. Distilling the
roles-and-phases idea from
[LangGraph's hierarchical-agent-teams tutorial](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/multi_agent/hierarchical_agent_teams.ipynb)
(not a structural port of its supervisors and sub-teams),
[`11_roles.py`](11_roles.py) is a report pipeline: `research -> write -> review`,
with a review that can reject and loop back to `write`. A researcher, a writer, and
a reviewer each drive their own session. Observed:

```
writer connects first, tries write@start -> invalid_transition   (phase_open: ['research'])
researcher: phase ['research']            -> research
writer:     phase ['write'], sees_notes "otters use rocks..."   -> write   # shared state
reviewer:   phase ['review']              -> review(reject)  -> phase_now: ['write']
writer (again) -> write ;  reviewer (again) -> review(approve)
final stage: approved   revisions: 2
one ledger, the whole collaboration: ['research', 'write', 'review', 'write', 'review']
```

The writer that connects too early is refused, because its entry point (the `write`
phase) is not reachable yet; it becomes reachable once research is done. Each role
enters at its own phase, the machine carries `notes`/`draft` across every session,
the reject sends control back to the writer, and one ledger records every role and
the revision loop. The machine *is* the supervisor, so the agents need no shared
routing logic; they just watch `theodosia://next` for their phase to open.

## Self-correction from a structured refusal

[LangGraph's code-assistant](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/code_assistant/langgraph_code_assistant.ipynb)
generates code, `exec`s it to check, and reflects-then-regenerates on error, with
that retry loop wired into the graph. [`10_code_assistant.py`](10_code_assistant.py)
moves the check server-side, so *any* driver gets the retry contract for free. A
bad submission is refused with the exact error and the FSM does not advance, so the
agent revises and resubmits. Observed:

```
submit "def solve(\n  return 6*7"          -> validation_failed  "SyntaxError: '(' was never closed"
submit "import os\ndef solve(): ..."        -> validation_failed  "forbidden operation ..."
submit "def solve():\n  return 6*7"          -> accepted; run -> 42
ledger: 4 entries, 2 refused drafts
```

The driver needed no retry logic; the server returned each error as data, and every
draft, rejected or accepted, is on the ledger. This is the property the repo's
small-model benchmark tests directly: self-correction from a structured refusal.

## Sourced scenario: LangGraph's own gate, plus slot-filling

The strongest version of "when it is straight-up better" does not use a workflow we
invented. [`05_airline_hitl.py`](05_airline_hitl.py) reproduces LangGraph's own
answer to gating a sensitive action, from the customer-support tutorial
([source](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/customer-support/customer-support.ipynb)):
tools are split into `safe_tools` and `sensitive_tools`, and the graph is compiled
with `interrupt_before=["sensitive_tools"]`, a client-side pause for approval.
`05` reproduces that gate verbatim (real `ToolNode` split, real `interrupt_before`,
the tutorial's `update_ticket_to_new_flight` signature) next to the Theodosia gate.

- **LangGraph world (the tutorial's gate).** The interrupt fires as designed: the
  graph pauses before the mutation, nothing commits until the client approves.
  Observed: **0 commits before approval, 1 after.** But that gate lives in the graph
  this client runs, and the sensitive tool is a plain `@tool`, so a second caller
  invokes it directly. Observed: **2 total mutations, one that never passed any
  gate.**
- **Theodosia world.** The same rebooking is an FSM whose `rebook` action is
  reachable only through `step`, after a server-enforced `confirm`. `rebook` up
  front and `rebook` before `confirm` are both refused `invalid_transition`; a
  `new_flight_id` the search never returned is refused `validation_failed` with the
  valid options. Observed: **1 mutation, 0 unconfirmed, 3 refusals on the ledger.**

**Slot-filling probe.** Because every Theodosia step is `step(action, inputs)`, the
driving model fills each action's typed slots through one tool, not a
purpose-named tool per action. Driving `05` live (Haiku 4.5, your authed session),
the agent filled all four slots correctly on the first pass, including the
cross-step one, `new_flight_id`, which it had to read out of `search`'s result
rather than the prompt:

```
step find     inputs={'booking_ref': 'TKT-42'}
step search   inputs={'date': '2026-08-02'}
step confirm  inputs={'acknowledge': 'Rebooking confirmed for TKT-42'}
step rebook   inputs={'new_flight_id': 205}     # 205 came from search, not the prompt
```

Cost about $0.03. When a weaker model fills a slot wrong, the refusal is the
recovery path: `validation_failed` on `rebook` carries the searched options and
`next_action_schemas`, which the deterministic transcript exercises.

## Does this make the case?

Honestly: for a single trusted caller running a fixed pipeline, no. The prompt
chain proves the mechanism (queryable next-states, refusals, a ledger) but a
script would do, and mounting only adds a round-trip. The examples say so.

The case for mounting a graph for an *agent* to drive rests on three things, and
these examples show each one concretely:

1. **The driver is not the party that hosts the guards.** When a second client, a
   different framework, or an untrusted agent drives the same capability, an
   in-graph guard does not travel with it. The airline scenario shows LangGraph's
   own `interrupt_before` gate working for its client and bypassed by another.
2. **The agent brings judgment the graph cannot bake in.** When there is a real
   choice among legal moves, letting the agent see the open doors and pick is the
   point. The routing example shows a live agent making the branch call itself,
   bounded to the legal set by the server.
3. **The record has to be trustworthy to someone other than you.** A first-party
   log of what your process did is not the same artifact as a server-side,
   tamper-evident record of what a caller attempted and was refused.

If none of those hold, the orchestrator is the right tool and Theodosia is
overhead. If any of them holds, "mount the graph and let the agent drive it" is a
legitimate choice, not a novelty. The examples are meant to let you locate your
own case on that line, not to argue that mounting is always better.

## When to use which

- **Graph over MCP (Theodosia)** wins when the graph is driven by an autonomous or
  untrusted agent, when more than one client drives the same workflow, or when you
  need a tamper-evident record of what was attempted and refused. The gate and the
  ledger hold server-side no matter what the caller does.
- **Orchestrator (LangGraph or Burr)** wins when you own the loop end to end,
  latency matters, and there is no adversarial or multi-client concern.
- **Burr over LangGraph** for the graph itself: declarative transition conditions,
  explicit state contracts, a static `doctor` check, and it is the same graph you
  later mount through Theodosia with one line if you ever need the trust boundary.

## Running it

Everything runs offline with no API key by default (the LLM calls are a
deterministic stub):

```bash
../../.venv/bin/python 01_langgraph.py         # one rendering
../../.venv/bin/python 02_burr_orchestrator.py
../../.venv/bin/python 03_theodosia_mcp.py     # prints the next/refusal/ledger transcript
../../.venv/bin/python run_all.py              # primary three-way + LOC
../../.venv/bin/python 06_routing.py           # routing three-way (the agent chooses)
../../.venv/bin/python 07_rewoo.py             # stored-program machine (agent = tape head)
../../.venv/bin/python 08_sql_agent.py         # gated SQL + a LangGraph MCP client drives it
../../.venv/bin/python 09_multi_agent.py       # two clients drive one machine, one ledger
../../.venv/bin/python 11_roles.py             # three role-agents, phases, one shared machine
../../.venv/bin/python 10_code_assistant.py    # self-correction from structured refusals
../../.venv/bin/python 05_airline_hitl.py      # sourced gate scenario (airline)
../../.venv/bin/python -m pytest ../../tests/test_comparison.py   # key-free
```

Opt into the live Claude-agent runs, which use your authenticated Claude Code
session, no API key. `03` drives the chain, `06` routes, `07` drives the tape
head, `05` fills slots:

```bash
claude auth login                              # once, if not already logged in
COMPARISON_LIVE=1 COMPARISON_MODEL=claude-haiku-4-5-20251001 ../../.venv/bin/python 06_routing.py
COMPARISON_LIVE=1 ../../.venv/bin/python 07_rewoo.py   # session model; Haiku fumbles the 5-step drive
```

| Env var | Effect | Default |
|---|---|---|
| `COMPARISON_LIVE=1` | drive `03`/`05`/`06`/`07` with a real Claude agent | off (deterministic) |
| `COMPARISON_MODEL` | model id for the live agent | the session default |
| `COMPARISON_BUDGET` | live-run spend cap, USD | `2.0` |

## Sources

- LangGraph prompt-chaining and routing workflows, reproduced verbatim in `01`
  and `06`: <https://docs.langchain.com/oss/python/langgraph/workflows-agents>
- LangGraph ReWOO tutorial (the stored-program plan/PC/register model) ported in
  `07`:
  <https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/rewoo/rewoo.ipynb>
- LangGraph SQL-agent tutorial (run model SQL; guard is prose) ported in `08`:
  <https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/sql/sql-agent.md>
- LangGraph multi-agent collaboration ported in `09`:
  <https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/multi_agent/multi-agent-collaboration.ipynb>
- LangGraph hierarchical agent teams (roles/phases) ported in `11`:
  <https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/multi_agent/hierarchical_agent_teams.ipynb>
- LangGraph code-assistant tutorial ported in `10`:
  <https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/code_assistant/langgraph_code_assistant.ipynb>
- LangGraph customer-support tutorial, the safe/sensitive split and
  `interrupt_before=["sensitive_tools"]` reproduced in `05`:
  <https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/customer-support/customer-support.ipynb>
- The shipped human-approval helper behind that pause, `HumanInterrupt` /
  `interrupt([...])`: `langgraph.prebuilt.interrupt` (moved to
  `langchain.agents.interrupt` in v1).

## Files

```
examples/comparison/
├── README.md                 this file
├── _shared.py                LangGraph's prompt chain: the LLM stub + the Burr graph
├── 01_langgraph.py           rendering 1: LangGraph prompt chaining (verbatim)
├── 02_burr_orchestrator.py   rendering 2: Burr, orchestrator style
├── 03_theodosia_mcp.py       rendering 3: the same Burr graph mounted over MCP
├── run_all.py                run all three on one topic, print outputs + LOC
├── 06_routing.py             routing three-way: the agent picks among legal moves
├── 07_rewoo.py               stored-program machine: the agent drives the tape head
├── 08_sql_agent.py           gated SQL + a LangGraph MCP client driving the machine
├── 09_multi_agent.py         two clients drive one shared machine; one ledger holds both
├── 11_roles.py               three role-agents enter at different phases of one machine
├── 10_code_assistant.py      self-correction: refuse a bad draft with the error, resubmit
├── _airline.py               scenario: LangGraph's airline sensitive-tool gate + the FSM
└── 05_airline_hitl.py        sourced gate + a live slot-filling probe over one `step` tool

tests/test_comparison.py      key-free: 9 tests, one per comparison; 03/05/06/07/08/09/10/11 enforce
```
