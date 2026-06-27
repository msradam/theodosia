"""Git-workflow FSM: a small approval-gated example.

Shape:

    status --> stage --> commit --> push
                  \\-> stage (loop)

Features it exercises:

* A linear write pipeline (``status`` is read-only; ``stage`` / ``commit`` /
  ``push`` mutate the working tree, index, and remote in simulation).
* A ``stage`` self-loop so the agent can stage more than one path.
* An escalation gate: ``push`` is the only action that touches the remote, so
  it refuses unless the caller states a ``reason``. Read-only by default;
  the write to the remote needs a stated justification.
* The builder seam: ``build_git_agent`` returns an *unbuilt*
  ``ApplicationBuilder``. Theodosia stamps ``app_id = session_id`` before
  building, so each MCP session's tracker dir is stable and named for the
  session.

Four actions, one terminal stage (``pushed``).

Run as a server:

    python examples/git_agent.py

Inspect from another shell with the FastMCP client or any MCP client.
``theodosia://state`` shows the working-tree shape; ``theodosia://next``
lists the legal next actions; ``theodosia://graph/mermaid`` renders the FSM.

No real git side effects: the actions update state only. Nothing is staged,
committed, or pushed for real.
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from theodosia import ServingMode, mount, tracker

# Simulated working-tree contents the FSM reasons over. No git is invoked.
_DIRTY_FILES = ["src/app.py", "README.md", "tests/test_app.py"]


@action(reads=[], writes=["phase", "branch", "changed_files"])
def status(state: State, branch: str = "main") -> State:
    """Inspect the working tree. Read-only entrypoint.

    Args:
        branch: Branch the simulated repo is on; defaults to ``"main"``.
    """
    return state.update(
        phase="inspected",
        branch=branch,
        changed_files=list(_DIRTY_FILES),
    )


@action(reads=["changed_files", "staged_files"], writes=["phase", "staged_files"])
def stage(state: State, paths: list[str] | None = None) -> State:
    """Stage paths from the working tree. Loops to stage more.

    Args:
        paths: Paths to stage; defaults to every changed file. Unknown
            paths are ignored (git would error; the sim is forgiving).
    """
    changed = state["changed_files"]
    already = state.get("staged_files") or []
    selected = paths if paths is not None else changed
    staged = list(dict.fromkeys([*already, *(p for p in selected if p in changed)]))
    return state.update(phase="staged", staged_files=staged)


@action(reads=["staged_files"], writes=["phase", "commits", "head"])
def commit(state: State, message: str) -> State:
    """Commit the staged files.

    Args:
        message: Commit message. Required; an empty index is rejected.
    """
    staged = state.get("staged_files") or []
    if not staged:
        raise ValueError("nothing staged; call stage before commit")
    commits = [*(state.get("commits") or []), {"message": message, "files": staged}]
    head = f"commit-{len(commits)}"
    return state.update(phase="committed", commits=commits, head=head, staged_files=[])


@action(reads=["phase", "commits", "branch"], writes=["phase", "pushed", "reason"])
def push(state: State, reason: str) -> State:
    """Push commits to the remote. Terminal. Requires a stated reason.

    This is the only action that affects anything outside the local repo, so
    it escalates: a blank ``reason`` is refused. The caller must justify the
    write before it touches the remote.

    Args:
        reason: Why this push should go through (e.g. "hotfix for prod
            incident"). Must be non-empty.
    """
    if not reason or not reason.strip():
        raise ValueError("push requires a stated reason; refusing to push to the remote")
    return state.update(phase="pushed", pushed=True, reason=reason.strip())


def build_git_agent() -> ApplicationBuilder:
    """Build the git-workflow Burr Application *builder* (unbuilt).

    Returns the ``ApplicationBuilder`` rather than a built ``Application`` so
    Theodosia's builder seam can stamp ``app_id = session_id`` before build,
    pinning each session's tracker dir under ``~/.git-agent/git-agent/``.

    Uses ``theodosia.tracker("git-agent")``, which defaults ``storage_dir`` to
    ``~/.git-agent`` — the same path a branded ``git-agent`` CLI derives — so
    ``git-agent ui`` / ``render`` / ``sessions`` find these runs without a
    working-directory dependency.
    """
    inspected = Condition.expr("phase == 'inspected'")
    staged = Condition.expr("phase == 'staged'")
    committed = Condition.expr("phase == 'committed'")
    return (
        ApplicationBuilder()
        .with_actions(status=status, stage=stage, commit=commit, push=push)
        .with_transitions(
            ("status", "stage", inspected),
            ("stage", "stage", staged),
            ("stage", "commit", staged),
            ("commit", "push", committed),
        )
        .with_tracker(tracker("git-agent"))
        .with_state(phase="new")
        .with_entrypoint("status")
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount the git-workflow builder factory as an MCP server."""
    return mount(
        build_git_agent,
        mode=mode,
        name="git-agent",
        instructions=(
            "A git-workflow FSM. Walk: status(branch?) -> "
            "[stage(paths?) loop] -> commit(message) -> push(reason). "
            "push touches the remote and refuses without a stated reason. "
            "Read theodosia://state for the working tree; theodosia://next "
            "for legal next actions; theodosia://graph/mermaid for the shape."
        ),
    )


if __name__ == "__main__":
    server = build_server()
    server.run()
