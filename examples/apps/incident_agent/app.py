"""An auditable incident-response / diagnosis agent.

A Burr FSM mounted by Theodosia that diagnoses a production incident from
real log files. It drives TWO upstream MCP servers through a single
``step`` surface:

  * a filesystem server (``@modelcontextprotocol/server-filesystem``) the
    runbook reads logs through, and
  * a memory server (``@modelcontextprotocol/server-memory``) the runbook
    records its incident timeline into as a knowledge graph.

The agent connected to this server sees ONLY ``step``. It never gets
filesystem or memory tools; every upstream read/write happens inside an
action via ``theodosia.call_upstream(...)``, so each one advances FSM
state and lands in the ledger.

The graph is a diagnosis runbook with hard gates an agent cannot skip:
``triage`` (index logs) -> ``inspect`` (read a log, loop) -> ``hypothesize``
-> ``record`` (write a timeline observation to memory) -> ``escalate``
(must come before remediation, with a stated reason) -> ``remediate``
(refused without a prior escalation and its own reason) -> ``report``
(terminal, refused without at least one recorded observation).

Run:

    theodosia serve incident_agent.app:build_server --app-dir examples/apps

Sample logs live in ``logs/`` next to this file and describe an unbounded
``CartCache`` leaking the JVM heap until the kernel OOM-killer fires.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from burr.core import ApplicationBuilder, Condition, State, action

import theodosia
from theodosia import ServingMode, call_upstream, mount

_TRACKER_PROJECT = "incident-agent"
_LOGS_DIR = str(Path(__file__).parent / "logs")
# Per-session memory files land here; one per MCP session (see build_server).
_MEMORY_DIR = os.environ.get("INCIDENT_MEMORY_DIR", tempfile.mkdtemp(prefix="incident-memory-"))
_INCIDENT = "production-incident"
_MAX_CONTENT = 6000


@action(
    reads=[],
    writes=[
        "phase",
        "logs_dir",
        "index",
        "reads",
        "hypotheses",
        "observations",
        "escalation",
        "log",
    ],
)
async def triage(state: State) -> State:
    """Open the incident. List the available log files via the filesystem
    server and seed the incident node in the memory graph. Start here."""
    listing = await call_upstream("filesystem", "list_directory", {"path": _LOGS_DIR})
    await call_upstream(
        "memory",
        "create_entities",
        {
            "entities": [
                {
                    "name": _INCIDENT,
                    "entityType": "incident",
                    "observations": ["triage opened; log index surveyed"],
                }
            ]
        },
    )
    return state.update(
        phase="inspect",
        logs_dir=_LOGS_DIR,
        index=str(listing),
        reads=[],
        hypotheses=[],
        observations=[],
        escalation=None,
        log=["triage: indexed logs, seeded incident node"],
    )


@action(reads=["logs_dir", "reads", "log"], writes=["reads", "last_content", "log"])
async def inspect(state: State, logfile: str) -> State:
    """Read one log file via the filesystem server. Its text is returned in
    the step result so you can judge it. Loop over the logs you need.

    Args:
        logfile: a log file name from the index (e.g. ``app.log``), or an
            absolute path inside the logs directory.
    """
    name = logfile.strip()
    if not name:
        raise ValueError("logfile must not be empty")
    path = name if name.startswith("/") else f"{state['logs_dir']}/{name}"
    content = await call_upstream("filesystem", "read_text_file", {"path": path})
    text = content if isinstance(content, str) else str(content)
    return state.update(
        reads=[*state["reads"], path],
        last_content=text[:_MAX_CONTENT],
        log=[*state["log"], f"inspect: read {path}"],
    )


@action(reads=["reads", "hypotheses", "log"], writes=["hypotheses", "log"])
async def hypothesize(state: State, cause: str) -> State:
    """State a candidate root cause for the incident. Read at least one log
    first so the hypothesis is grounded.

    Args:
        cause: the suspected root cause, in one sentence.
    """
    if not cause.strip():
        raise ValueError("cause must not be empty")
    if not state["reads"]:
        raise ValueError("inspect at least one log before hypothesizing")
    return state.update(
        hypotheses=[*state["hypotheses"], cause.strip()],
        log=[*state["log"], f"hypothesize: {cause.strip()[:80]}"],
    )


@action(reads=["reads", "observations", "log"], writes=["observations", "log"])
async def record(state: State, observation: str, evidence: str = "") -> State:
    """Record one timeline observation into the memory knowledge graph. This
    is the audit trail; at least one is required before you can report.

    Args:
        observation: a factual finding to append to the incident timeline.
        evidence: optional log file name this observation is drawn from; it
            is linked to the incident node as a separate evidence entity.
    """
    obs = observation.strip()
    if not obs:
        raise ValueError("observation must not be empty")
    if not state["reads"]:
        raise ValueError("inspect a log before recording an observation")
    await call_upstream(
        "memory",
        "add_observations",
        {"observations": [{"entityName": _INCIDENT, "contents": [obs]}]},
    )
    ev = evidence.strip()
    if ev:
        await call_upstream(
            "memory",
            "create_entities",
            {"entities": [{"name": ev, "entityType": "log_evidence", "observations": [obs]}]},
        )
        await call_upstream(
            "memory",
            "create_relations",
            {"relations": [{"from": _INCIDENT, "to": ev, "relationType": "evidenced_by"}]},
        )
    return state.update(
        observations=[*state["observations"], obs],
        log=[*state["log"], f"record: {obs[:80]}"],
    )


@action(reads=["observations", "log"], writes=["escalation", "log"])
async def escalate(state: State, reason: str) -> State:
    """Escalate the incident. REQUIRED before any remediation. The reason is
    appended to the memory timeline.

    Args:
        reason: why this needs escalation (severity, blast radius, on-call).
    """
    why = reason.strip()
    if len(why) < 12:
        raise ValueError("escalation needs a substantive reason (>=12 chars)")
    await call_upstream(
        "memory",
        "add_observations",
        {"observations": [{"entityName": _INCIDENT, "contents": [f"ESCALATED: {why}"]}]},
    )
    return state.update(
        escalation=why,
        log=[*state["log"], f"escalate: {why[:80]}"],
    )


@action(reads=["escalation", "log"], writes=["remediation", "log"])
async def remediate(state: State, fix: str, reason: str) -> State:
    """Propose a remediation. GATED: refused unless the incident has already
    been escalated AND you give a reason tying the fix to the diagnosis.

    Args:
        fix: the remediation action to take.
        reason: why this fix addresses the diagnosed root cause.
    """
    if not state["escalation"]:
        raise ValueError("remediate is gated: escalate the incident first")
    if not fix.strip():
        raise ValueError("fix must not be empty")
    if len(reason.strip()) < 12:
        raise ValueError("remediation needs a reason tying it to the diagnosis (>=12 chars)")
    note = f"REMEDIATION: {fix.strip()} (rationale: {reason.strip()})"
    await call_upstream(
        "memory",
        "add_observations",
        {"observations": [{"entityName": _INCIDENT, "contents": [note]}]},
    )
    return state.update(
        remediation={"fix": fix.strip(), "reason": reason.strip()},
        log=[*state["log"], f"remediate: {fix.strip()[:80]}"],
    )


@action(reads=["observations", "hypotheses", "log"], writes=["phase", "report", "log"])
async def report(state: State, summary: str) -> State:
    """Terminal. Write the incident report. GATED: refused without at least
    one recorded observation in the timeline.

    Args:
        summary: the incident summary (root cause, impact, remediation).
    """
    if not state["observations"]:
        raise ValueError("report is gated: record at least one observation first")
    if len(summary.strip()) < 80:
        raise ValueError("summary must be a substantive report (>=80 chars)")
    await call_upstream(
        "memory",
        "add_observations",
        {"observations": [{"entityName": _INCIDENT, "contents": [f"REPORT: {summary.strip()}"]}]},
    )
    return state.update(
        phase="done",
        report=summary.strip(),
        log=[*state["log"], "report: incident report filed"],
    )


_OPEN = Condition.expr("phase != 'done'")


def build() -> ApplicationBuilder:
    """Return the UNBUILT ApplicationBuilder (Theodosia stamps the session id
    as the Burr app_id before building)."""
    return (
        ApplicationBuilder()
        .with_actions(
            triage=triage,
            inspect=inspect,
            hypothesize=hypothesize,
            record=record,
            escalate=escalate,
            remediate=remediate,
            report=report,
        )
        .with_transitions(
            ("triage", "inspect", _OPEN),
            ("inspect", "inspect", _OPEN),
            ("inspect", "hypothesize", _OPEN),
            ("hypothesize", "inspect", _OPEN),
            ("hypothesize", "record", _OPEN),
            ("inspect", "record", _OPEN),
            ("record", "inspect", _OPEN),
            ("record", "record", _OPEN),
            ("record", "hypothesize", _OPEN),
            ("record", "escalate", _OPEN),
            ("escalate", "remediate", _OPEN),
            ("escalate", "record", _OPEN),
            ("remediate", "report", _OPEN),
            ("record", "report", _OPEN),
        )
        .with_tracker(theodosia.tracker(_TRACKER_PROJECT))
        .with_state(
            phase="new",
            logs_dir="",
            index=None,
            reads=[],
            last_content=None,
            hypotheses=[],
            observations=[],
            escalation=None,
            remediation=None,
            report=None,
            log=[],
        )
        .with_entrypoint("triage")
    )


def build_server():
    return mount(
        build,
        mode=ServingMode.STEP,
        name="incident-agent",
        upstream={
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", _LOGS_DIR],
            },
            # Per-session isolation: the {session} placeholder gives every MCP
            # session its own memory file, so concurrent incidents never share
            # a timeline graph. (The filesystem upstream is read-only, so it
            # stays shared.)
            "memory": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-memory"],
                "env": {**os.environ, "MEMORY_FILE_PATH": f"{_MEMORY_DIR}/{{session}}.json"},
            },
        },
        instructions=(
            "An auditable incident-response runbook FSM. It drives a filesystem "
            "MCP server (to read logs) and a memory MCP server (to record an "
            "incident timeline) THROUGH this server; you are given neither set "
            "of tools directly, only `step`. Walk the runbook: triage() indexes "
            "the logs and opens the incident; inspect(logfile) returns one "
            "log's text (loop over app.log, gc.log, deploy.log); "
            "hypothesize(cause) states a root cause; record(observation, "
            "evidence) appends a finding to the memory timeline (you must do "
            "this at least once); escalate(reason) is REQUIRED before any "
            "remediation; remediate(fix, reason) is refused without a prior "
            "escalation and its own reason; report(summary) is terminal and "
            "refused without a recorded observation. After each step read "
            "state.last_content (the log you just read) and state.current_prompt."
        ),
    )


if __name__ == "__main__":
    build_server().run()
