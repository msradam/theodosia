---
title: 'Authoring a graph'
description: 'Build a Burr Application from scratch and serve it with Theodosia.'
---

This is the part Theodosia does not do for you: writing the Burr state machine.
If you have never used Burr, this page is a complete, runnable starting point,
plus the two traps newcomers hit.

## A minimal served graph

A workflow is actions plus the transitions between them. Each `@action` declares
what state it reads and writes. Transitions wire actions together, optionally
behind a condition. Theodosia mounts the built `Application` as an MCP server.

```python
# incident.py
from burr.core import ApplicationBuilder, Condition, State, action
from theodosia import mount, tracker


@action(reads=[], writes=["acked"])
def acknowledge(state: State) -> State:
    return state.update(acked=True)


@action(reads=[], writes=["verified"])
def verify(state: State) -> State:
    # real work goes here; this just marks the gate satisfied
    return state.update(verified=True)


@action(reads=["verified"], writes=["resolution"])
def resolve(state: State) -> State:
    return state.update(resolution="closed")


@action(reads=[], writes=["resolution"])
def escalate(state: State) -> State:
    return state.update(resolution="escalated to owner")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(acknowledge=acknowledge, verify=verify, resolve=resolve, escalate=escalate)
        .with_transitions(
            ("acknowledge", "verify"),
            # resolve is gated: only reachable once verify set verified=True
            ("verify", "resolve", Condition.expr("verified == True")),
            # escalate is an unconditional escape from verify (see the trap below)
            ("verify", "escalate", Condition.expr("True")),
        )
        .with_tracker(tracker(project="incident"))
        .with_entrypoint("acknowledge")
        .build()
    )


if __name__ == "__main__":
    mount(build_application, name="incident").run()
```

Pass the factory function (`build_application`) to `mount`, not a built
`Application`, so each MCP session gets its own isolated state. Serve it
directly (`python incident.py`) or through the CLI:

```bash
theodosia serve incident:build_application --name incident
theodosia doctor incident:build_application   # validate before serving
```

`resolve` cannot be called until `verify` has run and set `verified=True`. That
is the whole point: the gate lives in the graph, not in a prompt.

## Return the builder, not a built Application

The factory above ends in `.build()` and returns an `Application`. You can
instead drop `.build()` and return the unbuilt `ApplicationBuilder`:

```python
def build_application():
    return (
        ApplicationBuilder()
        .with_actions(acknowledge=acknowledge, verify=verify, resolve=resolve, escalate=escalate)
        .with_transitions(...)
        .with_tracker(tracker(project="incident"))
        .with_entrypoint("acknowledge")   # no .build()
    )
```

When the factory returns a builder, Theodosia calls
`builder.with_identifiers(app_id=session_id).build()` for you, so the Burr
`app_id` equals the MCP session id and the tracker directory
(`storage_dir/project/<session_id>/`) is bound to that session. It stays bound
across `reset_session`, since the rebuild stamps the same id. This is the
recommended form when you wire Burr tracking; without it Burr generates a fresh
`app_id` on every build and the on-disk session id drifts from the live MCP
session. `mount`, `theodosia serve`, and `theodosia doctor` all accept the
builder form, the `() -> Application` form, and a bare `Application`.

## Trap 1: two unconditional exits from one action

Burr allows only one *default* (conditionless) transition per source action. If
a runbook needs more than one unconditional exit from the same step, for example
both `resolve` and `escalate` reachable from `verify`, a second bare transition
fails:

```text
ValueError: Transition `verify` -> `escalate` is redundant --
a default transition has already been set for `verify`
```

Give each such edge an explicit always-true condition instead of relying on the
default. `Condition.expr("True")` works, as shown above; an equivalent is a
named condition:

```python
ALWAYS = Condition(keys=[], resolver=lambda _state: True, name="always")
.with_transitions(
    ("verify", "resolve", Condition.expr("verified == True")),
    ("verify", "escalate", ALWAYS),
)
```

Theodosia returns every transition whose condition evaluates true as a valid
next action, so multiple true conditions from one source all show up in
`valid_next_actions`. The agent (or your code) then picks one.

## Trap 2: where your sessions are written

Theodosia's `tracker(project=...)` writes to `~/.theodosia`, which is also where
the observability CLI looks by default, so `theodosia sessions ls -p incident`
finds your runs with no extra flags. If you instead wire Burr's native tracker
(`with_tracker("local", project=...)`), it writes to `~/.burr`, and you must
point the CLI at it: `theodosia sessions ls --home ~/.burr -p incident`. Pick
one and stay consistent.

`~/.theodosia` is **shared** across every app on the machine. Two people running
the same example, or CI jobs running in parallel, collide in one store unless
you isolate it. Two knobs do that:

- `THEODOSIA_HOME=/path/to/store` (env) redirects the store directory. The CLI
  reads the same variable, so `sessions ls` / `verify` / `report` follow
  automatically. Equivalent per-call: `tracker(project=..., storage_dir=...)`.
- `project=` namespaces runs *within* a store. Give each app or tenant its own
  project name so `sessions ls -p <name>` stays legible.

Quick glossary, since these terms recur:

- **action**: one `@action` function; reads/writes named state fields.
- **transition**: a legal edge between two actions, gated by a `Condition`.
- **tracker**: where a session's steps are recorded on disk (the store).
- **project**: the namespace a session is filed under in the store.
- **app_id**: the unique id of one session (one Application run).
- **partition_key**: an optional tenant label set via Burr's
  `with_identifiers(partition_key=...)`; surfaces in `theodosia://session`.

## Trap 3: `Condition.expr` reads pre-step state

A transition's condition is evaluated against the state of the source action
*before* that action's writes land. If an action sets `borderline = True` in
its body, the next *outgoing* transition can gate on `borderline == True`, but
the transition *into* that action cannot. Concretely: write the gate-deciding
field in action N's body, then gate the N → N+1 edge on it.

## Typed inputs

Action functions can declare inputs with Pydantic models or built-in types.
Theodosia surfaces each input's JSON schema at `theodosia://graph` under
`input_schemas` so an agent can see the shape before calling.

```python
from pydantic import BaseModel

class OrderInput(BaseModel):
    item: str
    qty: int = 1

@action(reads=[], writes=["order"])
def take_order(state: State, order: OrderInput) -> State:
    return state.update(order=order.model_dump())
```

`theodosia://graph` for the action above:

```json
{
  "name": "take_order",
  "required_inputs": ["order"],
  "input_schemas": {
    "order": {
      "type": "object",
      "properties": {
        "item": {"type": "string"},
        "qty": {"type": "integer", "default": 1}
      },
      "required": ["item"]
    }
  }
}
```

The agent then calls `step` with the parameter name as the outer key:

```json
{"action": "take_order", "inputs": {"order": {"item": "mocha", "qty": 2}}}
```

Theodosia coerces that dict into an `OrderInput` instance before invoking
the action, so the body receives the typed object its signature declared.

Common trap: calling `step("take_order", {"item": "mocha"})` (without the
`order` wrapper) raises `missing required inputs: {'order'}`. The input keys
are parameter names, not the fields of the typed model.

## `reads=` is action-body discipline, not wire confidentiality

A Burr action's `reads=[...]` declaration is enforced at runtime when the
FSM uses `PydanticTypingSystem`. Burr synthesizes a per-action input model
containing only the declared fields; an action body that references an
undeclared field of state raises `AttributeError`, not `None`.

```python
@action(reads=["claims"], writes=["verification_answers"])
def answer_verifications(state: VerifierState) -> VerifierState:
    # state.claims works.
    # state.baseline raises AttributeError: 'VerifierStateanswer_verifications_input'
    # object has no attribute 'baseline' -- even though baseline exists
    # at the FSM level.
    ...
```

This is the architectural enforcement that makes patterns like
Chain-of-Verification (independent verification phase between action
bodies) structural. A prompt asking an action author to "remember not to
peek at the baseline" is exhortation; `reads=["claims"]` is a runtime
contract the action body cannot violate.

**What this does not do**: hide state from the LLM driving the FSM.
`theodosia://state` returns the full state dict; every `step` response
includes the post-step state. `reads=` is the contract between the FSM
graph and the action body's Python code. It is not a confidentiality
boundary on the MCP wire. If your threat model is "the agent must not
see field X", you need to add a state filter in `mount()` (see
[Security model](security-model.md)) rather than relying on `reads=`
alone.

The MCP wire boundary does tighten one separate property: `step`'s
`inputs` parameter only forwards keys that match the action body's
signature, so an LLM client cannot smuggle extra named parameters
through the wire.

## Bundling: `theodosia.Assembly`

An Assembly is a frozen dataclass bundling the workflow plus its mount-time
configuration (personas, upstream config, instructions, metadata) into one
declarative artifact. `mount(assembly)` and `assembly.serve()` are equivalent,
so an Assembly is a complete description of what to mount.

```python
from theodosia import Assembly
from incident import build_application

asm = Assembly(
    name="incident",
    workflow=build_application,
    upstream={"grafana": "http://localhost:8000/sse"},
    personas="personas/",  # a directory of PERSONA.md files
    default_persona="on-call-sre",
)

asm.serve().run()       # or: theodosia.mount(asm).run()
```

The `workflow` field accepts a built `Application`, a factory callable, or a
`module:attr` import string. `Assembly.from_yaml(path)` loads the same shape
from disk for declarative configuration. Per-call kwargs to `mount(asm, ...)`
override the Assembly's fields.

## Next

- [Architecture](architecture.md): the four-tool surface and how `step` drives Burr.
- [Observability](observability.md): tail, replay, and the Burr UI for any served graph.
- [CLI](cli.md): `primer`, `serve`, `doctor`, and the observability commands.
