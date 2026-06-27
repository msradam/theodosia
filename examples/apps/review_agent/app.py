"""Auditable code-review agent: a Burr FSM mounted via Theodosia that drives a
real upstream filesystem MCP server.

The agent connects to ONLY this server and sees ONLY the ``step`` tool. It never
receives filesystem tools. Every file read happens inside an action body via
``call_upstream("filesystem", ...)``, so each read advances FSM state and lands
in the tamper-evident tracker ledger. The gates (read-before-flag, two findings
before sign-off) are enforced in Python, not in the prompt, so the audit trail
is the product.

Review target: ``$REVIEW_TARGET`` (defaults to the shipped vuln_demo).

Serve over stdio (this wires the filesystem upstream; see README for why the
stock ``theodosia serve`` CLI cannot):

    python examples/apps/review_agent/app.py

Then connect an agent to ONLY this server (see README for the Haiku driver).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from burr.core import ApplicationBuilder, Condition, State, action

import theodosia
from theodosia import ServingMode, call_upstream, mount

_TRACKER_PROJECT = "review-agent"
_MIN_FLAGS = 2
_MIN_REPORT_CHARS = 120
_SEVERITIES = ("critical", "high", "medium", "low", "info")
_DEFAULT_TARGET = str(Path(__file__).parents[2] / "data" / "codebase_security" / "vuln_demo")


def _review_target() -> str:
    return os.environ.get("REVIEW_TARGET", _DEFAULT_TARGET)


def _tracker_storage_dir() -> str:
    return os.environ.get(
        "REVIEW_TRACKER_DIR",
        str(Path(tempfile.gettempdir()) / "review-agent-tracker"),
    )


@action(reads=[], writes=["target", "phase", "tree", "reads", "flags", "log"])
async def open_review(state: State) -> State:
    """Open the review and survey the target tree via the filesystem server."""
    target = _review_target()
    tree = await call_upstream("filesystem", "directory_tree", {"path": target})
    return state.update(
        target=target,
        phase="reviewing",
        tree=tree,
        reads=[],
        flags=[],
        log=[f"review opened: {target}"],
    )


@action(reads=["target", "reads", "log"], writes=["reads", "last_content", "log"])
async def read_file(state: State, path: str) -> State:
    """Read a file via the filesystem server. Its content is returned in the
    step result so you can judge it, and the read is recorded so you may later
    flag issues in it.

    Args:
        path: absolute path to a file within the review target.
    """
    if not path.strip():
        raise ValueError("path must not be empty")
    content = await call_upstream("filesystem", "read_file", {"path": path.strip()})
    text = content if isinstance(content, str) else str(content)
    return state.update(
        reads=[*state["reads"], path.strip()],
        last_content=text[:4000],
        log=[*state["log"], f"read {path.strip()}"],
    )


@action(reads=["reads", "flags", "log"], writes=["flags", "log"])
async def flag(state: State, path: str, issue: str, severity: str = "info") -> State:
    """Flag a security issue in a file you have read.

    Args:
        path: the file the issue is in (you must have read it first).
        issue: what is wrong and why it matters.
        severity: one of critical | high | medium | low | info.
    """
    if not path.strip() or not issue.strip():
        raise ValueError("path and issue must both be non-empty")
    if path.strip() not in state["reads"]:
        raise ValueError(
            f"flag an issue only for a file you read first; you have not read "
            f"{path.strip()!r}. Call read_file on it before flagging."
        )
    sev = severity.strip().lower()
    if sev not in _SEVERITIES:
        raise ValueError(f"severity must be one of {'/'.join(_SEVERITIES)}")
    return state.update(
        flags=[
            *state["flags"],
            {"path": path.strip(), "issue": issue.strip(), "severity": sev},
        ],
        log=[*state["log"], f"flag {path.strip()}: {sev}"],
    )


@action(reads=["target", "flags", "log"], writes=["phase", "report", "log"])
async def summarize(state: State, report: str) -> State:
    """Terminal. Compile the review report. Requires >= 2 flags and a
    substantive markdown report.

    Args:
        report: a markdown summary of the findings and recommended fixes.
    """
    if len(state["flags"]) < _MIN_FLAGS:
        raise ValueError(
            f"summarize requires >= {_MIN_FLAGS} flags; you have "
            f"{len(state['flags'])}. Read and flag more files first."
        )
    if len(report.strip()) < _MIN_REPORT_CHARS:
        raise ValueError(
            f"report must be a substantive markdown summary (>= {_MIN_REPORT_CHARS} chars)"
        )
    return state.update(
        phase="done",
        report=report.strip(),
        log=[*state["log"], f"report written ({len(state['flags'])} flags)"],
    )


_OPEN = Condition.expr("phase != 'done'")


def build() -> ApplicationBuilder:
    """Return an UNBUILT ApplicationBuilder. Theodosia stamps app_id=session_id
    and builds it per session (the builder seam)."""
    return (
        ApplicationBuilder()
        .with_actions(
            open_review=open_review,
            read_file=read_file,
            flag=flag,
            summarize=summarize,
        )
        .with_transitions(
            ("open_review", "read_file", _OPEN),
            ("read_file", "read_file", _OPEN),
            ("read_file", "flag", _OPEN),
            ("flag", "read_file", _OPEN),
            ("flag", "flag", _OPEN),
            ("flag", "summarize", _OPEN),
            ("read_file", "summarize", _OPEN),
        )
        .with_tracker(theodosia.tracker(_TRACKER_PROJECT, storage_dir=_tracker_storage_dir()))
        .with_state(
            target="",
            phase="new",
            tree=None,
            reads=[],
            last_content=None,
            flags=[],
            report=None,
            log=[],
        )
        .with_entrypoint("open_review")
    )


def build_server():
    target = _review_target()
    return mount(
        build,
        mode=ServingMode.STEP,
        name="review-agent",
        upstream={
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", target],
            }
        },
        instructions=(
            "An auditable code-review FSM that drives a filesystem MCP server "
            "THROUGH this server (you are not given filesystem tools directly). "
            "Walk: open_review() surveys the tree; read_file(path) returns a "
            "file's content; flag(path, issue, severity) records a security "
            "issue (you must read the file first; severity is one of "
            "critical/high/medium/low/info); summarize(report) finishes (needs "
            ">= 2 flags and a substantive markdown report). Read "
            "state.current_prompt and state.last_content after each step."
        ),
    )


if __name__ == "__main__":
    build_server().run()
