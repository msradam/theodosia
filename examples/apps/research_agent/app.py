"""An auditable research agent: a Burr FSM mounted via Theodosia that fetches
real web content through an upstream *fetch* MCP server and writes a cited
report through an upstream *filesystem* MCP server.

The agent connects to ONLY this Theodosia server and sees ONLY ``step``. It
never gets a fetch tool or a filesystem tool. The research actions call the
upstream servers from inside their bodies via ``call_upstream``, so every fetch
and every disk write advances FSM state and is recorded by the tracker. The
result is a research transcript you can audit: which URLs were fetched, which
claims cite which fetched source, and the final report that was persisted.

Two gates make the output trustworthy:

* ``extract(claim, source)`` rejects any claim that does not cite a source the
  FSM actually fetched (status OK). You cannot cite a page you never read.
* ``write_report`` / ``synthesize`` require >= 2 cited claims drawn from >= 2
  distinct fetched sources, plus substantive markdown. A small model cannot
  shortcut to a one-source or zero-citation report.

Upstreams (started by ``mount(upstream=...)``):

* ``fetch``  -> ``uvx mcp-server-fetch`` (official Python fetch server). Its
  ``fetch`` tool returns a URL as markdown. If it is unavailable, the FSM falls
  back to reading pre-seeded ``sources/*.md`` files THROUGH the filesystem
  upstream, so the workflow still runs hermetically.
* ``files`` -> ``npx @modelcontextprotocol/server-filesystem <out_dir>``. Its
  ``write_file`` tool persists the final report.

Run as a stdio MCP server, either through the CLI seam::

    theodosia serve app:build_server --app-dir examples/apps/research_agent

or directly::

    python examples/apps/research_agent/app.py

``build()`` returns an unbuilt ``ApplicationBuilder`` so the builder seam in
``mount`` stamps ``app_id = session_id`` per session. ``serve`` is pointed at
``build_server`` (not ``build``) because the upstreams are declared at mount
time: ``serve`` runs the returned FastMCP as-is, preserving its ``upstream=``
config. Connect an agent that uses only ``mcp__research-agent__step``.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from burr.core import ApplicationBuilder, Condition, State, action

import theodosia
from theodosia import ServingMode, mount
from theodosia.upstream import ERROR, OK, UpstreamError

_TRACKER_PROJECT = "research-agent"
_STORAGE_DIR = str(Path(tempfile.gettempdir()) / f"research-agent-{uuid.uuid4().hex[:8]}")

_DATA_DIR = Path(__file__).parent
_SOURCES_DIR = os.path.realpath(str(_DATA_DIR / "sources"))

_MIN_CLAIMS = 2
_MIN_DISTINCT_SOURCES = 2
_MIN_REPORT_CHARS = 120
_CONTENT_CAP = 6000
_SNIPPET = 600


def _out_dir() -> str:
    """Directory the filesystem upstream is allowed to write into.

    ``os.path.realpath`` resolves macOS' ``/var`` -> ``/private/var`` symlink;
    the filesystem MCP server rejects paths that don't match its allowed root
    after symlink resolution, so the resolved form is what we must serve and
    write under.
    """
    raw = os.environ.get("RESEARCH_OUT_DIR") or str(
        Path(tempfile.gettempdir()) / f"research-out-{uuid.uuid4().hex[:8]}"
    )
    Path(raw).mkdir(parents=True, exist_ok=True)
    return os.path.realpath(raw)


_OUT_DIR = _out_dir()


def _render_report(question: str, claims: list[dict], sources: dict[str, dict]) -> str:
    """Assemble a cited markdown report from the recorded claims and sources."""
    lines = [f"# Research report: {question}", "", "## Cited findings", ""]
    for i, claim in enumerate(claims, 1):
        sid = claim["source"]
        url = sources.get(sid, {}).get("url", sid)
        lines.append(f"{i}. {claim['claim']} [{sid}: {url}]")
    lines += ["", "## Sources", ""]
    for sid, src in sources.items():
        if src.get("status") == OK:
            lines.append(f"- **{sid}**: {src['url']}")
    return "\n".join(lines) + "\n"


async def _retrieve(server: str, tool: str, args: dict) -> tuple[str, str, str]:
    """Call a text-returning upstream (fetch / filesystem read) and classify it.

    These servers return prose markdown or raw file text, not JSON, so the
    generic ``classify_payload`` (which flags any unstructured string as
    MALFORMED and any string mentioning "error"/"failed" as ERROR) is the wrong
    classifier here: a non-empty string IS the success case. Treat a non-empty
    string as ``OK``; an upstream exception or an empty/None payload as
    ``ERROR``. Returns ``(content, status, detail)``.
    """
    try:
        payload = await theodosia.call_upstream(server, tool, args)
    except UpstreamError as exc:
        return "", ERROR, f"upstream {server!r} unavailable: {exc}"[:_SNIPPET]
    except Exception as exc:
        return "", ERROR, f"{type(exc).__name__}: {exc}"[:_SNIPPET]
    text = payload if isinstance(payload, str) else ("" if payload is None else str(payload))
    if not text.strip():
        return "", ERROR, "empty response from upstream"
    return text, OK, ""


# ── actions ─────────────────────────────────────────────────────────


@action(reads=[], writes=["question", "phase", "sources", "claims", "log"])
async def frame(state: State, question: str) -> State:
    """Entry. Record the research question and open the investigation.

    Args:
        question: the question the report must answer from fetched sources.
    """
    if not question or not question.strip():
        raise ValueError("question must be a non-empty string")
    return state.update(
        question=question.strip(),
        phase="researching",
        sources={},
        claims=[],
        log=[f"framed: {question.strip()}"],
    )


@action(
    reads=["phase", "sources", "log"],
    writes=["sources", "last_fetch", "log"],
)
async def fetch_source(state: State, url: str, max_length: int = 5000) -> State:
    """Fetch a URL as markdown through the upstream fetch server and record it.

    The fetched content is returned to you in the step result (truncated) so
    you can read it and form claims, and the source is filed under an id like
    ``s1`` that ``extract`` will require you to cite. If the URL is a bare
    filename (no scheme), it is read from the pre-seeded ``sources/`` dir
    through the filesystem upstream instead (offline fallback).

    Args:
        url: an ``http(s)://`` URL, or a pre-seeded ``sources/`` filename.
        max_length: max characters to retrieve (the page is truncated upstream).
    """
    if not url or not url.strip():
        raise ValueError("url must be a non-empty string")
    url = url.strip()
    sources = dict(state["sources"])
    sid = f"s{len(sources) + 1}"

    server, tool, args = (
        ("fetch", "fetch", {"url": url, "max_length": max_length})
        if url.startswith(("http://", "https://"))
        else ("files", "read_file", {"path": os.path.join(_SOURCES_DIR, Path(url).name)})
    )
    content, status, detail = await _retrieve(server, tool, args)

    sources[sid] = {
        "url": url,
        "status": status,
        "detail": detail,
        "content": content[:_CONTENT_CAP],
    }
    surfaced = {
        "id": sid,
        "url": url,
        "status": status,
        "detail": detail,
        "chars": len(content),
        "snippet": content[:_SNIPPET],
    }
    return state.update(
        sources=sources,
        last_fetch=surfaced,
        log=[*state["log"], f"fetch {sid} <{url}> -> {status} ({len(content)} chars)"],
    )


@action(reads=["sources", "claims", "log"], writes=["claims", "log"])
async def extract(state: State, claim: str, source: str) -> State:
    """Record a claim, requiring it to cite a source you actually fetched.

    The ``source`` must be a fetched source id (``s1``, ``s2``, ...) whose
    fetch succeeded (status OK). Citing an unfetched or failed source is
    rejected: you cannot cite a page the FSM never read.

    Args:
        claim: a factual statement supported by the cited source.
        source: the id of a successfully fetched source (e.g. ``s1``).
    """
    if not claim or not claim.strip():
        raise ValueError("claim must be a non-empty statement")
    sid = (source or "").strip()
    src = state["sources"].get(sid)
    if src is None:
        known = ", ".join(state["sources"]) or "(none fetched yet)"
        raise ValueError(
            f"unknown source {sid!r}; cite a source you fetched first. "
            f"Fetched sources: {known}. Call fetch_source(url) before extract."
        )
    if src["status"] != OK:
        raise ValueError(
            f"source {sid!r} did not fetch successfully (status={src['status']}: "
            f"{src['detail']}). Cite only a source whose fetch returned OK content."
        )
    return state.update(
        claims=[*state["claims"], {"claim": claim.strip(), "source": sid}],
        log=[*state["log"], f"claim cites {sid}"],
    )


def _gate_citations(claims: list[dict], sources: dict[str, dict]) -> None:
    """Raise unless there are >= 2 claims drawn from >= 2 distinct OK sources."""
    if len(claims) < _MIN_CLAIMS:
        raise ValueError(
            f"need >= {_MIN_CLAIMS} cited claims; you have {len(claims)}. "
            "Fetch and extract more before reporting."
        )
    distinct = {c["source"] for c in claims if sources.get(c["source"], {}).get("status") == OK}
    if len(distinct) < _MIN_DISTINCT_SOURCES:
        raise ValueError(
            f"claims must cite >= {_MIN_DISTINCT_SOURCES} distinct fetched sources; "
            f"yours cite {len(distinct)} ({sorted(distinct)}). Fetch another source "
            "and extract a claim from it."
        )


@action(
    reads=["question", "sources", "claims", "phase", "log"],
    writes=["report_path", "phase", "log"],
)
async def write_report(state: State, filename: str = "report.md") -> State:
    """Persist the cited report to disk through the upstream filesystem server.

    Gated identically to ``synthesize``: requires >= 2 cited claims from >= 2
    distinct fetched sources. Assembles the markdown from the recorded claims
    (you don't pass prose here), writes it via the filesystem upstream, and
    returns the path written. Not terminal; you still call ``synthesize`` to
    finish.

    Args:
        filename: output filename within the served output directory.
    """
    _gate_citations(state["claims"], state["sources"])
    name = Path(filename.strip() or "report.md").name
    path = os.path.realpath(str(Path(_OUT_DIR) / name))
    body = _render_report(state["question"], state["claims"], state["sources"])
    try:
        await theodosia.call_upstream("files", "write_file", {"path": path, "content": body})
    except UpstreamError as exc:
        raise ValueError(f"could not persist report via filesystem upstream: {exc}") from exc
    return state.update(
        report_path=path,
        phase="persisted",
        log=[*state["log"], f"wrote report -> {path} ({len(body)} chars)"],
    )


@action(
    reads=["question", "sources", "claims", "report_path", "phase", "log"],
    writes=["report", "phase", "log"],
)
async def synthesize(state: State, report: str) -> State:
    """Terminal. Emit the final cited report.

    Gated: requires >= 2 cited claims from >= 2 distinct fetched sources, and a
    substantive markdown ``report`` that references the sources. The report you
    pass must mention each cited source id so the citations are auditable in
    the final artifact.

    Args:
        report: the final markdown report, citing fetched sources by id.
    """
    _gate_citations(state["claims"], state["sources"])
    text = (report or "").strip()
    if len(text) < _MIN_REPORT_CHARS:
        raise ValueError(
            f"report must be substantive markdown (>= {_MIN_REPORT_CHARS} chars); got {len(text)}."
        )
    cited = {c["source"] for c in state["claims"]}
    missing = [sid for sid in cited if sid not in text]
    if missing:
        raise ValueError(
            f"report must reference every cited source id; missing {missing}. "
            "Mention each source id (e.g. 's1') where you use its claim."
        )
    return state.update(
        report=text,
        phase="done",
        log=[*state["log"], f"synthesized report ({len(state['claims'])} claims)"],
    )


_OPEN = Condition.expr("phase != 'done'")


def build() -> ApplicationBuilder:
    """Unbuilt builder for the CLI builder seam (stamps app_id = session_id)."""
    return (
        ApplicationBuilder()
        .with_actions(
            frame=frame,
            fetch_source=fetch_source,
            extract=extract,
            write_report=write_report,
            synthesize=synthesize,
        )
        .with_transitions(
            ("frame", "fetch_source", _OPEN),
            ("fetch_source", "fetch_source", _OPEN),
            ("fetch_source", "extract", _OPEN),
            ("extract", "extract", _OPEN),
            ("extract", "fetch_source", _OPEN),
            ("extract", "write_report", _OPEN),
            ("extract", "synthesize", _OPEN),
            ("write_report", "write_report", _OPEN),
            ("write_report", "synthesize", _OPEN),
        )
        .with_tracker(theodosia.tracker(_TRACKER_PROJECT, storage_dir=_STORAGE_DIR))
        .with_state(
            question=None,
            phase="initial",
            sources={},
            claims=[],
            last_fetch=None,
            report_path=None,
            report=None,
            log=[],
        )
        .with_entrypoint("frame")
    )


def build_server(mode: ServingMode = ServingMode.STEP):
    """Mount the research FSM as an MCP server with fetch + filesystem upstreams."""
    return mount(
        build,
        mode=mode,
        name="research-agent",
        action_timeout_seconds=45,
        upstream={
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
            "files": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", _OUT_DIR, _SOURCES_DIR],
            },
        },
        instructions=(
            "An auditable research FSM. You drive it with `step`; you are NOT "
            "given fetch or filesystem tools, the actions use them for you. "
            "Walk: frame(question) opens the investigation; fetch_source(url) "
            "retrieves a web page as markdown (its content comes back in the "
            "step result under last_fetch, filed as s1, s2, ...); "
            "extract(claim, source) records a claim and REQUIRES source to be a "
            "fetched source id whose fetch returned OK; write_report(filename) "
            "persists the cited report to disk; synthesize(report) is terminal. "
            "Both write_report and synthesize require >= 2 cited claims from "
            ">= 2 distinct fetched sources. Read theodosia://state for "
            "sources/claims/last_fetch and theodosia://next for legal actions."
        ),
    )


if __name__ == "__main__":
    build_server().run()
