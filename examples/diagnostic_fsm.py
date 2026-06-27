"""Incident-diagnosis FSM: a branching SRE triage machine.

Shape:

    triage --> check_cpu ----\\
           --> check_memory --+--> form_hypothesis --> [gather_evidence /
           --> check_disk ----+                         correlate_logs loop]
                              |                              --> remediate --> conclude
                              \\-> (check_disk only) quarantine_host  [DEAD END]

Features it exercises:

* Real branching: from ``triage`` the agent picks one of three competing
  hypotheses (``check_cpu`` / ``check_memory`` / ``check_disk``). All three
  are legal next actions; only the agent's reasoning over the symptoms in
  ``state`` tells it which is right.
* A dead-end branch: investigating disk leads to ``quarantine_host``, which
  reaches the ``quarantined`` phase with *no outgoing transition*. The agent
  is stuck and must ``fork_at`` an earlier step to recover.
* Conditional transitions via ``Condition.expr`` on ``phase`` and the running
  ``confidence`` score.
* An evidence loop: ``gather_evidence`` / ``correlate_logs`` repeat, each
  raising ``confidence`` until it crosses the ``remediate`` threshold.
* The builder seam: ``build`` returns an *unbuilt* ``ApplicationBuilder`` so
  Theodosia can stamp ``app_id = session_id`` before building.

Ten actions, two terminal stages (``concluded`` and the ``quarantined``
dead end). No real systems are touched: actions update state only.

Run as a server:

    python examples/diagnostic_fsm.py
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ServingMode, mount, tracker

# Simulated telemetry the FSM reasons over. The truth: a memory leak.
# CPU and disk look normal; memory shows OOM kills. No real probes run.
_TELEMETRY = {
    "oom_kills_last_hour": 7,
    "rss_growth_mb_per_min": 42.0,
    "cpu_utilization_pct": 31,
    "load_average": 1.2,
    "disk_used_pct": 44,
    "inode_used_pct": 12,
}
_CONFIDENCE_STEP = 0.2
_REMEDIATE_THRESHOLD = 0.8


@action(reads=[], writes=["phase", "incident", "telemetry"])
def triage(state: State, incident: str) -> State:
    """Open the incident and pull telemetry. Read-only entrypoint.

    Args:
        incident: One-line incident summary, e.g. "api pods OOMKilled,
            latency spiking". Recorded for the postmortem.
    """
    return state.update(
        phase="triaged",
        incident=incident,
        telemetry=dict(_TELEMETRY),
    )


@action(reads=["telemetry"], writes=["phase", "cpu_finding"])
def check_cpu(state: State) -> State:
    """Investigate CPU saturation as the cause. One of three hypotheses."""
    tel = state["telemetry"]
    hot = tel["cpu_utilization_pct"] > 85 or tel["load_average"] > 8
    finding = "cpu saturated" if hot else "cpu normal, not the cause"
    return state.update(phase="checked_cpu", cpu_finding=finding)


@action(reads=["telemetry"], writes=["phase", "memory_finding"])
def check_memory(state: State) -> State:
    """Investigate a memory leak as the cause. One of three hypotheses."""
    tel = state["telemetry"]
    leaking = tel["oom_kills_last_hour"] > 0 and tel["rss_growth_mb_per_min"] > 5
    finding = (
        "memory leak suspected: OOM kills with monotonic RSS growth"
        if leaking
        else "memory normal, not the cause"
    )
    return state.update(phase="checked_mem", memory_finding=finding)


@action(reads=["telemetry"], writes=["phase", "disk_finding"])
def check_disk(state: State) -> State:
    """Investigate disk/inode exhaustion. The misleading branch.

    Disk looks normal, but the only action reachable from here is
    ``quarantine_host`` -- a dead end. An agent that chases disk must
    ``fork_at`` back to ``triage`` to recover.
    """
    tel = state["telemetry"]
    full = tel["disk_used_pct"] > 90 or tel["inode_used_pct"] > 90
    finding = "disk pressure" if full else "disk normal, not the cause"
    return state.update(phase="checked_disk", disk_finding=finding)


@action(reads=["phase"], writes=["phase"])
def quarantine_host(state: State) -> State:
    """Cordon the host. DEAD END: reaches ``quarantined`` with no way forward.

    Quarantining was the wrong move; nothing useful follows. The agent must
    ``fork_at`` an earlier step (``triage``) and pick a real hypothesis.
    """
    return state.update(phase="quarantined")


@action(
    reads=["cpu_finding", "memory_finding"],
    writes=["phase", "hypothesis", "confidence"],
)
def form_hypothesis(state: State, hypothesis: str) -> State:
    """Commit to a root-cause hypothesis from a productive check.

    Args:
        hypothesis: The suspected root cause in one line, drawn from the
            ``*_finding`` written by ``check_cpu`` / ``check_memory``.
    """
    return state.update(phase="hypothesized", hypothesis=hypothesis, confidence=0.4)


@action(reads=["confidence"], writes=["phase", "confidence", "evidence"])
def gather_evidence(state: State, source: str) -> State:
    """Pull one more piece of evidence. Loops, raising ``confidence``.

    Args:
        source: Where the evidence came from, e.g. "heap profile",
            "pprof", "grafana dashboard".
    """
    evidence = [*(state.get("evidence") or []), source]
    confidence = min(1.0, state["confidence"] + _CONFIDENCE_STEP)
    return state.update(phase="gathering", confidence=confidence, evidence=evidence)


@action(reads=["confidence"], writes=["phase", "confidence", "evidence"])
def correlate_logs(state: State, window: str) -> State:
    """Correlate logs over a time window. Alternative evidence loop.

    Args:
        window: Time window to correlate, e.g. "last 30m", "since deploy".
    """
    evidence = [*(state.get("evidence") or []), f"logs:{window}"]
    confidence = min(1.0, state["confidence"] + _CONFIDENCE_STEP)
    return state.update(phase="gathering", confidence=confidence, evidence=evidence)


@action(reads=["hypothesis", "confidence"], writes=["phase", "fix", "reason"])
def remediate(state: State, fix: str, reason: str) -> State:
    """Apply the fix. Gated: only reachable once ``confidence`` >= 0.8.

    Args:
        fix: The remediation applied, e.g. "roll back deploy 1841",
            "bump memory limit and restart".
        reason: Why this fix follows from the evidence.
    """
    if not reason or not reason.strip():
        raise ValueError("remediate requires a stated reason")
    return state.update(phase="remediated", fix=fix, reason=reason.strip())


@action(reads=["hypothesis", "fix"], writes=["phase", "root_cause"])
def conclude(state: State, root_cause: str) -> State:
    """Close the incident with a root-cause statement. Terminal.

    Args:
        root_cause: The confirmed root cause for the postmortem.
    """
    return state.update(phase="concluded", root_cause=root_cause)


def build() -> ApplicationBuilder:
    """Build the incident-diagnosis Burr Application *builder* (unbuilt)."""
    triaged = Condition.expr("phase == 'triaged'")
    checked_cpu = Condition.expr("phase == 'checked_cpu'")
    checked_mem = Condition.expr("phase == 'checked_mem'")
    checked_disk = Condition.expr("phase == 'checked_disk'")
    hypothesized = Condition.expr("phase == 'hypothesized'")
    more_evidence = Condition.expr("confidence < 0.8")
    enough_evidence = Condition.expr("confidence >= 0.8")
    remediated = Condition.expr("phase == 'remediated'")
    return (
        ApplicationBuilder()
        .with_actions(
            triage=triage,
            check_cpu=check_cpu,
            check_memory=check_memory,
            check_disk=check_disk,
            quarantine_host=quarantine_host,
            form_hypothesis=form_hypothesis,
            gather_evidence=gather_evidence,
            correlate_logs=correlate_logs,
            remediate=remediate,
            conclude=conclude,
        )
        .with_transitions(
            # Three competing hypotheses, all legal from triage.
            ("triage", "check_cpu", triaged),
            ("triage", "check_memory", triaged),
            ("triage", "check_disk", triaged),
            # Productive checks lead to a hypothesis.
            ("check_cpu", "form_hypothesis", checked_cpu),
            ("check_memory", "form_hypothesis", checked_mem),
            # Disk is the trap: its only successor is the dead end.
            ("check_disk", "quarantine_host", checked_disk),
            # quarantine_host -> quarantined has NO outgoing transition.
            # Evidence loop, raising confidence toward the remediate gate.
            ("form_hypothesis", "gather_evidence", hypothesized),
            ("form_hypothesis", "correlate_logs", hypothesized),
            ("gather_evidence", "gather_evidence", more_evidence),
            ("gather_evidence", "correlate_logs", more_evidence),
            ("correlate_logs", "gather_evidence", more_evidence),
            ("correlate_logs", "correlate_logs", more_evidence),
            ("gather_evidence", "remediate", enough_evidence),
            ("correlate_logs", "remediate", enough_evidence),
            ("remediate", "conclude", remediated),
        )
        .with_tracker(tracker("diag-dogfood"))
        .with_state(phase="new")
        .with_entrypoint("triage")
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount the diagnosis builder factory as an MCP server."""
    return mount(
        build,
        mode=mode,
        name="diag",
        instructions=(
            "An incident-diagnosis FSM. Walk: triage(incident) -> pick ONE "
            "hypothesis from valid_next_actions (check_cpu / check_memory / "
            "check_disk) -> form_hypothesis(hypothesis) -> [gather_evidence / "
            "correlate_logs loop until confidence >= 0.8] -> "
            "remediate(fix, reason) -> conclude(root_cause). Beware: check_disk "
            "leads only to quarantine_host, a DEAD END with no next action; "
            "if you get stuck there, fork_at an earlier step to recover. Read "
            "theodosia://state for telemetry and findings; theodosia://next for "
            "legal next actions; theodosia://graph/mermaid for the shape."
        ),
    )


if __name__ == "__main__":
    server = build_server()
    server.run()
