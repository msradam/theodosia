"""Research orchestrator that spawns Burr-native child sub-applications.

Where ``parallel_research.py`` fans out through Theodosia's own
``spawn_subapp`` helper (which indexes runs under
``theodosia://subruns``), this example uses Burr's *native* spawn link.
The ``dispatch`` action reads the running ``ApplicationContext`` and
builds each worker sub-Application with
``with_spawning_parent(app_id, sequence_id)``. Burr records that link in
the parent's tracker dir as ``children.jsonl``, which Theodosia surfaces
at ``theodosia://children``; the ancestor side (``parent`` /
``spawning_parent`` pointers) shows up at ``theodosia://session``.

FSM shape:

    plan --> dispatch --> collect --> synthesize

* ``plan`` (entrypoint): records the topic and the subtopics to fan out.
* ``dispatch``: spawns one worker sub-Application per subtopic via
  ``with_spawning_parent``, runs each to completion, and keeps the child
  ``app_id``s and findings.
* ``collect``: folds the per-child findings into one ordered list.
* ``synthesize`` (terminal): renders the combined report.

Each worker is a two-step sub-FSM (``investigate -> conclude``) that
"researches" its subtopic. The research bodies are simulated (hash-based,
deterministic, no network), so the example is hermetic.

Both parent and children write to the same throwaway tracker project and
storage dir, which is required: Burr logs the child link into
``<storage_dir>/<project>/<parent_app_id>/children.jsonl``, so the child
tracker has to share the parent's storage base and project for the link
(and ``theodosia://children``) to resolve. The storage dir is a unique
temp directory per process so concurrent dogfood runs don't collide.

Run as a stdio server:

    uv run python examples/fanout_research.py

Or through the CLI builder seam (stamps ``app_id = session_id``):

    theodosia serve fanout_research:build --app-dir examples

Try:

    plan(topic="vector databases")
    dispatch()
    collect()
    synthesize()
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from pathlib import Path

from burr.core import ApplicationBuilder, State, action
from burr.core.application import ApplicationContext

from theodosia import ServingMode, mount, tracker

_TRACKER_PROJECT = "fanout-dogfood"
_STORAGE_DIR = str(Path(tempfile.gettempdir()) / f"fanout-dogfood-{uuid.uuid4().hex[:8]}")

_DEFAULT_SUBTOPICS: tuple[str, ...] = ("architecture", "tradeoffs", "alternatives")


# ── simulated research primitives (pure, deterministic) ─────────────


def _digest(text: str) -> str:
    """Stable short hex digest for a string. Stands in for a real search."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _confidence(topic: str, subtopic: str) -> int:
    """Deterministic 0-100 'confidence' score for a (topic, subtopic)."""
    return int(_digest(f"{topic}::{subtopic}"), 16) % 101


# ── worker sub-FSM: investigate one subtopic, then conclude ─────────
#
# These MUST be module-level functions: Burr's tracker calls
# inspect.getsource on every child action when the child is built, so a
# lambda or local closure would raise OSError at build time.


@action(reads=["topic", "subtopic"], writes=["notes"])
def investigate(state: State) -> State:
    """Gather simulated notes for this worker's subtopic."""
    topic = state["topic"]
    subtopic = state["subtopic"]
    notes = [f"source:{_digest(topic + subtopic + str(i))} on {subtopic}" for i in range(3)]
    return state.update(notes=notes)


@action(reads=["topic", "subtopic", "notes"], writes=["finding"])
def conclude(state: State) -> State:
    """Fold the notes into a single finding for this subtopic."""
    topic = state["topic"]
    subtopic = state["subtopic"]
    finding = {
        "subtopic": subtopic,
        "confidence": _confidence(topic, subtopic),
        "n_sources": len(state["notes"]),
        "summary": f"{subtopic} of {topic!r}: reviewed {len(state['notes'])} sources.",
    }
    return state.update(finding=finding)


def _build_worker(topic: str, subtopic: str, ctx: ApplicationContext):
    """Build and return a worker sub-Application linked to its spawner.

    ``with_spawning_parent`` records the parent app_id and the sequence_id
    at which the spawn happened, so Burr writes the child link into the
    parent's ``children.jsonl``. The worker shares the parent's tracker
    project and storage dir so that link lands where Theodosia reads it.
    """
    return (
        ApplicationBuilder()
        .with_actions(investigate=investigate, conclude=conclude)
        .with_transitions(("investigate", "conclude"))
        .with_spawning_parent(app_id=ctx.app_id, sequence_id=ctx.sequence_id)
        .with_tracker(tracker(_TRACKER_PROJECT, storage_dir=_STORAGE_DIR))
        .with_state(topic=topic, subtopic=subtopic, notes=[], finding=None)
        .with_entrypoint("investigate")
        .build()
    )


# ── parent orchestrator ─────────────────────────────────────────────


@action(reads=[], writes=["topic", "subtopics", "phase"])
def plan(state: State, topic: str, subtopics: list[str] | None = None) -> State:
    """Record the research topic and the subtopics to fan out across.

    Args:
        topic: The thing to research. Threaded into each worker.
        subtopics: Optional list of subtopic names. Defaults to
            ``architecture``, ``tradeoffs``, ``alternatives``. Each spawns
            one worker sub-Application.
    """
    if not topic or not topic.strip():
        raise ValueError("topic must be a non-empty string")
    chosen = list(subtopics) if subtopics else list(_DEFAULT_SUBTOPICS)
    if not chosen:
        raise ValueError("must plan at least one subtopic")
    return state.update(topic=topic.strip(), subtopics=chosen, phase="planned")


@action(
    reads=["topic", "subtopics", "phase"],
    writes=["child_app_ids", "child_findings", "parent_app_id", "phase"],
)
def dispatch(state: State) -> State:
    """Spawn one Burr-native worker per subtopic and run each to completion.

    Reads the live ``ApplicationContext`` to link every child back to this
    run via ``with_spawning_parent``. Each child gets its own ``app_id``;
    the parent ``app_id`` is unchanged. The spawn links are recorded in
    this run's ``children.jsonl`` (surfaced at ``theodosia://children``).
    """
    if state["phase"] != "planned":
        raise RuntimeError(f"dispatch requires phase=='planned', got {state['phase']!r}")
    ctx = ApplicationContext.get()
    if ctx is None:
        raise RuntimeError("no ApplicationContext; cannot establish spawn lineage")
    topic = state["topic"]
    child_app_ids: list[str] = []
    child_findings: list[dict] = []
    for subtopic in state["subtopics"]:
        worker = _build_worker(topic, subtopic, ctx)
        worker.run(halt_after=["conclude"])
        child_app_ids.append(worker.uid)
        child_findings.append(worker.state["finding"])
    return state.update(
        child_app_ids=child_app_ids,
        child_findings=child_findings,
        parent_app_id=ctx.app_id,
        phase="dispatched",
    )


@action(reads=["child_findings", "phase"], writes=["collected", "phase"])
def collect(state: State) -> State:
    """Order the per-child findings by confidence, highest first."""
    if state["phase"] != "dispatched":
        raise RuntimeError(f"collect requires phase=='dispatched', got {state['phase']!r}")
    collected = sorted(state["child_findings"], key=lambda f: -f["confidence"])
    return state.update(collected=collected, phase="collected")


@action(reads=["topic", "collected", "child_app_ids", "phase"], writes=["report", "phase"])
def synthesize(state: State) -> State:
    """Terminal: render the combined report over every worker's finding."""
    if state["phase"] != "collected":
        raise RuntimeError(f"synthesize requires phase=='collected', got {state['phase']!r}")
    topic = state["topic"]
    lines = [f"Research report on {topic!r} ({len(state['child_app_ids'])} workers spawned):"]
    lines.extend(
        f"  - [{finding['confidence']:>3}] {finding['subtopic']}: {finding['summary']}"
        for finding in state["collected"]
    )
    return state.update(report="\n".join(lines), phase="done")


def build() -> ApplicationBuilder:
    """Build the orchestrator *builder* (unbuilt) for the CLI builder seam.

    Returns the ``ApplicationBuilder`` rather than a built ``Application``
    so Theodosia can stamp ``app_id = session_id`` before build, keeping
    each MCP session's parent app_id stable while spawned children get
    their own ids.
    """
    return (
        ApplicationBuilder()
        .with_actions(
            plan=plan,
            dispatch=dispatch,
            collect=collect,
            synthesize=synthesize,
        )
        .with_transitions(
            ("plan", "dispatch"),
            ("dispatch", "collect"),
            ("collect", "synthesize"),
        )
        .with_tracker(tracker(_TRACKER_PROJECT, storage_dir=_STORAGE_DIR))
        .with_state(
            topic=None,
            subtopics=None,
            child_app_ids=None,
            child_findings=None,
            parent_app_id=None,
            collected=None,
            report=None,
            phase="initial",
        )
        .with_entrypoint("plan")
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount the orchestrator builder factory as an MCP server."""
    return mount(
        build,
        mode=mode,
        name="fanout-research",
        instructions=(
            "A research orchestrator that spawns Burr-native child "
            "sub-applications. Walk: plan(topic, subtopics?) -> dispatch "
            "-> collect -> synthesize. dispatch spawns one worker "
            "sub-Application per subtopic via with_spawning_parent, so the "
            "spawn links appear at theodosia://children (child app_ids, "
            "event_type=spawn_start) and the lineage at theodosia://session "
            "(parent / spawning_parent pointers). Read theodosia://state "
            "for phase/findings; theodosia://next for legal next actions."
        ),
    )


if __name__ == "__main__":
    build_server().run()
