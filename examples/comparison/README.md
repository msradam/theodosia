# Comparison: the same LangGraph workflow, three ways

Eight real LangGraph tutorials, each rendered three ways: the LangGraph original,
a plain Burr orchestrator, and the same graph mounted over MCP with Theodosia. The
only variable is the architecture. Everything runs key-free; the model calls are
deterministic stubs, so the three renderings produce identical output.

The thesis: MCP normally exposes a flat list of tools. Theodosia uses it to expose
a *machine* (legal transitions, typed slots, a ledger) behind the single `step`
tool, so any MCP-speaking agent loop drives an audited, gated workflow with no
framework-specific code. Full walkthrough:
**[Why mount a graph](https://msradam.github.io/theodosia/comparison/)**.

## Run it

```bash
../../.venv/bin/python run_all.py
```

```
same workflow (LangGraph prompt chaining), topic='cat'
  01 langgraph           verdict=Fail
  02 burr orchestrator   verdict=Fail
  03 theodosia mcp       verdict=Fail
  all three agree: A cat walked into a bar and ordered a byte to eat. Turns out it was a robot all along!
```

All three renderings agree; only the architecture differs. Run any file directly
(`../../.venv/bin/python 07_rewoo.py`), or the tests: `../../.venv/bin/python -m
pytest ../../tests/test_comparison.py`.

## The eight comparisons

Each file's module docstring has the detail and the LangGraph-node to Burr-action
mapping.

| Comparison | Sourced from | What mounting shows (observed) |
|---|---|---|
| [`01`–`03`](01_langgraph.py) prompt chaining | prompt chaining (verbatim) | Callee vs caller; ceremony is a wash (23 vs 23 LOC); the `next`/refusal/ledger surface (5 ledger entries, 2 refused) |
| [`06`](06_routing.py) routing | routing (verbatim) | The agent chooses: `theodosia://next` -> `[story, joke, poem]`, it picks one; a live agent stepped `[load, joke]` |
| [`07`](07_rewoo.py) ReWOO | ReWOO | Agent as tape head: a program counter walks `results[#En]` registers; a malicious `PUBLISH` is refused server-side |
| [`08`](08_sql_agent.py) SQL agent | SQL agent | `DROP TABLE` refused before the DB is touched; a LangGraph MCP client drives the same machine and is refused too |
| [`09`](09_multi_agent.py) multi-agent | multi-agent collaboration | Two independent clients drive one built machine; one ledger holds both: `[research, chart, chart, finalize]` |
| [`11`](11_roles.py) roles and phases | hierarchical teams (distilled) | Agents enter at different phases; a writer that connects too early is refused; one ledger for the whole review loop |
| [`10`](10_code_assistant.py) code assistant | code assistant | Self-correction: a bad draft is refused with the exact error, the agent resubmits; no retry logic in the client |
| [`05`](05_airline_hitl.py) airline gate | customer-support (verbatim gate) | LangGraph's own `interrupt_before` gate is bypassable by a second caller; the mounted `confirm` gate is not; plus slot-filling |

## Live agent runs

Opt-in, using your authenticated Claude Code session, no API key:

```bash
claude auth login   # once
COMPARISON_LIVE=1 COMPARISON_MODEL=claude-haiku-4-5-20251001 ../../.venv/bin/python 06_routing.py
COMPARISON_LIVE=1 ../../.venv/bin/python 07_rewoo.py   # session model; Haiku fumbles the 5-step drive
```

| Env var | Effect | Default |
|---|---|---|
| `COMPARISON_LIVE=1` | drive `03`/`05`/`06`/`07` with a real Claude agent | off (deterministic) |
| `COMPARISON_MODEL` | model id for the live agent | the session default |
| `COMPARISON_BUDGET` | live-run spend cap, USD | `2.0` |

## Fidelity

- **Frameworks are real.** Real LangGraph (`StateGraph`, `ToolNode`,
  `interrupt_before`, `langchain-mcp-adapters`), real Burr, real Theodosia.
- **LLM content is stubbed** deterministically; only the model content, never the
  graph. Live runs are opt-in.
- **Structural fidelity varies and each file's docstring states it:** verbatim
  (`01`, `05`, `06`), faithful data model plus one added opcode (`07`), or
  simplified with dropped nodes and loops noted (`08`, `09`, `10`, `11`). Each Burr
  action carries the LangGraph node's name and verbatim docstring.

## When to use which

For a single trusted caller on a fixed pipeline, mounting is overkill; a script
would do. It is worth it when at least one holds: the driver is not the party that
hosts the guards (multi-client, untrusted); the agent brings judgment among legal
moves; or the record must be trustworthy to a third party. The full argument
(callee vs caller, why Burr over LangGraph, "does this make the case") is on the
[Why mount a graph](https://msradam.github.io/theodosia/comparison/) page.

## Sources

Each file cites its exact source in its docstring. The tutorials:

- Prompt chaining, routing:
  <https://docs.langchain.com/oss/python/langgraph/workflows-agents>
- ReWOO, SQL, multi-agent, hierarchical teams, code assistant, customer-support:
  the [`langgraph` tutorials](https://github.com/langchain-ai/langgraph/tree/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials)
  (pinned commit `23961cff`).

## Files

```
examples/comparison/
├── README.md                 this file
├── _shared.py                LangGraph's prompt chain: the LLM stub + the Burr graph
├── 01_langgraph.py           rendering 1: LangGraph prompt chaining (verbatim)
├── 02_burr_orchestrator.py   rendering 2: Burr, orchestrator style
├── 03_theodosia_mcp.py       rendering 3: the same Burr graph mounted over MCP
├── run_all.py                run all three on one topic, print outputs + LOC
├── 06_routing.py             routing: the agent picks among legal moves
├── 07_rewoo.py               stored-program machine: the agent drives the tape head
├── 08_sql_agent.py           gated SQL + a LangGraph MCP client driving the machine
├── 09_multi_agent.py         two clients drive one shared machine; one ledger holds both
├── 11_roles.py               three role-agents enter at different phases of one machine
├── 10_code_assistant.py      self-correction: refuse a bad draft with the error, resubmit
├── _airline.py               scenario: LangGraph's airline sensitive-tool gate + the FSM
└── 05_airline_hitl.py        sourced gate + a live slot-filling probe over one `step` tool

tests/test_comparison.py      key-free: 9 tests, one per comparison
```
