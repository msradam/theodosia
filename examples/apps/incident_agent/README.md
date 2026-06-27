# incident_agent

An auditable incident-response / diagnosis agent built on Theodosia. A Burr FSM
mounted as an MCP server that diagnoses a production incident from real log
files. It drives two upstream MCP servers through a single `step` surface:

- a **filesystem** MCP server (`@modelcontextprotocol/server-filesystem`) the
  runbook reads logs through, and
- a **memory** MCP server (`@modelcontextprotocol/server-memory`) the runbook
  records its incident timeline into as a knowledge graph.

The connected agent sees only `step`. It is never given filesystem or memory
tools. Every upstream read and write happens inside an action via
`theodosia.call_upstream(...)`, so each one advances FSM state and lands in the
Burr tracker ledger.

## The runbook

A seven-action diagnosis FSM with hard gates the agent cannot skip:

```
triage → inspect (loop) → hypothesize → record → escalate → remediate → report
```

- `triage` lists the logs via the filesystem upstream and opens the incident
  node in the memory graph. Entry point.
- `inspect(logfile)` reads one log via the filesystem upstream; its text is
  returned in the step result. Loops.
- `hypothesize(cause)` states a root cause (refused before any log is read).
- `record(observation, evidence)` appends a finding to the memory timeline and
  links a log-evidence node. At least one is required before reporting.
- `escalate(reason)` writes an escalation to the timeline. **Required before
  any remediation**; a trivial reason is refused.
- `remediate(fix, reason)` is **gated**: refused without a prior `escalate` and
  its own reason tying the fix to the diagnosis.
- `report(summary)` is terminal and **gated**: refused without at least one
  recorded observation.

Gates are enforced in the action bodies (`ValueError` → a `step` refusal with
`valid_next_actions`), not just by the transition graph.

## Sample incident

`logs/` describes a real-shaped incident: a checkout service whose RSS climbs
from 312 MB to an OOM kill in 90 minutes after a deploy.

- `app.log` — rising RSS, a `CartCache` with 1.9M entries and no eviction, an
  `OutOfMemoryError`, then the kernel OOM-killer.
- `gc.log` — full GCs reclaiming less and less until the heap is exhausted.
- `deploy.log` — build `r4` (PR #4127) added an unbounded `CartCache` with no
  TTL or max-size five minutes before the incident; auto-rollback never fired.

Root cause: the unbounded `CartCache` from PR #4127 leaks the JVM heap.

## Run it

Build the mounted server and serve it over stdio (this app returns a mounted
`FastMCP` from `build_server()`, so run the module directly rather than through
`theodosia serve`, which mounts a builder itself):

```bash
python examples/apps/incident_agent/app.py
```

Point any MCP client at that stdio server. It will see one tool, `step`, plus
the standard Theodosia surface (`reset_session`, `fork_at`, resources). `npx`
must be on `PATH`; the two upstream servers are launched on first use.

## Tracker

Sessions are tracked under the `incident-agent` project via
`theodosia.tracker(...)`, which defaults to `~/.theodosia` (override with
`THEODOSIA_HOME`). The builder is returned unbuilt from `build()` so Theodosia
stamps each session's id as the Burr `app_id`.
